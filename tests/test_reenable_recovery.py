from __future__ import annotations

import contextlib
import importlib.util
import io
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


class DisabledDisplayRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        log_patch = patch.object(dlm, "_log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

        self.known = {
            "left-28": dlm.KnownScreen("left-28", "s-left", resolution="100x100"),
            "center-32": dlm.KnownScreen("center-32", "s-center", resolution="100x100"),
            "right-28": dlm.KnownScreen("right-28", "s-right", resolution="100x100"),
            "macbook": dlm.KnownScreen("macbook", "s-mac", resolution="100x100"),
        }
        self.layout = dlm.Layout(
            name="Full desk",
            positions=["left-28", "center-32", "right-28", "macbook"],
            main="center-32",
            match=["left-28", "center-32", "right-28", "macbook"],
            enabled=["left-28", "center-32", "right-28", "macbook"],
        )

    def _matched(self, key: str, ctx: int, *, active: bool = True):
        display = dlm.Display(
            contextual_id=str(ctx),
            serial_id=self.known[key].serial_id,
            resolution="100x100" if active else "",
            hertz="60" if active else "",
            color_depth="8" if active else "",
            scaling="on" if active else "",
            origin="(0,0)" if active else "",
            enabled="true" if active else "false",
        )
        return dlm.MatchedDisplay(display, key, self.known[key], 1)

    def _active_matched(self):
        return [
            self._matched("left-28", 2),
            self._matched("center-32", 3),
            self._matched("right-28", 5),
            self._matched("macbook", 1),
        ]

    def _matched_without_center(self):
        return [
            self._matched("left-28", 2),
            self._matched("right-28", 5),
            self._matched("macbook", 1),
        ]

    def _matched_with_disabled_center(self):
        return [
            self._matched("left-28", 2),
            self._matched("center-32", 3, active=False),
            self._matched("right-28", 5),
            self._matched("macbook", 1),
        ]

    def test_missing_required_display_aborts_without_applying_layout(self):
        matched = self._matched_without_center()
        with (
            patch.object(dlm, "_get_disabled_displays", return_value=[]),
            patch.object(
                dlm, "_wait_for_stabilization",
                return_value=(matched, {"center-32"}),
            ),
            patch.object(dlm.subprocess, "run") as run,
        ):
            rc = dlm._apply_layout(self.layout, matched, self.known)

        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_cgs_failure_triggers_displayplacer_fallback(self):
        cgs_failure = dlm.ReenableResult(
            False, "cgs", [3],
            failed_stage="complete_configuration",
            return_code=1001,
        )
        fallback_success = dlm.ReenableResult(
            True, "displayplacer", [3],
            message="fallback ok",
        )

        with (
            patch.object(dlm, "_reenable_displays_cgs", return_value=cgs_failure) as cgs,
            patch.object(
                dlm, "_reenable_displays_displayplacer",
                return_value=fallback_success,
            ) as fallback,
        ):
            result = dlm._reenable_displays([3])

        self.assertTrue(result.success)
        self.assertEqual(result.method, "displayplacer")
        cgs.assert_called_once_with([3])
        fallback.assert_called_once_with([3])

    def test_displayplacer_fallback_compacts_repeated_output(self):
        output = "\n".join(["Unable to find screen 3 - skipping changes for that screen"] * 6)

        with patch.object(
            dlm.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stderr=output, stdout=""),
        ):
            result = dlm._reenable_displays_displayplacer([3])

        self.assertFalse(result.success)
        self.assertEqual(
            result.message,
            "Unable to find screen 3 - skipping changes for that screen",
        )

    def test_cgs_success_but_inactive_display_tries_fallback_then_aborts(self):
        matched = self._matched_with_disabled_center()
        cgs_success = dlm.ReenableResult(
            True, "cgs", [3],
            message="accepted",
        )
        fallback_success = dlm.ReenableResult(
            True, "displayplacer", [3],
            message="fallback accepted",
        )

        with (
            patch.object(
                dlm, "_get_disabled_displays",
                return_value=[(3, 144429, 0x6B3, 0x32E1, False)],
            ),
            patch.object(dlm, "_reenable_displays", return_value=cgs_success),
            patch.object(
                dlm, "_reenable_displays_displayplacer",
                return_value=fallback_success,
            ) as fallback,
            patch.object(
                dlm, "_wait_for_stabilization",
                side_effect=[
                    (matched, set()),
                    (matched, {"center-32"}),
                    (matched, {"center-32"}),
                ],
            ),
            patch.object(dlm.subprocess, "run") as run,
        ):
            rc = dlm._apply_layout(self.layout, matched, self.known)

        self.assertEqual(rc, 1)
        fallback.assert_called_once_with([3])
        run.assert_not_called()

    def test_successful_fallback_proceeds_to_apply_layout(self):
        matched = self._matched_with_disabled_center()
        active = self._active_matched()
        fallback_success = dlm.ReenableResult(
            True, "displayplacer", [3],
            message="fallback ok",
        )

        with (
            patch.object(
                dlm, "_get_disabled_displays",
                return_value=[(3, 144429, 0x6B3, 0x32E1, False)],
            ),
            patch.object(dlm, "_reenable_displays", return_value=fallback_success),
            patch.object(
                dlm, "_wait_for_stabilization",
                return_value=(active, set()),
            ),
            patch.object(
                dlm.subprocess, "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rc = dlm._apply_layout(self.layout, matched, self.known)

        self.assertEqual(rc, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "displayplacer")

    def test_reset_reports_detailed_failure(self):
        failure = dlm.ReenableResult(
            False, "cgs", [3],
            failed_stage="complete_configuration",
            return_code=1001,
            message="could not complete",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(
                dlm, "_get_disabled_displays",
                return_value=[(3, 144429, 0x6B3, 0x32E1, False)],
            ),
            patch.object(dlm, "_reenable_displays", return_value=failure),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = dlm.reset_main()

        self.assertEqual(rc, 1)
        self.assertIn("complete_configuration", stderr.getvalue())
        self.assertIn("rc=1001", stderr.getvalue())
        self.assertIn("CGDisplayID=3", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
