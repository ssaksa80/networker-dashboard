"""DPAPI key wrapping, key files, Fernet ciphers, and secret encryption.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import base64
import ctypes
import os
import sys
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - dashboard still runs without WMI credential persistence.
    Fernet = None
    InvalidToken = Exception

from .config import AUTH_KEY_FILE, DATA_DIR, LOG, SESSION_KEY_FILE

DPAPI_MARKER = b"DPAPI1\n"
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


def _dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]


def _dpapi_protect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))


def _dpapi_unprotect(blob: bytes) -> bytes:
    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))


def _key_file_is_wrapped(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(DPAPI_MARKER)) == DPAPI_MARKER
    except OSError:
        return False


def _write_protected_key(path: Path, key: bytes) -> None:
    payload = key
    if _dpapi_available():
        try:
            payload = DPAPI_MARKER + _dpapi_protect(key)
        except OSError as exc:
            LOG.warning(f"DPAPI protect failed; storing key unwrapped: {exc}")
            payload = key
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
        path.chmod(0o600)
    except OSError:
        pass


def _read_protected_key(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw.startswith(DPAPI_MARKER):
        try:
            return _dpapi_unprotect(raw[len(DPAPI_MARKER):])
        except OSError as exc:
            LOG.warning(f"DPAPI unprotect failed for {path.name}: {exc}")
            return None
    return raw


def _load_or_create_stable_key() -> bytes:
    """Load persisted Fernet key (DPAPI-wrapped on Windows); create if absent.
    Legacy plaintext keys are migrated to wrapped form on first read.
    """
    if Fernet is None:
        return b""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    raw = _read_protected_key(SESSION_KEY_FILE)
    if raw is not None:
        candidate = raw.strip()
        try:
            Fernet(candidate)  # raises if malformed
            if _dpapi_available() and not _key_file_is_wrapped(SESSION_KEY_FILE):
                _write_protected_key(SESSION_KEY_FILE, candidate)  # migrate
            return candidate
        except Exception:
            pass
    key = Fernet.generate_key()
    _write_protected_key(SESSION_KEY_FILE, key)
    return key


WMI_CREDENTIAL_KEY = _load_or_create_stable_key()
WMI_CIPHER = Fernet(WMI_CREDENTIAL_KEY) if (Fernet and WMI_CREDENTIAL_KEY) else None


def _load_or_create_auth_key() -> bytes:
    """Stable 32-byte HMAC key for signing auth cookies (DPAPI-wrapped on Windows).
    Legacy plaintext keys are migrated to wrapped form on first read.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    raw = _read_protected_key(AUTH_KEY_FILE)
    if raw is not None and len(raw) >= 32:
        if _dpapi_available() and not _key_file_is_wrapped(AUTH_KEY_FILE):
            _write_protected_key(AUTH_KEY_FILE, raw)  # migrate
        return raw
    key = os.urandom(32)
    _write_protected_key(AUTH_KEY_FILE, key)
    return key


AUTH_SECRET_KEY = _load_or_create_auth_key()



def _derive_profile_key() -> bytes | None:
    """Derive a 256-bit AES key from the stable master key via HKDF-SHA256."""
    if not WMI_CREDENTIAL_KEY:
        return None
    try:
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes as _hashes
        from cryptography.hazmat.backends import default_backend
        hkdf = HKDF(
            algorithm=_hashes.SHA256(),
            length=32,
            salt=b"networker-profile-passwords-v1",
            info=b"profile-aes256gcm",
            backend=default_backend(),
        )
        return hkdf.derive(WMI_CREDENTIAL_KEY)
    except Exception:
        return None


def encrypt_profile_secret(plaintext: str) -> str:
    """Encrypt with AES-256-GCM. Returns 'enc:v1:<base64>' or empty string on failure."""
    if not plaintext:
        return ""
    key = _derive_profile_key()
    if not key:
        # Fallback to Fernet if hazmat unavailable
        return encrypt_process_secret(plaintext)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import os as _os
        nonce = _os.urandom(12)   # 96-bit nonce — GCM standard
        ct    = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), b"profile")
        blob  = base64.b64encode(nonce + ct).decode("ascii")
        return f"enc:v1:{blob}"
    except Exception:
        return encrypt_process_secret(plaintext)   # Fernet fallback


def decrypt_profile_secret(value: str) -> str:
    """Decrypt AES-256-GCM profile secret. Handles legacy Fernet blobs too."""
    if not value:
        return ""
    if value.startswith("enc:v1:"):
        key = _derive_profile_key()
        if not key:
            return ""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            raw   = base64.b64decode(value[7:])
            nonce = raw[:12]
            ct    = raw[12:]
            pt    = AESGCM(key).decrypt(nonce, ct, b"profile")
            return pt.decode("utf-8")
        except Exception:
            return ""
    # Legacy Fernet blob
    return decrypt_process_secret(value)


def encrypt_process_secret(secret: str) -> str:
    if not secret or not WMI_CIPHER:
        return ""
    return WMI_CIPHER.encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_process_secret(encrypted_secret: str) -> str:
    if not encrypted_secret or not WMI_CIPHER:
        return ""
    try:
        return WMI_CIPHER.decrypt(encrypted_secret.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def encrypt_wmi_password(password: str) -> str:
    return encrypt_process_secret(password)


def decrypt_wmi_password(encrypted_password: str) -> str:
    return decrypt_process_secret(encrypted_password)
