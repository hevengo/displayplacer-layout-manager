from __future__ import annotations

import contextlib
import importlib.util
import io
import plistlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "display-layout-manager.py"
SPEC = importlib.util.spec_from_file_location("display_layout_manager", SCRIPT)
dlm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dlm
assert SPEC.loader is not None
SPEC.loader.exec_module(dlm)


def _plist(entries: list[dict]) -> bytes:
    return plistlib.dumps(entries)


def _framebuffer_entry(
    dispext: str,
    *,
    alpha: str = "",
    product: str = "VG28UQL1A",
    serial: int = 16843009,
) -> dict:
    prod = {
        "AlphanumericSerialNumber": alpha,
        "ManufacturerID": "AUS",
        "ProductName": product,
        "SerialNumber": serial,
        "WeekOfManufacture": 4,
        "YearOfManufacture": 2021,
    }
    return {
        "IONameMatched": f"{dispext},t605x",
        "DisplayAttributes": {"ProductAttributes": prod},
    }


class DisplayIdentificationTests(unittest.TestCase):
    def test_ioregistry_uses_modern_framebuffer_class_for_edid(self):
        modern = _plist([
            _framebuffer_entry("dispext1", alpha="1322131231233"),
        ])

        def run(args, **_kwargs):
            if args[3] == "IOMobileFramebufferShim":
                return SimpleNamespace(stdout=modern)
            return SimpleNamespace(stdout=b"")

        with patch.object(dlm.subprocess, "run", side_effect=run):
            info = dlm._query_ioregistry()

        self.assertEqual(info["dispext1"].alpha_serial, "1322131231233")
        self.assertEqual(info["dispext1"].product_name, "VG28UQL1A")
        self.assertEqual(info["dispext1"].brand, "ASUS")

    def test_ioregistry_falls_back_to_legacy_appleclcd2_class(self):
        legacy = _plist([
            _framebuffer_entry("dispext2", alpha="R7LMTF073477"),
        ])
        calls: list[str] = []

        def run(args, **_kwargs):
            calls.append(args[3])
            if args[3] == "AppleCLCD2":
                return SimpleNamespace(stdout=legacy)
            return SimpleNamespace(stdout=b"")

        with patch.object(dlm.subprocess, "run", side_effect=run):
            info = dlm._query_ioregistry()

        self.assertEqual(
            calls,
            ["IOMobileFramebufferShim", "IOMobileFramebuffer", "AppleCLCD2"],
        )
        self.assertEqual(info["dispext2"].alpha_serial, "R7LMTF073477")

    def test_modern_edid_map_disambiguates_duplicate_serial_displays(self):
        modern = _plist([
            _framebuffer_entry("dispext1", alpha="1322131231233"),
            _framebuffer_entry("dispext2", alpha="R7LMTF073477"),
        ])
        displays = [
            dlm.Display(contextual_id="2", serial_id="s16843009"),
            dlm.Display(contextual_id="4", serial_id="s16843009"),
        ]
        known = {
            "left-28": dlm.KnownScreen(
                "left", "s16843009", alpha_serial="R7LMTF073477",
            ),
            "right-28": dlm.KnownScreen(
                "right", "s16843009", alpha_serial="1322131231233",
            ),
        }

        def run(args, **_kwargs):
            if args[3] == "IOMobileFramebufferShim":
                return SimpleNamespace(stdout=modern)
            return SimpleNamespace(stdout=b"")

        with (
            patch.object(dlm.subprocess, "run", side_effect=run),
            patch.object(
                dlm,
                "_query_coredisplay",
                return_value={2: "dispext1", 4: "dispext2"},
            ),
        ):
            matched, hw_resolved = dlm.match_displays(displays, known)

        by_key = {m.key: m.display.contextual_id for m in matched}
        self.assertTrue(hw_resolved)
        self.assertEqual(by_key["left-28"], "4")
        self.assertEqual(by_key["right-28"], "2")

    def test_detect_prints_displays_yaml_without_layouts_or_config(self):
        active = dlm.Display(
            contextual_id="2",
            serial_id="s16843009",
            type="28 inch external screen",
            resolution="2304x1296",
            hertz="100",
            color_depth="8",
            scaling="on",
            origin="(0,0)",
            enabled="true",
        )
        stdout = io.StringIO()
        missing_config = ROOT / "missing-config-for-detect.yml"

        with (
            patch.object(dlm, "_CONFIG_PATH", missing_config),
            patch.object(dlm, "run_displayplacer_list", return_value=""),
            patch.object(dlm, "parse_displays", return_value=[active]),
            patch.object(dlm, "_disabled_display_objects", return_value=[]),
            patch.object(
                dlm,
                "build_hw_info_map",
                return_value={
                    2: dlm.HWInfo(
                        alpha_serial="1322131231233",
                        product_name="VG28UQL1A",
                        manufacturer_id="AUS",
                        year_of_manufacture=2021,
                        week_of_manufacture=4,
                    )
                },
            ),
            patch.object(dlm, "load_config") as load_config,
            patch.object(Path, "write_text") as write_text,
            patch.object(sys, "argv", ["display-layout-manager.py", "detect"]),
            contextlib.redirect_stdout(stdout),
        ):
            rc = dlm.main()

        out = stdout.getvalue()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("displays:\n"))
        self.assertIn("edid_serial: '1322131231233'", out)
        self.assertIn("brand: ASUS", out)
        self.assertIn("product_name: VG28UQL1A", out)
        self.assertIn("resolution: 2304x1296", out)
        self.assertNotIn("layouts:", out)
        load_config.assert_not_called()
        write_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
