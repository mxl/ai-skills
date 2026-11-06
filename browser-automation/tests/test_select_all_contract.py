"""Bounded selection shortcuts only; no browser or runtime-state access."""
import importlib.util
from pathlib import Path
import sys
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "browser_session.py"
SPEC = importlib.util.spec_from_file_location("browser_session_selection_contract", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SelectAllContractTests(unittest.TestCase):
    def test_only_reviewed_selection_chords_allowed(self):
        for key in ("Control+a", "Meta+a", "Shift+ArrowUp", "Shift+ArrowDown"):
            args = ["press", key]
            self.assertEqual(MODULE.validate_run_command(args), args)

    def test_clipboard_navigation_and_lifecycle_chords_denied(self):
        for key in ("Control+c", "Meta+c", "Control+v", "Meta+v", "Meta+l", "Meta+q", "Control+w", "Meta+w", "Shift+Delete"):
            with self.subTest(key=key):
                with self.assertRaises(MODULE.BrowserSessionError):
                    MODULE.validate_run_command(["press", key])

    def test_additional_arguments_and_injected_chords_denied(self):
        for args in (["press", "Meta+a", "--new-tab"], ["press", "Meta+a;Delete"], ["press", "Meta+a\nDelete"]):
            with self.subTest(args=args):
                with self.assertRaises(MODULE.BrowserSessionError):
                    MODULE.validate_run_command(args)


if __name__ == "__main__":
    unittest.main()
