"""A slow live viewer must not hold anyone up, and frames must arrive whole.

The defect (review F3): sse_broadcast ran in the request that built the
dashboard (a Connect) and wrote every viewer's TLS socket itself, with no lock.
One viewer that stopped reading held that request until the socket timed out
(~30 s), and two broadcasts, or a broadcast and the viewer's own heartbeat,
could interleave their bytes on one stream. Each viewer now has a bounded
queue; a broadcast only enqueues, and the viewer's own handler thread is the
only writer to its socket. A viewer that falls too far behind is dropped.

The stream tests run the real DashboardHandler on an in-process HTTPS server.
"""
import json
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from nwdash import config as _cfg
from nwdash import models, server
from nwdash.certs import server_tls_context, write_embedded_dev_certificate

HANDLER_TIMEOUT_SECONDS = 8  # a broadcast that waits on the stalled viewer waits this long
BROADCAST_BUDGET_SECONDS = 1.0


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _dashboard(tag, n, size):
    return json.dumps({"ok": True, "sessionId": "s-1", "tag": tag, "n": n, "pad": tag * size}, separators=(",", ":"))


class TestSseClientQueue(unittest.TestCase):
    def test_frames_come_out_in_order_then_a_heartbeat_slot(self):
        client = models.SseClient(limit=4)
        self.assertTrue(client.offer(b"one"))
        self.assertTrue(client.offer(b"two"))
        self.assertEqual(client.next_frame(1), b"one")
        self.assertEqual(client.next_frame(1), b"two")
        self.assertEqual(client.next_frame(0.05), b"", "nothing queued: time for a heartbeat")

    def test_a_viewer_too_far_behind_is_closed_not_waited_on(self):
        client = models.SseClient(limit=3)
        for frame in (b"1", b"2", b"3"):
            self.assertTrue(client.offer(frame))
        started = time.monotonic()
        self.assertFalse(client.offer(b"4"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(client.closed)
        self.assertIsNone(client.next_frame(1), "a dropped viewer gets nothing more")
        self.assertFalse(client.offer(b"5"))

    def test_close_wakes_a_waiting_handler(self):
        client = models.SseClient()
        got = []
        waiter = threading.Thread(target=lambda: got.append(client.next_frame(30)))
        waiter.start()
        time.sleep(0.1)
        client.close()
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(got, [None])


class TestBroadcastOnlyQueues(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(models, "SSE_CLIENTS", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_viewer_gets_the_push_with_its_session(self):
        # Review F1: pages apply only their own session's pushes, which needs
        # every push to carry its session; the TV takes any push.
        viewers = [models._sse_register() for _ in range(3)]
        with mock.patch.dict(models.SHARED_DASHBOARD_STATE, {"lastError": ""}):
            models.set_shared_dashboard("session-a", {"ok": True, "stale": True, "reportNotice": "cached"})
        for viewer in viewers:
            frame = viewer.next_frame(1)
            event, data = frame.decode().rstrip("\n").split("\n")
            self.assertEqual(event, "event: dashboard")
            self.assertEqual(json.loads(data[len("data: "):])["sessionId"], "session-a")

    def test_a_full_viewer_is_dropped_and_the_others_kept(self):
        stalled = models._sse_register()
        healthy = models._sse_register()
        for n in range(models.SSE_QUEUE_FRAMES + 1):
            models.sse_broadcast("dashboard", str(n))
            self.assertEqual(healthy.next_frame(1), f"event: dashboard\ndata: {n}\n\n".encode())
        self.assertTrue(stalled.closed)
        self.assertEqual(models.SSE_CLIENTS, [healthy])

    def test_the_viewer_cap_still_holds(self):
        with mock.patch.object(_cfg, "MAX_SSE_CLIENTS", 2):
            self.assertIsNotNone(models._sse_register())
            self.assertIsNotNone(models._sse_register())
            self.assertIsNone(models._sse_register())


class _Viewer:
    """An /api/stream reader on its own thread, collecting whole events."""

    def __init__(self, port, read=True, rcvbuf=None):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        raw.settimeout(15)
        raw.connect(("127.0.0.1", port))
        self.sock = ssl._create_unverified_context().wrap_socket(raw, server_hostname="localhost")
        self.sock.sendall(b"GET /api/stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.events = []
        self.heartbeats = 0
        self.headers = b""
        self._cond = threading.Condition()
        self._thread = None
        if read:
            self._thread = threading.Thread(target=self._read, daemon=True)
            self._thread.start()

    def _read(self):
        buffer = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            if not self.headers:
                if b"\r\n\r\n" not in buffer:
                    continue
                self.headers, buffer = buffer.split(b"\r\n\r\n", 1)
            *whole, buffer = buffer.split(b"\n\n")
            with self._cond:
                for event in whole:
                    if event == b": heartbeat":
                        self.heartbeats += 1
                    else:
                        self.events.append(event)
                self._cond.notify_all()

    def wait_for_events(self, count, timeout=15.0):
        with self._cond:
            self._cond.wait_for(lambda: len(self.events) >= count, timeout)
            return list(self.events)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TestStreamWriters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cert, key = Path(cls._tmp.name) / "server.crt", Path(cls._tmp.name) / "server.key"
        write_embedded_dev_certificate(cert, key)
        cls.httpd = server.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler, max_connections=16)
        cls.httpd.ssl_context = server_tls_context(cert, key)
        cls.port = cls.httpd.server_address[1]
        cls._serve = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls._serve.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._serve.join(5)
        cls._tmp.cleanup()

    def setUp(self):
        for patcher in (
            mock.patch.object(_cfg, "AUTH_ENABLED", False),
            mock.patch.object(models, "SSE_CLIENTS", []),
            mock.patch.object(server, "shared_dashboard_payload", lambda: {"ok": False}),
            mock.patch.object(server.DashboardHandler, "timeout", HANDLER_TIMEOUT_SECONDS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def viewer(self, **kwargs):
        before = len(models.SSE_CLIENTS)
        viewer = _Viewer(self.port, **kwargs)
        self.addCleanup(viewer.close)
        self.assertTrue(_wait_until(lambda: len(models.SSE_CLIENTS) > before), "viewer never registered")
        return viewer

    def test_a_stalled_viewer_does_not_delay_the_broadcast_to_the_others(self):
        stalled = self.viewer(read=False, rcvbuf=4096)  # connects, then never reads
        healthy = self.viewer()
        frames = models.SSE_QUEUE_FRAMES + 8
        size = 512 * 1024  # far more than the stalled socket's buffers hold
        for n in range(frames):
            started = time.monotonic()
            models.sse_broadcast("dashboard", _dashboard("x", n, size))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, BROADCAST_BUDGET_SECONDS, f"broadcast {n} waited {elapsed:.1f}s on a viewer")
            self.assertEqual(len(healthy.wait_for_events(n + 1)), n + 1, f"healthy viewer never got frame {n}")
        received = [json.loads(event.split(b"\ndata: ", 1)[1]) for event in healthy.events]
        self.assertEqual([item["n"] for item in received], list(range(frames)))
        self.assertTrue(all(item["pad"] == "x" * size for item in received))
        self.assertTrue(_wait_until(lambda: len(models.SSE_CLIENTS) == 1), "the stalled viewer was never dropped")
        self.assertIn(b"text/event-stream", healthy.headers)
        stalled.close()

    def test_frames_stay_whole_when_two_broadcasts_overlap(self):
        healthy = self.viewer()
        size = 256 * 1024
        per_round = models.SSE_QUEUE_FRAMES // 2  # both senders together never overfill the queue
        rounds = 5
        with mock.patch.object(server, "SSE_HEARTBEAT_SECONDS", 0.01):  # heartbeats race the frames too
            for r in range(rounds):
                barrier = threading.Barrier(2)

                def send(tag, r=r, barrier=barrier):
                    barrier.wait()
                    for i in range(per_round):
                        models.sse_broadcast("dashboard", _dashboard(tag, r * per_round + i, size))

                senders = [threading.Thread(target=send, args=(tag,)) for tag in "ab"]
                for sender in senders:
                    sender.start()
                for sender in senders:
                    sender.join(10)
                expected = 2 * per_round * (r + 1)
                self.assertEqual(len(healthy.wait_for_events(expected)), expected, f"round {r}: frames missing")
                beats = healthy.heartbeats
                self.assertTrue(_wait_until(lambda beats=beats: healthy.heartbeats > beats), "no heartbeat between rounds")
        seen = {"a": [], "b": []}
        for event in healthy.events:
            head, _, data = event.partition(b"\ndata: ")
            self.assertEqual(head, b"event: dashboard")
            item = json.loads(data)  # a torn frame is not JSON
            self.assertEqual(item["pad"], item["tag"] * size, "frame bytes interleaved")
            seen[item["tag"]].append(item["n"])
        for tag in "ab":
            self.assertEqual(seen[tag], list(range(rounds * per_round)), f"sender {tag}'s frames out of order")
        self.assertEqual(len(models.SSE_CLIENTS), 1, "a keeping-up viewer must not be dropped")

    def test_a_new_viewer_first_gets_the_shared_dashboard_with_its_session(self):
        shared = {"ok": True, "dashboard": {"ok": True, "sessionId": "session-a", "summary": {}}}
        with mock.patch.object(server, "shared_dashboard_payload", lambda: shared):
            healthy = self.viewer()
            first = healthy.wait_for_events(1)[0]
        self.assertEqual(json.loads(first.split(b"\ndata: ", 1)[1])["sessionId"], "session-a")

    def test_a_dropped_viewer_s_connection_is_closed(self):
        viewer = self.viewer(read=False)
        models.SSE_CLIENTS[0].close()  # what a broadcast does to a viewer too far behind
        viewer.sock.settimeout(HANDLER_TIMEOUT_SECONDS + 5)
        started = time.monotonic()
        data = b""
        while True:
            try:
                chunk = viewer.sock.recv(65536)
            except (ConnectionResetError, ConnectionAbortedError, ssl.SSLError):
                break
            if not chunk:
                break
            data += chunk
        self.assertLess(time.monotonic() - started, HANDLER_TIMEOUT_SECONDS, "connection left open after the drop")
        self.assertIn(b"200 OK", data)
        self.assertTrue(_wait_until(lambda: not models.SSE_CLIENTS))


if __name__ == "__main__":
    unittest.main()
