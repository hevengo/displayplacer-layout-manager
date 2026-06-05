from __future__ import annotations

import importlib.util
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "display-port-recovery-diagnostics.py"
SPEC = importlib.util.spec_from_file_location("display_port_recovery", SCRIPT)
dpr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dpr
assert SPEC.loader is not None
SPEC.loader.exec_module(dpr)


class DisplayPortRecoveryDiagnosticsTests(unittest.TestCase):
    def test_parse_displayplacer_display(self):
        displays = dpr.parse_displays(
            "\n".join(
                [
                    "Persistent screen id: ABC",
                    "Contextual screen id: 3",
                    "Serial screen id: s144429",
                    "Type: 32 inch external screen",
                    "Resolution: 2560x1440",
                    "Hertz: 60",
                    "Color Depth: 8",
                    "Scaling: on",
                    "Origin: (0,0) - main display",
                    "Rotation: 0",
                    "Enabled: true",
                ]
            )
        )

        self.assertEqual(len(displays), 1)
        self.assertEqual(displays[0].contextual_id, "3")
        self.assertEqual(displays[0].serial_id, "s144429")
        self.assertTrue(displays[0].is_active)

    def test_displayplacer_arg_uses_current_geometry(self):
        display = dpr.Display(
            contextual_id="3",
            resolution="2560x1440",
            hertz="60",
            color_depth="8",
            scaling="on",
            origin="(0,0) - main display",
            rotation="0",
            enabled="true",
        )

        arg = dpr._displayplacer_arg(display, resolution="1920x1080", hertz=60)

        self.assertEqual(
            arg,
            "id:3 res:1920x1080 hz:60 color_depth:8 enabled:true "
            "scaling:on origin:(0,0) degree:0",
        )

    def test_usb_candidate_ranking_prefers_adapter_over_hub(self):
        hub = dpr.UsbDevice(
            name="Element Hub",
            product="Element Hub",
            vendor="CalDigit, Inc.",
            location_id=1146880,
            candidate_reason="billboard, caldigit",
        )
        adapter = dpr.UsbDevice(
            name="VMM7100",
            product="VMM7100",
            vendor="Synaptics",
            location_id=17825792,
            candidate_reason="billboard, synaptics, vmm",
        )

        ranked = dpr._rank_usb_recovery_candidates([hub, adapter])

        self.assertEqual(ranked[0].product, "VMM7100")

    def test_sls_detect_command_reports_result(self):
        args = dpr.build_parser().parse_args(["sls-detect", "--empty-transaction"])
        stdout = io.StringIO()

        with (
            patch.object(
                dpr,
                "sls_detect_displays",
                return_value=dpr.CgsResult(True, "sls-detect", message="ok"),
            ) as detect,
            contextlib.redirect_stdout(stdout),
        ):
            rc = args.func(args)

        self.assertEqual(rc, 0)
        detect.assert_called_once_with(empty_transaction=True, permanent=False)
        self.assertIn("sls-detect succeeded", stdout.getvalue())

    def test_suggestions_start_with_sls_detect(self):
        snapshot = dpr.Snapshot(
            displayplacer=[dpr.Display(contextual_id="3")],
            coregraphics=[],
            coredisplay_locations={},
            display_attributes=[],
            usb_devices=[],
            thunderbolt=[],
        )
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            dpr._print_suggestions(snapshot)

        self.assertIn(
            "1. ./display-port-recovery-diagnostics.py sls-detect",
            stdout.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
