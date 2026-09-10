#!/usr/bin/env python3

from __future__ import annotations

import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import audit_runtime  # noqa: E402


PYTHON = sys.executable


def python_command(source: str, *args: str) -> list[str]:
    return [PYTHON, "-c", source, *args]


def wait_for_path(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.005)
    return path.exists()


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="audit-runtime-test-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def output_paths(self, name: str = "run") -> tuple[Path, Path]:
        return self.root / f"{name}.stdout", self.root / f"{name}.stderr"

    def run_direct(
        self,
        command: list[str],
        *,
        timeout: float,
        name: str = "run",
        max_output_bytes: int = 8 * 1024 * 1024,
    ) -> tuple[audit_runtime.CommandResult, bytes, bytes]:
        stdout_path, stderr_path = self.output_paths(name)
        result = audit_runtime.run_command(
            command,
            timeout=timeout,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            max_output_bytes=max_output_bytes,
            guarded=False,
        )
        return result, stdout_path.read_bytes(), stderr_path.read_bytes()

    def three_level_programs(self) -> tuple[str, str, str]:
        grandchild = (
            "import pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text('grandchild-ready'); "
            "time.sleep(float(sys.argv[3])); "
            "pathlib.Path(sys.argv[2]).write_text('late')"
        )
        child = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],"
            "sys.argv[3],sys.argv[4]]); time.sleep(5)"
        )
        leader = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],"
            "sys.argv[3],sys.argv[4],sys.argv[5]]); time.sleep(5)"
        )
        return leader, child, grandchild

    def three_level_command(
        self, ready: Path, late: Path, delay: float
    ) -> list[str]:
        leader, child, grandchild = self.three_level_programs()
        return python_command(
            leader, child, grandchild, str(ready), str(late), str(delay)
        )

    def test_output_files_are_private_and_existing_data_is_not_overwritten(self) -> None:
        stdout_path, stderr_path = self.output_paths()
        result = audit_runtime.run_command(
            python_command("pass"),
            timeout=2,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            guarded=False,
        )
        self.assertEqual(result.outcome, "finished")
        self.assertEqual(stat.S_IMODE(stdout_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(stderr_path.stat().st_mode), 0o600)

        stdout_path.write_bytes(b"existing")
        duplicate = audit_runtime.run_command(
            python_command("pass"),
            timeout=2,
            stdout_path=stdout_path,
            stderr_path=self.root / "second.stderr",
            guarded=False,
        )
        self.assertEqual(duplicate.outcome, "spawn-error")
        self.assertEqual(stdout_path.read_bytes(), b"existing")

    def test_reserved_output_rejects_symlink_hardlink_bad_mode_and_fifo(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        symlink = self.root / "symlink.stdout"
        symlink.symlink_to(outside)
        result = audit_runtime.run_command(
            python_command("pass"),
            timeout=1,
            stdout_path=symlink,
            stderr_path=self.root / "symlink.stderr",
            guarded=False,
        )
        self.assertEqual(result.outcome, "spawn-error")
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")

        hardlink_source = self.root / "hardlink-source"
        hardlink_source.write_bytes(b"hardlink")
        hardlink_source.chmod(0o600)
        hardlink = self.root / "hardlink.stdout"
        os.link(hardlink_source, hardlink)
        result = audit_runtime.run_command(
            python_command("pass"),
            timeout=1,
            stdout_path=hardlink,
            stderr_path=self.root / "hardlink.stderr",
            guarded=False,
        )
        self.assertEqual(result.outcome, "spawn-error")
        self.assertEqual(hardlink_source.read_bytes(), b"hardlink")
        self.assertEqual(hardlink.read_bytes(), b"hardlink")

        bad_mode = self.root / "bad-mode.stdout"
        bad_mode.write_bytes(b"protected")
        bad_mode.chmod(0o644)
        result = audit_runtime.run_command(
            python_command("pass"),
            timeout=1,
            stdout_path=bad_mode,
            stderr_path=self.root / "bad-mode.stderr",
            guarded=False,
        )
        self.assertEqual(result.outcome, "spawn-error")
        self.assertEqual(bad_mode.read_bytes(), b"protected")
        self.assertEqual(stat.S_IMODE(bad_mode.stat().st_mode), 0o644)

        fifo = self.root / "output.fifo"
        os.mkfifo(fifo, 0o600)
        started = time.monotonic()
        result = audit_runtime.run_command(
            python_command("pass"),
            timeout=1,
            stdout_path=fifo,
            stderr_path=self.root / "fifo.stderr",
            guarded=False,
        )
        self.assertEqual(result.outcome, "spawn-error")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(stat.S_ISFIFO(fifo.stat().st_mode))

    def test_identical_output_paths_are_rejected_before_creation(self) -> None:
        path = self.root / "same"
        result = audit_runtime.run_command(
            python_command("pass"),
            timeout=1,
            stdout_path=path,
            stderr_path=path,
            guarded=False,
        )
        self.assertEqual(result.outcome, "spawn-error")
        self.assertFalse(path.exists())

    def test_output_aliases_cannot_share_a_file(self) -> None:
        alias = self.root / "directory-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        stdout = self.root / "aliased-output"
        stderr = alias / "aliased-output"
        with mock.patch.object(audit_runtime.subprocess, "Popen") as popen:
            result = audit_runtime.run_command(
                python_command("print('must not run')"), timeout=1,
                stdout_path=stdout, stderr_path=stderr, guarded=False,
            )
        self.assertEqual(result.outcome, "spawn-error")
        popen.assert_not_called()
        self.assertEqual(stdout.read_bytes(), b"")

    def test_cleanup_deadline_passed_does_not_interrupt_signalling(self) -> None:
        with mock.patch.object(audit_runtime.time, "sleep") as sleep:
            audit_runtime._sleep_during_cleanup(-0.01)
        sleep.assert_called_once_with(0.0)

    def test_term_race_does_not_override_successful_kill_and_readback(self) -> None:
        with mock.patch.object(audit_runtime, "_send_group_signal", side_effect=[False, True]), \
                mock.patch.object(audit_runtime, "_group_state", return_value=(False, True)):
            self.assertTrue(audit_runtime._terminate_process_group(123))

    def test_invalid_inputs_fail_fast_before_output_or_spawn(self) -> None:
        invalid_timeouts: tuple[object, ...] = (
            0,
            -1,
            True,
            False,
            math.nan,
            math.inf,
            -math.inf,
            "1",
        )
        invalid_limits: tuple[object, ...] = (0, -1, True, False, 1.0, "1")

        cases: list[tuple[str, dict[str, object]]] = []
        for index, value in enumerate(invalid_timeouts):
            cases.append((f"timeout-{index}", {"timeout": value}))
        for index, value in enumerate(invalid_limits):
            cases.append((f"limit-{index}", {"max_output_bytes": value}))
        cases.extend(
            [
                ("argv-nul", {"argv": [PYTHON, "-c\x00"]}),
                ("argv-empty", {"argv": [""]}),
                ("cwd-nul", {"cwd": str(self.root / "bad\x00cwd")}),
                ("env-key-nul", {"env": {"BAD\x00KEY": "value"}}),
                ("env-value-nul", {"env": {"BAD": "value\x00"}}),
            ]
        )

        for name, overrides in cases:
            with self.subTest(name=name):
                stdout_path, stderr_path = self.output_paths(name)
                kwargs: dict[str, object] = {
                    "timeout": 1.0,
                    "stdout_path": stdout_path,
                    "stderr_path": stderr_path,
                    "guarded": False,
                }
                kwargs.update(overrides)
                with mock.patch.object(audit_runtime.subprocess, "Popen") as popen:
                    command = kwargs.pop("argv", [PYTHON, "-c", "pass"])
                    result = audit_runtime.run_command(
                        command, **kwargs  # type: ignore[arg-type]
                    )
                self.assertEqual(result.outcome, "spawn-error")
                popen.assert_not_called()
                self.assertFalse(stdout_path.exists())
                self.assertFalse(stderr_path.exists())

    def test_timeout_preserves_partial_stdout_and_stderr(self) -> None:
        command = python_command(
            "import sys,time; sys.stdout.write('before-out'); sys.stdout.flush(); "
            "sys.stderr.write('before-err'); sys.stderr.flush(); time.sleep(3)"
        )
        result, stdout, stderr = self.run_direct(command, timeout=0.15)
        self.assertEqual(result.outcome, "timeout")
        self.assertTrue(result.timed_out)
        self.assertIn(b"before-out", stdout)
        self.assertIn(b"before-err", stderr)

    def test_three_level_timeout_has_readiness_and_kills_late_writer(self) -> None:
        leader, child, grandchild = self.three_level_programs()
        control_ready = self.root / "control-ready"
        control_marker = self.root / "control-marker"
        subprocess.run(
            python_command(grandchild, str(control_ready), str(control_marker), "0"),
            check=True,
        )
        self.assertEqual(control_ready.read_text(), "grandchild-ready")
        self.assertEqual(control_marker.read_text(), "late")

        ready = self.root / "timeout-ready"
        late = self.root / "timeout-late"
        readiness_seen = threading.Event()

        def watch_readiness() -> None:
            if wait_for_path(ready, 1.5):
                readiness_seen.set()

        watcher = threading.Thread(target=watch_readiness)
        watcher.start()
        result, _, _ = self.run_direct(
            python_command(
                leader, child, grandchild, str(ready), str(late), "1.2"
            ),
            timeout=1.0,
            name="three-level-timeout",
        )
        watcher.join(2)
        self.assertEqual(result.outcome, "timeout")
        self.assertTrue(result.timed_out)
        self.assertTrue(readiness_seen.is_set(), "three-level child never reached readiness")
        time.sleep(1.5)
        self.assertFalse(late.exists())

    def test_leader_exit_kills_three_level_remaining_group(self) -> None:
        leader, child, grandchild = self.three_level_programs()
        ready = self.root / "leader-ready"
        late = self.root / "leader-late"
        # The leader exits after spawning the child; the child then spawns the
        # grandchild. Readiness proves the complete chain ran before cleanup.
        leader_exits = (
            "import pathlib,subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],"
            "sys.argv[3],sys.argv[4],sys.argv[5]]); "
            "exec(\"while not pathlib.Path(sys.argv[3]).exists():\\n"
            " time.sleep(0.005)\")"
        )
        result, _, _ = self.run_direct(
            python_command(
                leader_exits, child, grandchild, str(ready), str(late), "1.0"
            ),
            timeout=2,
            name="three-level-leader-exit",
        )
        self.assertEqual(result.outcome, "finished")
        self.assertTrue(wait_for_path(ready, 1.0))
        time.sleep(1.3)
        self.assertFalse(late.exists())

    def test_output_limit_is_hard_on_disk_and_preserves_prefixes(self) -> None:
        command = python_command(
            "import sys; sys.stdout.write('o'*80); sys.stdout.flush(); "
            "sys.stderr.write('e'*80); sys.stderr.flush()"
        )
        stdout_path, stderr_path = self.output_paths("limit")
        stop = threading.Event()
        maximum_seen = [0]

        def monitor() -> None:
            while not stop.is_set():
                total = sum(
                    path.stat().st_size
                    for path in (stdout_path, stderr_path)
                    if path.exists()
                )
                maximum_seen[0] = max(maximum_seen[0], total)
                time.sleep(0.001)

        monitor_thread = threading.Thread(target=monitor)
        monitor_thread.start()
        try:
            result = audit_runtime.run_command(
                command,
                timeout=2,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                max_output_bytes=128,
                guarded=False,
            )
        finally:
            stop.set()
            monitor_thread.join(1)
        stdout = stdout_path.read_bytes()
        stderr = stderr_path.read_bytes()
        self.assertEqual(result.outcome, "output-limit")
        self.assertGreater(len(stdout) + len(stderr), 0)
        self.assertLessEqual(len(stdout) + len(stderr), 128)
        self.assertLessEqual(maximum_seen[0], 128)
        self.assertIn(b"o", stdout)
        self.assertIn(b"e", stderr)

    def test_group_permission_failure_is_not_reported_as_finished(self) -> None:
        stdout_path, stderr_path = self.output_paths("permission-failure")
        with mock.patch.object(
            audit_runtime.os, "killpg", side_effect=PermissionError()
        ):
            result = audit_runtime.run_command(
                python_command("pass"),
                timeout=1,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                guarded=False,
            )
        self.assertEqual(result.outcome, "spawn-error")

    def test_parent_sigterm_cancels_three_level_chain_and_blocks_late_write(self) -> None:
        leader, child, grandchild = self.three_level_programs()
        ready = self.root / "cancel-ready"
        late = self.root / "cancel-late"
        signal_sent = threading.Event()

        def cancel_after_readiness() -> None:
            if wait_for_path(ready, 2.0):
                os.kill(os.getpid(), signal.SIGTERM)
                signal_sent.set()

        canceller = threading.Thread(target=cancel_after_readiness)
        canceller.start()
        result, _, _ = self.run_direct(
            python_command(
                leader, child, grandchild, str(ready), str(late), "1.2"
            ),
            timeout=3,
            name="three-level-cancel",
        )
        canceller.join(2)
        self.assertEqual(result.outcome, "cancelled")
        self.assertTrue(signal_sent.is_set(), "cancellation raced before readiness")
        self.assertTrue(ready.exists())
        time.sleep(1.5)
        self.assertFalse(late.exists())

    def test_guard_failure_prevents_exec(self) -> None:
        with mock.patch.object(
            audit_runtime,
            "enable_no_hydration",
            side_effect=audit_runtime.GuardUnavailable("raw path should not escape"),
        ), mock.patch.object(audit_runtime.os, "execvpe") as execvpe, mock.patch(
            "sys.stderr.write"
        ) as write:
            exit_code = audit_runtime.main(["--guard-exec", "does-not-run", "secret"])
        self.assertEqual(exit_code, 78)
        execvpe.assert_not_called()
        write.assert_called_once_with("audit_runtime: no-hydration guard unavailable\n")

    def test_native_set_failure_and_readback_mismatch_fail_closed(self) -> None:
        for set_result, readback in ((1, 1), (0, 0)):
            with self.subTest(set_result=set_result, readback=readback):
                library = mock.Mock()
                set_policy = mock.Mock(return_value=set_result)
                get_policy = mock.Mock(return_value=readback)
                library.setiopolicy_np = set_policy
                library.getiopolicy_np = get_policy
                with mock.patch.object(audit_runtime.sys, "platform", "darwin"), mock.patch.object(
                    audit_runtime.ctypes, "CDLL", return_value=library
                ):
                    with self.assertRaises(audit_runtime.GuardUnavailable):
                        audit_runtime.enable_no_hydration()
                set_policy.assert_called_once_with(3, 0, 1)
                if set_result == 0:
                    get_policy.assert_called_once_with(3, 0)
                else:
                    get_policy.assert_not_called()

    def test_unsupported_platform_fails_closed(self) -> None:
        with mock.patch.object(audit_runtime.sys, "platform", "not-darwin"), mock.patch.object(
            audit_runtime.ctypes, "CDLL"
        ) as cdll:
            with self.assertRaises(audit_runtime.GuardUnavailable):
                audit_runtime.enable_no_hydration()
        cdll.assert_not_called()

    def test_check_guard_is_machine_json(self) -> None:
        completed = subprocess.run(
            [PYTHON, str(SCRIPT_DIR / "audit_runtime.py"), "--check-guard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        document = json.loads(completed.stdout.decode("utf-8"))
        self.assertIn(document["ok"], (True, False))
        self.assertEqual(document["guard"], "no-hydration")
        if completed.returncode == 0:
            self.assertTrue(document["ok"])
        else:
            self.assertEqual(completed.returncode, 78)
            self.assertFalse(document["ok"])
            self.assertEqual(document["error"], "guard-unavailable")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin-only native guard")
    def test_native_guard_readback_and_exec_inheritance(self) -> None:
        check = subprocess.run(
            [PYTHON, str(SCRIPT_DIR / "audit_runtime.py"), "--check-guard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check.returncode != 0:
            self.skipTest("native no-hydration policy unavailable")

        child = (
            "import ctypes,json; lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib'); "
            "lib.getiopolicy_np.argtypes=[ctypes.c_int,ctypes.c_int]; "
            "lib.getiopolicy_np.restype=ctypes.c_int; "
            "print(json.dumps({'policy':int(lib.getiopolicy_np(3,0))}))"
        )
        inherited = subprocess.run(
            [
                PYTHON,
                str(SCRIPT_DIR / "audit_runtime.py"),
                "--guard-exec",
                PYTHON,
                "-c",
                child,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(inherited.returncode, 0, inherited.stderr.decode())
        self.assertEqual(json.loads(inherited.stdout.decode())["policy"], 1)


if __name__ == "__main__":
    unittest.main()
