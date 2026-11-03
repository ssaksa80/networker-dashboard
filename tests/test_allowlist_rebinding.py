"""The host allow-list holds when connecting, not only when a request is
checked (review S8).

validate_payload checked an allow-listed name against one DNS lookup, and
urllib then looked the name up again to connect. An answer that changed in
between (DNS rebinding) sent the request, NetWorker credential included, to an
address the allow-list never approved. The connection now dials the addresses
from the lookup it checks, and still gives TLS and HTTP the name.

DNS is faked and addresses come from the documentation ranges (RFC 5737), so
nothing here leaves the machine.
"""
import http.server
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from nwdash import nwui, profiles, restapi
from nwdash.certs import EMBEDDED_DEV_CERT_PEM, EMBEDDED_DEV_KEY_PEM
from nwdash.models import RestApiError

NAME = "nw01.example.test"
CHECKED_IP = "192.0.2.10"
REBOUND_IP = "203.0.113.66"


class FakeDns:
    """getaddrinfo that answers `answer` for NAME and asks the real resolver
    about anything else (IP literals, in practice)."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.real = socket.getaddrinfo

    def __call__(self, host, port, *args, **kwargs):
        if str(host).lower() == NAME:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (self.answer, int(port or 0)))]
        return self.real(host, port, *args, **kwargs)


class AllowListedName(unittest.TestCase):
    """NAME is the only allow-list entry, pinned to `first_answer`. A proxy
    from the environment or the registry would take the dial away from us."""

    first_answer = CHECKED_IP

    def setUp(self):
        self.dns = FakeDns(self.first_answer)
        for target, value in (("socket.getaddrinfo", self.dns), ("urllib.request.getproxies", dict)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        profiles.configure_allowed_hosts(NAME)
        self.addCleanup(profiles.configure_allowed_hosts, "")

    def checked_config(self, port: int = 9090):
        return profiles.validate_payload({
            "restApiHost": NAME, "restApiPort": port, "username": "admin", "password": "not-a-real-password",
            "useWmiHealth": False, "timeoutSeconds": 5,
        })


class TestARebindAfterTheCheckIsNotFollowed(AllowListedName):
    def setUp(self):
        super().setUp()
        self.dialled: list[str] = []
        patcher = mock.patch("socket.create_connection", self.refuse)
        patcher.start()
        self.addCleanup(patcher.stop)

    def refuse(self, address, *args, **kwargs):
        """Stands in for the network: notes where the connection would have
        gone, looking the name up as socket.create_connection does, and refuses."""
        host, port = address
        self.dialled.append(socket.getaddrinfo(host, port)[0][4][0])
        raise ConnectionRefusedError("refused by the test")

    def rebound_config(self):
        config = self.checked_config()
        self.dns.answer = REBOUND_IP  # the record changes once the check has passed
        return config

    def assertRefusalLogged(self, logs):
        self.assertIn("allowlist_connect_refused", [getattr(record, "event", "") for record in logs.records])

    def test_a_rest_request(self):
        config = self.rebound_config()
        with self.assertLogs(profiles.LOG, "WARNING") as logs:
            with self.assertRaises(RestApiError) as caught:
                restapi.fetch_json(f"{restapi.api_base_url(config)}/alerts", {}, 5, restapi.ssl_context_for_api(True), "test")
            self.assertNotIn(REBOUND_IP, self.dialled)
            self.assertIn(f"Host '{NAME}' is not in the configured allow-list", caught.exception.message)
        self.assertRefusalLogged(logs)

    def test_an_nwui_login(self):
        config = self.rebound_config()
        with self.assertLogs(profiles.LOG, "WARNING") as logs:
            _status, body = nwui.build_dashboard_nwui(config, create_session=False)
            self.assertNotIn(REBOUND_IP, self.dialled)
            self.assertIn(f"Host '{NAME}' is not in the configured allow-list", body["sources"]["nwuiLogin"]["error"])
        self.assertRefusalLogged(logs)

    def test_without_a_rebind_the_checked_address_is_dialled(self):
        config = self.checked_config()
        with self.assertRaises(RestApiError):
            restapi.fetch_json(f"{restapi.api_base_url(config)}/alerts", {}, 5, restapi.ssl_context_for_api(True), "test")
        self.assertEqual(self.dialled, [CHECKED_IP])


class _JsonHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hosts.append(self.headers.get("Host", ""))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _RecordingContext(ssl.SSLContext):
    """Notes the peer and the name each TLS connection is opened with. The
    name is what OpenSSL sends as SNI and checks the certificate against; the
    check itself is off, as security software may re-sign loopback TLS."""

    def wrap_socket(self, sock, *args, **kwargs):
        self.seen.append((sock.getpeername()[0], kwargs.get("server_hostname")))
        return super().wrap_socket(sock, *args, **kwargs)


class TestTlsAndHttpStillCarryTheName(AllowListedName):
    first_answer = "127.0.0.1"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = Path(tempfile.mkdtemp(prefix="nwdash-rebinding-"))
        (cls.folder / "cert.pem").write_text(EMBEDDED_DEV_CERT_PEM, encoding="ascii")
        (cls.folder / "key.pem").write_text(EMBEDDED_DEV_KEY_PEM, encoding="ascii")
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cls.folder / "cert.pem", cls.folder / "key.pem")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
        cls.server.hosts = []
        cls.server.socket = server_ctx.wrap_socket(cls.server.socket, server_side=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.folder, ignore_errors=True)
        super().tearDownClass()

    def test_a_request_reaches_the_pinned_address_by_name(self):
        port = self.server.server_address[1]
        context = _RecordingContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.seen = []

        config = self.checked_config(port)
        result = restapi.fetch_json(f"{restapi.api_base_url(config)}/probe", {}, 5, context, "test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(context.seen, [("127.0.0.1", NAME)])
        self.assertEqual(self.server.hosts, [f"{NAME}:{port}"])


if __name__ == "__main__":
    unittest.main()
