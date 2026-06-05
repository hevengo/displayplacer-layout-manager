from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "display-layout-manager.py"
SPEC = importlib.util.spec_from_file_location("display_layout_manager_preflight", SCRIPT)
dlm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dlm
assert SPEC.loader is not None
SPEC.loader.exec_module(dlm)


class ConfigPreflightTests(unittest.TestCase):
    def _existing_config(self) -> Path:
        return ROOT / "config.yml"

    def _run_main(self, argv: list[str], *, load_error: BaseException):
        stderr = io.StringIO()
        with (
            patch.object(dlm, "_CONFIG_PATH", self._existing_config()),
            patch.object(dlm, "load_config", side_effect=load_error) as load_config,
            patch.object(dlm, "_append_config_preflight_log") as append_log,
            patch.object(sys, "argv", ["display-layout-manager.py", *argv]),
            contextlib.redirect_stderr(stderr),
        ):
            rc = dlm.main()
        return rc, stderr.getvalue(), load_config, append_log

    def test_invalid_config_blocks_status_and_logs_error(self):
        error = dlm.ConfigError("'displays' in config.yml must be a list")
        with patch.object(dlm, "status_launch_agent") as status:
            rc, stderr, _load_config, append_log = self._run_main(
                ["status"], load_error=error,
            )

        self.assertEqual(rc, 1)
        self.assertIn("Config error: 'displays' in config.yml must be a list", stderr)
        append_log.assert_called_once_with(
            "Config error: 'displays' in config.yml must be a list",
        )
        status.assert_not_called()

    def test_invalid_config_blocks_start_before_launchctl(self):
        error = dlm.ConfigError("display 'desk' is missing 'match.serial'")
        with patch.object(dlm, "start_launch_agent") as start:
            rc, stderr, _load_config, _append_log = self._run_main(
                ["start"], load_error=error,
            )

        self.assertEqual(rc, 1)
        self.assertIn("Config error: display 'desk' is missing 'match.serial'", stderr)
        start.assert_not_called()

    def test_invalid_config_blocks_default_overview(self):
        error = dlm.ConfigError("layout 'Desk' references unknown display 'side'")
        with patch.object(dlm, "show_displays") as show:
            rc, stderr, _load_config, _append_log = self._run_main(
                [], load_error=error,
            )

        self.assertEqual(rc, 1)
        self.assertIn(
            "Config error: layout 'Desk' references unknown display 'side'",
            stderr,
        )
        show.assert_not_called()

    def test_exempt_commands_skip_config_preflight(self):
        for command, target in [
            ("init", "init_main"),
            ("detect", "detect_main"),
            ("reset", "reset_main"),
        ]:
            with self.subTest(command=command):
                with (
                    patch.object(dlm, "_CONFIG_PATH", self._existing_config()),
                    patch.object(dlm, "load_config") as load_config,
                    patch.object(dlm, target, return_value=0) as action,
                    patch.object(sys, "argv", ["display-layout-manager.py", command]),
                ):
                    rc = dlm.main()

                self.assertEqual(rc, 0)
                load_config.assert_not_called()
                action.assert_called_once()

    def test_generic_load_exception_is_reported_without_traceback(self):
        with patch.object(dlm, "status_launch_agent") as status:
            rc, stderr, _load_config, append_log = self._run_main(
                ["status"],
                load_error=ValueError("invalid literal for int() with base 10"),
            )

        self.assertEqual(rc, 1)
        self.assertIn(
            "Config error: invalid literal for int() with base 10",
            stderr,
        )
        self.assertNotIn("Traceback", stderr)
        append_log.assert_called_once_with(
            "Config error: invalid literal for int() with base 10",
        )
        status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
