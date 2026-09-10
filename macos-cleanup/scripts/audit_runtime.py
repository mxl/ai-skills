#!/usr/bin/env python3
"""Small, fail-closed runtime used by the macOS cleanup audit.

The runtime deliberately owns only subprocess lifecycle and the macOS I/O
policy. It does not inspect a user's home directory, cloud providers, or any
other machine data.
"""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Sequence


# Darwin's VFS materialize-dataless-files policy uses type 3, process scope 0,
# and policy value 1 for "off". Keep the SDK values local so importing this
# module never requires an SDK or a header search.
_IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
_IOPOL_SCOPE_PROCESS = 0
_IOPOL_MATERIALIZE_DATALESS_FILES_OFF = 1

_GUARD_EXIT_CODE = 78
_GUARD_ERROR = "audit_runtime: no-hydration guard unavailable\n"
_EXEC_ERROR = "audit_runtime: command execution failed\n"
_INVALID_INVOCATION = "audit_runtime: invalid invocation\n"
_TERM_GRACE_SECONDS = 0.15
_CLEANUP_WAIT_SECONDS = 0.75
_POLL_SECONDS = 0.01


class GuardUnavailable(RuntimeError):
    """Raised when the no-hydration process policy cannot be verified."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    outcome: str
    timed_out: bool
    duration_seconds: float


def enable_no_hydration() -> None:
    """Enable and verify the inherited Darwin no-hydration process policy."""

    if sys.platform != "darwin":
        raise GuardUnavailable(_GUARD_ERROR.rstrip())

    try:
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        set_policy = libsystem.setiopolicy_np
        get_policy = libsystem.getiopolicy_np

        set_policy.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        set_policy.restype = ctypes.c_int
        get_policy.argtypes = [ctypes.c_int, ctypes.c_int]
        get_policy.restype = ctypes.c_int

        result = int(
            set_policy(
                _IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                _IOPOL_SCOPE_PROCESS,
                _IOPOL_MATERIALIZE_DATALESS_FILES_OFF,
            )
        )
        if result != 0:
            raise GuardUnavailable(_GUARD_ERROR.rstrip())

        readback = int(
            get_policy(
                _IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                _IOPOL_SCOPE_PROCESS,
            )
        )
        if readback != _IOPOL_MATERIALIZE_DATALESS_FILES_OFF:
            raise GuardUnavailable(_GUARD_ERROR.rstrip())
    except GuardUnavailable:
        raise
    except Exception as exc:
        # Do not expose errno, library details, or filesystem paths to the
        # caller. The CLI has the same constant error response.
        raise GuardUnavailable(_GUARD_ERROR.rstrip()) from exc


def _send_group_signal(pgid: int, signum: int) -> bool:
    """Send a signal to the owned group and report permission/lifecycle errors."""

    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        # The group has already gone; that is a successful cleanup state.
        return True
    except PermissionError:
        return False
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return True


def _group_state(pgid: int) -> tuple[bool, bool]:
    """Return (exists, query_succeeded) for an owned process group."""

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False, True
    except PermissionError:
        return True, False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False, True
        return True, False
    return True, True


def _sleep_during_cleanup(seconds: float) -> None:
    try:
        time.sleep(max(0.0, seconds))
    except KeyboardInterrupt:
        # A second Ctrl-C must not interrupt the TERM/KILL cleanup sequence.
        return


def _terminate_process_group(
    pgid: int, process: subprocess.Popen[bytes] | None = None
) -> bool:
    """TERM then KILL all members of a group created by this runtime."""

    success = True
    if process is not None:
        try:
            # Reap an already-exited leader first. This avoids Darwin's
            # transient EPERM result for a process group containing its zombie.
            process.poll()
        except OSError:
            success = False
    # TERM may race with a leader already exiting (including from our signal
    # handler). The final KILL/readback decides whether cleanup succeeded.
    _send_group_signal(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        exists, query_ok = _group_state(pgid)
        if not query_ok and process is not None:
            # On Darwin a TERMed but unreaped leader can transiently make
            # killpg(..., 0) report EPERM. Reap that known child before
            # treating the permission result as a real cleanup failure.
            try:
                process.poll()
                exists, query_ok = _group_state(pgid)
            except OSError:
                query_ok = False
        if not query_ok:
            # Permission-looking results can be transient while a TERMed
            # leader is becoming a zombie. Keep polling until the bounded
            # grace period ends; a persistent error remains a failure.
            _sleep_during_cleanup(min(_POLL_SECONDS, deadline - time.monotonic()))
            continue
        if not exists:
            break
        _sleep_during_cleanup(min(_POLL_SECONDS, deadline - time.monotonic()))

    # Reap a leader that exited from TERM before sending KILL. Darwin can
    # report EPERM for a process group containing an unreaped zombie; that is
    # not a permission failure, and reaping removes the transient condition.
    if process is not None:
        try:
            process.poll()
        except OSError:
            success = False

    # Always issue KILL, including when the leader already exited. A
    # descendant can still retain the pipe endpoints in that state.
    kill_ok = _send_group_signal(pgid, signal.SIGKILL)
    if process is not None:
        try:
            if process.poll() is None:
                process.wait(timeout=_CLEANUP_WAIT_SECONDS)
            process.poll()
        except subprocess.TimeoutExpired:
            pass
        except KeyboardInterrupt:
            pass
        except OSError:
            success = False
    if not kill_ok:
        # A Darwin group containing the just-reaped leader may have reported
        # EPERM for the first KILL. Retry after the leader is known reaped;
        # persistent permission failure remains non-success.
        kill_ok = _send_group_signal(pgid, signal.SIGKILL)
    _exists, query_ok = _group_state(pgid)
    query_deadline = time.monotonic() + _CLEANUP_WAIT_SECONDS
    while not query_ok and time.monotonic() < query_deadline:
        _sleep_during_cleanup(_POLL_SECONDS)
        if process is not None:
            process.poll()
        _exists, query_ok = _group_state(pgid)
    return success and kill_ok and query_ok


def _reap_leader(process: subprocess.Popen[bytes], pgid: int) -> bool:
    """Reap the direct child without waiting indefinitely."""

    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=_CLEANUP_WAIT_SECONDS)
        return process.poll() is not None
    except subprocess.TimeoutExpired:
        kill_ok = _send_group_signal(pgid, signal.SIGKILL)
    except KeyboardInterrupt:
        kill_ok = _send_group_signal(pgid, signal.SIGKILL)
    else:
        return process.poll() is not None

    try:
        process.wait(timeout=_CLEANUP_WAIT_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        return False
    return kill_ok and process.poll() is not None


def _install_term_handler(
    pgid: int, cancelled: threading.Event
) -> signal.Handlers | None:
    """Install a main-thread-only handler which starts group cancellation."""

    if threading.current_thread() is not threading.main_thread():
        return None

    previous = signal.getsignal(signal.SIGTERM)

    def handle_term(_signum: int, _frame: object) -> None:
        # Signal handlers do not sleep or reap. The polling thread performs
        # the bounded TERM/KILL sequence after observing this event.
        cancelled.set()
        _send_group_signal(pgid, signal.SIGTERM)

    try:
        signal.signal(signal.SIGTERM, handle_term)
    except (OSError, ValueError):
        return None
    return previous


def _restore_term_handler(previous: signal.Handlers | None) -> None:
    if previous is None or threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (OSError, ValueError):
        return


def _safe_output_metadata(fd: int) -> os.stat_result:
    metadata = os.fstat(fd)
    if (
        metadata.st_uid != os.geteuid()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise OSError("unsafe existing output")
    return metadata


def _open_private_output(path: str) -> BinaryIO:
    """Create, or safely reopen a caller-reserved, private output file."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("O_NOFOLLOW unavailable")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow
    try:
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            _safe_output_metadata(fd)
        except Exception:
            os.close(fd)
            raise
    except FileExistsError:
        # The orchestrator may reserve an empty 0600 tempfile before calling
        # this API. O_NONBLOCK ensures a FIFO or other unusual node cannot
        # make this reopen block; fstat then rejects it.
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | nofollow)
        try:
            _safe_output_metadata(fd)
        except Exception:
            os.close(fd)
            raise

    try:
        return os.fdopen(fd, "wb", buffering=0)
    except Exception:
        os.close(fd)
        raise


def _write_output(file_object: BinaryIO, data: bytes) -> bool:
    view = memoryview(data)
    try:
        while view:
            written = os.write(file_object.fileno(), view)
            if written <= 0:
                return False
            view = view[written:]
    except OSError:
        return False
    return True


def _consume_stream(
    file_descriptor: int,
    output_file: BinaryIO,
    written_total: int,
    max_output_bytes: int,
) -> tuple[int, bool, bool, bool]:
    """Read available pipe bytes without blocking.

    Returns (new_total, reached_limit, write_failed, reached_eof).
    """

    while True:
        try:
            chunk = os.read(file_descriptor, 64 * 1024)
        except BlockingIOError:
            return written_total, False, False, False
        except OSError:
            return written_total, False, True, False

        if not chunk:
            return written_total, False, False, True

        remaining = max_output_bytes - written_total
        if remaining <= 0:
            return written_total, True, False, False
        if len(chunk) > remaining:
            chunk = chunk[:remaining]

        if chunk and not _write_output(output_file, chunk):
            return written_total, False, True, False
        written_total += len(chunk)
        if written_total >= max_output_bytes:
            return written_total, True, False, False


def _drain_available(
    selector: selectors.BaseSelector,
    written_total: int,
    max_output_bytes: int,
) -> tuple[int, bool, bool]:
    """Drain already-readable bytes after cleanup without waiting for EOF."""

    while written_total < max_output_bytes and selector.get_map():
        events = selector.select(0)
        if not events:
            break
        for key, _ in events:
            _name, output_file = key.data
            written_total, limited, failed, eof = _consume_stream(
                key.fd, output_file, written_total, max_output_bytes
            )
            if eof:
                try:
                    selector.unregister(key.fd)
                except KeyError:
                    pass
            if failed:
                return written_total, False, True
            if limited:
                return written_total, True, False
    return written_total, False, False


def _valid_inputs(
    argv: list[str],
    timeout: float,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str] | None,
    cwd: str | None,
    max_output_bytes: int,
) -> tuple[bool, str | None, str | None]:
    if (
        not isinstance(argv, list)
        or not argv
        or not argv[0]
        or any(not isinstance(item, str) or "\x00" in item for item in argv)
    ):
        return False, None, None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return False, None, None
    try:
        timeout_is_valid = math.isfinite(float(timeout)) and timeout > 0
    except (OverflowError, TypeError, ValueError):
        timeout_is_valid = False
    if not timeout_is_valid:
        return False, None, None
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
    ):
        return False, None, None
    if cwd is not None and (
        not isinstance(cwd, str) or "\x00" in cwd
    ):
        return False, None, None
    if env is not None:
        if not isinstance(env, dict):
            return False, None, None
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\x00" in key
            or "\x00" in value
            for key, value in env.items()
        ):
            return False, None, None

    try:
        stdout_name = os.fspath(stdout_path)
        stderr_name = os.fspath(stderr_path)
    except TypeError:
        return False, None, None
    if (
        not isinstance(stdout_name, str)
        or not isinstance(stderr_name, str)
        or "\x00" in stdout_name
        or "\x00" in stderr_name
    ):
        return False, None, None
    try:
        if os.path.abspath(stdout_name) == os.path.abspath(stderr_name):
            return False, None, None
    except (TypeError, ValueError):
        return False, None, None
    return True, stdout_name, stderr_name


def _result(
    returncode: int,
    outcome: str,
    timed_out: bool,
    started: float,
) -> CommandResult:
    return CommandResult(
        returncode=returncode,
        outcome=outcome,
        timed_out=timed_out,
        duration_seconds=max(0.0, time.monotonic() - started),
    )


def run_command(
    argv: list[str],
    *,
    timeout: float,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    max_output_bytes: int = 8 * 1024 * 1024,
    guarded: bool = True,
) -> CommandResult:
    """Run one command with bounded, group-aware lifecycle management."""

    started = time.monotonic()
    valid, stdout_name, stderr_name = _valid_inputs(
        argv,
        timeout,
        stdout_path,
        stderr_path,
        env,
        cwd,
        max_output_bytes,
    )
    if not valid:
        return _result(-1, "spawn-error", False, started)

    stdout_file: BinaryIO | None = None
    stderr_file: BinaryIO | None = None
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    previous_handler: signal.Handlers | None = None
    outcome = "spawn-error"
    timed_out = False
    cancelled = threading.Event()
    cleanup_attempted = False
    cleanup_ok = True
    reaped_ok = True
    written_total = 0
    command_argv = list(argv)

    if guarded:
        command_argv = [
            sys.executable,
            os.path.abspath(__file__),
            "--guard-exec",
            *command_argv,
        ]

    def cleanup_owned_group() -> bool:
        nonlocal cleanup_attempted, cleanup_ok
        if process is None:
            return True
        if not cleanup_attempted:
            cleanup_attempted = True
            cleanup_ok = _terminate_process_group(process.pid, process)
        return cleanup_ok

    try:
        try:
            stdout_file = _open_private_output(stdout_name)  # type: ignore[arg-type]
            stderr_file = _open_private_output(stderr_name)  # type: ignore[arg-type]
            stdout_identity = os.fstat(stdout_file.fileno())
            stderr_identity = os.fstat(stderr_file.fileno())
            if (stdout_identity.st_dev, stdout_identity.st_ino) == (
                stderr_identity.st_dev, stderr_identity.st_ino
            ):
                return _result(-1, "spawn-error", False, started)
        except Exception:
            if stdout_file is not None:
                stdout_file.close()
                stdout_file = None
            return _result(-1, "spawn-error", False, started)

        try:
            process = subprocess.Popen(
                command_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
            selector = selectors.DefaultSelector()
            assert process.stdout is not None
            assert process.stderr is not None
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout_file))
            selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr_file))
        except Exception:
            if process is not None:
                cleanup_owned_group()
                reaped_ok = _reap_leader(process, process.pid)
            return _result(-1, "spawn-error", False, started)

        previous_handler = _install_term_handler(process.pid, cancelled)
        deadline = time.monotonic() + float(timeout)
        outcome = "running"

        while True:
            if cancelled.is_set():
                outcome = "cancelled"
                cleanup_owned_group()
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                timed_out = True
                cleanup_owned_group()
                break

            try:
                events = selector.select(min(_POLL_SECONDS, remaining))
            except KeyboardInterrupt:
                cancelled.set()
                outcome = "cancelled"
                cleanup_owned_group()
                break

            for key, _ in events:
                _name, output_file = key.data
                written_total, limited, failed, eof = _consume_stream(
                    key.fd,
                    output_file,
                    written_total,
                    max_output_bytes,
                )
                if eof:
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
                if failed:
                    outcome = "spawn-error"
                    cleanup_owned_group()
                    break
                if limited:
                    outcome = "output-limit"
                    cleanup_owned_group()
                    break
            if outcome != "spawn-error" and outcome in {
                "cancelled",
                "timeout",
                "output-limit",
            }:
                break
            if outcome == "spawn-error":
                break
            if cancelled.is_set():
                outcome = "cancelled"
                cleanup_owned_group()
                break

            returncode = process.poll()
            if returncode is not None:
                outcome = "finished"
                cleanup_owned_group()
                break

        # Cleanup happens before draining so descendants cannot keep this
        # function blocked through inherited pipe endpoints. Only bytes already
        # available are drained, and never beyond the hard output bound.
        if selector is not None and outcome != "spawn-error":
            written_total, limited, failed = _drain_available(
                selector, written_total, max_output_bytes
            )
            if failed:
                outcome = "spawn-error"
            elif limited and outcome == "finished":
                outcome = "output-limit"

        reaped_ok = _reap_leader(process, process.pid)
        returncode = process.poll()
        if returncode is None:
            returncode = -1

        if not cleanup_ok or not reaped_ok:
            outcome = "spawn-error"
        if (
            guarded
            and outcome == "finished"
            and returncode == _GUARD_EXIT_CODE
            and _guard_failure_output(stderr_file)
        ):
            outcome = "guard-unavailable"
        return _result(returncode, outcome, timed_out, started)
    except KeyboardInterrupt:
        if process is not None:
            cancelled.set()
            cleanup_owned_group()
            reaped_ok = _reap_leader(process, process.pid)
            returncode = process.poll()
            if returncode is None:
                returncode = -1
            return _result(
                returncode,
                "spawn-error" if not cleanup_ok or not reaped_ok else "cancelled",
                False,
                started,
            )
        return _result(-1, "spawn-error", False, started)
    except Exception:
        # Keep unexpected runtime failures from leaking an owned process
        # group. The public result intentionally does not expose exception
        # text, argv, cwd, or output paths.
        if process is not None:
            cleanup_owned_group()
            _reap_leader(process, process.pid)
        return _result(-1, "spawn-error", False, started)
    finally:
        _restore_term_handler(previous_handler)
        if selector is not None:
            selector.close()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()


def _guard_failure_output(stderr_file: BinaryIO) -> bool:
    error_bytes = _GUARD_ERROR.encode("utf-8")
    try:
        if os.fstat(stderr_file.fileno()).st_size != len(error_bytes):
            return False
        return os.pread(stderr_file.fileno(), len(error_bytes), 0) == error_bytes
    except OSError:
        return False


def _print_guard_status() -> int:
    try:
        enable_no_hydration()
    except GuardUnavailable:
        print(
            json.dumps(
                {"ok": False, "guard": "no-hydration", "error": "guard-unavailable"},
                separators=(",", ":"),
            )
        )
        return _GUARD_EXIT_CODE

    print(json.dumps({"ok": True, "guard": "no-hydration"}, separators=(",", ":")))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check-guard"]:
        return _print_guard_status()

    if len(args) >= 2 and args[0] == "--guard-exec":
        command = args[1:]
        try:
            enable_no_hydration()
        except GuardUnavailable:
            sys.stderr.write(_GUARD_ERROR)
            return _GUARD_EXIT_CODE

        try:
            os.execvpe(command[0], command, os.environ)
        except OSError:
            sys.stderr.write(_EXEC_ERROR)
            return 127

    sys.stderr.write(_INVALID_INVOCATION)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
