"""Boot the real app in a subprocess for end-to-end tests.

The server runs from a temp-dir copy of the entry script + nwdash package so it
creates its own data/ and .certs/ and never touches the project's state (the
same pattern as tests/test_server_http.py). A test may pass `prelude` code that
runs in the child before nwdash.main.main(), to patch the app from inside that
process — e.g. to force the startup self-test to fail.

Not a test module itself: discovery only collects test*.py.
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT_SECONDS = 60

_LAUNCHER = """\
import sys
{prelude}
from nwdash.main import main
raise SystemExit(main(sys.argv[1:]))
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AppServer:
    """One dashboard process on 127.0.0.1:<free port>."""

    def __init__(self, password: str = "", args: tuple[str, ...] = (), prelude: str = "") -> None:
        self.password = password
        self.extra_args = list(args)
        self.prelude = prelude
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.ctx = ssl._create_unverified_context()
        self._tmp = tempfile.TemporaryDirectory(prefix="nwdash-e2e-")
        self.root = Path(self._tmp.name)
        self._log_path = self.root / "server-stdout.log"
        self._log = None

    def launch(self, bind: str = "127.0.0.1") -> None:
        """Start the process without waiting for it (see start())."""
        shutil.copy2(PROJECT_DIR / "networker_dashboard.py", self.root / "networker_dashboard.py")
        shutil.copytree(PROJECT_DIR / "nwdash", self.root / "nwdash", ignore=shutil.ignore_patterns("__pycache__"))
        entry = "networker_dashboard.py"
        if self.prelude:
            entry = "launch_for_test.py"
            (self.root / entry).write_text(_LAUNCHER.format(prelude=self.prelude), encoding="utf-8")
        env = dict(os.environ)
        env.pop("DASHBOARD_AUTH_PASSWORD", None)
        if self.password:
            env["DASHBOARD_AUTH_PASSWORD"] = self.password
        env["PYTHONUNBUFFERED"] = "1"  # prints reach the log as they happen
        self._log = open(self._log_path, "wb")  # noqa: SIM115 — lives as long as the process
        try:
            self.proc = subprocess.Popen(
                [sys.executable, entry, "--port", str(self.port), "--bind", bind, "--no-launch", *self.extra_args],
                cwd=str(self.root), env=env, stdout=self._log, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self.stop()
            raise unittest.SkipTest(f"could not launch server subprocess: {exc}") from exc

    def start(self, fail_on_exit: bool = False) -> "AppServer":
        """Launch and wait for /api/health. An early exit is a SkipTest (the port
        can't be served here) unless `fail_on_exit`, when exiting is the defect."""
        self.launch()
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        last_error = "no response"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                message = f"server exited early (code {self.proc.returncode}):\n{self.output()[-2000:]}"
                self.stop()
                if fail_on_exit:
                    raise AssertionError(message)
                raise unittest.SkipTest(message)
            try:
                if self.request("GET", "/api/health", timeout=5)[0] == 200:
                    return self
            except OSError as exc:
                last_error = str(exc)
            time.sleep(0.25)
        self.stop()
        raise unittest.SkipTest(f"server did not become healthy within {STARTUP_TIMEOUT_SECONDS}s ({last_error})")

    def request(self, method, path, body=None, headers=None, cookie="", timeout=20, data=None):
        """(status, body bytes, headers). `body` is sent as JSON; `data` as raw bytes."""
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = cookie
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(f"https://127.0.0.1:{self.port}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def login(self) -> tuple[str, str]:
        """(cookie header value, CSRF token) for the configured password."""
        status, body, headers = self.request("POST", "/api/login", {"password": self.password})
        if status != 200:
            raise AssertionError(f"login failed: {status} {body!r}")
        return headers.get("Set-Cookie", "").split(";", 1)[0], json.loads(body)["csrfToken"]

    def wait_exit(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def output(self) -> str:
        try:
            return self._log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        if self._log is not None:
            self._log.close()
            self._log = None
        try:
            self._tmp.cleanup()
        except OSError:
            pass  # transient Windows file locks on the temp dir; the OS cleans it later
