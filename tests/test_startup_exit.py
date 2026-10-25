"""The startup self-test is advisory, and fatal errors exit non-zero (review O4).

The defect: a failed self-test raised SystemExit, and run()'s finally block
ended in os._exit(0), so the process stopped right after the self-test while
reporting success. Security software that intercepts loopback TLS presents its
own certificate, which fails the pinned-certificate self-test on a perfectly
healthy listener; the service then exited ~8 s after every start and NSSM
restart-looped it. The same os._exit(0) also reported any real crash as success.

Each test forces its failure from inside the child process (see e2e_server.py).
"""
import time
import unittest

from e2e_server import AppServer

FAIL_SELF_TEST = (
    "import nwdash.main as _m\n"
    "_m.self_test_dashboard_listener = lambda url, **kw: (False, 'forced self-test failure')\n"
)

CRASH_AFTER_SELF_TEST = FAIL_SELF_TEST + (
    "def _crash(url, enabled=True):\n"
    "    raise RuntimeError('forced fatal error')\n"
    "_m.auto_launch_dashboard = _crash\n"
)

# serve_forever() returning without shutdown() having been called.
LISTENER_DIES = FAIL_SELF_TEST + (
    "import time\n"
    "_real_bind = _m.bind_dashboard_server\n"
    "def _bind(*args, **kwargs):\n"
    "    server, port, random_port = _real_bind(*args, **kwargs)\n"
    "    server.serve_forever = lambda *a, **kw: time.sleep(0.5)\n"
    "    return server, port, random_port\n"
    "_m.bind_dashboard_server = _bind\n"
)

EXIT_TIMEOUT_SECONDS = 60


class TestSelfTestIsAdvisory(unittest.TestCase):
    def test_failed_self_test_keeps_serving(self):
        server = AppServer(prelude=FAIL_SELF_TEST).start(fail_on_exit=True)
        self.addCleanup(server.stop)
        deadline = time.monotonic() + 30
        while "Startup self-test:" not in server.output() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIn("forced self-test failure", server.output())
        # The old code shut the listener down and exited straight after printing
        # the result, so poll for a while rather than checking once.
        for _ in range(8):
            time.sleep(0.25)
            self.assertIsNone(server.proc.poll(), server.output())
            self.assertEqual(server.request("GET", "/api/health")[0], 200)
        self.assertIn("startup self-test failed; still serving", server.output())


class TestFatalErrorsExitNonZero(unittest.TestCase):
    def _exit_code(self, prelude):
        server = AppServer(prelude=prelude)
        self.addCleanup(server.stop)
        server.launch()
        code = server.wait_exit(EXIT_TIMEOUT_SECONDS)
        self.assertIsNotNone(code, f"process still running after {EXIT_TIMEOUT_SECONDS}s")
        return code, server.output()

    def test_unexpected_error_exits_non_zero(self):
        code, output = self._exit_code(CRASH_AFTER_SELF_TEST)
        self.assertNotEqual(code, 0, output)
        self.assertIn("forced fatal error", output)

    def test_listener_stopping_on_its_own_exits_non_zero(self):
        code, output = self._exit_code(LISTENER_DIES)
        self.assertNotEqual(code, 0, output)
        self.assertIn("HTTPS listener stopped unexpectedly", output)


if __name__ == "__main__":
    unittest.main()
