"""Test certificates made with the standard library only: no OpenSSL tools,
no third-party packages (CI and the hosts' embedded Python have neither for
certain), and no private keys committed to this public repository.

An RSA key is two random primes; a certificate is hand-encoded DER signed with
PKCS#1 v1.5 / SHA-256. Pure-Python prime search takes a moment, so test
modules make their keys once per class.

Not a test module itself: discovery only collects test*.py.
"""
import base64
import hashlib
import ipaddress
import secrets
from datetime import UTC, datetime, timedelta

_SMALL_PRIMES = [n for n in range(3, 2000, 2) if all(n % d for d in range(3, int(n ** 0.5) + 1, 2))]
_E = 65537
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _probably_prime(n: int, rounds: int = 32) -> bool:
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        # Top two bits set, so the product of two has exactly 2 * bits bits.
        candidate = secrets.randbits(bits) | (0b11 << (bits - 2)) | 1
        if (candidate - 1) % _E and _probably_prime(candidate):
            return candidate


# ── DER ─────────────────────────────────────────────────────────────────────

def _tlv(tag: int, content: bytes) -> bytes:
    size = len(content)
    if size < 0x80:
        length = bytes([size])
    else:
        raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + content


def _int(value: int) -> bytes:
    return _tlv(0x02, value.to_bytes(value.bit_length() // 8 + 1, "big"))


def _seq(*parts: bytes) -> bytes:
    return _tlv(0x30, b"".join(parts))


def _oid(dotted: str) -> bytes:
    first, second, *rest = (int(part) for part in dotted.split("."))
    body = bytearray([40 * first + second])
    for value in rest:
        chunk = [value & 0x7F]
        value >>= 7
        while value:
            chunk.append(0x80 | (value & 0x7F))
            value >>= 7
        body.extend(reversed(chunk))
    return _tlv(0x06, bytes(body))


def _time(moment: datetime) -> bytes:
    moment = moment.astimezone(UTC)
    if 1950 <= moment.year < 2050:
        return _tlv(0x17, moment.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, moment.strftime("%Y%m%d%H%M%SZ").encode())


def _name(common_name: str) -> bytes:
    return _seq(_tlv(0x31, _seq(_oid("2.5.4.3"), _tlv(0x0C, common_name.encode()))))


def _pem(label: str, der: bytes) -> str:
    text = base64.b64encode(der).decode()
    lines = "\n".join(text[i:i + 64] for i in range(0, len(text), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n"


_NULL = b"\x05\x00"
_SHA256_WITH_RSA = _seq(_oid("1.2.840.113549.1.1.11"), _NULL)


class RsaKey:
    def __init__(self, bits: int = 2048) -> None:
        p, q = _prime(bits // 2), _prime(bits // 2)
        while p == q:
            q = _prime(bits // 2)
        self.n, self.p, self.q = p * q, p, q
        self.d = pow(_E, -1, (p - 1) * (q - 1))

    def pem(self) -> str:
        """PKCS#1 "RSA PRIVATE KEY", unencrypted."""
        p, q, d = self.p, self.q, self.d
        return _pem("RSA PRIVATE KEY", _seq(
            _int(0), _int(self.n), _int(_E), _int(d), _int(p), _int(q),
            _int(d % (p - 1)), _int(d % (q - 1)), _int(pow(q, -1, p)),
        ))

    def public_key_info(self) -> bytes:
        rsa_public_key = _seq(_int(self.n), _int(_E))
        return _seq(_seq(_oid("1.2.840.113549.1.1.1"), _NULL), _tlv(0x03, b"\x00" + rsa_public_key))

    def sign(self, data: bytes) -> bytes:
        size = (self.n.bit_length() + 7) // 8
        digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(data).digest()
        padded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
        return pow(int.from_bytes(padded, "big"), self.d, self.n).to_bytes(size, "big")


def certificate(key: RsaKey, *, cn: str = "localhost", dns: tuple[str, ...] = ("localhost",),
                ips: tuple[str, ...] = ("127.0.0.1",), days: int = 365, not_before: datetime | None = None,
                issuer: tuple[RsaKey, str] | None = None, ca: bool = False) -> str:
    """A PEM certificate for `key`, self-signed unless `issuer` is (key, CN).
    `ca` marks it as a CA (critical basicConstraints), as an intermediate must be."""
    start = not_before or datetime.now(UTC) - timedelta(days=1)
    names = [_tlv(0x82, name.encode()) for name in dns] + [_tlv(0x87, ipaddress.ip_address(ip).packed) for ip in ips]
    extensions = [_seq(_oid("2.5.29.17"), _tlv(0x04, _seq(*names)))] if names else []
    if ca:
        true = b"\x01\x01\xff"
        extensions.append(_seq(_oid("2.5.29.19"), true, _tlv(0x04, _seq(true))))
    signer, issuer_cn = issuer or (key, cn)
    tbs = _seq(
        _tlv(0xA0, _int(2)),  # v3
        _int(secrets.randbits(63) | 1),
        _SHA256_WITH_RSA,
        _name(issuer_cn),
        _seq(_time(start), _time(start + timedelta(days=days))),
        _name(cn),
        key.public_key_info(),
        *([_tlv(0xA3, _seq(*extensions))] if extensions else []),
    )
    return _pem("CERTIFICATE", _seq(tbs, _SHA256_WITH_RSA, _tlv(0x03, b"\x00" + signer.sign(tbs))))
