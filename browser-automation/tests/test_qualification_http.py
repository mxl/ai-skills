"""Fixture-server regressions without binding any network listener."""
from pathlib import Path
import importlib.util
import sys
import threading
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "qualify_shared_runtime.py"
SPEC = importlib.util.spec_from_file_location("qualification_http_tests", SOURCE)
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


class FixtureHTTPTests(unittest.TestCase):
    def test_idle_preconnection_does_not_serialize_the_next_request(self):
        server = harness.FixtureHTTPServer(("127.0.0.1", 0), harness.SyntheticPage,
                                          bind_and_activate=False)
        first_entered = threading.Event()
        first_finished = threading.Event()
        release_first = threading.Event()
        second_served = threading.Event()

        def finish(request, address):
            if request == "idle-preconnection":
                first_entered.set()
                release_first.wait(3)
                first_finished.set()
            else:
                second_served.set()

        server.finish_request = mock.Mock(side_effect=finish)
        server.shutdown_request = mock.Mock()
        try:
            self.assertEqual(server.socket.getsockname()[1], 0)
            server.process_request("idle-preconnection", ("127.0.0.1", 1))
            self.assertTrue(first_entered.wait(1))
            server.process_request("navigation", ("127.0.0.1", 2))
            self.assertTrue(second_served.wait(1))
            self.assertFalse(first_finished.is_set(), "Idle connection serialized navigation")
            self.assertFalse(server.daemon_threads)
            self.assertTrue(server.block_on_close)
        finally:
            release_first.set()
            server.server_close()
        self.assertTrue(first_finished.is_set())

    def test_handler_reads_are_bounded(self):
        handler = harness.SyntheticPage.__new__(harness.SyntheticPage)
        handler.request = mock.Mock()
        with mock.patch.object(harness.BaseHTTPRequestHandler, "setup") as parent:
            handler.setup()
        handler.request.settimeout.assert_called_once_with(5)
        parent.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
