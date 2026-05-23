import sys
import unittest

import networker_dashboard as nd


class _FakeResolver:
    """Monkeypatch target for socket.getaddrinfo returning fixed IPs per host."""
    def __init__(self, mapping):
        self.mapping = mapping

    def __call__(self, host, *args, **kwargs):
        ips = self.mapping.get(host)
        if ips is None:
            import socket as _s
            raise _s.gaierror(f"no fake entry for {host}")
        return [(None, None, None, "", (ip, 0)) for ip in ips]


class SsrfAllowlistTests(unittest.TestCase):
    def setUp(self):
        import socket
        self._orig_gai = socket.getaddrinfo
        nd.configure_allowed_hosts("")

    def tearDown(self):
        import socket
        socket.getaddrinfo = self._orig_gai
        nd.configure_allowed_hosts("")

    def _patch(self, mapping):
        import socket
        socket.getaddrinfo = _FakeResolver(mapping)

    def test_disabled_allows_anything(self):
        self.assertFalse(nd.ALLOWLIST_ENABLED)
        self.assertTrue(nd._host_allowed("anything.example.com"))

    def test_cidr_entry(self):
        self._patch({})
        nd.configure_allowed_hosts("10.0.0.0/24")
        self.assertTrue(nd.ALLOWLIST_ENABLED)
        self.assertTrue(nd._host_allowed("10.0.0.5"))
        self.assertFalse(nd._host_allowed("10.0.1.5"))

    def test_hostname_pinned_ip_allowed(self):
        self._patch({"nw1.local": ["10.0.0.5"]})
        nd.configure_allowed_hosts("nw1.local")
        self.assertTrue(nd._host_allowed("nw1.local"))

    def test_hostname_rebinding_rejected(self):
        self._patch({"nw1.local": ["10.0.0.5"]})
        nd.configure_allowed_hosts("nw1.local")
        self._patch({"nw1.local": ["66.66.66.66"]})
        self.assertFalse(nd._host_allowed("nw1.local"))

    def test_unlisted_hostname_rejected(self):
        self._patch({"evil.local": ["10.0.0.5"], "nw1.local": ["10.0.0.5"]})
        nd.configure_allowed_hosts("nw1.local")
        self.assertFalse(nd._host_allowed("evil.local"))

    def test_assert_raises_on_blocked(self):
        self._patch({"nw1.local": ["10.0.0.5"]})
        nd.configure_allowed_hosts("10.0.0.0/24")
        cfg = nd.ApiConfig(
            rest_api_host="9.9.9.9", rest_api_port=9090, backup_server_host="9.9.9.9",
            backup_server_port=9090, username="u", password="p", api_mode="nwui",
            api_version="auto", report_range="24h", custom_start_date="", custom_end_date="",
            use_wmi_health=False, wmi_username="", wmi_password="", timeout_seconds=30,
            verify_tls=False, use_authc_header=False,
        )
        with self.assertRaises(nd.BadRequest):
            nd._assert_host_allowed(cfg)


class SsrfValidateTests(unittest.TestCase):
    def setUp(self):
        import socket
        self._orig = socket.getaddrinfo
        socket.getaddrinfo = lambda host, *a, **k: [(None, None, None, "", ("9.9.9.9", 0))]
        nd.configure_allowed_hosts("10.0.0.0/24")

    def tearDown(self):
        import socket
        socket.getaddrinfo = self._orig
        nd.configure_allowed_hosts("")

    def test_validate_payload_blocks_disallowed_host(self):
        with self.assertRaises(nd.BadRequest):
            nd.validate_payload({"restApiHost": "8.8.8.8", "username": "u", "password": "p"})


class DpapiHelperTests(unittest.TestCase):
    def test_read_plaintext_passthrough(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        p = d / ".somekey"
        p.write_bytes(b"raw-legacy-key-bytes")
        self.assertEqual(nd._read_protected_key(p), b"raw-legacy-key-bytes")
        self.assertFalse(nd._key_file_is_wrapped(p))

    def test_read_missing_returns_none(self):
        from pathlib import Path
        import tempfile
        self.assertIsNone(nd._read_protected_key(Path(tempfile.mkdtemp()) / "nope"))


@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
class DpapiWindowsTests(unittest.TestCase):
    def test_protect_unprotect_roundtrip(self):
        data = b"\x00\x01secret-key-bytes\xff\x0a"
        blob = nd._dpapi_protect(data)
        self.assertNotEqual(blob, data)
        self.assertEqual(nd._dpapi_unprotect(blob), data)

    def test_write_then_read_wrapped(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        p = d / ".wrapkey"
        nd._write_protected_key(p, b"the-key")
        self.assertTrue(p.read_bytes().startswith(nd.DPAPI_MARKER))
        self.assertTrue(nd._key_file_is_wrapped(p))
        self.assertEqual(nd._read_protected_key(p), b"the-key")


if __name__ == "__main__":
    unittest.main()
