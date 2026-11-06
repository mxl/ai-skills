"""Bounded browser diagnostics must not admit arbitrary command execution."""
import importlib.util
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location(
    'smoke_browser_session', Path(__file__).parents[1] / 'scripts/browser_session.py')
bs = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bs
spec.loader.exec_module(bs)


class SmokeDiagnosticGrammarTests(unittest.TestCase):
    def test_allowed_diagnostics(self):
        for command in (
            ['console'], ['errors'], ['screenshot'], ['screenshot', '--full'],
            ['set', 'viewport', '390', '844'],
            ['set', 'viewport', '1440', '900'], ['get', 'url'],
            ['get', 'attr', '@e1', 'href'], ['get', 'attr', '@e2', 'alt'],
        ):
            with self.subTest(command=command):
                self.assertEqual(command, bs.validate_run_command(command))

    def test_rejects_exports_mutation_and_arbitrary_selectors(self):
        for command in (
            ['console', '--clear'], ['errors', '--clear'],
            ['screenshot', '/tmp/overwrite.png'], ['screenshot', '--annotate'],
            ['set', 'viewport', '100', '900'],
            ['set', 'viewport', '1440', '900', '--device-scale-factor', '2'],
            ['set', 'viewport', '10000', '900'],
            ['set', 'viewport', '390\n', '844'], ['set', 'headers', '{}'],
            ['get', 'attr', 'body', 'href'], ['get', 'attr', '@e1', 'value'],
            ['eval', 'document.body.innerHTML'], ['network', 'har', 'start'],
        ):
            with self.subTest(command=command):
                with self.assertRaises(Exception):
                    bs.validate_run_command(command)


if __name__ == '__main__':
    unittest.main()
