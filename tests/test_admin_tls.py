"""Replacing the serving certificate from the admin console's TLS tab.

Unit level: the checks an upload must pass before anything is replaced, the
files it writes and keeps, and putting the previous pair back. End to end: an
upload changes the certificate the running listener presents to new
connections, without a restart.

Certificates come from tests/certgen.py (standard library only), so no
private key is committed to this public repository.
"""
import json
import socket
import ssl
import unittest
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from unittest import mock

import certgen
from e2e_server import AppServer
from test_admin import TempDataMixin

from nwdash import admin
from nwdash import config as _cfg
from nwdash.certs import EMBEDDED_DEV_CERT_PEM, server_tls_context, write_embedded_dev_certificate
from nwdash.server import listener_trust_context

# Dates and names are read with ssl's certificate decoder, a private API that
# every supported Python has; the checks that need it are skipped without it.
needs_decoder = unittest.skipUnless(hasattr(getattr(ssl, "_ssl", None), "_test_decode_cert"), "ssl has no certificate decoder")


class Keys:
    """Keys are slow to make in pure Python: one set per test class."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = certgen.RsaKey()
        cls.other_key = certgen.RsaKey()
        cls.ca_key = certgen.RsaKey()
        cls.cert = certgen.certificate(cls.key)  # localhost, 127.0.0.1
        cls.ca = certgen.certificate(cls.ca_key, cn="Test Issuing CA", dns=(), ips=(), ca=True)


class TestParseUpload(unittest.TestCase):
    CERT = EMBEDDED_DEV_CERT_PEM

    def test_blocks_are_cleaned_and_text_around_them_dropped(self):
        key_body = "MIIB" + "A" * 60
        chain, key = admin.parse_certificate_upload(
            "Bag Attributes\n    localKeyID: 01\r\n" + self.CERT.replace("\n", "\r\n") + "subject=/CN=x\n" + self.CERT,
            f"-----BEGIN PRIVATE KEY-----\n  {key_body}  \n-----END PRIVATE KEY-----\n")
        self.assertEqual(chain, [self.CERT, self.CERT])
        self.assertEqual(key, f"-----BEGIN PRIVATE KEY-----\n{key_body}\n-----END PRIVATE KEY-----\n")

    def test_the_key_may_come_in_the_certificate_box(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----\n"
        chain, found = admin.parse_certificate_upload(self.CERT + key, "")
        self.assertEqual((chain, found), ([self.CERT], key))

    def test_refusals(self):
        key = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
        old_style_encrypted = ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,00\n\nMIIE\n"
                               "-----END RSA PRIVATE KEY-----\n")
        cases = {
            "pfx": ("MIIKdQIBAzCCCi8GCSqGSIb3DQEHAaCCCiAEggocMIIKGDCCBM8GCSqG", key),
            "No private key": (self.CERT, ""),
            "one private key": (self.CERT, key + key),
            "passphrase": (self.CERT, "-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIF\n-----END ENCRYPTED PRIVATE KEY-----\n"),
            "passphrase ": (self.CERT, old_style_encrypted),
            "too large": (self.CERT + " " * admin.TLS_UPLOAD_MAX_CHARS, key),
            "at most": (self.CERT * (admin.TLS_CHAIN_MAX + 1), key),
            "characters": (self.CERT.replace("MIID", "MIÏD"), key),
        }
        for expected, (cert_text, key_text) in cases.items():
            with self.subTest(expected), self.assertRaisesRegex(ValueError, expected.strip()):
                admin.parse_certificate_upload(cert_text, key_text)

    def test_non_text_is_no_certificate(self):
        with self.assertRaisesRegex(ValueError, "No certificate"):
            admin.parse_certificate_upload(None, ["x"])


class TestNames(unittest.TestCase):
    def test_request_host_drops_the_port(self):
        for header, host in (("nwdash.example.test:8443", "nwdash.example.test"), ("[::1]:8443", "::1"),
                             ("[::1]", "::1"), ("198.51.100.7", "198.51.100.7"), ("", ""), (None, "")):
            self.assertEqual(admin.request_host(header), host, header)

    def test_covers_like_a_browser(self):
        names = (("DNS", "NWDash.example.test"), ("DNS", "*.apps.example.test"),
                 ("IP Address", "198.51.100.7"), ("IP Address", "2001:DB8:0:0:0:0:0:1\n"))
        for host in ("nwdash.example.test", "NWDASH.EXAMPLE.TEST.", "a.apps.example.test", "198.51.100.7", "2001:db8::1"):
            self.assertTrue(admin.certificate_covers(names, host), host)
        for host in ("example.test", "a.b.apps.example.test", "apps.example.test", "198.51.100.8", "other.example.test"):
            self.assertFalse(admin.certificate_covers(names, host), host)

    def test_the_common_name_alone_covers_nothing(self):
        self.assertFalse(admin.certificate_covers((), "localhost"))


class TestInstall(Keys, TempDataMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cert_file, self.key_file = self.dir / "certs" / "nwdash.crt", self.dir / "certs" / "nwdash.key"
        write_embedded_dev_certificate(self.cert_file, self.key_file)
        for name, value in (("TLS_CERT_FILE", str(self.cert_file)), ("TLS_KEY_FILE", str(self.key_file)), ("TLS_CERT_AUTO", True)):
            patcher = mock.patch.object(_cfg, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.applied = []

    def apply(self, context):
        self.applied.append(context)
        return True

    def install(self, cert, key, **kwargs):
        kwargs.setdefault("host", "localhost")
        return admin.install_certificate(cert, key, sid="s1", client_ip="127.0.0.1", apply=self.apply, **kwargs)

    def assert_untouched(self):
        self.assertEqual(self.cert_file.read_text(encoding="ascii"), EMBEDDED_DEV_CERT_PEM)
        self.assertEqual(self.applied, [])
        self.assertEqual(sorted(p.name for p in self.cert_file.parent.iterdir()), ["nwdash.crt", "nwdash.key"])

    def test_upload_replaces_the_pair_and_keeps_the_old_one(self):
        status, body = self.install(self.cert, self.key.pem())
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertTrue(body["applied"])
        self.assertEqual(len(self.applied), 1)
        self.assertIsInstance(self.applied[0], ssl.SSLContext)
        self.assertEqual(self.cert_file.read_text(encoding="ascii"), self.cert)
        self.assertEqual(self.key_file.read_text(encoding="ascii"), self.key.pem())
        self.assertEqual((self.dir / "certs" / "nwdash.crt.previous").read_text(encoding="ascii"), EMBEDDED_DEV_CERT_PEM)
        self.assertTrue((self.dir / "certs" / "nwdash.key.previous").exists())
        self.assertFalse(list(self.cert_file.parent.glob("*.new")))
        self.assertFalse(_cfg.TLS_CERT_AUTO)
        tls = body["tls"]
        self.assertFalse(tls["auto"])
        self.assertIn("previous", tls)
        self.assertEqual(tls["keyFile"], "nwdash.key")
        self.assertNotIn("PRIVATE", json.dumps(body))
        row = self.audit_rows()[0]
        self.assertEqual((row["action"], row["item"], row["field"]), ("TLS certificate replaced", "nwdash.crt", "certificate"))
        self.assertNotIn("PRIVATE", json.dumps(row))

    def test_a_key_from_another_certificate_is_refused(self):
        status, body = self.install(self.cert, self.other_key.pem())
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("does not belong", body["error"])
        self.assert_untouched()

    def test_a_short_key_is_refused(self):
        small = certgen.RsaKey(1024)
        status, body = self.install(certgen.certificate(small), small.pem())
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("too short", body["error"])
        self.assert_untouched()

    @needs_decoder
    def test_a_certificate_outside_its_dates_is_refused(self):
        now = datetime.now(UTC)
        expired = certgen.certificate(self.key, not_before=now - timedelta(days=400), days=30)
        future = certgen.certificate(self.key, not_before=now + timedelta(days=2))
        for cert, words in ((expired, "expired on"), (future, "not valid until")):
            with self.subTest(words):
                status, body = self.install(cert, self.key.pem())
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertIn(words, body["error"])
                self.assert_untouched()

    @needs_decoder
    def test_a_certificate_for_other_names_needs_confirming(self):
        cert = certgen.certificate(self.key, cn="nwdash.example.test", dns=("nwdash.example.test",), ips=())
        status, body = self.install(cert, self.key.pem(), host="127.0.0.1")
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual((body["code"], body["host"], body["names"]), ("name_not_covered", "127.0.0.1", ["nwdash.example.test"]))
        self.assert_untouched()
        status, body = self.install(cert, self.key.pem(), host="127.0.0.1", confirm_name=True)
        self.assertEqual(status, HTTPStatus.OK, body)
        status, _body = self.install(self.cert, self.key.pem(), host="nwdash.example.test")
        self.assertEqual(status, HTTPStatus.CONFLICT)

    @needs_decoder
    def test_a_chain_pasted_issuer_first_is_put_in_order(self):
        leaf = certgen.certificate(self.key, issuer=(self.ca_key, "Test Issuing CA"))
        status, body = self.install(self.ca + leaf, self.key.pem())
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertEqual(self.cert_file.read_text(encoding="ascii"), leaf + self.ca)
        self.assertEqual(body["tls"]["issuer"], "commonName=Test Issuing CA")

    def test_one_file_holding_both(self):
        with mock.patch.object(_cfg, "TLS_KEY_FILE", str(self.cert_file)):
            status, body = self.install(self.cert, self.key.pem())
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertEqual(self.cert_file.read_text(encoding="ascii"), self.cert + self.key.pem())
        self.assertEqual(self.key_file.read_text(encoding="ascii").count("PRIVATE KEY"), 2)  # the old key, untouched

    @needs_decoder
    def test_an_expiring_certificate_is_used_with_a_warning(self):
        status, body = self.install(certgen.certificate(self.key, days=10), self.key.pem())
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertRegex(body["warning"], r"expires in \d+ days")

    def test_not_serving_from_files(self):
        with mock.patch.object(_cfg, "TLS_KEY_FILE", ""):
            status, _body = self.install(self.cert, self.key.pem())
        self.assertEqual(status, HTTPStatus.CONFLICT)

    @needs_decoder
    def test_put_back_swaps_the_pairs(self):
        status, _body = admin.restore_previous_certificate(apply=self.apply)
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.install(self.cert, self.key.pem())
        status, body = admin.restore_previous_certificate(sid="s1", client_ip="127.0.0.1", apply=self.apply)
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertEqual(self.cert_file.read_text(encoding="ascii"), EMBEDDED_DEV_CERT_PEM)
        self.assertEqual((self.dir / "certs" / "nwdash.crt.previous").read_text(encoding="ascii"), self.cert)
        self.assertTrue(body["tls"]["auto"])  # the built-in one, recognised by its contents
        self.assertEqual(self.audit_rows()[0]["action"], "TLS certificate put back")
        admin.restore_previous_certificate(apply=self.apply)
        self.assertEqual(self.cert_file.read_text(encoding="ascii"), self.cert)
        self.assertEqual(len(self.applied), 3)


def _handshake(server_ctx: ssl.SSLContext, client_ctx: ssl.SSLContext) -> None:
    """A TLS handshake through memory buffers: no socket, so nothing on the
    machine (antivirus inspecting loopback TLS) can step in. Raises what the
    client's certificate verification raises."""
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    sides = [client_ctx.wrap_bio(to_client, to_server, server_hostname="localhost"),
             server_ctx.wrap_bio(to_server, to_client, server_side=True)]
    for _ in range(10):
        for side in list(sides):
            try:
                side.do_handshake()
                sides.remove(side)
            except ssl.SSLWantReadError:
                pass
        if not sides:
            return
    raise AssertionError("the handshake did not finish")


class TestListenerSelfTestTrust(Keys, TempDataMixin, unittest.TestCase):
    """The HTTPS-listener self-test trusts exactly our certificate file. An
    uploaded CA certificate comes with intermediates but not the root, and must
    still pass on the hosts' Python 3.12, which does not allow partial chains
    unless asked."""

    def files(self, chain):
        cert_file, key_file = self.dir / "serving.crt", self.dir / "serving.key"
        cert_file.write_text(chain, encoding="ascii")
        key_file.write_text(self.key.pem(), encoding="ascii")
        return cert_file, server_tls_context(cert_file, key_file)

    def test_a_ca_issued_chain_without_its_root(self):
        intermediate_key = certgen.RsaKey()
        intermediate = certgen.certificate(intermediate_key, cn="Test Intermediate", dns=(), ips=(), issuer=(self.ca_key, "Test Root"), ca=True)
        leaf = certgen.certificate(self.key, issuer=(intermediate_key, "Test Intermediate"))
        cert_file, server_ctx = self.files(leaf + intermediate)
        _handshake(server_ctx, listener_trust_context(cert_file))

    def test_a_self_signed_certificate(self):
        cert_file, server_ctx = self.files(self.cert)
        _handshake(server_ctx, listener_trust_context(cert_file))

    def test_any_other_certificate_fails(self):
        _cert_file, server_ctx = self.files(self.cert)
        other = self.dir / "other.crt"
        other.write_text(certgen.certificate(self.other_key), encoding="ascii")
        with self.assertRaises(ssl.SSLCertVerificationError):
            _handshake(server_ctx, listener_trust_context(other))


def _served_subject(port: int, folder) -> str:
    """The subject a new connection is shown. Not the bytes: antivirus that
    inspects loopback TLS re-signs the certificate, keeping its subject, dates
    and names but not its issuer, key or serial."""
    ctx = ssl._create_unverified_context()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw,             ctx.wrap_socket(raw, server_hostname="localhost") as tls:
        der = tls.getpeercert(binary_form=True)
    pem = Path(folder) / "served.pem"
    pem.write_text(ssl.DER_cert_to_PEM_cert(der), encoding="ascii")
    return admin._name_text(ssl._ssl._test_decode_cert(str(pem))["subject"])


class TestUploadOverHttps(Keys, unittest.TestCase):
    PASSWORD = "tls-upload-test-pw"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = AppServer(password=cls.PASSWORD).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        super().tearDownClass()

    def post(self, path, payload, cookie, csrf):
        status, body, _headers = self.server.request("POST", path, payload, headers={"X-CSRF-Token": csrf}, cookie=cookie)
        return status, json.loads(body or b"{}")

    def sign_in(self, admin_window):
        status, body, headers = self.server.request("POST", "/api/login", {"password": self.PASSWORD, "admin": admin_window})
        self.assertEqual(status, 200, body)
        return headers.get("Set-Cookie", "").split(";", 1)[0], json.loads(body)["csrfToken"]

    def test_upload_needs_the_admin_window(self):
        cookie, csrf = self.sign_in(admin_window=False)
        status, body = self.post("/api/admin/tls", {"certificate": self.cert, "key": self.key.pem()}, cookie, csrf)
        self.assertEqual((status, body.get("code")), (403, "unlock_required"))

    @needs_decoder
    def test_new_connections_get_the_new_certificate_at_once(self):
        folder = self.server.root
        self.assertEqual(_served_subject(self.server.port, folder), "commonName=localhost")
        cookie, csrf = self.sign_in(admin_window=True)
        uploaded = certgen.certificate(self.key, cn="Uploaded by the admin console")  # still for localhost, 127.0.0.1
        status, body = self.post("/api/admin/tls", {"certificate": uploaded, "key": self.key.pem()}, cookie, csrf)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["applied"])
        self.assertEqual(_served_subject(self.server.port, folder), "commonName=Uploaded by the admin console")
        self.assertEqual(self.server.request("GET", "/api/health")[0], 200)
        status, body = self.post("/api/admin/tls/restore", {}, cookie, csrf)
        self.assertEqual(status, 200, body)
        self.assertEqual(_served_subject(self.server.port, folder), "commonName=localhost")

if __name__ == "__main__":
    unittest.main()
