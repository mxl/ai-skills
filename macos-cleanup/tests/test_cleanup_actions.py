"""CLI consent separation. All owner and execution dependencies are synthetic."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cleanup_actions


class ActionCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = dict(adapter="uv", action="uv-cache-prune", target=str(self.root / "cache"),
                         version="fixture", scope="owner-managed-dynamic-cache", risk="rebuild",
                         expires_at=900, estimated_reclaimable_bytes=None)
        self.digest = "a" * 64
        self.contract = types.SimpleNamespace(
            plan_digest=mock.Mock(return_value=self.digest),
            validate_plan=mock.Mock(side_effect=lambda value: value),
            write_private_json=mock.Mock(), load_plan=mock.Mock(return_value=self.plan),
        )
        self.adapters = types.SimpleNamespace(
            discover=mock.Mock(return_value={"supported": False, "status": "REVIEW-ONLY"}),
            prepare=mock.Mock(return_value=self.plan),
        )
        self.executor = types.SimpleNamespace(execute=mock.Mock(return_value={"status": "BLOCKED"}))
        self.cloud = types.SimpleNamespace(inventory=mock.Mock(return_value={"actionable": False}))

    def invoke(self, argv, guard_error=None):
        modules = dict(cleanup_contract=self.contract, cache_adapters=self.adapters,
                       cleanup_executor=self.executor, cloud_inventory=self.cloud)
        output = io.StringIO()
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(cleanup_actions, "enable_no_hydration", side_effect=guard_error), \
                contextlib.redirect_stdout(output):
            code = cleanup_actions.main(argv)
        return code, json.loads(output.getvalue())

    def test_prepare_is_not_approval_or_execution(self):
        output = str(self.root / "plan.json")
        code, result = self.invoke(["prepare", "--adapter", "uv", "--output", output])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "PREPARED-NOT-APPROVED")
        self.assertEqual(result["digest"], self.digest)
        self.contract.write_private_json.assert_called_once_with(output, self.plan)
        self.executor.execute.assert_not_called()

    def test_review_never_prepares_or_executes(self):
        _, result = self.invoke(["review", "--adapter", "homebrew"])
        self.assertFalse(result["actionable"])
        self.adapters.prepare.assert_not_called()
        self.executor.execute.assert_not_called()

    def test_invalid_prepare_result_is_not_saved(self):
        self.adapters.prepare.return_value = {"status": "REVIEW-ONLY"}
        self.contract.validate_plan.side_effect = ValueError("not an action plan")
        code, result = self.invoke(["prepare", "--adapter", "uv", "--output", str(self.root / "plan")])
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "BLOCKED")
        self.contract.write_private_json.assert_not_called()
        self.executor.execute.assert_not_called()

    def test_show_is_read_only(self):
        _, result = self.invoke(["show", "--plan", str(self.root / "plan.json")])
        self.assertEqual(result["digest"], self.digest)
        self.executor.execute.assert_not_called()

    def test_execute_forwards_explicit_digest_to_executor(self):
        journal = str(self.root / "journal")
        code, _ = self.invoke(["execute", "--plan", str(self.root / "plan.json"),
                               "--confirm-digest", self.digest, "--journal-dir", journal])
        self.assertEqual(code, 1)
        self.executor.execute.assert_called_once_with(
            self.plan, confirmed_digest=self.digest, journal_dir=journal)

    def test_wildcard_or_missing_consent_rejected(self):
        for argv in (["execute", "--plan", "x", "--journal-dir", "y"],
                     ["execute", "--plan", "x", "--journal-dir", "y", "--confirm-digest", "all"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                self.invoke(argv)
        self.executor.execute.assert_not_called()

    def test_parser_errors_do_not_echo_private_arguments(self):
        sentinel = "PRIVATE-CREDENTIAL-SENTINEL"
        inputs = (["review", "--adapter", sentinel],
                  ["cloud-metadata", "--root", "fixture", "--timeout", sentinel],
                  ["review", "--adapter", "uv", "--token", sentinel])
        for argv in inputs:
            with self.subTest(argv=argv):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        self.assertRaises(SystemExit):
                    cleanup_actions.main(argv)
                self.assertNotIn(sentinel, stdout.getvalue() + stderr.getvalue())
                self.assertIn("invalid arguments", stderr.getvalue())

    def test_guard_failure_prevents_plan_read_and_owner_probe(self):
        for argv in (["review", "--adapter", "uv"], ["show", "--plan", "x"]):
            code, result = self.invoke(argv, cleanup_actions.GuardUnavailable())
            self.assertEqual(code, 78)
            self.assertEqual(result["status"], "BLOCKED")
        self.contract.load_plan.assert_not_called()
        self.adapters.discover.assert_not_called()

    def test_cloud_command_never_dispatches_executor(self):
        root = str(self.root / "fake-provider")
        _, result = self.invoke(["cloud-metadata", "--root", root, "--max-entries", "3"])
        self.assertFalse(result["actionable"])
        self.cloud.inventory.assert_called_once_with(root, max_entries=3, max_depth=3, timeout=10.0)
        self.executor.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
