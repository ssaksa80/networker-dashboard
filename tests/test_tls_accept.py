"""A client that never completes a TLS handshake must not block anyone else.

The defect (review S3): run() TLS-wrapped the LISTENING socket, so accept()
ran each handshake on the serve_forever thread. One TCP connection that never
sent a ClientHello blocked every new connection until it closed; no login was
needed. Handshakes now run in each connection's worker thread, bounded by the
request timeout.
"""
import socket
import time
import unittest

from e2e_server import AppServer

REQUEST_TIMEOUT_SECONDS = 5  # run() floors --request-timeout at 5


class TestHandshakeOffTheAcceptLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = AppServer(args=("--request-timeout", str(REQUEST_TIMEOUT_SECONDS))).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _idle_connection(self):
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def _health_within(self, seconds):
        started = time.monotonic()
        status = self.server.request("GET", "/api/health", timeout=seconds)[0]
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, seconds)

    def test_idle_tcp_connections_do_not_block_health(self):
        for _ in range(3):
            self._idle_connection()
        time.sleep(0.3)  # let the server accept them
        self._health_within(2.0)

    def test_garbage_instead_of_a_client_hello_drops_only_that_connection(self):
        sock = self._idle_connection()
        sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: x\r\n\r\n")  # plain HTTP to a TLS port
        self._health_within(2.0)

    def test_stalled_handshake_is_dropped_after_the_request_timeout(self):
        sock = self._idle_connection()
        sock.settimeout(REQUEST_TIMEOUT_SECONDS + 10)
        started = time.monotonic()
        try:
            data = sock.recv(1)
        except (ConnectionResetError, ConnectionAbortedError):
            data = b""
        self.assertEqual(data, b"", "server should close a connection that never handshakes")
        self.assertLess(time.monotonic() - started, REQUEST_TIMEOUT_SECONDS + 5)
        self._health_within(2.0)


if __name__ == "__main__":
    unittest.main()
