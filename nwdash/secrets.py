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


class KeyMaterialError(RuntimeError):
    """The on-disk key material is present but unusable, or cannot be stored safely.

    Raised instead of quietly replacing a key: overwriting an unreadable key file
    orphans every secret encrypted under it (saved profile passwords, persisted
    sessions, email profiles) with no way back. Refusing to start leaves the old
    key intact so an operator can restore it or delete it deliberately.
    """


class SecretEncryptionError(RuntimeError):
    """A secret could not be encrypted, so it must not be treated as stored."""


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


def _write_protected_key(path: Path, key: bytes, *, required: bool = True) -> None:
    """Write a key file, DPAPI-wrapped where DPAPI exists.

    Fail-closed on Windows: this is the master key for every stored NetWorker
    credential, so a DPAPI failure is NOT downgraded to an unwrapped key on
    disk. An unwrapped key sitting next to the ciphertext it protects (see the
    stale debug_test/data tree) hands every saved password to any local read.
    CryptProtectData does not fail on a healthy machine, so a failure here is a
    broken host, not a routine condition worth trading the secret for.

    Off Windows there is no OS key store wired up, so the key is written
    unwrapped as before — but at ERROR level, not as a passing remark.

    `required=False` marks a best-effort rewrite (the legacy plaintext -> wrapped
    migration) where the key already loaded fine, so a write failure is logged
    rather than fatal.
    """
    payload = key
    if _dpapi_available():
        try:
            payload = DPAPI_MARKER + _dpapi_protect(key)
        except OSError as exc:
            message = (
                f"DPAPI could not protect data/{path.name}: {exc}. Refusing to write the "
                "master key unwrapped — every saved profile password and persisted session "
                "would then be readable by any process on this machine."
            )
            LOG.error(message)
            if not required:
                # Best-effort rewrite of a key that already loaded fine: leave the
                # existing file alone rather than take the process down with it.
                return
            raise KeyMaterialError(message) from exc
    else:
        LOG.error(
            f"data/{path.name} is stored UNWRAPPED: no OS key store is available on "
            f"{sys.platform}. Anyone who can read data/ can decrypt every saved credential."
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
        path.chmod(0o600)
    except OSError as exc:
        # A key that never lands on disk means a fresh random key every restart
        # and every previously stored secret permanently undecryptable. Never silent.
        message = f"could not write data/{path.name}: {exc}"
        LOG.error(message)
        if required:
            raise KeyMaterialError(
                f"{message}. Without a persisted key every saved credential becomes "
                "unreadable after a restart."
            ) from exc


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


def _refuse_key_replacement(path: Path, reason: str) -> KeyMaterialError:
    """An existing key file that cannot be used is an operator problem, not ours to fix.

    The previous behaviour warned and then overwrote the file with a fresh key,
    which silently and irreversibly orphaned every secret encrypted under the old
    one. Raising leaves the file untouched so it can still be restored.
    """
    message = (
        f"data/{path.name} exists but is unusable ({reason}). It was NOT replaced: "
        "generating a new key would permanently orphan every saved profile password, "
        "persisted session, and email profile encrypted under the old one. "
        "Restore the original data/ directory (a key copied from another machine or "
        "Windows account cannot be unwrapped here), or, accepting that every stored "
        f"secret is lost, delete data/{path.name} and re-enter the credentials."
    )
    LOG.error(message)
    return KeyMaterialError(message)


def _load_or_create_stable_key() -> bytes:
    """Load persisted Fernet key (DPAPI-wrapped on Windows); create if absent.
    Legacy plaintext keys are migrated to wrapped form on first read.
    Raises KeyMaterialError when an existing key file cannot be used.
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
        except Exception as exc:
            raise _refuse_key_replacement(SESSION_KEY_FILE, f"not a valid Fernet key: {exc}") from exc
        if _dpapi_available() and not _key_file_is_wrapped(SESSION_KEY_FILE):
            _write_protected_key(SESSION_KEY_FILE, candidate, required=False)  # migrate
        return candidate
    if SESSION_KEY_FILE.exists():
        raise _refuse_key_replacement(SESSION_KEY_FILE, "could not be read or DPAPI-unwrapped")
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
    if raw is not None:
        if len(raw) < 32:
            raise _refuse_key_replacement(AUTH_KEY_FILE, f"truncated ({len(raw)} bytes, need 32)")
        if _dpapi_available() and not _key_file_is_wrapped(AUTH_KEY_FILE):
            _write_protected_key(AUTH_KEY_FILE, raw, required=False)  # migrate
        return raw
    if AUTH_KEY_FILE.exists():
        raise _refuse_key_replacement(AUTH_KEY_FILE, "could not be read or DPAPI-unwrapped")
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


# v1 bound every profile secret under one constant AAD, so ciphertexts were
# interchangeable: anyone able to write data/profiles.json (but unable to read
# the key) could move the production password onto a profile pointing at a host
# they control, or swap _enc_password and _enc_wmiPassword. v2 binds the AAD to
# name||field, so a blob only decrypts in the exact slot it was written for.
_AAD_V1 = b"profile"
_ENC_V1_PREFIX = "enc:v1:"
_ENC_V2_PREFIX = "enc:v2:"


def _profile_aad(name: str, field: str) -> bytes:
    """Bind a ciphertext to one (profile, field) slot. NUL-separated so no
    (name, field) pair can be spelled by a different pair."""
    return b"nwdash-profile-v2\x00" + str(name).encode("utf-8") + b"\x00" + str(field).encode("utf-8")


def encrypt_profile_secret(plaintext: str, *, name: str, field: str) -> str:
    """Encrypt with AES-256-GCM, bound to the (profile name, field) it is stored under.

    Returns 'enc:v2:<base64>'. Raises SecretEncryptionError when no ciphertext
    could be produced — returning "" here used to read to the caller as a
    successful save of an empty password.
    """
    if not plaintext:
        return ""
    key = _derive_profile_key()
    if key:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = os.urandom(12)   # 96-bit nonce — GCM standard
            ct    = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), _profile_aad(name, field))
            return _ENC_V2_PREFIX + base64.b64encode(nonce + ct).decode("ascii")
        except Exception as exc:
            LOG.error(f"AES-GCM encryption failed for {name}.{field}: {exc}; trying Fernet")
    # Fernet fallback: only reachable when cryptography's hazmat layer is broken
    # or absent. It carries no AAD, so the slot binding above is lost.
    fallback = encrypt_process_secret(plaintext)
    if not fallback:
        raise SecretEncryptionError(
            "Credential storage is unavailable: the optional 'cryptography' package is "
            "missing or unusable, so the password cannot be encrypted. Install it "
            "(pip install cryptography) or save this profile without a password."
        )
    LOG.warning(f"stored {name}.{field} with the unbound Fernet fallback (hazmat unavailable)")
    return fallback


def decrypt_profile_secret(value: str, *, name: str, field: str) -> str:
    """Decrypt a profile secret from the slot it claims to belong to.

    Accepts v2 (AAD bound to name||field), v1 (constant AAD) and legacy Fernet
    blobs, so upgrading never invalidates an already-saved profile. Returns ""
    when the value cannot be decrypted — callers must treat that as an error,
    not as "no password was saved".
    """
    if not value:
        return ""
    for prefix, aad in ((_ENC_V2_PREFIX, _profile_aad(name, field)), (_ENC_V1_PREFIX, _AAD_V1)):
        if not value.startswith(prefix):
            continue
        key = _derive_profile_key()
        if not key:
            return ""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            raw = base64.b64decode(value[len(prefix):])
            return AESGCM(key).decrypt(raw[:12], raw[12:], aad).decode("utf-8")
        except Exception:
            return ""
    # Legacy Fernet blob
    return decrypt_process_secret(value)


def profile_secret_needs_rebinding(value: str) -> bool:
    """True for a stored blob that predates the name||field AAD binding."""
    return bool(value) and not value.startswith(_ENC_V2_PREFIX)


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
