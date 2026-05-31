#!/usr/bin/env python3
"""
Single-file HTTPS dashboard for Dell NetWorker backup and recovery status.

The password is accepted only for the current browser request. This server does
not write credentials to disk, does not place them in URLs, and serves every
response with no-store cache headers.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import email.utils
import html as html_lib
import json
import logging
import logging.handlers
import ctypes
import hashlib
import hmac
import ipaddress
import os
import re
import shutil
import signal
import smtplib
import subprocess
import socket
import ssl
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - dashboard still runs without WMI credential persistence.
    Fernet = None
    InvalidToken = Exception


APP_NAME = "NetWorker Backup & Recovery Dashboard"
APP_VERSION = "2.2.8"
APP_DEBUG = False
DEFAULT_PORT = 8443
DEFAULT_API_PORT = 9090
DEFAULT_TIMEOUT_SECONDS = 30  # outbound NetWorker REST/API call timeout
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30  # inbound per-request socket timeout (slowloris guard)
DEFAULT_MAX_CONNECTIONS = 200
REQUEST_TIMEOUT_SECONDS = DEFAULT_REQUEST_TIMEOUT_SECONDS
MAX_CONNECTIONS = DEFAULT_MAX_CONNECTIONS
SERVER_HEALTH_REFRESH_SECONDS = 60
MAX_POST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# NetWorker has no server-side time filter for /global/jobs, so the whole jobs
# database (bounded only by NetWorker's completed-job retention) is returned and
# trimmed to the report window client-side. On busy servers that easily exceeds
# the default response guard, so the jobs-history fetch gets a higher ceiling.
MAX_JOBS_RESPONSE_BYTES = 64 * 1024 * 1024
TABLE_LIMIT = 80
HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
API_VERSION_PATTERN = re.compile(r"^v[0-9]+$")
API_VERSION_CANDIDATES = ("v3", "v2", "v1")
REPORT_RANGES = {
    "24h": ("Last 24 Hours", 1),
    "7d": ("Last Week", 7),
    "30d": ("Last Month", 30),
}
THEME_PALETTES: dict[str, dict[str, str]] = {
    "default": {
        "bg": "#eef3f6",
        "surface": "#ffffff",
        "surface2": "#f7fafb",
        "ink": "#172026",
        "muted": "#5f6d76",
        "line": "#d7e1e7",
        "brand": "#126e82",
        "brandInk": "#ffffff",
        "green": "#18764a",
        "red": "#bd2b3a",
        "amber": "#a96800",
        "blue": "#2457a6",
    },
    "midnight": {
        "bg": "#101719",
        "surface": "#172124",
        "surface2": "#1e2b2f",
        "ink": "#edf6f8",
        "muted": "#9db1b8",
        "line": "#314249",
        "brand": "#2aa6b8",
        "brandInk": "#071113",
        "green": "#6fcf97",
        "red": "#ff6b78",
        "amber": "#f2c14e",
        "blue": "#7db7ff",
    },
    "graphite": {"bg": "#f1f2f3", "surface": "#ffffff", "surface2": "#eff1f2", "ink": "#1f2326", "muted": "#666f75", "line": "#d1d6da", "brand": "#3d5a5f", "brandInk": "#ffffff", "green": "#1d7c55", "red": "#b93246", "amber": "#ad7300", "blue": "#3d64a8"},
    "contrast": {"bg": "#ffffff", "surface": "#ffffff", "surface2": "#f2f2f2", "ink": "#0b0b0b", "muted": "#3d3d3d", "line": "#202020", "brand": "#005fcc", "brandInk": "#ffffff", "green": "#006b3c", "red": "#b00020", "amber": "#8a5a00", "blue": "#004fb8"},
    "ocean": {"bg": "#e8f4f6", "surface": "#ffffff", "surface2": "#edf8fa", "ink": "#102a31", "muted": "#527179", "line": "#bfd8de", "brand": "#087f8c", "brandInk": "#ffffff", "green": "#11845b", "red": "#c03546", "amber": "#b27900", "blue": "#1c6eb8"},
    "forest": {"bg": "#eef5ef", "surface": "#ffffff", "surface2": "#f2f8f1", "ink": "#17251b", "muted": "#5f7565", "line": "#cfddcf", "brand": "#2f6f45", "brandInk": "#ffffff", "green": "#1f7a45", "red": "#b83b4b", "amber": "#a06c00", "blue": "#3867a8"},
    "ruby": {"bg": "#f8eef1", "surface": "#ffffff", "surface2": "#fff4f6", "ink": "#2d1720", "muted": "#7a5d66", "line": "#e6cbd3", "brand": "#9f2d55", "brandInk": "#ffffff", "green": "#17794e", "red": "#b92345", "amber": "#aa7200", "blue": "#445ca8"},
    "steel": {"bg": "#edf1f5", "surface": "#ffffff", "surface2": "#f3f6f9", "ink": "#17202b", "muted": "#5d6a78", "line": "#ccd6e1", "brand": "#425c78", "brandInk": "#ffffff", "green": "#26724a", "red": "#aa3d45", "amber": "#9a6b12", "blue": "#376da9"},
    "arctic": {"bg": "#edf7f8", "surface": "#ffffff", "surface2": "#f4fbfb", "ink": "#10272d", "muted": "#5a737a", "line": "#c8dee3", "brand": "#0d7891", "brandInk": "#ffffff", "green": "#168059", "red": "#b83245", "amber": "#9e7207", "blue": "#2d68a7"},
    "citrus": {"bg": "#f5f7ec", "surface": "#ffffff", "surface2": "#fbfcf3", "ink": "#202817", "muted": "#68705b", "line": "#dde5ca", "brand": "#617d18", "brandInk": "#ffffff", "green": "#23733f", "red": "#b43a47", "amber": "#a16d00", "blue": "#3f6fa5"},
    "harbor": {"bg": "#eef3f4", "surface": "#ffffff", "surface2": "#f5f8f9", "ink": "#17242a", "muted": "#5e7077", "line": "#d0dce0", "brand": "#235f73", "brandInk": "#ffffff", "green": "#24764f", "red": "#b63548", "amber": "#9d6e08", "blue": "#335fa3"},
    "ember": {"bg": "#f6f1ee", "surface": "#ffffff", "surface2": "#fbf7f4", "ink": "#2a1f1a", "muted": "#75665f", "line": "#e2d4cd", "brand": "#8d4a36", "brandInk": "#ffffff", "green": "#26734a", "red": "#b23545", "amber": "#9b6a10", "blue": "#3c67a2"},
}
# The dashboard brand/header card uses a fixed navy->teal gradient over the
# brand color (see the in-page .brand-card CSS). Email reports and the PNG
# snapshot reuse these so the brand card looks identical to the live dashboard.
# Outlook ignores CSS gradients, so the email also carries a solid fallback.
BRAND_CARD_GRADIENT = "linear-gradient(135deg, rgba(11, 32, 42, 0.98), rgba(18, 110, 130, 0.96)), #126e82"
BRAND_CARD_SOLID = "#103a47"
BRAND_CARD_INK = "#ffffff"
CUSTOM_REPORT_RANGE = "custom"
DEFAULT_REPORT_RANGE = "24h"
SESSION_TTL_SECONDS = 365 * 24 * 60 * 60  # 1 year — sessions persist until server restart or explicit clear
SHARED_REFRESH_SECONDS = 60
ALERT_AUTOMATION_MIN_INTERVAL_MINUTES = 1
ALERT_AUTOMATION_MAX_INTERVAL_MINUTES = 1440
APP_BASE_DIR = Path(sys.executable).parent.resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DATA_DIR = APP_BASE_DIR / "data"
DASHBOARD_SNAPSHOT_FILE = DATA_DIR / "networker_snapshots.json"
AUTO_SNAPSHOT_FILE = DATA_DIR / "auto_snapshot_config.json"
UI_PREFS_FILE = DATA_DIR / "ui_prefs.json"
SESSION_KEY_FILE = DATA_DIR / ".session_key"
SESSION_PERSISTENCE_FILE = DATA_DIR / "sessions.json"
LAST_GOOD_DASHBOARD_FILE = DATA_DIR / "last_good_dashboard.json"
PROFILES_FILE = DATA_DIR / "profiles.json"
EMAIL_CONFIG_FILE = DATA_DIR / "email_config.json"
AUTH_KEY_FILE = DATA_DIR / ".auth_key"
AUTH_CONFIG_FILE = DATA_DIR / "auth.json"
COOKIE_NAME = "nwdash_auth"
AUTH_TTL_SECONDS = 43200  # 12 hours
PBKDF2_ITERATIONS = 200_000
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
AUTH_ENABLED = False  # set in run() once a password is configured

# ── Logging ────────────────────────────────────────────────────────────────
LOG_DIR = APP_BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "networker_dashboard.log"
PROCESS_START_TIME = time.time()
LOG = logging.getLogger("networker_dashboard")
_LOG_EXTRA_KEYS = ("request_id", "client", "status", "path", "event")


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _LOG_EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                obj[key] = val
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=True, default=str)


def configure_logging(debug: bool) -> None:
    LOG.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
    formatter = _JsonLogFormatter()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        LOG.addHandler(file_handler)
    except OSError:
        pass
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    LOG.addHandler(stream_handler)
    LOG.propagate = False


_PROFILE_PW_SENTINEL = "__profile_password__"
_PROFILE_PW_SAVED    = "(saved)"


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


def _hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def set_auth_password(password: str) -> None:
    salt = os.urandom(16)
    record = {
        "salt": salt.hex(),
        "hash": _hash_password(password, salt).hex(),
        "iterations": PBKDF2_ITERATIONS,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(AUTH_CONFIG_FILE)
    try:
        AUTH_CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def _load_auth_config() -> dict[str, Any] | None:
    try:
        if AUTH_CONFIG_FILE.exists():
            data = json.loads(AUTH_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("hash") and data.get("salt"):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def auth_password_configured() -> bool:
    return _load_auth_config() is not None


def verify_auth_password(password: str) -> bool:
    config = _load_auth_config()
    if not config:
        return False
    try:
        salt = bytes.fromhex(config["salt"])
        expected = bytes.fromhex(config["hash"])
        iterations = int(config.get("iterations") or PBKDF2_ITERATIONS)
    except (ValueError, TypeError):
        return False
    candidate = _hash_password(password, salt, iterations)
    return hmac.compare_digest(candidate, expected)


def _make_auth_cookie() -> str:
    now = int(time.time())
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": now, "exp": now + AUTH_TTL_SECONDS}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.new(AUTH_SECRET_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return f"{payload}.{sig}"


def _verify_auth_cookie(value: str) -> bool:
    if not value or "." not in value:
        return False
    payload, _, sig = value.rpartition(".")
    if not payload or not sig:
        return False
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(AUTH_SECRET_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return int(data.get("exp", 0)) > int(time.time())
    except (ValueError, json.JSONDecodeError):
        return False


LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()


def _login_rate_limited(ip: str) -> bool:
    now = time.time()
    with LOGIN_ATTEMPTS_LOCK:
        attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        LOGIN_ATTEMPTS[ip] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    now = time.time()
    with LOGIN_ATTEMPTS_LOCK:
        attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        LOGIN_ATTEMPTS[ip] = attempts


def _clear_login_failures(ip: str) -> None:
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.pop(ip, None)


def _is_loopback_bind(host: str) -> bool:
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1")


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
NETWORKER_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "networker-logo.png"
NETWORKER_LOGO_PNG_BASE64 = """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FgPPNq+n1xdYlP+JvyoZWjBzz/M49NBDOeaYY1i0aBEnnHACzc3NGCpUp4CajHqRSITGxsYAJGXRpKWlha9//etcd911fPvb32bq1KmUSiWqatFFAOUDH/gAH/nIR/jQhz7EmWeeySmnnMIJJ5zAMcccw1FHHcVhhx3GzjvvzLx585gzZw4zZsygs7OTxsZGEspswlWrndJ5hQSo8vk8AwMDrF69msbGRlpbW0mn07z66qs89thjPP/88zz22GPce++93H777fz2t7/ll7/8JevWrcNWq5Dbtm2jWCySTqeZOXNm8K6GhgYikQh1dXWMjY3R19eHaZrss88+RKNR2traKJVKHHDAAZTUljJdkhaJvK6ujr333hvLsujp6WHTpk0BECWTSbZs2cLFF1/M1KlTKagFjIsvvpg77riDxsZGpk6dyty5c5k9ezatra1MTEywZMkSfvCDHwSgcMQRR9DS0kI2m32DjlgGI1OpPTLK7CSutrOVy2VKKm4zQEtLC7FYjBdffJHVq1dTX1/PnDlziKv412Wlm21vbycWiwWrz9VqlWq1SiqVolwuc/TRR3PWWWcFEtXNN98cSFpxtcOkrq4uKJtpmlSVIbXMahoaGt5QxkKhwODgIP39/QwNDZFVq+kjIyNMTEzgao4kZCo9depUcrkcW7Zsoampie7ubiJKl1zW7CInJiaCAVTa0PynNczW4VBmpCoVR0bID49QLRSw7AjRRJxyocCm1zaybr1H0oFkwaXSP8DMtjRfvfgcfnzNf/Avn7uI404+ns6Z3ZR8l7F8nlRTO10zdqYu04HnJYnYNS8avlPEKeZomNZFdWyIysgA0YY0bj6r4qaoabzx+lTZN16Pu/LXJNM0KRaLgZL47LPP5stf/jJf/vKXueyyy9htt93o7OwkpgWU97S9oMViEV8FVJcV1KGhIVKpVCDx5XK5AASi0SjJZJKxsbFAihDA7e/vZ+3atSxdupSnnnqKxx9/nCVLlvDggw+yePFi/ud//odbb72VG264gauvvpqrrrqKzZs343ke69ev54orruDUU0/l05/+NJdccgkf/ehHOffcc7ngggu4+OKLWbNmTdBZvve97/Gxj32Mf/mXf+ErX/kKV1xxBd/+9re59tpr+d73vsfIyAhz586lWCzS19eHpXR1s2fPJpvNklexlwVEpE6mTJnCTjvtFKgEIpEI/+f//J9g4SWqTETkmnSwrq4uPM+jp6eHtWvXYihd7PDwMG1tbZx99tlBJ/7oRz/K6tWrmTVrVqDCKBQKDA0NEYlE6O7uplwu88ADD/CrX/2KdDoNwJVXXklBLTjlcrlg0BCpdXBwMJByBQxs2w6k523btuG6Lh0dHZRKJbZt2xbwg0jvtm0zMTERAF17ezvFYpHR0VFaW1sZHh4mkUhw9dVXB2W5/PLLWbp0KQsWLCCfzzMxMUFXVxfbtm0LBtmIUmukUilczaKhqgywI2o1vbm5mVQqRSKRoK6ujoaGhgDMRUIsa3ud82ol3lYzl/7+fgYHB4PZkK/UGqIuSCi7Rcnjb0k7BBBfX0jRpDANH81IDMMwsWMxDLOmf4il0wwPDPDja67mN7++h7qYRbouxs9/+hO+dNnlPPXkMqZP6+Soow/iXy8+j//63pf4l4svYe6C3SCSoOKaFMoe+UIJCwfDyWNbVar5YYh6RFIRSmMD+NR0h4bh12b1holhWGDaGEYEw7RrwavEEatuKe/XvOSItOYrnYYAj6GmmW9HlUqFVCoVAFc8HsdUAeABvvKVrzA8PExV6VqEEWQ0l+cnVJB1IJhKyzQmlUpRLBaJq4WIUqlEUim6UZLmr3/9a44//ng+/elP87nPfY4vfOELXHLJJfzbv/0bX/va17j88sv51re+xdVXX81Pf/pTbr31Vn7/+99TrVaDqebTTz/NwMAAL730EqtXr6a3t5dcLkelUgn0Yq2trZimSTabZdq0abS2ttLZ2Ul9fT1NTU00NzcHv/v6+kgkEjz77LOBdLHPPvsE9eoqtcHw8DCNjY0MDw8zf/78oE4MNTVramrCNE3y+TyG0nF5arGqUCiw1157kVSLU6+88gqNjY1MTExgmiZDQ0N8+MMfJpVKYZomt99+O08//TQLFy5ky5YtgWTnKklZB9vOzk5+8YtfsHHjRgzDYNq0aey7774BwItqQjp+JpMJBi0UgBSLRaLKjrG9vZ3BwUFiavFLFn5E8hP9qKV0wMIzpVKJ5uZmBgcHMQyDb33rWxhqOvrf//3fLF26lLq6Onp6egJ1xcjISLAAJINxValvpOzCi2llKpXNZkHNenxt4U2mubJYBpBSekfhZbk3EokwZcoU1q5dy8yZM0ko+9mtW7diKmm6r6+PeDweAOzfit6+N78N1aokcLcanDfkol/bEWIYFhgWGHbtr2mDaeNbUZ5+ZSUf/+KXWL5pK//38svY76DD+eH3f8S/fvpirv72tdxz+6M89chKdpqb4UsXvY9Lv/YRPv/Vj3HuZz7JvN0XUFcXoTg+SG5oGzglMs1pnNIEbiGLnYzhe7XwA77nKYwzQKLy+bWYzH9NSqVSVDSD4rgyNhbpwbIs/u3f/o2hoSEstQe0ubmZhoYGJiYmwtm9iYSJwn8tzfOIALht28RisWB0l6lXOHV0dATHIoXG43ESyiA5kUiQSCQCcJd3hZOQdJ7wMar8PT09gTSw8847ByusjrbqDhCPx5k5cyZRZRf38ssvB51t5513Dr5VOpLkOWXKFEwlpfX19QXlq1ardHR0sO+++1KpVCgWi9x7771MnTo1kFjejsbGxnjyySeD6eZee+0VvKukjM19zdhepF8Bm6amJgYGBqhWq6TT6UA9kM1m2WefffCU7lFmCr6mEolGo28AxVwux2WXXRZMsdesWcPdd98drH6n0+kAzExl3zo2NsbY2BhFtco+MTFBRJn/VJV5TVQtaFkK2KvaNj9fWQ2I1CvSoAzU9fX1VCoVbNumThn0b9iwgSlTpnDsscdSV1fH1q1b2bZtG47SR8fj8WBA+1vSuwbE10XB15ctXv+r/hmmAkOVTLU6a0Zxo0kGrCSVKXP59s9+xyU/+B3vP+VIbvr1jeyxYD+2rdnCk/c9zKN33sdlH/86n/niD3nx+U1YEWifaXDepw7hkq98hMu/8UVO+eBpVPLjZPu34VVLxNJJTKO2gm7o4UYNKVltJ81fGxEty6KitjfZavGhUCgQUXqUSqXCkUceyRlnnMGmTZuIqp0oIh29UwqP2oTAxwgtnMTU4oowdlm5eJKpnJwXkEGBlw684SRkmq97U5mMJD9fSUubNm2iv7+fSqXC9OnT6ezsDKZcIjmWy2VisRg77bQTvu/T39/P3XffHeR14IEH4qhV+mQyiaVWwU3TZP78+RiGQTabZf369di2jamMtqdNm8bOO+9MJBJhzZo1PPHEE7S0tLxBr/d2tGTJEorFIgD77bdf0G6u2qUiK8immhnE1QJDXJlJxdUi1tDQEJ7n0dnZSSKRYHh4OKj/bDZLTO08knoVALPUzGK//fbjsMMOC6anF110EcPDw6TUgpRMfx1lwmIYBo2NjdTX1weDna8Ga0fteunp6Ql0hKOjowwPD5NTdoeoRR5LLboIua7L0NAQmzdvJpvNMjQ0RF9fHxs2bMBRC0jnnnsup5xyCr7vs2zZMoaGhrDVBoV4PE5FmRD9LWkHACKTTpNf1yDW9HU17zImvm/hY+EbNhg2nhmn4MXpHauSnrmAzSNl/vVLN/L083187otnccWV36Ih1QhVj9bmZpxykZt+ci2X/9t/8pPrf8k11/4Py5ZuIZYwmDV7tgJfAzsWw45GKY6OvB5kylcd21dllb9/ZZIVxpjaVlYsFgNpsVKpEIvFGB8f54tf/GLACJZlsW3bNpqbm8PZvYlEAkGtaMt00VOb9gWcdJCUKeBkDKdLdqjVR5HUdCDzQ44H9PMCzq62Sih/wxSPxykUCrz88stBfnPmzGFCGXgL+BYKBTKZDDNnzgw66iOPPBJ0zt122w2UvWJMrZBXKhWSySQ77bQThmHQ39/P5s2bA8m2XC4zZ84cXGW79/TTTwfgooPxW1FbWxsbNmxgbGwMgM7OzkDCtW2b5ubmAKBttZNEVB3FYpHx8XEM5VdQVlpXrVpFZ2cnxxxzDNFolFdffTX4fiCQyDylU60q29brrruOWCyG4zhceumlVCoVpkyZQmtrK5VKhYmJCTwleclAXVDOWguFAiMjI3R2dgaDm6zgz5o1i7lz57JgwQKOPPJIjjvuOObNm0dZ6UeFB0vKImK33Xbjgx/8ICeeeCKHHXYY/z/2/jvu0quu98bfq1xlt7vPPTOZmUwqJQWCR/FQLCCiBxFpHtAgGn5YEJCDSGgiTZAjGKQcShCUkgN4EAj8aD4+oSMIQiCkTspk+tx996uttZ4/1rr27Ayh+CQk+b1+fud1zX3va997X22tz/rWz/eP/uiPeNWrXsXLX/5y3v72t/PWt76Viy66iOFwyL/+67/yhS98gcFgQBx8h7UmelfLnQqIE7abiZwAQ4fEWYmzAudkAEUFMiHubCfddhqDIXT7hu7Q8L4PfIQ3/K8r2Bo7/uZNz+X8n/pp1jc2scD2XaeQNFLWV1c4evgwf/fWS/mb1/9vPvHJ/4vSCtpLOyiyknFvQBpqOk+ckcM5+319YH6SUoNSFKpPbHD8A3zta19DKUWz2WRmZoaLLrqILMsmUcppreuHyTTg1a/5AfRQ02Bmg9aqtZ6YOidv9eSpgWn6da3x1lsNxrXUgHIykE7vI5ANfOtb3yIN0c8HPOABk6BKfR55nrNr1y62b9+OlJKjR49y/Phxjh8/jjGGPXv2MD8/TxZK/lyorqjNf2st+/fvZ2tri0ajAeHZnHvuuQwGAwCuuuoqTjnllMnrH+f+d0L1xrFjxzAhsry0tIQLZmkecvVEyDao75nWmkajgVKK5eVler0e6+vr9Ho9pJQ87GEPY/fu3QB84QtfoCxLdKidTpJkcq+PHTvGxsYGr3rVq+iHypgbb7yRyy+/nNNPP32i8femqo100CCLYMpGUUSz2eSBD3wg73znOyfbRz/6Uf7pn/6J97///bz5zW/m9a9/PX/1V3/FS17yEh70oAcxGAxYWFjAhCDMcDikLEse/OAH88pXvpIXv/jFvOxlL+OP//iPefSjH80v/uIvcu9735tTTz0VG5LwP/CBD/D1r399MhbrAGG9GN6V8qOf9n9IwkCfvPaA6H12dVsBiXMSnDedhYhRURslGkjZQCVtkoUlVGeWq27YxxvecSnv+vCVPP6ih/HS172M3Wfei6ywxHELVwlGvYyFXafT7Rcc3H+YqhJURgBeE40b7UmTqRovfMaNjzg7cRKG/wQkDvXKLpgiaZrSarUoioJPfepTfPGLXyQOgYOnPOUpnHfeeVx//fWccsopEzPsh4kIvkEdzMAatOrJfDIA1e/XnznZTJ42l2tNqf58DaTTUr+uAbY+Rq0l3p5Mn1NRFHQ6HW644YbJ5+v8u/pcTAhknHnmmcQhLeTmm29GCMENN9yAEIKZmRnuda97UYZ8Ohc0lnPOOYc0VHdcd9112Kk6aCklZ5555kRLX1lZmSwCVSB9+FFSa9CHDh1ChKDB3NwcZYjSEkBdSsnq6ipVVXHkyBEOHTrE8ePHOXz4MFmWTXx3D3nIQ3j+85/PxRdfjAzBhiuvvBIRIt21X7JeMK21POc5z+E+97kPWmsOHz7M/e9/f37913+da6+9llFguCEE11TwNwK0AoFDURRUwYzes2cPe/fupdVqIUMQifBMu90ut956K1dddRVHjhzBBX+iCLXkrVYLay2rq6t8/etf54orruArX/kK//Iv/8J3v/tdZkLxwGWXXcajH/1onva0p3HttdeS5zmzs7O0AlFIFQIxRQg83lVypwDiiTy/aTlhMguhQmT3BMuMkH6fEBJZWIpDx3DjjKTZwFhLFSmYa9PH8n9/8+v8xd98mP2blov+x+N46CMeTZlrGnqe7dvPREWzxDPbiDvbSGeXMZWksbAdlTQZrm2EqpNae3I+Bcc5nDMn0nF+ghKf1EinBiopJcePH+fSSy+d5K61Wi1++7d/ezIRTwaf25N60k0D17SGaIO2dDI41VplrSHUADm91efhgvZXBd9SPSGnz68+ppginpgGxZOPX4sL2tKRI0e46aabSNOU7du3s3fv3slEJdyvOnCS5znXXXcdURRx5ZVXQvDVPuABD5hM+Pqa7n//++PCYnTDDTegQ+5jrXHJkGPZDb0/8pDIrUIU90dJFczFlZUVCEG0+vtl8BkWIa1l586dPO95z+N1r3sdb3/723nHO97BBz/4QV7zmtfwgQ98gM985jNccsklPOIRjwBgfX2dD37wgxw5cgQViBXageVHBJ/wM5/5TH7v934PGbTRutTxr/7qrzjvvPOw1tJqtSZmen0+MlgtnU5nEkW+9dZbednLXsaf//mf85znPIff+73f4zGPeQxPfOITueiii3jWs57FM57xDF7ykpfwhS98gZnA7FPfxySQz15xxRW8+MUv5qUvfSnPeMYzuPjii3nTm97E2toaaZpy/vnnTwKG9QIyPe5sMOvvNkAUBFw4acxOw8jkZ72z3iNcSHD2pXrSKYSTk657SIuTBifLsFmQIW0FgShLZnbupNlpU42G2CqnzEbk2Zh0bg7VmuHg/kO87c1v4//7qZv41V+/Fy/4iz/kzNNP59C1V2PyISYb4soMrSVld4tqnCGEJGp1bhPQESH3sAaDu0JqQKoBIg9JrlprlpeX+fznP883v/nNiS/t537u5/jv//2/T1I5fpTU31+D3/Q2bbLWf2OC77AGtmnQnAbPen+tbZ58z6Zfn7xfnqSl1jINivXvOlQk9Ho9rr32WgjgtmvXLlwIujQaDdrtNmecccZEKzpy5Ahaa/bt24cJvsozzjiDKCST15r46aefPrmeOh3GBYBXSrG5uTk5n83NTWzQHNOQCP2jpDbtal9xPZFtMPsajQbj8ZiNjQ2stfzsz/4sj3jEI3jQgx7Efe97X84//3z27t3Lve99b7TWlCFa/b3vfY9XvOIVfPjDH2Y0Gk3u09LS0kSDqo8/OzvLYDDgla98JR/+8Ifpdrs0m02e9KQnsTlFYVbfzzSw//R6PVZXV7HBXM3znPe+9738y7/8C9deey233HILLgSw6hrsItSTW2sn6U5RFLG+vs7W1tYEuFdWViY+yIWFBQ4ePMjVV1/NYDDgggsuYM+ePbcZI5ubm4wCc1F9XfquTrvxOtxJGBfkZDCsxQFOTPU41gIkXuNzGmljlIuRKKQEp0pclFHJHqXdxDFEihJZ5FTdHrYqqIohthiiRUUqHQmOyEBkFGZQsbB8Ko3mAv/XRy7n9a/+MMUAnvmMh/Hc515EWq3Ru/W7xKbLfEtS5T2kqJACysEgXECoTRYRQuipQA/UfsR6gteAEHZOfj958t/m736I1KtxPRFFSJcgfMfu3bt59atfTa/Xm6R5/M7v/M7EN0PQjmoTrApR1G63CwE86vdUSNuxQXOrJ2Y9wa21NBqNSaJzp9NBTvW+mJ2dJY5jer0eLhAs1MeIAoWZDlFqOcVxVwNO/bPWIE8GyulNBvN+NBpNcuGuuuoqTGDtedCDHsSxY8fQWrO2tsb27ds599xzKcuSa665hoMHD3Laaaexb98+brrpJoqi4IILLpiYhUePHmVhYYGzzz4b5xw33ngj+/btY9u2beR5zkxgUqkDV0WgL5tOk6qDF/Vzmx4j9aJy8u+1VCGtqq4GaYR8u69//et8/vOf50Mf+hAf/OAHecc73sGBAwcm4+k73/kOv/Irv8LFF1/Ml770Jebm5iZBoDolJw7J5y64BQDe85738KlPfYr3vOc9E636sY99LI997GMnNcRaa4bDId1ud2ISt0NlSxSCfNu3b2dxcZFOpzPJ3ex0OszNzU1AKkkSXFhAdHC7xIERiDBem4HIpPZrWmt597vfTZIkZFnGC1/4QtbW1ibjeSZU29Ra4nA4nPh67yqR/CBA/CHz3NVkq/gPTjoGILz25STCqomDrhp2QVtEAlaUIEqwBUo4ZhbmUZFCKklV5uS9LVyZ0U5jmlGEy3MSHZFtdRHWMDM/w/6rv81f/fnFfPg9H+Os3fO86ZI/5TGP+VWy3ho3f+ebNGZadOZnfJ+WwNj9/dtdJ9OgcHsShZred7zjHZPVd3Z2losvvphDhw7dBsxcqFgZn8QVNw3StdSgU5u9VVWxsrLCVVddxc0338z6+jo333wzBw8e5NChQxw5coTDhw9PzPd2u83Kygp5yJerQbYGvfrYtyc1cPw4UmtXzWaTQ4cOTa5h7969LC8vT463c+dOTCDbvfXWW3FhcVBKsX///gkQ7tq1CxECGDt27EAHc+6GG26YuC/qa4pCdca0xlVfqwj+wGkQnN7q/fWiUyd221D2poIf8vDhw5OJfuzYMd7ylrfwwhe+kFe96lVceumlvP3tb+clL3kJ4/GYlZUV7nvf+06Oe9ppp00WIhHSmEwIYJShUmVmZoYvfOELfOQjH+GMM87AWstf/MVf4Jyj3+9z0UUXcfrpp7O+vj4B+ukofn1/65/T1kMN6kWoMa+fe70o1gtmLSePPxeS0YfDIWmacv311/O5z32OKIo47bTTeNzjHsfa2trEL1oGguEa0H/Q+PpJyR0/2qRHSY2MQULE2eEQUYyQCjPOoKpIWi2EEAw3NxhubTJcOc6o3wcpiYOGtLm2ytrhA2wdP0Q13qIcrlMN11ieT3nA/c9mx3zCNd/4Ah9676WsHK/41V9/KBf90R+zfe9pICUbR45R9QckM7MnzgmmT7jWdU96/ycr9WSaFmMMjUaDT37yk3zpS1+iFdiKf+3Xfo2f/umfZnNzkyxUnpjgt6urG2ot7GRQrPfJoFmWZckFF1zAH/zBH/DHf/zHPPe5z+VVr3oVL3vZy7jkkkt4wxvewN/+7d/y13/917z+9a/nXe96F2984xv5/d//fcaB/64+bzulgdaDvpb692kQ+VGiQ7VGs9nkhhtuYGNjg6qqOOOMM9i1axdV0LzOOeecyd9fddVVqJBnqLXmyiuvnGixNelFURTc5z73mdyHb37zmxPNppjKcasnYqvVmtzjaQCanpTT106YsCr4S3fs2AFAN1Bq1QvRzMwMzcAAtGPHDmzw6S0sLDA/P0+73ebo0aP827/9G9u3b6fT6XDRRRextrbGMJBb1M+9vq/1cbXWbGxs8JrXvGYCcABf+cpX+PSnP00n1Ib/wR/8AceOHaMV5t5gMCALNc31/Zne6uMQFqx6Xz1+6/swfS+Y8nlPj0dCatUglP+9613vmmjMv/mbv0kzED7U91uHKHwN0nel3HFAhACGTABwAjQBeFSaIqXClRVYn8BtyoK03eScnzqfh/zaw3ngQy7ggT97Po/45Yfy+Mf9N377wifyJ897Fq99w4v40+f/Ia9+/Z/xwpf+D37vogt57vN+j7e8+S+55M1v4E9f8BxUoplvwKlnz9CcnUMnDU/u0GoitZ5SDH1qkD9HH0yZrq75Scn0wKhlGjgGgXnFWss//MM/TCbq6uoqz3ve80iSZFJpoEPaRW22FMHpfHvHqCdOPcAe+MAHcvHFF/Pc5z6Xxz/+8TzykY/kl37pl3jkIx/Jwx/+cB7+8Ifz0Ic+lPPOO489e/awY8eOSTSWMMhrcKgnxsnHPXlC/DgDWgcnehRFrK6ucsMNN1AFqv9du3YxCtT85513HjJEPa+//vqJOSWl5Lvf/S4u+MfOOeecyfmde+652KD1XHfddZPrqc87D/RoUkrm5+cneYA6RJrriToNBtPXLoL7I45j9u7di7WWLMvY2tqa5EKakKTc7XYpQ2pKo9EgSRLG4zHbtm2j0Wjwtre9jWEou3zSk540YbaugUEEjVVPtQIF+OpXv8p3v/tdzjrrLNbW1mg2m8zNzfGe97yHY8eOkWUZj3rUo/jFX/xF1tfX2blzJ5ubmxOQPRl46nFTX1+91cest2mp78/tjZVpV9DS0hLXXXcdX/va16iqitNPP51f+IVfmATB4lB5U/8sf4yg1p0pdx4gUtN/efbpSZ6fAJPlgEA1WogoIev1yQd99t7rTH7vKefwpEft5Q8fdzbPePy5PPWRZ/LEB+/mUQ89lfueMU9kT5xkBaxsZfzrlSu8/5Pf4u0f/Cz/8E9f4l+/dT2vfvMneMubPsyhW26lMhZTVbiy9InZAaA9c7bfXDjPuwoUazl5MNWD3IZUkKuuuoqPfvSj5CEN5vzzz+cxj3nMREuLgwO/3W7jnJtoIT9MRIj6iimi2n5gKFlbW5v4tL74xS/ypS99acJK8/GPf5x/+7d/m4BD/R0/aHJM/15PiJMnzg+S+m+llJOocRRF3Pve96Yf2Gr27t2LCw7+I0eO0Gw2J+B26NAhVlZWUEpNAivNZpMzzzyTqqpYW1ubAJ8LvlEVotFHjx4lCTXHdcmeDRyT9QT/YTIIrRVqPsKiKFhbW0MG8zkNVSmzs7NEgWyi1iDTwINYliUHDx7k/e9//8QMfdrTnkYSkvkJmlr9DMqQWpTnOTfeeCNnnXUWo8CSnobS0P379/O2t70NwkL1ghe8ABHo15aXl4miiHGg+aq10OlnWj+7MqQV1Qt1DZjToEd4/icDYr2vqirm5uYYDofs2rWLSy+9dHIPHve4x02uWQffYa3J16bzXSV3AiAKRGDG9uK1L99GIICNc5iyQkpNnDaQSuGsYTTocuiAY1bDPIL9193K6/7qb7nwqX/I71/0bP7iBS/mVS99Ba/+i1fw0otfyytf9Jf8zatfy9vfeikf+8S/8KV/+x5f/MZVfPTjn+JbX/8Gg1HG3Cm70GmDxsICze3bSebnggLrQfGE9urB8EexXd8ZUoPI9CCbljopdxSK+9/xjndw6623snv3bvr9Pk996lMnq38R0jeccxNuRaa0hWmZHoyj0YiPf/zjXHTRRTzucY/jd3/3d3nqU5/Kk5/8ZF75ylfyspe9jJe+9KW8+tWv5nWvex2vfe1r+bu/+zu+/OUvEwV/WC23B4r161rbqDWM2zuvk6U2+20I8lx99dWTyXfBBRdQVRU7d+5kbm6Oqqr43ve+R7/fJwkVGTKU4NUR6p07d7KwsMDOnTvZtm0bANdee+1tNC0d/IpCCA4cODA5l3POOQcTetlEoZ53+hqnt3p/zdozOzuLEILDhw9PotP1va8j0ARgq81yGVJVZmdnWVpa4nOf+xxlWbK1tcUjHvEI9uzZM/l8nucTjW76u+o0m83NTVqt1sT90Gg0+MxnPsM3vvENAE499VT+5E/+hGuuuQaCKV8/qxps660+NxEW7FozrQGwCr7FLAR0pp9//breVwfgRiEfUgjB9773Pa688kqiKOKCCy7gfve7H3meQwDg+px+nPFzZ8qdAIh47TBEbJ0ImlfdfQ8QcYKrHLYw2MqidESj0+L4wf289+/exT9c+r+55tobOP8+p/GSFz2XN73ljTzz2c/ivPvdDx1FWGtotFvsPuts9tznHOZ27mFm+y4aizvJrUbGTeZ27sYJRXd1g/6Bg5jKkA0GjFdWpoDPb7WmeAIk71qZfsgiaG0m+BFnZmY4cOAAH//4x6mqiizL2LlzJ7/1W79FKzQ3ciGyWPv1auCrv7eerPXE0aGVQLfb5ZprrmFzc3My4Obn5ydmdw1MdXSx1Wpx6qmnTsCwPk9+iAZYv/6PACJTFS1pmnLLLbdMIuj3ute9aLfbnHXWWZOI71VXXQVBgzQhdSYK+Ygm9EXZtWsXp59+Os1QMvfv//7vk3s97aNqt9tcf/31DEPp2kMe8hCWlpYmQZfanK+lvu5abEiz+aVf+iXSUHb59a9/nWqK9bw+BxvYwmvNNgstBEajEWVZsr6+zsGDB/k//+f/0AgNt57whCdMjjcNhi5kFGitmZub48iRI8zOziJDLmKn05m4Vd71rndx8OBBtra2eNKTnsRDH/pQVldXkcGdMi12KqhShHxFc1J70mmwm7430+NwenzUrp085Hdubm6ysLDAZZddNiE0efzjH08ZgjaN0KoiCQw8d6XcYUD0rrlgMkMAnKAZ1tqXsUipkSrCZDnleETaapC2ErpbW3z3upv5y795G7/zjBfwhnf+I7euDTnjfvfmyU97Mq983Uv481e+knPOO4ettWOsHd5Pb/UYg811TJmhtMLiSwOzcU7c7qAXFpBRhB2NSJe3Bcg7AYj1q1pvvLtBsc6n27lzJysrK+zevZsPf/jDfPrTn2bbtm30ej2e8IQncPbZZ08GLKHcTUyx2TC1UtfHsCEAQ/CXRcGxHwcCCQKwdDodGoG9ZmZmhjT0Wpl2dtcTgynAO/l4tZz83g8TFaKx9cRfX1+fcPTNz88zMzPD7t270SE16NChQxOtRQRtL45j9u/fPwGipaWlSYlfFKjrCcCrQ9maDRrp/v376Xa7FEXBWWedNWkdOh6PJ/fW/RANcWFhgZ/7uZ9DBRP829/+9gSwkkBQW4OrCYQdJtROzwSyWB0IDaqqmrhMer0ej3vc41haWmIudMGrgbXWzsqypNvt0g4NsFzwo9aAt7i4yL/927/x0Y9+lLm5OcbjMW984xuJomhSQ12DVZZljMdjRqMRg9CAvtfrsbGxQa/Xm9yPKLgjOp0Os7MnBy2/X6Io4vjx4+wMTbDq1KYrrriCffv2kec5D3vYwyCMmzrx/GSwvivkDgMiCJx1wQINE0JYHMbXDFsLDmxWIlHoKCVKYsa9LlUxZsdpZ5KnO8k7Z+GWzuFLVx3lL990Gc/40zfwpr/7JP/yhRuY2yb4zd/8Jd7xhv/BS//8T3ni4x7JjjmN7R1luHoQU+SoOMXPVUHSblP0+witKfp9D3zW4ly9uvpzuiulHnhJqEGto4FxHLO4uIgxhuPHjxPHMZ1A0fShD32IXq9HHMdUVcUrXvEKVlZW6ARK9+kkYjVF9cVU/W99jGrKF+MCc0orlA/aKRKIUSj0J0RpCYBVg1sNBLVfqd5fX1MV/EJZqKRgCjynjz8NngTzrfYVOuf41Kc+RRzYWH7lV36F+9znPhhj2Nzc5IYbbmBhYWESaa9B7zvf+c7Er1ZXV/R6vUnCd91eoAanPLDnHD16lK9+9auTNKPnPve5jAJ7eRTcE/VxarO19nmtra3xW7/1W2zfvh1jDF/5ylf4zne+M2GaHoW64zikt8zNzU2eWX3+LrAfDQYDzjjjDG666Sbe9ra3MTMzw2Aw4BnPeAbrgS+zHg81y/o0MNcLShnowLTWdLtdTjvtND7xiU9wxRVX0AgEw7/92789yTWt71+9GL70pS/l4osv5jWveQ3vfOc7efe7380HP/hB3vWud3H55Zfzzne+k4997GNcfvnlk+vo9/ssLy/TD6zXhPEjgs9ydnaWjY0N4uAD39ra4vTTT5/wNl599dUT90B93zlJA70r5E4AREJTppM1RO9HdMIhdYLQMc6ALQ3lOKMqMnacsswTnvxYdpx5HrK5nXHVwMhZdHsnRHPsP7DK57/4r7zgT/+aN/7N3/G5L+8j0ZLH/7cH8PJX/jF//tIX8zsXXcTOPacSxQkLO08h2+piygpnPBirJEE4H9k+MSXrV7Xv88fTZH5SUoUcuBokTfAvHTp0iA9+8IOTlIVWq8ULX/hC9u3bx3g8ZufOnRPw+mGibqfSpAbRWsv6YduPktv729vb94OkDPl0MkSQG43GxF8qhOCXf/mXOfvssxFCTKK1IkTnXcgpbDabVFXFrbfeSpIknHvuuezdu5dOp8ONN97IaDSiCuk70xq1CezU//iP/8jm5ibLy8ucd955PPOZz+To0aPkoRSuBtr5+XniOGZlZQUhBA9+8IN59KMfjQoMOZdccgl79uxBSsmNN96IEGIC8vViY4whCfyS9TXGccy2bdvY2Nig2Wzy5S9/mW9/+9u0220e/vCH88u//MvY4P6oAuv0KNQ119H2WhONpny+UcjpK8uSt771rQxCP5YnPelJRCGYV5blpFrn3HPP5VGPehSPetSjeNCDHsT555/Peeedx969e9m7dy+NwIO5vr7OoUOHJmZ6K5QG7tu3j6997WvcdNNNk3HH1Hio73s9LpxzPPGJT+Sv//qvueaaaybPpgglm3e1lnjHAXFiLocLFDUQnvAhOms9LZcTRHFKnKSYImM87HHqKYKl5WWkinBOIqOUuDVH3JqnyB0bx7fozC+zvtHnAx/4MC9+4V/wP178Vi77x6/Rt3DuA/fy7D/9DR7/5CfwsEc+widiO0hmZ5FJgq1rIZ0/V1H7O0MS+Z1xC+6o2OCH0sFfVQZSzsFgwMc+9jGuueYa5ubmkFJy4YUXct/73pfV1VUaofLhB0mtOdRyMjjVoFiG9IZ6MMqTOBN/lNTnMH0sOeWU/1FSm69R6EyYpik33XQTq6urECbp/Pw8ZVly/fXXT4IGLvinakAdDAZ8/etfn/jkhsMh4/GY73znOxNtVga/Vn2u9WL0ve99jw9+8INUoW/whRdeyIte9CL2798/YcghgPehQ4fY2tri53/+5/nbv/1bFhYW6Pf7XHLJJRw4cGASJOl0OiwuLrK+vk5VVbRarUkgaBwIXuv7r0Mk3wQf6NGjR/n0pz8N4b7u2bOH9fV10jSd5C7WyfNJktAOtP9Syok2W4aUlThUkNxyyy28973v5eDBg9xwww3Mzs7Sbrc5/fTTOXLkCF/4whe44oor+NKXvsSnPvUpPvShD/H+97+f17/+9bzoRS/ihS98IS95yUu4+OKL+bM/+zP+4i/+giuvvHJiyq+vr3P55Zfz13/911xxxRUQugMyBYj19dTinOPAgQP0Q0/vhYWFCejX539Xyg+eTf8BOQEyeOQJQZWJD9HhezfnJThIGyk60qwfP0J/CLEwuHJMlGi0VpiyBAtx2mJuxy6OHzxGWUJnYQd77n1/KtXmS1+9kkve+AFecvHf85GP3UBZwFn3luy+733BGvKNDZypiBppTWwzOU+vFXq27FBNPX05d7mcPFCqUMo0FzqrvfWtb4UpTer5z38+MjCn1H7Ek4FnetCZKU7C6WPVW7vdnkxWFXxURWCPrqOlP0xu7/j1vpP3357U4KlCDbAOlSj79+/HBL9lFUz+q666ChlMvHritAJDSj3pa22xEQhP637GNejU1y1DWkxRFPz8z/88l19+OZ/97GdZCG1EH/vYx/KpT32Kpz71qZx33nkTDezJT34y7373u3nWs55FHPx1n/nMZ/jEJz7BeeedNwFAFdwaaUjfKUKVSA2WVVVx9OhRbAhY9EN7VhF8oF/96lf53Oc+h7WWU045hUajQR76WzvnuPXWW1lZWeHmm2+eLD4ulBHW910IMTmHdrvNpZdeylve8hb+5//8n+ShQVgNZK9+9at5xStewate9Sr+8i//kr/5m7/hrW99K5///Of53Oc+x1e/+lU+//nPc9VVV3HLLbdw8OBBdu/ePQGuudC7eit0Xmw0GpPxU4+F+t5Pv961a9fENZHnOWVo1zut6d5VcqcAoge88EttLk+cdAIhNSpKwEIxGFJkGc12iyhWzLWgLXPINmjIAmUzqsEm1XiArUriOOH3n/d87vtf/iurx7c4vtKnP5QYN4NIdhDP7OZrn/sSn/roR3n76z/B6oFbidOUxsI8OorI1nxP4hNSQ6NAoMIt+NGT9icpdopwQYcIqDFm4rT+4he/yGc+8xl04Ir72Z/9WR75yEcyDhRXtUyDXf1zegDWWui0RudC+k4/MDrnoQMawdSuzfgfJvXAnjZF/yOAqKZ8lC4AVZZlE3KL+r7Eccz1119PEhKaCTmASYhiNptNrrrqqklwwFpLr9ebmNH1ddXXPa2V33DDDezbt4+3vOUtfOMb30CFpOwzzjiDxzzmMbz97W/nK1/5Cu973/v4/d//fX72Z3+WmZkZ1tfX+cd//EcuueQSlpeXJ6kl9f0ogz+v0+kQhxrxbrc70fbOPffcybnVvtf6mo4ePcr73vc+nve85/Hxj38cPUWtv7q6ystf/nKe8pSn8OUvf3liepehJ0r9vGutMw7+3bm5Ob7yla+wsbExAaD5+XlMKAesK2iWQu/kU045BWMMrVZrUt+8uLjI7OwsZaD9qjX7KIqYnZ2l2WxOFtl6MT4ZEGuxIdq+sbFBlmWTZ5SEdKf6ft5VcocBMaxDJ4GKB8Y6cuGy3DeZarRQUUKZZSglEc6SAI1YU46HWOMd+yqK0UkKFsqi5D7negJNZx2NzizNuUV02saUUPSGxGmTfDhibf+tVFnOeKvL8NBhqnFGe3l56nROaIdeM7z7tUOC5kcYHFGoW63NPkLf5be//e10Qw9iay3Petaz2LNnz2QA/TCZ1sBOBigReOzqrdlsft/240gNAPYkJ/j04P9BUk+YeuJaa5mdnWXfvn1UgaOv1oiPHDkyMTtrkKyBPIoiDhw4wC233ML8/DxRFE1aE+hQ4TMNivV92bZtG81mk1/91V/l8OHDXHjhhbzmNa/hyJEjDAYD9u7dOzHRa0DY3NzkQx/6EM997nN5//vfPwmQ9Pt92u02NjDBNEP60qc+9Sme/vSn82d/9mccPXqUbdu2MR6P+d73vocIgYf5+flJoKN+feONN3LNNdewf//+iRulCoQUJpjIq6urzATquFrTIlgaVVWxbds2RqMRcRxPktZ1SMlph86NMpjaaSjlq4ErC20UWlPciPV7YipQmIcWuXUvmCzkJ+rgA6w12OnnXQNkfT7z8/MTEK23k8frT1ruMCDW2tZtTGbqfESfeiOkxOQFQkqSVgtnvQY0Gg7oF6Abc1QmpnIpTjYRqoFOO8go8W1LBcy1mihhEeWQvLuCyXpECaSzLQ+sScLue92bOG0ws7SN1o6dAPSPHJnSCQMoOuGbS02A/K696bcn9SSvJ7oLkdw8z1lcXOTGG2/kYx/7GIS0lHPOOYdTTjllou2dPHDqSVFPfILpXJtttSZYD/ph6PuxsbExIS299dZbueWWW27zvT9I3A8AxB9Has2yDJU4eZ4zNzfH1Vdfzdra2mTifeMb32AwGKCnOPPqCRSH6Hz9uSpUVqyvr5OFnLZao5nWwo0x3HzzzZRlyb59+zjzzDM599xz+chHPsKf/umf8uIXv5g/+ZM/4bWvfS2XXXYZl156Kc9+9rP5nd/5Hd761rdy4MABhBDc7373o9frsbi4SBn6loxGI7rdLnEcMx6P2bdvHzfeeCONkG/aarUmGhghKhuFZHARgjFJYIapryENlGY1aNX+1NnZWWyIXschOl9nELjQtjZNU44cOYKUkiOhD7YJlUt5yLusx4INARzCgl2GZPFxaKc7Ho9phPLDQeipMjMzw/z8PPPz86RpigmpS0xZDDWY1uJCAKVWAGoz3gbqtJnQMuGukjsMiE5YrKhwsvJg6CTSaZSNkDZCWEUyv4jJM4ruBlUxwtoSrRVSa6SCOGkgZIRUEdY6suGIbDikKguEFGQZaAFKOHzWoUFpwFWM+xvkWY/zf+o8nv6MX6bVabB5+ACj7iYqTWgsL/tKGuu5GgUSQmMp4QTYux8MVchXM6FCQilFGlpFAmxsbLBt2zb+/u//nquuuopGo0ERup39uD4WO+UXHIeIaRkCKbU5dOqpp3K/+92PX/iFX+A3fuM3uPDCC7noootO/qrblXqgTw/2ev+PkvpzJiRZj8djkiTh5ptvnpCuZlnG9773PZjSeGsShUYjRUrBeDwiy8a8852X8vrXv46LL34+//N/vhYpBY1GihAQRZoo0jhnqaqSqiw4/dRTUUCV52AMg16PHcvbwTqu/Pa3+fdvfpPLP/Yx3vGOd3DZZf+bb33r2wyHI5LEN4batWc33/3ed9FaURY5VVnQbjTodbdot5o4Z+nMdlhYWqA906bRSOn3e2ysryGdRWPpNBJWjh5mYW4GnAeD9Y1NrIO5+QWEVAyHvmKlKgsG/R7tZkqn1QBnGY+GrK+tko1HSAEyLJBxHFGWBe12iyjSlGVBmsbs2bOLKFII4ajKAhWAtNbW4pAbWS/SzoGQitm5eRrNFkVZUVaGwXBEnCY0Wy2EkGxtbLGxuk42HCMRtFtNwIIwnhd1umAjzL9Wqz1xX+hAShyF6PiPk0VxZ4qwzjmDQ2KQrsDnEkogopQaE/Qnn0pTMkJzxEqe+rzX0o93UOo2VvrGTjXoSCsRTnkfnZOYIke3UpzIEWQUo1VabcXm0f28+q9fyY03bPGB9/4jjYVl8spSWXDOohWkseIFL3g8h/blvPPNb8I6QWfHqRQipiglcVszWLue//qgn+HJv/lQ3va2L3LTzQeJZhawBpROkOPS50kKsFJglY8uK6MARyVLnChwLkc4G7RHhUAjbElDjDHHbyRev4G5couOyxFVReVihIpC7+cfPPFrU6PX6zE/P8+b3/zmSQXIa1/7Wi6//HJmZ2cnaRi1P0aFhGUXTIx+v89pp53GZZddhgz9hGsygqc85Sl0u90J8DUaDbrdLr/2a792G2qpzc1NyhDJrf09delXvYrXUoPZq1/9aj772c8ipWQmcNapkGZSa1txHHPo0CHe8pa38JCHPISqqvjd3/1dDhw48GOb3T9IolCRUptQtenvnMNaAziMPVFnW2sahAhr8kMi5cKBDoQjYZpO/fT1+bLmewQcJ6o1jPU1+wbjSwMcYXMI540mKwQmrLsuzKUTfwfSGVQYPw6JFRKDwgqFReHq4AgupI9ZJBbpbOgk6YsSCOfmf4ZPeJPIv74dhieBP0nhvC/9xAgO3xOGwm3ui5ii/qvfFRbhQFk5uS4IRCrCYKXBCht0D4GwGuk00ioEAitM4BW4fakX/izLaLValCG4GMcxn/zkJ7/POrojcsc1RBxOVjhhcMIFpmwdtEONcL5BvbMlphxjbAFCIFVEa3aR2VlBQglFl/HmEbQqiGKD1hXFcJN8uEUkoMzGtNsdtu/agzEOa/yKlfUHSGWJREkqIJUOKS1gcBisCQw71vlE8UmDKYuzBqwJi8BPTkzw9cyE3rr1ZJbBV3jyA61f14AkAnFrmqYcO3aMSy+9lNXVVZaWljDBGS9DICJJksmgKQIxbW2KnnrqqVxwwQX8zM/8DOeff/6kKVN9LBuiubVZnYXgxMrKym2OU2sNBLAfj8dorWm321x22WU85znP4dnPfjb79u2bgO0dkdr8rbf6+PV+IQRSCpSSaK2IIk2SxKRpQpLEUyDw/dv09J6GE7/Dg4m1FuMsxk5vDmMdlfW5tnbConTbTo5uAgz+b2zNuBRKSetj+vMIHwzA4XlHb7vVZ/n9Mr2/BsNwBHHbI7gTlbaTj9XfPH2EH7fM/0QAleCa8mz5cjrYKoKWiM8+EYT85Ymr7Z4hdxgQvf8wBCjcia+rH7gTFhEprDNY53BC4oTCWAUiYWPF8cDzl/mp++4mMVuUmwcotw6ybT5mYT4mVQUNBb2N42wcP0qW5fTWtxAqIZlfJmrNoJ0gqSqaQEsKlDVgSl+vbAqgwomwyRObVRVWTqUH/YREhQTVaT9W7Seqta0aJG9PxlNMN1prPv7xj3Po0CEIpmTdda42PWvzQwjBysoK+/bt49prr+Xb3/42X/3qV/nnf/5nPvKRj3DZZZfxD//wD7zmNa/hVa961aRC4TnPeQ5/+Id/yEUXXcRTn/pUDh8+DEHbYoo/sNaU6tSf2dlZrrrqKv793/+dK6+8cuKwv6NSX9f07ye24IO0J2a6kppIx5PtBAJ8/+YQGHHbzUow0gXNzlFiKJ2hcgaLpXKGylVeN3QGKwKZXK0Jhp/15jwMfN9+K8AIQSUiKhFhRISpNUOETxeb2ggAZ5GYoEkalH/t9UavWYr697ChJtqn1zw1FnXiWBMtdHLHJ9v3QXLQTG+7nZAaeL0mWYO4v3gRNllf09Tn7ilyx01m1fEmqJAIK5BWBZPZ++mcACetNyuVwdkCW4xoNBP6x4/xi7/4YP7bQ/4Ly4uSZgO+c2vJP7zvfQz6A7qrKwhTcdl7Xsu3v3Wcv3/3e2kv7eTA4XVUZxGRzqJjg9i6gYecfx+e9nuP4Z1//yW+fs0NuNYMldS40hEZiXBgpcNogdUBwKtwnhKv4f6ETGZzUrrHJZdcwv3udz+MMVxyySV88pOfnPgMa62n9pPVzu0qJBY3m02uvfZaHvWoR3H22WezublJkiRcccUV2FDjWju46+jjyf7D2tkOXvusKfRrqc+zdsi3QolfXVLYC+0s09Bk3YbUifo8ZwPrixCCjY0N0pNYlf+jIqeinm4qQlmLMf7+T++v/66+hz9cpqBAEEb7idfOOazzmp8L50O4botFaIkAlAUVTEbBNOidAErC+ypsHujCXAnncltN8PbgZnJ24f36s1N/WWuA9WuYaLzTe6UDPTFz671T/9d4duLt2+xn8p5AWol0EllfqLA4UWFlNfEdCjybvnAR0moP8OH9HyTuLjSZ1cte/vKX+9vqENQpHALwK019y71YSiR9J/joP3+ZQrWxMgWhJ42lJmqyINwQi3EFaIGMNKYscdaRNDrkvRFr610+//kvcfWNR1ktmuw8fZ6HPPyn+M1f+Vn2nHV/qmzIeWefx9qh/Xzp//4syhVs27FIZ65Fv7eGyzZJiy675mZ54AXncdV3b+XA0eMQJ6AUrjIoJxBYrHRY5Xx7FTn1VGV4gMEX5E++1notkahwww3UeJ3UZiQYhLVYFEKqsP7/cKkjjbWJefPNN/PFL36Rb3zjGxOT8+TJXEuz2WQ4HCJDQnIZqiWuueaaSW+RIvQDyQIVe52vVkfvRNBK62qAbdu2sW3bNpaWlhiHdgS1Mz1N00kKTj0Ak5AXVmuiIpSb2RDZPNktUJvfbirK/f9WaqCb3ur9HiztpMXsNGgy5a74QVL3BrJSYMPvk35BwaITUgbTF4QUYQMh8JaQ9MeSwT84eXJTQFhvItCGCo9POCGxQmPRuNAd0r/vYdH7Cm+jawXty/vBHfL7ATRoZ5NvEd78ro9cf9aPcd+w93Yg1X9b+NrpT/vNn5c/7G0Dlg5/cxw14p849zr/V1D7LsM9/BFWWu2qqcdcDZIXXnjhPQsQnUx98yZU4EVkouM7aXGyXimcB8SiACdptGYRRFS5obW4g6NH1/jOt77D1795HVdfe5ihbXP2Gcv86i+ez3JDsrSwzNFDB+htrZNlI9ZXj6K0YO+uZYqtVbbNzvEz/+X+fPfqA9xy5BguSiCwiqjQZc+3gxagg0brfCaif+o/OUB0oRrEhoqEffv2cfXVV/PNb36Tra0tWoHWffrBuinzuV4RdSAk2L59OypQsDcaDfqhn68Kibj1Z4QQpIEItTbLa6DK83yiMdbNg+rvqIGvDOkWKkTBi5D8WwdSao1WhwoQQtP2ui7WhnzC8g6WYE3fixrwpjVcKU8EOjwYSpTyKSMesD3Q3O5WA+CUpuPfqv8GZAA8fzz/2j8qF8DGT3Tpal3Pby78UoOh3+mnR/23OIkVEU4oEH7+SOdhTgaTtH4thENwAtymIHICtjWw1O+I8J9/Pb3Xf34aVr9fPOCdOErYVwNh/YYLACskLpjgTgiPAQIf/PF96ILZXFuQtRV5zwHEH7x0/gekvq0BDgE3Re5gcLHAlRkOh0xShNDko4oomSFqLTEaa6LZ3cyfcT9Ua4nDB1f44Lvex9++8R948zs+zv/+2JdwEl75omfy1re/lt///1zEjm0LiGrITdd8Fx1prFRYCWjth5EQoLR/SJPBHc6x9mXUpv0PfxZ3WIpAr5Sm6SSPrNlssry8zL3uda/v8yHWD3j6dZIkyMC2Mp6qUKmqiplA11Vn9aeBRoqplJ4okDjUgFKbvK3Qxa3f7zMajW6TzlPnvU1rgSpUr0yD0ng8nuSvbW1tTdImTIis31GpJwDhntSv633WAdI/Z++rEwilEOrE5PyBG9629KOj1vKm/FyT7YSWdgJObhsxrs3l/9A2uUqB16Q8EEpnUM6gQxRaUaGcQTpzG62xRlh/5WG/swgXotF1RNp5YPW+O/+524ORyVSYAryTp8fJC8gJnVaGqLr0vlFZK1V1kCUsGuF++4+f+KZ7gtxxQAy2xfdFjMKFOmERUmDLAosjarQQQlH0hlSZQYgYIWMQEdYIqsLQXtpOY2Ebo9zwjW9+l8s//Tn+7M9fx4ve8AE+84UbOPP+p/HCVz2P1/+vl/PE330aJA1MFGEiMDrGBKeyE942FrLuyRz8gmgfCXcKGdT3n6TMzs5OoqObm5uTQENRFBw7duxHrnC1ZielZHZ2liKQdlaBFEGHqoPpyHIcaJaGw+FtuOzUFMNNGWjo6wTbOorsAnV7DeS16VsDpQ5J0bXG22g06IRm51loXFSf050htfbstcETFQ/1PmNPRKCnwZOpReUHyQQITwYqJwLACYT1P6UDETIW6v3COqStJ7v/mxoYlQVlBap+L/w+eV2DQ51OczsBCw+ANQgaJMa/N/lbFyyUKTCswyz1ZyehF7+dANMTcgIi64XCf+Pkr8MC4gHOH8EGc73eajCspKSUkkr44I8TYakJ131itN+zwBDuhKBKJeeAxPc6dhYh/MOxVNia9cYZZLOJKwxKpygXYYYFkUxIGg2ycRdjc4zJvFGuHVoLJBbKkoW5eY7fegDnBHmWs+Oss1navZufevADuPfZkiUBoy7Mz8D/+ei1fPIz/0J6yh5ko4WrQAwLpAWUwCmBUR7AozKYl6LCyfInFlT5T/nJyvQ6fHsyveBMa5r+F1DfpxdMPUvhfaBlVSGEz4kkuCScc+hIUxmfhyetQxofqBAAQmCVQCUxlbOUIcVLOb8sK+dBqFSa0hq0hEgJMCW2LEgiRbPhK0GiKMIiyYsKg0BHCUJrTNDSvW/XoqTA2YqqKpHBvVEUBQiJ0jEyiikryzgvkVrTTFOq8ZA48jXuZeV9xDqOAEdR+nJaqRRFVRJFvgqmqiq/Ly+IhMY6gVERIkqogLKqwFZEwpG4Eu0qtPP3yWuTHqI90E5zH3y/1AvxXRFUOXkk3EEJgy28qk9TJAk4EELijKHKMlxVYcuSrNdFSkHcSGnOzpA0mzjryLOSshQ43ebosR4uXmD+tPPonHI2g0xyw9U383/e+wle+eL384HLv8HB1R5OQHN+gXh+HpmkWOsoj63gan+FFWClDwAZiTACYe68m/mfcvdJrWv8R7fa4PNR31prO/FTOImktiQkzoIx7sRWgTM+pCycREmNlhFaRijlfxZFhTFu8j3Ogq2c/5wDLR1KGJwtsbbCWENpDcMsY7M/YFSU9LOc/jgjK0uM9Zm0VeUtgjqzduJOoK4dBud8JVIU/L7OOZSOSNMGOkqwTmAdWOfzKq0DA5TGUlpBhSAzjtxAhWJcOUalY1TBuBJUQlM5MM5ROse4qhhbSykVMm2QtNoTrfLEyiUmQaofDoV3vdxxDVHN4lwSzFEbhll4RMJhscgkwlYVQvlEbXKLVjFaxL6kLxuBBhEJZCwR2t84ZxxYgSTC5BWthQV6x47ibEnUaSAiKEfraNkn723RThfRrQX6RoBKUDPzxI0ZiiOrqHAhTkqc9mAYlQqHo4gqnPrJVar8p/zkxOF9Wv8RmTajhYMoAODkfa/eTUAzThOvOeFCEA2vFQqfDF4UBdL50KJ2PoZKPQsEjE2J0CpwdTrfUsM6NBKlBYXLKe0J5hilNNWEsi2AByHNJ7g9lFIY6+u/ozj1Z+osOONL9/DN3Zy1xElClpcUxiJUjFBxyAXGm9g2J9YCIRVlZbAOhFIgJJULxxY+aFL7a4X0ubWxFOjKB9sKqRk7QRZyQzEVkSmY0RCbisiaELzxpnglPVBOeyRvT/5/T0M8uakUJ5IwvW9RQlFNBpPDoCIJosRhmNmxTNrp+EhSXlAMRhTDMaawIGJ0YwaspMwNUkdEzZhmK8LlXZTLcGXO8vIyzbSBq0LliQCcJV/zJKOTWy6mk3pDi4Mf8jD+U+75IoQIEc4T/ixHrflwm9/r1/XmpY6cBJd47cELic2Dcc4oLxjlFcO8ZFgYcgOFFeQGnIpwSuOUD+5VCEoHpYPCOkQUY6WidFAhsFLhtP+MRWBsiZAOoSQVgrF15CKijJqYtMNIpIxFQiZTcpmQocisoBISooQcidEJVsWUKEoUTsU4FVFaQX9cYoRGxS2cSsiNZFwKjEyQSQshfT6gw4OiFZLKCiokqIS4NYuRMVYlGJ2im7O0F7YTdRaIWnN05heYX1pifts2FnZsZ3n3LhZPOYXG7Ayl8J5Pv2j5AJZfxOo8TQ/39xS5EwDR+f4pIuj/hKt1dT2zRAoNSJRUOFthq5wi6zPcXMHZnO7RQ+T9LkormnOzJJ0OQukTi54THuhsRZJqYm1gvEa1dZCzds+hizHZxiZbRw8zWl9FGoOMtPcZugowk9KhSeRbWiyhhvLOW2D+U+5GuT1fISFK/cPEm25+ck6SqaWvWqmkBx2RNhFpExen2CjBJQ1MlFKqmMxBjteMMiHIpaSUikppjI5wcUIpFZmDsYNCKoyOKKWiAESk0UkMWjM2ls1xydq4ZL2AjUrTdSk9Goxkk7FsMLCavoEchY0bFETYqAVph0r715VKqGRCIWIyq9DNOWaWTiGdXcZGbUrVQLUWmVk6ZWLSOurMjBAdVjEybtCaW8KqhIKIzCpc0iGeWaIgYWQEm90BG1tbrPe6dIdDKilozM6Qzsz4xaAOclLnSNZljFPpSPcQucOA6H0BtYlca1viRCqOU7jKem1RCJwxqFghhAFRsePUZRa3NRDlBt1D19E/vA9XdEkaCt2IEUpiigKhIqxxVGVJmWWYImN+rs2F//0JvOn1L+MJj3oUP3XOuezZuZM01lAWmFHf0+RI55k2wuaBscLJuqj8nrNC/af8R0UgxO23Kqgtgenfp4GSaU1F+ooSMwFDqKTXwiopcTrCRjGVjqiUdyflQjEyNoChIwdyoBCCSilsFOHimNw6CiEwWmO0plLK/61zZNb4hHApvXYpNCZqIJpzyPYSorON2V1n0N6xl+a23bS27SKZ24ZodChkRI4iQ0PSQjZmsbpJ7rQHRelBUcQtVGMW1ZjF6SYFMUY1EUmHuD2HRXnNOZjFTp7I0pBxStRoUTpJgWJcQm4llYjpjko2+2N6wxHdfp+V9XUOHz/K8fV1+tmY0nlnkq3Tm7yC6PUl4bGjLm28p8gdBkRvbkyB4cQKmUrALB1IBdbhqpIoiVGRJEo1F5x3Bi977mN4+uMfzE/fZ4FZ3cVs7Sdbv5VqtIGgpBoOkEkDGTVBNlHpLLmJOHZ8i8pqGhIe84gLeMnznsBPn3sOw+PHEaZCKYkZ9QP4Bi1RGpyyOGWxyoSyoXvSI/lP+Y+Kd4fc1lye3k42l082m315nZ+cRkI1pSEaKdgYDulmGYOyZFiWjIwhc46RswytRTYakCQe7KSkFIJCCMqgYQ6rCuKYdHaW5twcMk0ppSSHkJoiKI0lMw4bJaQzi8xu301r6RRUexFaC5h4BpN00J1F2ks7ac4tI+IWmZGUMoakg2rOYHWDkpjcRVQyppIx6cwiLmrSyw2bw4JRJahkSqVSMiMorcMYS2W8/9A64UsUpELoGOMkpREgY58R6RRGaLIKCiNotmeYnZtnZnaWOG3gpASt0UlC0myGCqBpF8UUMN6j4DAAoh9QJ8n3L7gTqbvY3VZ81MgRPlv7ZPAOWqkjD4hFibOWqiipqpLFuTbbG/DYnz+XVz/vIv7yz/6Q33jYA9nWcJjuUcqtY8RNTdqOsdmQqtdFomk0ZnGlhkqyEAnMlqUtBTsX52g3U5SzuDwnarb8+dSpUOEfdSWNut2rvwvF4YefDXlciiqYLIRgl6JC4l0SJya/d7dL53PilBVo61AhKOaL9zVWSAQW5QwqJOfW7gxl/ed81YDyVQahhKwuIzt5INz2btV5cMKfaZ13FhJxfeTW1R65cL6EhHiNIaYSMYYYhw6+Zz+2HF4LyWWDTLbIZItcNChEShm2QjQpZEolJZUUlDIK7zWpaFKJlFIk5CpmpBMGUcogajDSKYVMsGh/7qGSyp+3nNQYOyEopSaaXybZtpv28l7md5zO8imnsWPPGWw/9SyWTz2buLVA1JzDpg1yrcikI6eiqCqK0lBaiYs7qPll5OJ2yvYsmU6olEZGMVpFOBSFjchp4tJ5RDKLdZpsNObI/ls4vP8mjh857HuUqAjdaOPiBiUK4yRSx6iojVAtjIuwTvsIsIpIF7dhkwaj3OedYiviWOC0YGQqxkgyJzC2dlFJjFEYF+FETGUdBl9pJqIIF3qsx0rSTBI2+yNGlUCoFCkjMA6Xl7iygsr4e+kD8ZPySP+EvSF9TxJZD/DbO61638ng5/EuZL3fphQHzw8nDEY5bAAcU2ZIJcm3ttCNFo3OLMUwQ8dN8rIikdCWggUluP/uRf748Y/gjS99Ds956hP5L2ftxK3vx6ztp1g7QLsVEVclDTRN3YGRoQ2ctqRwxpHEBqoMUZSoSmBHpS9DDIwfnqvS4oxnK7EBuO8uEVgUJYKKSkhyGZHLmFIqEAZFTkxOM3LgDHlZetCREmsKIgG6EjSIiMsKXVYoGVOJhJHROJ0SS0goiF1FIiWxbhLrJonT6NLhSoGUKY4UK1JU3MERUeQGLJjSRzyl0ug4RmqFxQAVWgmqoiJOmpRRjElSrNTYytGJUhKgmURIYRGmInKClARhE0rZpkoW6Bea0sbYKkCUtYxzw4gmfTnPhlhkxcyyYTqM5DyZmqNnO2y6OYZ6jjyOyCPJZinZLBpEndMZmDlUcxeivQM7vwO1+3Tynaex1lpgTc+wbiKyUpFEDZzw/mSsRFaapJIkFThjGRlB2d5O+7QLcJ09VGoBKTv0eiXrQ0eviInSncxvO4N4fpmhlmSRhVigpUA7jXAtCjFD3t7BoLODNd1iU0S4qEkSNVBGYAtJ5To05k6jtXAGw0yxcew4crjJohiwM8npuCHZ1grDXg8ZJTTa88gopdVM6W91wSaUuSbVbcajnNIa2tsWMa02q6MxW90tGsqw2DREtktuelQNyUBFMDM/adsRqRZJPEtlm2Rl3cfaYUSFUw4nBZiKlsuJqrEPuqSzFEbjCmg6zVLSZFZFyNxH4J2o3Q9eU5TOEVnjI88/Inn+rpSJyey4XYXg+0AxgPtt5Da55/V3TLUQQIWCeKWxZUk+GiOUpixKvnfN9ayPoVs5xkBe+fyuHU3Box98Bn/+7EfxNy//A57ymJ9j77YI2TtIfvQGzNqtNKo+sy4jtZ6xw5+F17YkoITPC8OFKKQLqqL1QH67F3MXi9eaDEJ4X5IRXrMzeA1ROOvz00yJkJIobaDTBjKKISTIOudbvSocUoCTCqtibJRipMbifE4alryyDErLoHCUTvjvmeRoWjAWVxmscVgnQUVYqUNE1YbNee1DxowtGKUY49gyhg0r2LSK9UqxkkuOZ4LjpWDNSnpOMRLa+72sZGAcW6VFzC5Ce5YqSjCRxkQRrtFAzS0i57eRLO+iuWM37Z2nMLNjB/M7T2Fx124Wd+9lafepOK1RSUJjdpHZ5d0s7DydxZ2ns7hjNwvLO4lbM8hmm3hukZlT9rJ46hnM7tiDSpp0h6OJj8uhkE6hLUTOa9UIx6gs2eoPWV1Z5fjBA6wdvIXNI7fSWznC1toKvf6IcQWV0lQSKoxXBpzFGodUMXE6w6iU9DOLbsygkzZl6X3rzvkR63QDqxrkRlJVFmlLYjOiaQa07ZCWy0hsAUWOzQtc5UdJVY5BWKzU5EZRyZTGzBKthSXizgwb/T6jbIwwBZHJiIohshr7uam1pzxzDlyJcpW3Miw+9UwoPDeGwbgK4wyVtZSmgqqAqmCz12erNyLPS7RQpEIhiwpVlKRSBcA7YUF6myJU84SSyHuK3Ak+xB8tIrCNyLpXSJYRhbKu6667hac953X8+Vs/zme/s8qqBaMEZTi5BeDMRcGTf3EPb3jxU3jd8y/kN3/+DM5I1thhbqU5uIWYmpnkhK9CSO8g9v2ep56ExWuzvqDmniNTiuqJdclfj5CaorIUFionKazABLDykVBHISxGgVWCwhpK5zBCklWekKMQkkwohkLQs44egp7SZDrCKAGuRJoMZUZIMwZbYKXERQ1kc5ZSxGQWCgslChu3KNN5+iIljyOGCgZxTD63QLVjL+UpZ5Pvui/5nvMpTj2fau95uL33gVNOg6UlmG1RpYKRLLFzHcr5OfppTDdSDGJF3oyRC23UfBPTFNiGwyYVpRqRuS6Z2WRUrDIYrTIa9MiGGVmWk1UFw2LMRm+d4yuHOXbkVgYbx+kdP8pobQ2R5aQ6otPpoJsNxtZRTdwUvkGFX6QqtCuI7Zi2ymnSp1Gt0zZrLLh1tosNtst15nSPrf4xRjZDKYikQFrrCxCwFBhUomk2E6p+n3Jji4UoZSFpeFATUGAxWiASBdqRFwOKvI90Gdpm6CojNh4MozJDjAbYfg81HhGZAucyRAKFFvStY6RSmN0GzTlGlaXf6yKKES1R0nQVkTGIwuGqCElM5CpiNyaxGZHLUeQI5zcpDEoGYgnhSZlRCqF9VUqUNlhcmGdupsniXIftSwvMtBqYIiMfD3G28p7aH2CIeQfJ7b1z98hdAohSKR8plhId6nhVHBOlKdmoYOmsB3Dl9cf4y9f+L5518Rt5y3s/w9e/cysb3QoDzElBA1hqCX7mrAWe+/Qn8uoX/wlPfeJ/I3YjlPK31INiKPIPKu+JKONEdQ06bYiEe4fGPUb82YVzDgNGRQmFFYxLy6h09McFo8LgZOTTGrSklBajBJWArKqoHFipGFeWHOlz16SmjBvY9ixle4Zh0mALH/1EWKTLiFyGJkdQQRRDYwbdWaSKmtiQ22ZVjGrOQXuZTHcwKsYqBXFKMrNAc3En8fxO5OxO5MwOTHMJWovQmIWkiYgSdBwRaYHSEqsTbNJm5BQZihxJbh1CayyWrc01eltr9DaO0107wubKAbprB+itHmDz2AEaUqGFTxRGCUpRMiwG9AYbDHprFP0txmvHGR0/zmBlhf7GJpW1qHYL0WpSyZpI9YR/q64HVpRUWR9XDNBmSFOWzCWG+dTSlhkRY8ZmSC58EC+WEo0A56iEo5COqJWQJBFmMKTa6DKLZi5uIJ2v/y0EFFKg0gihHEUxoCoGaJej7RhVjYhM7rcqh2yE6fdwwwGqykGU2MhSakkhFWXSwjZmGFrJsdV1qqIgEpZOJGkriCoDpcPZCIwmFo7I5Cib4/X3AuEKBNUJQMQihe+iF6UpOm0Qt9q0ZmZYWpxjrt1gppUw00xQGIb9LQbdTUyZTfzH08z0dUDrngSG3FWAKKRPnXHOoYNmaEMtZNxos3rgOKq5yI773J9oZoF//eZ3eNs7383b/u7dfOyTV3B8ULGVO7rDkmMDw2bmWNq+xCMe/Vh+5r/+11rx8z+lZ9ywgAmZ/SdCQNM/A71XaDh1d0o9LDwbSQ2D/h2HwsqI3EqImuhmh0rokHwbUQkohSV3pdcQtaISIOOEtD2DTJs+XcJJMhVjW7PopWXk0nbGrQ5bQlJFEVY4lKiIZUEkSxAGoxNccxbXWqDQDYyMQGms1JC2sa1tZLKNJEGVAl1JIqMgs4zWemwdPs7W4eOs33qIjQNH2Dx0jO7hY4xW1nDdAVFWEBUWZSMS1UaYCGUilNGI0hEZsIMxVbcH/T56NCAeD2hXGQtULEnDvIS4Amn8cxRaIlONaioabc1sJ2Z7p8FSErGUxDStoxyNKMoKGg1Eq0UVHP312ljnItZ5cirSKK0Co44vU6uMwVhL4Qy0YkwkAEeEIsJzgxYCqkiiminGVjDKUMOcODO0ZUKSpOTWkQtJIQXEGqEFRT7AVUNiUaJtRkxBTEmCIcESmRJZZOiyIMJgKchdQalAtFtEcwvI9hyljBgXnvRCOes/aw3KWqSTCBcjnEYD2pVIm6OEQVDhKHGUSOXB0FmDC/XSzvlKndJajHP0NlbZWj3C1upR+purDLvrDLsblPnIZ72FoJqfodQ8ZZPg4D3HYL6LADGQx51YIZyjzHNsVaGSBN2awaIZZ4bMRthkhl6l+cY1+7ns4//C0577Et7+gU9w00aOaynyRLDhYBRL1hxkDkxwe7hgKjshPL288R4LMclX81RbUnouQ8+Vd/dLPUQkNSj6vRbp60hFRDqzwOy2U0jac6B9wmtRGYyAwhrP9agkCEXabNGZXaDZnsPKmAJNIWJM0kTOLKDmFnEzc5SNNpmMKaXGCesngLA4KTA6wSQdMpmSEVEhfM2qqbyvM2qRmRjnYkQlUBUk1hHnGfQ2YGsV3V+jMe7SzHs0xj30sEc0HhGXJYmxaAPSKCLZQBiNEjGRjFFWEYsIl1ckQtPUEW0d0RSSJtCWkrYSNBHI0mKyiqIoyauSsc0ZVyMKM0LYDF2OaZqKOSVpS4nNC0pjqLSmUNIH1sKE9d4VX6VihKIUEVFrDtGYodQtBjZms1RsVpoeDTLdpLm4iNOSMitRRhATg1NUKsI1G4hmSn80QlSO1CqK7ghhBY25eUZIqlBhInSEkAJTFQhbkkiLchWxdGjhiLUi1ppYa5QQvgulBOMMuSlxUtCY6dDstBFaoaKY2YUFryhY5yPepfHN16RGSY1zIIXyFYW+UhYrBVXo/4LExwOs709kTUVR5IzGQ3qDPv1Bl2LUpxr1KAZd8sEW5XiAMCWxEjTT+DbUaUwsnxOj/p4kdwkg2qpCBpaQKs99xHKK2l0lMXGzhYyb5KViUGiKZAm9fC/SUx+APuX+fPSL1/GMF72Bp7/sf/POT93At9YsmzhWnWMA+M4pPqyP0h74pK8fFSHh1AOir5wRotYQ1d28RIVB4eoeGrcFRScklVNUIkK3Zonbc4ioQVUT+AoQSoTyfi/GWASKJGqiZAoywamUSsSUxBQixsYtVHueZGGZkUgYEVMKjZUCpyQlkkxGlCFFJbcCpEBIhzGFr1NXmtJKLDHCahIEHemYI2PJ9dkle5yZjjk9HnBGMubUJGdnVDKvDG0pSKWnYMMKlNA4I8AqX+HkJJGKEU4S64RYJ2gRIZ0EA6a0mNxQZiXtpE0zbhLHKVGaIBKJ0xYnSiQlZX8L09tCjzNUXlBlOZWx2DimUsK7CzCBINlhhSctKELaz0Yu2TINBmqefrzEVrSdreQU+q1TqTp7SOeXsRayfobLICIFF1OpGNHuYBsNNodDhBM045T+Zp/SCJKlZfpIKpVgXYQQUaDY9/zZsZYI50mbTUhydlphtcIoSSkcFQIRpRgrkQgSJbHZkN7KEap8wMLCHHHagKhBLiLGzlfQyChCKoe1FVZGlCKhEJG/ZhVRCEUp5KSjJlikcL4dMBZbFVTlGFOOaaeaTqJpRpJYWGJhSbUk1coH+gLLTQ1/IgQ374lm810DiEWB1NpHRYsCAJ0kSOXbgOIs1lQ+vUNGqOYiqr2dkW1xdL2iW7WJFk5n7vT7061SPvShT/Bnz389b3zPV/nSv+4nrxylL9DzARVxIqhymy1oieCZd2o+vbtfTpyDd0DXJkYwnnWMkRFGxlRoxqUhKysILTkjrVHCt2+wlaXIS8rSUFWOojBIESNEDDLyQZnSUTmJSpqks4uMiBmJhDE6TApNIRQFmlLEVFJjhV9ctAJJhRIWpYK/VvkytFJAZUuqcoQp+2g3pKEyyuFxqtEatuhiywHWFlhhKJ2lxFLgKAVk1pA7S46v7LBRTKUiRtZ60gCpyVREoRNs2sQ22shGmyKvqPKSqqowWF8WFiqSJIaZRkJba1J8y1HPaiAhihCB5srf87AI1R5E4StHrEzQzTk6y7vZtvfe7DjzXHaceT7bzzyfHWecQ9KYwVZQDkrIBMrE4HztL2kTG8cMsgyEJNIxo3FG7kDMzDCSCitX6r/rAABy0ElEQVQScL6HuUL5+gEkSnly/8JCZgUjC2OhyJUil4Kxs4ytRUUtpIvQ1qHLjHLjKFsH9zFaO0IsHa2ZWVRrljJqM5YplY4hkiByT7snYjKaZKJJphrkMiGX/pnaoFRoqYikINGSRqRoRJI0lqSRBJMjTYE0Ba7MsWWOdBXYElPkt2FNvK3ycc8CQ+4qQNSNhm8dgPcn+lw2305ARxHVeEAx6FJmY4SMUGkHdAeRLBDNnIIVbVRridJG5IVlbudOduzZyec++VE+84mP8D/+5Lm84x3v5tprDzMzM4NzDlNWmKIgajR8Oon1RA7WGKwxmKrCVhXmDtLb3xlSr5L1oPGpzScmZ2Es6IRBVvl0GqGIk5QsHwOOKs9pJqnvH4OgEacIJ9EqQqDIxwWmdCgZ46yg1eqwcWyFzbV1oqRJZ3kPI9kkj9pkqkkeTOR+ViKiBKTCEsyzIiOWkGooR12kcnTLMQPlyBLFOFGMYskoEvSVY4sSZpvkqWKkIUsk4xh6smKgDEVDUaaCHjliNiWLYSANeRqxWuXkzZQesGYtK86yriNWVcQRKziGoqdjCudIYt9udJyNqKyvPqpMhXOGYjxiYaZDOc4oswwtJWVZoGONijRm0srUk+PWxLc1GW0WGMrXV1YYDYccOXKEQ4cOcevNN3PjtdexdnSV2eYCp+86i5lkjnxoMSZCxi2Mijm2vkEJDMcjNrpbVEKQCdgyhmhuAUQClWLcyxCVoNOcxVSOjc0+ViXkIqJKWuRxix6avlCUjQa0m7g0xZgI7WLUOMNtrdEuuuxIKprVgNWDt7CwbQdl1GTLxWTpDGWS0C8G5PkGcQIimWGriBiKJnJmO1U6w8BKCqGRSYPheIwpCxIhqIZ9isEW2pWIagzlGKoCYY1P6xd1taxFWOM5UoNv/Psjzf9/CojOOa/NaI1UygNSWaKiiLn5Dj/zoPuxfblBGmV02oI0tmRbaxRrqx6whMKMc6yxqDimyDP63U2Wlhf4jcf8Gq/5y79kbXWVN77hDbz1TW/yWqgAncQUve6U1+KE9+JutpOnZGJIQG0y14Oopn0Kjct95UlNrAnU2f4T5mUPq8HxM/kpVeQ1YwsYhzBeS7JZTjYYETVnUK05ctViq5SMXIxqzKDiBkVZ3ua++TP1TM7alkhX0Gil6GYKcUwVJRRRkyxuM9BturLF/m7B4aFhrXD0nSKTykd2lQj9iuvextZvwWxHa4hjlveezvKpp7F86uls23MaS7v3Mr9rL3O7TmV+5+7AyHyi654IjM8ES0ApxTjzLSziOJ64a4wxWOODbq7m7AtaugzVPdpVRKJCVCPMaAszWMMN1pDjdUS2AVmPcjBka2WDYlgQ6yaCGGMkUqdEaYPW7CzzS4vs3L2TPXt3s23nMlGrgYkUUauFQKGdRuE7VmqhUSpGxU1k2qGKmhRRi6GI6TuBa7RoLy+TzM2QG4OpILaKpnU0ihGN8Sbtsk+zGqLKMUWeEzVnSGaXKFRML88pbYFSBdiM9swCMpmjO3YcWe+xPsiwOiFpz5A0UrLRyFN5YWlIQSIckSuJhSGSvtXBpF1B2OrI8g82ir9/zz1B7hJArIMpvoRPe0AsCoRSLM02+KMn3puXPu2/8dgHn4lcvZq1q79IU/VYOKVFmpQ0WgKpKqSoSBsRrU4TrZWnuc8z5mZb/MVLXsD7L309T3ziE3x6QKRoznR8ZDBM6Mk/EV7XieN387Op9UKfCHRCS8T5cWOFONFvN/gNCQndvs9GKImj9s/476xLpYTyfiCBQ1iDthZVVbjRiKzbJYpi0s48ojnHwET0jUakbaI4ocgypPP09jVUeIPOoV1BZAvIhlTDEdkwZzSyDKuUsVhgILbRFdvR284lWrovydLZpHN7aDSXSHQTbUEUBbGxxNYQGYM2lsQ5EgSJUN7PaByycogKXGExhQ+iVOOKMgsafmiyJMM/hUII7dtIRDGDoqASAhnHOO8OhbLCFFVgbz5RriidQ7uK2BUkdsx8YpmTI9pmk5lyjSW3zk65yS61xTY9RJdj8sEIgaTdmUXpFKlims02sY6psjHCVmgFUSwwrmBra51+v4vWCmkdorIIa5FCEEUxcdzACs2wdJi4TRW3yGRCRoSNU+J2C6cFw9EASkPiJA1nifMhSd6nZYY0bYauxmxtrCGlojO/SNRoBlLWCsyIbNSlMzPPzOwyzfY8MmoSN9vMLizR7PjGaOORD5JE1tDUgshVUIyJhUGHxmxTy/ptt9vVO8LCE/zm9yS5awARvNk6lYdkjfHVFa6kbeCCHTF/8hsP4X2XXMzrX/psHvaA0xBb+9m44Zv0D16DND0aKbh8wGhrgzLPETLCOEmiBArojhxnnX46WAPWsr7/FlyZT8DQI0yIJk6B5N0rYqLJ1QPIp99M+7TqThjTWowPBNQNhzxY1UQFnpTAbz5ZGwkuAFuEIcaiypxq0KccD3xflPklRNohs5LShhaVrkK6CuFs8KtFGKFxSJSzRK6kJSVNoUhFRCvpMNtZYn5hN4vbTmdp+UwWtp3J/NJpdGZ20mwsEusm2ilU6VCFIbGQVI64csSlJSoMcWWIS0NUGgbHVxgcX6F/9DjDYysMjh7z+44dp3vsGNLfNG/mCuE1Led75zh8TW8hFC5OyK2hKEuSOEYDVZbdRqv266PX0pUzRK7E5QPcuIcbbSLGW8RFj7Ts0yh7xOXAN4ISgjjxtP6ls+goJoliiuGQ0eY64+4aw83j9NaP0V0/xubqYcrBFrONmEj6KhFTZDhborUkShMMiv64ZFhCv4CCiKjRQce+A6IpcmLpiFxFEjZVFWhTEDtDTEXkKvrrq+TDHpGwzLWbzLUaxMJSjAYMu1uUwyGtJGHHtm2csmOZ5aVFZmfbOFOwtnIUV2QkWJQp0baCMqcY9RG28mZxWIAnuUvT2w+QeuG+p8ldAoguMPfaqsJZ6yO/UlLlOb2tTZpKkA26dHsbNE3Jw++zg+f9zq/wimdfyPP/6DfZNWNIizXytf2U/VUaacTcwhIqadEb5GSZoyOh3RCU4zHdlRXiKKKztERn+/YJsNRmH942nWiKJ/bf9eKPPr2m1tDmtxOnduIq6gkrXR0Z9QaKETWHn6CUglJCKaHAYKQFaRFURMKSCEPiSmKTM944jnIlM7NztGbncTImLyuELUm11wQlFisiCpFSiAaGGAFoa0mMJa0sSWVJKkdSBYAzEBsYr24yWt1ktLrFaL1L2R3BuCI2gqbTpEaSGkmjEjQqR1pYkqwiznLi0Zg0z2nmBZ0yp1PkzFUl81XBvCmZCQuDE0GzdqGxk1UIp7FE5E7hkgYuTenlORWWRhIjqwpZlifcDeEG1y6Ier9EIIUk0ppYR8Rao5XnxRbW4kxBFEmcdvTGPUblmCjVRMIxWD2OzIbEVYYqh6hyQOoykmpMsypYiDXzMylR7CjKIaNxD0tFo9mgPTtL2p6hQlFYSZK2mZ9fohmn5L0ejIfs6DRpkKPMEFeN/UgQEimV76mC82bz5grl5nEaZsxsJIiNwWQFFCW940dwg01SWxDbDIoB1WiLrL/BcGOF2FU0FMiqQJYFNhuTD4dga1ZvP4YnlWLTWxjTftze9j7fE+UuA0TwRJ22qhBCoOMYawwbmz3+4cNf4Jb1nPbMAiWa45tbyMrwU3sXeNSD7s3f/dUz+YMn/Srn7l2gxZBs4wjrh29h1N2g1UhYbAg2+xkmd8y0Wpx9zjmMel36qyv0V1eDZlonhk5v3nd198uJgTMNhh4C6wFX46U/74mGGNJtvFntwdADIpRKUClHQeWjrtKGzm0BFPGgWPQ3KAdbCBzNVoek2cYYiy0zmpFAuwrhHEZoKpFQCp/C41OFHEVZ+txEU5CPe4y6q/RWD9E9dgvdozdR9o9R9FfI+6vkoy2KYoxxFqc0Mk49y4rzBK8CgRT4Vg6mxJkcTUVESUJJKgoasqAlS1qqoKF8ErHFJw1jHdIIhBU4q7BBQzRRQqk0Y2uImw3iOKIYDoicQ1uvaft0p/opBMYgIpxKIWpBPINLZihUhzFNxqJFLlKiOKbRiildQTfrIxJJq9MgEhayITof0aKiQU5KxmwMHWlQgy3M1iozTU2cgLFjxlmfLB8itaQ9O8vi8nbmF5eZmV3wW3sWZRyjjQ2KrU2SKqMpClzeIx/3sc4gotiXIjqBwjGTKHTep9w4QrVxDDnuE1tDLDSpimDUh8EGxdZxBquH6R4/wGjrOLIc0YoFibQ0lECUOcJUuLKkKgoEvq3BbUfsia1e4CdyYhBPje97ltwlgIjzZVhSqUDZ70v3hJQMc8nlX7qRP371ZVz0qvfxyX+7BdeeJdEKB8wqQQv49Qfdi0ue/zu8+VV/xtOf9CjO2buIHK+xfuhGDh9ZY1snZTEVbG1ucHj/LTRbLeZ2bEdKgZsCQycCoe1EQ7z7xZ9FqLcOe2owvD3Dwg+mE85rF3yFtalcSaiU1w5rDdEqh0+5rHBVjjAF2lZEQSsYd1cZdrfQKqLd7hDpCGE9cHpNFAzxhHqrEkkI9EhsM4VOAzUTE7cFUTImUhukcoWmXGEm3qAdbdJIBuhojI0q8giGkWIQKQYKH4GOJGWqKRNJGUMVQ6UNmRuTuSGZ7VO4PoXpUdgupelSmB5WlFh8JoFvEwoisL5aFEZqCgQja5BpyvziElIIemvrtLRCh/7HvmVnCGQJTSViCtlgK5N0y5Su7bBuZlgxs6ywyLrcziheojm/SNqMGJVDMjMm6SREqaLKBugqJyozGiZD5X0Yd0ltRssUiO4Go5UjFOMtLGNkZDEuZ5QN6A97jPMxZVURxQmRijBFxajbI+t2EdkYnY0R4z6pyCmzTYbDLqWzVDr2BBrGD6iYkhlV0ir7iN4KYtijIRTNqIWsoC0NSdVHZpuovIsqB6hySExBOxbEVCTSIU3lq1qUJ0xRynfbO5EXcWIDT1oR9MKprZZ6fN/+GL+75E4CxJOBxV+8wPdVwQqUjlFR7N+zvr5ZaY0TGpFuQ8/v5cDamLe+5x957kvfxDs+9M/ctH8VLSAVloaoiGzFzgY84RE/w6ue/0z+8Hd+k/udvYczTlkC4Hiv4vQzdhAnCVVZsnngEI3ZuQl7N4Gn7/tWrrtZfITVYKXxEVfwQRanQxsGnzzsRImTBU74JvTCaoSNPb8gDiEqnKioJBihcEQIp1HWoZ23ASvhyHCUzqBsQWTGNHVJMdxguLWOMIZmo4NOmlRSkwfz24jgaQt9hYXzQ9lI6OYj+uWYwpRYV2FdgXE5iBytclTVR1d9lBkiXIYRBQUlhTPktqKkwlB5ujNp0RpkBC7Cl8SlGpFE6NhzB0YqRskIdAw6IjS9wzeL8J3iKuF7A+dCUMYRAwdDB7LRZGZ+HoxhvLFOW0oiU3fdqxCUIKpAeOzN5tlGi7lWh5mZBdrz22kt7aazfCqLp5zGrt2nMdtIiCnIRpvkVR8dGcqqz6i3CsWAdiRoRAKoKKsRUlQ0YmhhiIox3fWjmHxIW1haGFzepdc9ytr6YdZXjzI4tkZ+fIPRoSN0D+4n76/STgRznRapjlDOUuYjxkWPigwhDTiDqgyxsbhsRKqhkSgwFa6yNKKEWArMaEBiCnSVobE0Y818pJlxlmScYftDrAvd9RBIpUikRAuB0wmFjHyuZsh+gNCb2oJ2NnBw1j2nTyz0EyXgHjYX7wRADFpXSCQWiAkBKc77cZRKPS+h9cwt5WiMKSvPqxf8ighQrYjZXTv57jW38LFP/yvPft5r+MZ3DlNVlhaSljTMSIceOuYVPPphD+LRD/0ZSgrGrmJxRrPVHYOUFEVJMrvAeGuIIEa6aKpJva+E8NvJ13PXim+9UGJUQaVKKuUBzaLBpgiboGVEng0gyshdFxEZjHW00+1oM8NcYx47HtPUhjSyVM5hXEyVxyS2zTwJcZZjK4NoNWF2nkJrhM2Y0SURQ2ZbksHKMcabPeY6S6jGPH2RsEbEppCMcdhsTMtWxPkYmY+I0piRKdCNCCSI0iKMBJliZINCen5FCkGTmLZOSKRACotWEONQVYEiI1Wlj5COR+jxCDseUWEY2opc+iTxqohweQJ5E1t2KMwMY9siac0xKioKJKLZZKvMaWxbwLYTxrGkrwRqcYG5PaexvPdMRqOczWPHWYwVotujQYI2yi8QKkPoAscY7Qo6rmDJjqmO3Mypcw1mE0mSeGIKN9xgfHw//f376N16I2SbNOOSbLzC2vGbyIerNGJHpA3jcoRJJWquxcBmjPIhibDoIqMa9ekoaI/GxJvrtN2AVA1w9OgkMDOu2JEJdlvLMjkpXQxd8qpgmFusjVhYWIK4IJ0x2GqTlstYAJqFD7Q4YRkhyHSKiGeoKoHN+rR0SUM6Eqm9S6QSNArHYilZKiXNUuKiDutGUaZNZJJAnkFRMhYRZWOGvoGxAWO9qyJB0JSCJpDYitj6vsySKvQ2cgEIa43yngOKdwIg4jVEEX5OMN9riNTVIQ6EUCTNFlGjgSkLqiJHxQpcQaMVYW3Fjf/+LX776X/I05/5HPIK3vTWSylK52mUEPQ3N9nWEcwlgsVmzOa4jyZmbAyrY0dWWVScML+8g/bCElGjFdqKhu5/QbsJRat3/wo1FYzzZrzXDp1T4DQ4jUCiIuW1Ju0ppfp5QT8TFDZlszumKi3GVORFDlLSaM2QRC1cZomNQxtfDVRWhtyBEdKv2qYAU6AxpEpQjcYM+0OStEVrcRuZBRdFiMi3mdXOkOA1TiFAaElZZriq8Jpo4Jp0+M5teVEhnURbAqnACXNfOePjwFJ5J2IIvjkHQkUQxahmC5ukmKSJTVvQnEV05pFzi4j5bUTzS1iEr6KINWkc0WwktNtNFhbm2LZtkcWlRZqtFsYahoM+w16PajRElzmxrVC2bojmG5DVmrrAEjnD4Phh+kf2s3HwFlYP3sLxQ7ewemQ/veO3Ml49iM4H6HKErsbENqfhCtoYZp1jzkGjMCSV5wSXwnODKi3QwqGERSqBlpIGkqazpFTE5EiXIUxOE+mDTWVFaku0KhHK+2CRCXkpyEpLZkuMKBEyNL1HkkiNDP5lo2JM1MCoBqiYKNKkjZTMQd8KumjGqkElYpxVRESkcYteZhhYyB0gHO1Y0tKAKcmKHJU2UI0mMk581z7jsKXPTNBCBHwIOabCniDPuB3b8u6WOwkQ61ntZ7a/yPpSHSpSiEhjq4JiOMDaalIloJSlGK8y3DhM4gSv+ds38IiH34cH/NQsZ97nVIZZj//19r+nN4Cs1GxbWuRDH/8UH7r80/QyS6fRYctBpRKaDcH8zhZF5ej1BvTWVim6W2FFclOBlRPa+t0e/XcSYTXSaqRVKKtQTiKn7mdpKk/jLiTECardwaQtxnGKac1gGx10ex6jm+RGIGREkqTgHEU+9qWoziFC2Zq0FlkHDcLIFNaRRDGmLOhurJFGisWFOYStkM5n6FlrMKbyd9JZTFWicFB51pU6L80nM3tiAkyFwh8fW1csePZv5SwSQRS3saqFTWYo4jZ53KJKWhQywcVNtp1+JgtnnE7rzL3oM06B03dgT92G3b2I3j6LoySxJWmVI4ZdyvUVirUVqo11XLeL6PepNjcYrx5nvHYcM9hCm4JEOiLta4ImPthA6mACP6JD0Go2aMSaVDlSUdGSFR1ZMZ84FpuaWBli5YidIa0MjbyiNSxpdwvamwWzA8vcGGZyaBQ+vUgai3WGwlQYqTAqQugIKRXaQex8BB9jfBdfLJmzvlmV1FQojNNYEUPU8MxEqi7x9LXIWSA+cTqhcoqxcWQGRsbRLyoGuaFvBMPmLOtJh814hm5jlq24w4pTrFaOARLZaJCkKQILVU5MQZOMZkg7anea6GYT4hijte8pAxTOUSnPkl3VqWChkZeVfrvb599JcicAYiAmOGmX9xP4f7Yq0XEEWKrRAFdVaK1wVcm4u8625SYxI6p+j7aCHS1fmPynz3sGWTnm69/4Lv/8f38NIslGZvm1xzyKf77is/zhHz2Tz37hWxRA1zgOZ47VvmeCjhpNomaLeG4WMDjvaQq+uBqs7/6n4f2sCuEilNVIK5HW+2EEPkdBhuTyLM8orSNpzxLNzDNUEVtW4NI2pUrIjMQQEUcpwlpMMfYaiHBEShADCRAjiKVCyghjfRKzzUsSpdACht1NTDZkJoloKEnsLJEQyLCgKC18hN4UJEqQCEglxMIiywxpchJpSbUgks7nCVqLC/1SBD5BXDpDLATkhmpc+na1UuOcwJSW8UaPfLPH+q2HOHbrrRw4tJ+bDt3C9Qf3cd2tN3DDLddy403XIqqcyBSofIjtbpCtHCU7foRs5TjjlRXGKysUG+vY3hYyG5JQ0YwEsRY4W/kULAgMNzIQxmqqAIwOgbPGM067ktjmJDYjtRkNURIJgxaOSDgiHKkLaURW07SapFKkNiJ1Edp6N40QnrG9AnIkpfTHs06gLCROkjqBcpbKGUa2ZOQMhVJUUlMYSWkURjboVYrNQrKZC/qlYmAjekbTtZq+1bh0hjEJA6MpohaiswidRWxzDtOYQS0uwcICLCzC/BJlZ45BlLLloGsM8zt2Mre0jIgShllOlhfYqiSyJe1Y0ZmZQcSJB0IHVmusCg23nG/YVdVgKOtcWXxP5nDv7ymiXvbyl7+8rkFQ+CRLEL5cTvjVs0ZNh6VCMrCCj/7zl8lVBysT34RahKBFneBKUJVxmCIHZ5BaIiToWGPKHGdLTtmzzOMf9VDOOX0XX/3sF9g8vsF973sesy3Btk6EQ7LvugNcf/0tPO6xv8SwhJWt4/yXn/lp/v0b1/DRj3+Gz3z5G6yMYH7X6ci24ItfuZ5sXFEVFa1t2yjGo9t2BqyvRwh/vqH0y7vkXQDKwIbjLJGocMMN1Hid1GYkGIS1WDyjjtc8/9+KAJSnaw+aoaqrToLmQhrTNyWZlOhGCx03cEYxHpQMhmMMglE2xLmSpD1LPLMdoZqYrEQVQxrlBrEoEXGM0wkz8zvIe330sEvsDFIJyrwgjRqBfCDHCUfS8GSfsipwoyGJNSRSEGlJlER+YBdDYjumqQRYSyQlJs+pxmMaWqCsL/mqk7s9EAi821ggiMhGFpflqKzrGZtNCUWFHVUUW0MS40iqgqQakZD79BvpaErFLI5WPkQbi0OhBMQCUiwdBZ0o8lUvShPLiFgIrxmKElmNqKrctxidTNSgIUqNERFWRFQVWOMrSTzFfo40Y1RgFjfW+Ni0dSh8YrhWMVGUEMUpeWVwUnsNCd/ZDyURWmKiiC1nUXELXWkoKyJZBV5CixYR2JTMQB4JXCumigVVBbpqouMZmNtGmbah2abRWaDdXiRpzZN2lkgWdjCQCUMbkZuIpLPI/PIudGcB22oTz85RSkelwTYSolYbHaeBaFlipGBmeQkZReRZQTbM0Xi6MKsganVIF5YZZDmj4QicJVYKpQTGGipnAkepH8s+NxFfOeV/mYz1HyZS+t4ucRz7VB/nUEpx4YUX3qkELXcKIJ5gk/HlZ36onxAdx5TZ0D/gSCOEpRwNWd69iyf/91/hQWfNcOqOZfbvO0RvfQ1TDrjg/mcxzkr+6/3uw/euO8qhwyt84avf4nGPfig2Ttg1v8gth1aRSZshmn0Hj/OFr17J1//9VrpbQxb2nI5xUORZCPhMpd4QHgg1tVEN3ncHIMoAiP4uS+c758lAA2aFI8eSYymk74HSbHQ8tRcaJSOkgCgSdFoxswuLRK0F8sww7vWReZ+W2SKmRGjfoa3Rmme81UWP+zQUCCymKEhVTKQj8iLH2BKlQNqSYtDDjX0KiXIVzlUYW1FWOTYfEZmMVAlfPWQtVVFgioxESTAlkQRsKPEKrEP1sqSkphiXyCojESMSWZBK34cvKiEpLW0kqS1I7IjEjolNTmJK0sLQKEtS53uLIHznPu0csfDasAa0UCihcBaqqsBWBZISJSs/GpQOqTbeZK6k8qYnMVZEaJ2iVESiI9JIEWtQVGhpibTytFwIjHGUlaMI/rYMyQjBwFoKJckkjKwhszaAhCQTgpGOiNMOcaVxeUlMSaSc78YoIoxrkKFx7ZRoto2LFUXhSFyLRmuRmd27MUmMjL2rJNVxoLzTECWMnGCYVxQVpI02jVaHUVmwORpSVDnj4Tp5OaBwBgdEOvJpV85RWkNmjK9rLwR54VC6SSm8BpjMzGHjJr3BiHw0QgOxVj5v1fo7gxQ4GUh4JwhRzzI/Gn4UpN1VgHjnmMzUYFhLXRLnN6kFKo6wpqQcD3HWILVCOLBF0NmynGc8/bcZbh3hG1/5HNdffSPLacQtqwN+9dcfza4zTud7N9zIJ754FbGO6AJP/4Mn0x+NMDKBuEXpIrpbA1qLyzgpqcrCM/wG7dBzBtbnNX3+d5+4kDJihX8lQrBB4lNAJCX5cIt2JJlRAtvrk29sIoqCdrPF0rZttGdnWN6+g7m5RTCOot+jGmxiRutQ9JA2R7sCXWbQ38JsrmL6m75gX0q0tbS0j7JSDEjJSc0YequUa0dx3TVUMUSaDGEzXDWiHG9hBhvoauwjxaZCmwpR5IgiQ9kS5Yw3SQM1m3G+oga8NimdJRYl7SgjFZskukcktojpMiNHLNgxu5XjFAw7rWE7liVjWCxLFvOSpVFOZ1wSSW+iORzYCirf/rLMK8bDnGxcUpUmkJyWGFtghKNUChPryYJpBRgpQ6lfRCljT3+G533MKkdufCO0UeEYlZAZidYpQvoG9mOt6cYRG42E9U6D9bkWw53bGGxfpD8/S6/ZpK8TRiLypL1WohodRKONi5tYqT1TkRVEzlO6lVjKSGDTBNlqQZRiQxWOilJGoyGD3ibd1RU2jx1h5egBjhy8hSOH/GazPhQjpM0RriDLe6xvHGPj+BH6a0egdwzdP47bOk62doxy0PO8h0pQlCVbWwPfFbGxSN5Ypt9cppduY5RsQ7V30O1nFOMMYUFJBRaqyuAsaO2bmPm65doVVFcC+RLV20zHu1nuBED04mFlClwmecaOrNclShPiZhNTVSgdoaOYA9dcw7ve9k7yMSzNppy2p8kTnvCr7LvuKi7/p3/iSDfjlG1tzjl/if/68w9iccd23vS/3s5VNxyjCZTG8YIXPp+19XUsikZnhirwBA42NnB5TjLbOaEd1nVDk9O8e8EQAOFCk3TP3yeEQQifoiBEiaRA2Zz5VLOgFaK7Rf/QQXqHD5NtbeDKDFxJnHjSjLVjx9k4cpiyv0FMRkNXSJcjXYkqRrhBF9tbh1EXYQqUFCj7/7T351G35eldH/b5TXs48zvc905Vqp4bTTQoGEsgWSbG2FhJDHhQsiCGZIXYSUjssCDLsVeISIwxxCteyEFCBi3s4IW8BCw7qCNiREtIbk20Wj2hVrfUqq6uunXrTu94ztnTb8ofv73Pu+9b1V0td6tuqXmfu/Z999l7n3329PvuZ/g+zxOYGIWMqX9IRstMOVR9jj15wCR0zGRgmkmKTJCbiKYjp2OmBSpGtFBoIfsSCQHdF1uAvgtg3106oHprImWjKBxF7hByi+eCxp5StadYtyG4CoWjXp9TbzbUVU3bWWyIxChQwqBUhpU51uQEpRFap2etmGDKGTKb4WWGzEryckIxKTBF6kNTC8HW9Wl/Pf8zkADRCbObRDYBlRNk+mxFhpUFVk3xeoYlwwlDMAVM5ui9AyZ37jB7xwss3/0u5u98B9Ove4Hyzl30wRFisYLJgpjNCHqCKmboYgpZTlTJTTS4T2Ik1YgU4E0qVCGEIgRBiMlCaesL/PYUqlNUc4FxWwrRMNGWRe4J68dk3Tkz1bEsAqWx5LJllnkOS8lSeVYqMLEOWTXQdJj+XoauQ7iAUTkim2PNnK2asVVTXLagmB+w3WzxnUUBBgE+4DuLiGCURkYxmhJHMRUkSamWb4NRuJOvrsmcbNHRGSYAGrTD6H0q/+UswXtMUYDrCFXF17/3BTIl+Obf8m5e+sIX+JkP/xzL5Q3e9w3vQQp473tu8Y9/5WU26y2f/tRn+ad/1++izATZRPHeD3wrP/zX/wa33v1eos64uP+AydFNstWK5vzsDa540lJ23ouBGvAMTOYgwKlIlCkjJGmHfY6y8EQRMJnGdR2xtRRCU0qD8YHQtrTVmmpzyub0Mb66SKRZlfpkGOEppSX3W3LhE3BJhW0dsyInN9DVGwyR6B0Q0FpgNClaHCyFTGazCI6sp9gQLWWuEa5LVVyEZrutkVJhTIbveaU+RjrnkSbDB3AhPU1Ka3JjUAKs76hCg8gjTlQIE8mmJTYoKqvpREEsZ9iypC0Vba5pswyXTejUhErlbITAm5zaemSWY4opL7/6kGy25KzuaDzUztO4FicCQaXK0zHLyPKCUG/RCJyU1B7aqJD5lMl8n8XygNVyn+lswWKxZLZc9aR1k7JBoiQKTVSG2gvyxYr923epPdx/9ITzbc29V1+jtRZrA+VkxnK5z8W64vR8Q7FcUcWA0jl75Yrt6Sl0WxbTnE21TqZ7ueLxtuHOu99N5y3Hjx4yzSaERjKdlpi8ZXPyCivtWWlP7rcUdIT2nIwOE1qmGqKtiHbD/rJgffIaE+nIQ0NmawyaPN+jriEzUw73D3DVhnp9QWlyTk7XmOk+e7ef594r9wgCfus3fT0PXvk8rt6Qi0AhBDoEtE/BMi0E0bnd8NtphgxE+N6u/DJM3rfKZP7qAGJvMu+QR6Stx1fiUilLc6LnJyrgH3/kI9w6OuL977iJJ/LOd7yLz/3Ki7z4ay9z4+YL3L17wCZEvuEbfxu/8plf4/GrTzh+cMzv+bZvxAsBmSGUKz7xix9julwRTJ6qJ/s++VxewlyKiA++zt6H+AwBMUpwfRUBHVP2bOrTnCo/pxJl6UEyEYwH0/ehTux/j5Q+5ft6m5YjEkUidkRfkcU2peD1HEGBQklBxBOjR/d9Zga6ppIRFUPK8Q0eLSVSCKIi9XiWIImokLihykyIMkObbJeSqU2GLkpUVuCFRhcTiums78fd0bYNMTiUVjgiUgHRQgStcmzI6JgRiz0e14HzCOcxsAYqqWlkTi1KamGohMBJRdU0hCjIyikdkqPn34WeLjm8c5fV4QHL/SXLvTnTxYxyMSebzpmUE+rTE8osp42SLiqK5QHT5QFIQwhwdnJKtdnStS3OJf5fUIbKCzatJYrU8CsrpmT5BO8j1brCNTaZvS6QSwOdR0ZJZgp8lFif2jA4AUpoZvkcW20J7YbcCFz0OGXYihKflSwODqirCldtyYTGtTBbzAm64+L8mJyIiSFRmiQgIkVR4jqHVoYoBLoomO2tOD07w3ctsbPgIlUd8GKJypYU0xVaKerzx0jbUEqVWnbLHFVOsNFTlob9UnL22svo0KGDx4SADukY9NAKg8RXHTCgH3WXxTP6kRffBNR+UwFiatw0sr53tJZ0xsnB20eeh+CL6JtgC8VsMuOTn/w4f/C7vp0OwY3Vgu3W8tMf/jkePX7I7/j2b0UoSVGAbRSf+MgnCF3NzZsL7ty6SV4I3v/1z/PjP/HzuBAJQmImU9r1BlWW6ZB2WD0A92h6loAoIl4lOpAOIQVU+uwf3/ePT0ctyLxIYBgSGCYT2yFkQAmP8QFNahqklCAID9GmdpIkoi4BhNQgwYmIjyClSaUiJKAEQoTUwMinYglESZSSNkJQkigFwTtMCBAVbUwmowuR1jpa5+h8IsnXLlI7sFHSebAucRmJAaNEct57jY4G7SLaa7SYYP2EVu7B/BbF0dchV4eo5R7F/iH53g2y1RFqeQuzOkDmBUU5oes6OudRWUEbBKvbz9Oi2LQdre3oui1Ns0k+t7pm3XTU2xrVWcqipHJgVcbi8Ba6mFBVDV3TUF9cEGxH1zR0XYfQBlVMUnsDm3pgK52xXOxRqIzm+Bx/umFqI2XnmXnBxEOsW2LrycuSrJxgEVRti4jpZbWYrSBG6vVxugeZpkGyjjnl6pDJbMbF6THGWQySpnbMDw5wZclp1aESZZ4mCJzMqNEEPaGzhqBmVGR02QSzd4OH5xU+ZkRysnLFpjWo8g754jZmuqDrGprzB8x0oPAO4T21jyijmS5KFoUg3z6mevASuYp9qbT0UtcxEfAlqZ/zznk2gGIPhqovHD1En7+UvFWAOEKx/36yO5QBA3czA7AkiImRPpaUCncKlSFUDqrE6wWxPOB7vv9vcuEiVYj8z/61f4lv+Ib3U20v+K//zt8iV4I9KfiD/+Jv57f/tm9iuz7jr/2V70e6hiUwcfDv/Jt/jCcvfoZZaeg255T7K6JPcN8r6mm+d/A+67S9QQbXphxyhGPyuVmhsFLh+sisjGBCIAsOEy2KDkkHvk4PZO+Qx6eyYUprRJbRCE2DwiKRUqcKOcHhiDhtqNG9pqWpgAZBFyMRgeyvm4+KyoHTOU7n1C7io8IFybaLBDMh6BKLBlMi8wlBF0QzQZZzqqA4rjrOW99XY56jsgxnAyrm5L6g6HIKW2C6HOEzUFPUdA8xXRHzGcjUvImgsZbUp9pBvjhkuneTYr4iKkMbAm2AjYucVC0PT895dHrGyekpF6cnVBfnNOs19bqi2rYoM6H1EhsVeTknL6ZUVc3Z8RO6as2sUMxygYkN7faUentGxGOKnHw6Sw2/pMFkJTpK/HpL1jTsE5nVNStnmXcdZddBtcVuK4zSmLLEhYh0gdB02BAwsymdMaxDwGdF6msTBbP5imgDdlOTxYgKlhBbgpacW0UVJ1RxytZP2MYJWzljK6esYwHlAV4vaJhSMaVSc8LkkHzvOaZH76Q8+jomR19HuX8XVe7RRUVjLcG35KIj69YshEW12zSuMkUmI/XxIzLXYqJFR4uKtg8GpipMQwiNK6iQHvpRstjbSL5iQEyS3gKXZIoBDOnPnJ1miFAgTSrqIDOiygnFnI01/MOf/Bi/+tIJUgrOm8if+/P/Huv1Y372wx/mv/3RH+P+aYcF/tgf/df5+vd/HaePH/FjH/wRcu9Y4fnd33DIH/oDv5/T+y+lIIO1CKV6/Sr9HQxKIqOsidGpvMVy+bYUfYaKAjReZFiZ0fWT64uyJpM3UTKicERhCb5DSVL1kSCxNmAdWASt0DQqp1YlncjxvTboACslLstp9YRKTdjIjDWabd/Y3qLxZERZJE1DFojJHpQLmpgRZIkXBTGbUa5uMju4xWTvBvs373J49wX2bz/P4d0XWN56nuXNu8wOb5Et9iAvU/w8QkAgSb2Yc2eYOE1mJbIDKTU6zzm/uOD8+Jjtgwd0rz3AvvoazSv32LzyBc7vv8r6bEPnBFJlSJW0dpWlwg82Jv04RFAikkuYSJhrxcJkzPMJyIJ1E/GyYLo8QEjD+vycZntBoQIFDTPZMNcdJRWxPcPVp0S3RckASiF0BpCYDa5lYmBmAnmoKEJFQc1EOXRs6eoLvGvRKnXYyzxI62jbjphnMC2ppKTTqT2s0AVFMSG2HtFashgQrgXR4VRATleYxW2m+88zP3yB5c13snf3PSzvvJvl7Xfh9QKvF4RsScxX+HKPONmH8gAx3cOVE/LDA8xyReU8p+sLGlcjZYfyW1R3zoSWQkR82xJcwLaW6mLNvCgSx1TYPgho+xqdyf8d++h9X+OFIBMPM2WsJO7n2ylb5asDiHH4b5h6EYOO3GtjQhJjMs6JKR4ZMTRtYH7nXRw8/0183w/8EJbkbHeh5bv/9X+Zi9NzfuHnP4pCsABu3cz4H/6eb+VP/8n/I3/wX/ou5sJxt9TILvK//5//Pm4d7RNdh2uanXaYQLEH5YE4/gyBcCwyClSQKac2aGJMHDgrcjqZ9/QPg0eluodAEAGvPF56RF8tOiIJQeKCxKPooqIKklaV1CKnFVkPcgIXI1ZKvCmQsz1iucLlM1pd0kiTGhuJDC8zop5ANsPpEjXfQ832saoAMyGokmimZLM9RDalDZLGCyobOK9azqqOrQ3IyZzV0R2m+zdwUrNpWmwI6LKgVdDqRPRFQsTjoqWTjpBHnGwIYYvq1ky7DXt2w77dsPRbprFlfXKKdxGjDEpqIDUzQ+nkQol9TU5vU0Vp16G7Ft1Z6Dw+atZtRGRTismCprW0dcMkU8wMuPVjRH3CJG5ZGEsRa1x1Qrs5xrYblJAURYkPgU29IUiHyiNBdcSsw8otTmyRmUNlAetrrE3FXHWELERMgPV2Q0fEzBc4Y2hIVWbyyQyBxlYtxkfyGMA1SOWJdBSFpiwyZplhogRGxD5tssU1W7rtOXR1qniNR0SLbbdsz084PX7E6fqElpYgHdtmzXp7Qmc3SNEgQpXI8sFitMJ7aJyg9Zq2lRgz6V/QLjUCFpfBwNjzaIfpEggvp3CliOyzlq8KIL7ebB7LyHcYB7+cTIAYJDFKhDKcvfqYEOYgl/zZ//D7OZzmOL/lf/o/+p/wnd/xz/Lxj3yCv/af/SAP146T4wf8M9/2LXzb7/ydrMoJUwXCXnCUJ5rC/+aP/y9pLk6ZrpbEvpBl78od+RAv/RnPUkQUqCDQQaKCQgYN0RBIgGSHSabS/SmLIgVirAw4FVBGAwLbm7F5MWO62CefryCfYk3qpNeJBKwWSRcFXmlCVjI5uE22dxMx3cMXM6wusSpPICwLOgxOFrQig3yOyOfYAbCjIsgMdEHtIk/O1jw8OeO1Jye89uSEB8cn3HvwiNN1RRtTD2FU1lNxBE4LzgvPaWlZ55Yqt2xNw0Zv2ZgNTb6l02t8PMXYY+buhH17wqE9Zt89YckGZTtylTPJC3SfKhq8SxzUGNFaY7SkkIJCRMoYKIKnCAETBSFqvMjJyyUBzdnZOUTPclpAu6GILcauMfaCmagpRYVoTwn1CdJVqBiYlBOiiKybNY22VIXjVG/ZTjrOsopjecE2q/Glx0tLCF0qbhECOZArRbXdUNuObDEjZDltgCAN5WRO8BFbN2SkSC6uQ5tI9Fva01dwT16ke/BZqld+ic0XPsnFS59g3f+dNY+ZNY+Ytg8pmgfk9QPU5j7q4h7h/BXW5y+zrR8R4gZtOoRqgQoRazLtmOaa6D0RjRcFTs4IZo9OLGhjmbKphE/ZYFzyfoOMKWd55A9PNTt7LXHnI3/7yFcFEF+PKW9wliMtMkZP7DMeYrSIUkMmUVnB6cma1x6v+eEf+ylW+T7nrubf+O7v5u7Bkqxbc2uu+fpbtykJdNUW7yPraos2Ja+dnaMF7B8Zfse3fzvnDx+iixRUuTzKy6N9O7yZEignDTqOSNqRtGzgbomYHiAnI743MyQRHSK5TM3e6yipTQHLA8qj20z2jsimewRR4imwMWmINtLXDDQENSFb3ELNbxJm+9hiRpflOKURQqOiItiA95E2BDql6JSijRHnA84FgtREk+GQ2BCQAkqtmBvNwbQAW9E1a6p2gxUeMynJyxnRazbndR+gClgNrZHUWtJIkVqfhkAhI5mwKGWRyqG0R0iPIUUzQ3QIEcmQKA/YgIgCrTN0XuB8oHOROkAVoIqRNoB3gdgFTNBkZGQmx3nH+cUpEUdhBM36lP15QalBhA5ChwwO6Tqy4JiIREsqcwXKU/uaDovF0oWWKAMBh/UtFkfUEaFjKoFGSIUwcBjhcU2VeLrFDG+mVKQXoSkygq/xdotWPV8ypMi08p7cbSn9mpmvWISGA+G5nWvuzgqeW07YLwSLLJCLlizW5L5mGjv2FBxmGlFXxG6LNi2TOUxmCiUjoJB6iizmtBFc6BCiI9OeskhFKxubgm5DndEokhskaX49A3gIegzEk13hkj6+cGVMPEv5igEx0o9WUumqnc8wDsGLfjuXfCsRS4wN3q3xfo1QNe3ZPaYHOc6fk80nlIu7/Dcf/DkebCIyFjy3v+CHfuA/5nv+D/8Gs+aYKTUZHXvzKUEJzjA8joaqWPAjP/MZvvcH/g6/+JGPJa1pu02aAiHRTIZq2X01sCDHnKBnI6KPKlsVsNrjpUPSUfiOqeuYuI4sWKKwOJn6owgChYtMLMhNQ6ZyLkTGI5GxXR2wna7YBoPSc2ytkXGKyWfIrEAYA9qAKnFiwulWcmFzxOoWcv8GZ85TOUehNartyFzfqS/XtMJRiw5ygRKBTElchI6UUTGZFBQiYOoLDmNDuX7MvmyYqorjky9QuQsm+wu80NBlzP2MvVoxryE4SSNyOrNA6BUTN2WyUcy2MHWafDqlnZU8VJrTbEql9zjZwmR/hRcWu63ILExEgXCKpvFYJ+isoMWw1iVnxYyL+Yp2sUfIZ+AkEys4NAWia2jbDdNVQVYKbLemNILt+Vnq+6wNtYvovCQ3JaqNKc8aS709BuWYLAukThHUeTSUdWSfkj01ZaJyZBSEkAqN5CJS6pjGgltjQouvW7LiADO/y+OtwBdzFqsJx49fJMQzzCTijUYWS4LN0N4wV5LcNbjzM+ZI9kSOWltMDTSBEAVWRNqYuL/ttmUqJ5Qhp7AZt4p96senPHn0MtOlopgoNtsa6+esuwX3NpIwXWLjOUY8Zs5D1PYemenwwSKjQoZUjDjGVCzW9x0iIRGyVRCYIDA+/VUhLU8+8bePfMWAOMjTKN+bpGP4l4lMJ+jpIjIghIfYsH+jxK/vU2Ydxy//Csev3eeVz32B/+df+MscZIKphiw2rErFXlkmj0U0eJPx2sayMXN+7vP3+b6/+V/z//rB/5KPf+STqHLG4vbdHe0m9VW5nOIOwN8eEkkZK14Gokzmh44eExwmJMI2fQ+YKJJuq0PiJeZC413ASgPTOTbP2XhPFyJZVqJljhAa52PqZSISlzBGiEEAkmpbU9UdMp+STVfYoGhtROusz69Ox5DqUafMn55lNlxNokiEchMdhbdMXMvUNUxCg/E10dcELIiYot1Ro72icJD7mMCC1H86YpDBoL0m1IHtRcXxestp5zgJgmOvqdWCbHmT5Y1DfOzYVhdYZ3tNGkSZMdlf8sL73scL73kvz73z3Rx93TtY3L3L9PYt5jePWN44xAYPAmSmiApa24KATBpiF8hlTlNZWieIpqQOgtpHbJQ4n15OREcQHqEV0hgQmugloRPYOqBFQYwa68FkOXmeI0IkOkvWV9QutQBrcV1EqhJTLhA6o63XeJuqjQff0jmP9YAXiNDHdZXCCkkbBI2Fpou0NlLbQB0CVfS0IhCNQiiJ9x7XWbAe3UUmSHy7pavPmGSS5WyOllOcy4hqCtqQm0gpawp7Tu7XFCr0ecuid4Mlt9Tlc5G0wCFweHUSb7MsFb6agPilZKhwsiNlC5E67wkBwSLqJ7z6qZ/mf/Dem/z57/mT/OBf+lN86IN/mT/0L3w7v/LZz6deF0pj8gUNJQ9qwUMrOQGOheYHfvj/x3/0/X+Dv//ffYzZzXdx55v+Kbomsrn/KEX/djylVIQUQqq8EoeiD89WemV6R0NI75DRK6ZXs9O79MokBFJrGmtBwGwxQyuoNufYtqIwikmZIQl41+FdC3GgRjhEaMhFQ3v6gO70hInMWUwPEWpOFTMaleGFQOLJfcfEWXIXUEH2kXADIiCxyL4qcgLPgAlpyiIo7xHWIr1DiYhWAqVIDniZHPGy57CpkIaSF5JOKaY3jij2bjBdHTHfu8Py4Dmmh8+TH30dk5t30ZmhbjZcNBe0macpA9vc0RaBkMO2OqfZXNCcn7E5fcLpyWMenT3m/vljXt0eszaRM+nockPIMloX8Z3AUKJcRqmW+NYQY6IRbVFUWYZdzNgoiZMSH8F68KnsAx0GKwtCPsPKgpjPqL1k2wVkViag61zSoBBkRpMpiW8b2s0aI2BWFhRG0Ww3RNv1JUAi0dvUX1tAkJKNzrnIZ6zLGef5hPOs4CIv2BQ5VZGxNZKtjLQaYqEIuaCVLQ01QaZGXhOtoKlpzs6YSMnBdEYhBMKlakU6BvIImQ+EqoKmJouBXCbWw9eKvCWAOHTde2q+B6noOi7uv8Rf+ovfw//uj/4L/FNff8BhmcqPf9N738EH3v9OYgxMyyVbBK9VNV2Wc+rg7/zYP+ZP/9nv47/5Bz/Pa+cRs/c8PlvReoOZ7aMX++AHIOm1xBgS1aYHx4Q1z+6GPq23Pj3t5p5O+ukXJwSNSIKU1NYilGQyKSF4thdndPUGKTxlmaO0TJF7nzQ8KUIq/hpaCtkg3JqwXSMbR6nnTOZH+HzBmYu0KjX8yoKndJ7CRVToiyAojZOBIB1BulRxeiim0Zdbk6RrH6LHR58c7RqcjljlcdITRdJAdUiDT8TkiO+kxCyXzG4csdy/xXR+yGx5i8WN5xHzQ7ZecfrwMe3pSeJiZoKoAkF5XOhYnz6hPj6me/IEf3xCODnDnV9gNxu6tqJxHT4XrH1DHR2yKJC6oLMC6zRCz4lqRjRzKBZ0MmcbJWqxojy8gTMaH8FZTwgQUHRRUzlBK3JEsUKUK1py1m3AYTD5lK5zbLcVmTY4a8GnrCBXb2k352QEVtOSqdG4OgVujEz5xTIGjIwYCVIKtg4qmdOaKU02oTYFNi8J5ZRYllDkBK0S6V4B0hOwCOXIc1DBMtca1bW0pyeIpqYgdSPUMSC8ha7FeIfuOtzFOW69RtgWGXw/nr425C0BxKSVpb+pRHw/3GMk2I7f8u538B1f/wIzBDMlKEhcsZuHK2rnWdc1F77DYphOJjw4rfkL/8kP8J/+5b+C1yXzO+9hcuvdBLNie9HRbS3STFHlIpVASr8GA1ey5x/u1PVneT/HHK2Rprhb/YZmxaAhJvPEIuhCRGmDManWZFdt8G2N6xrKSY7UifPVW8g9BzM1mspkzVRbTNfgztaITjCf30AtDjgLkqYv9qmDoHCRwkV0EFipaZWk05FOOzodaHWg04FOhVRnT0a8ShQLS6QTkVZFGh2pM6izSKcuAdH0KWByAEQteLi+4KxuObuoefjglOOTLbVTbILm5HRD9fCYctOwrxSTEKFN2kvhPfbJMZOqZa/23GgjNzrBYZDso1jpjEWRkxlJ51q29RahDflsicWwtoJYrlhHgyuX2GLOeRDUQlMs9yiXSwKp7FnwHq0y8nKOyqa0QbPpBFuvqck5awKNV0yXB0xmS9rOUVcVRkqCtUTboUnN4GNfxHZZGGZGEuot0nuMIAGQd7uATPSOcrbHfP82q5vPsbxxh/mN28wPb7I4OGJ1cAMlDUYqRPD4tsY3G4SrMcKRyYAOHVMFExETcfzsjLBZp1qWIlVfEn27BeMtcbuBeoOJDuG7aw3x1yuv0xBj4s0RU1GBf/UP/AGenFZ0dWQKtG1k3USkBqEVerqgljn3Ts95YiOrg5IPfOCbuXvnkO36DBsE1dbio6I8vE2+d4PYOfx6A0N5oR0JuwfCHfIM07OTpPCl4/jSj9blMQ8+moCiQxFMji4nCKnomjYVa4iB7XadqpVrTdAqVTOWKnkBY0qtirRkxlPg8BfnuPUGozR6NseVJVuTTOcYDTLovgl8qoBsVeJEpmngnPX8MiEvK7cIhZIaISQuRLoeHJ0WWBXxMqV66ZAmSdqfk5FNW7FtKrbrDZvTCzanF3SNReQ5k9WKXCpKBFOhMN4T64YsRGZKk/uIqltM05G1jrzzmMYia4tsLLLpyLyjEAFbb+nahsl8jp7N2QpFnZU8CbDNcjZSUUWJLiYYnUHrEJ0FawltgyQynUyYL1aYYkoXFZs20HhJ6wX5dMFitY/WhtC7DxIQRvA+VR5XIG2NsjV5tGjXEbsGFRwyeKJLbRlkTBW8fV1B22G8IIsKHSVYT+gcobO4bYO0jjxA7gJhvcZdXKDalix4lG0xOIRrKEVkQsStL7DVJrk+RCA3MlU3J1KK1BhMhwSmyaf8pZ/a30zylgCiEAIp+3zm3peYMgokMcLe3iF39yYEARfA2sMP/d2f4k/8mf+U/9v3/Vf8xb/2w7x0UrO/WtJ46NrIH/6u383/9o/+Iez5q5zef5lisWB2cEBz8oTmtVeRZUZ2sCQGR4y+r7sWe0J2UpNS9krf5vMZyUhvfYMgT28yQw+Gadt022TKPheaRhhCPkWXM3wU2M6ihEQpRVXVyCwjGkMwOTErCMr02RsSpKTxHVH6lILVbPHnx9jtGSITlDcP2ZqMrS5p5QRPCdGkFEPpicIm31+fY53I5SnjRgaFDhrtNXnIKMgwQSMdxN7EjEKlnhsynWqqgpJqJSaCr0dlAvoq2fuTnImI2M05InYc3Nxnub8HCFznyIUmjxLjAgWCQmm0EBityXRGZjK0NBihMUGTO8i2W25mmqxrqU6PMblmerBHW2jOdORUw6mGcxGR0wl7qz1U57CPjinrjqWRxLaiOjvBNjVFnrNYrpgvVkxmC2aLFYvVPgcHhygpOXvyGFdtmGhB7GqMFIiQNMBSguxq7PoUe3FCc36MiT61JxAph12JiJYRhYNmy+nnPsf2pc9T37tH9eqrbO69ysW9Vzl/+R7nL98jr1uWHhYukm9bsm3D1EWmPqJshxaRrttihGeWa6S3Pc9R4KND6sQzVCL0Wqrrm2MFQrTP2MT66spbAoj0QRTZB1KElJdBlQivvPQSAHtFok3/f3705/gbf/uDPG4UP/bzn+bDv/Qy/+s//Wf58//vv8vjyoKGFvhdv+Ob+Q/+L3+a23cOcdsz6pMH5PMCczDHbc9oTx6lKip9VDRphvSnrUaVql+HRM9AhmO4PJanj2qwnQfNMNEanNCpGVM+RxWzlJbnPEoqtFQ0XUeQCq+zXR6yVX39PjRBaqoQaYJDBkvma0R9gl0/JsqO8mBBbTK2ZkKtZrRiihdZr6imXGoTIpmTGC93BHPlNdobtM/IfEbuDYXPyL1GO4GyoDwoUh8TL9SOr5YKAwzaR0f0Nb65II8NB4WgCBUXD17i/OEX8H6LXpZciMDGW2SWURQFAoHzHi+hFp6NDKwNbLSgUalfs4siuQ6aLfu5ogyW9uIUIwKzxQRpFLXv8LmkEYEWRzEtONhboJylOzmhdJa93CDaLevjh2xPnyCDYzGdcHiwz9GNQ27dPOL20RH7ywW23nL82iu46oKZBuValEjEQhkcOjhis6W7OKE+O6Y+e0IuI5kUyYcoU4fBTEkUAeNblnQcSMdRBkeF5GhiOJrmHE1LbkxLss4yRTAXSWM2LlIISS4kwqeAVttWqOiZZAYtUh8cqQUWR5ARFz3EVKJOxMQmiMJjox31KPrNL28NIMb4tN/QOby1hBBQSjAxSUNSKfmHj378U+SLI/zkBn56mwu5x/T5b+Zvf/Cn+Pf//PfysV9+GQvMteCb33OLf/uP/2FOX/wUs8ziLh5gzx8y25sQY4drNoiYzMfEmeyzZkSfV516wj0zSQbwkEqYACEtu9QdnbNImTQ+Fzyd90SpECp1WGtlzvzoLpiS09M1tnUYbZBCopTh5GJLNl8hyjnnbcCqApHPsFFjMcTJgioKpIRSOVamI/NnbM5fQ5eK8uiIdVCsQ06tpgQzASGw7TnLQmIaz56aYjpJ2HgKCkzICLWgZMLB5JDmuCbvNPt6ztQbZjGj9BITFTH0zZyE7tM7YyrzH1I7gTw2zI3HdGeEi9dYiopb00jhzlifvcq2BPnCTc4ywcNmS6cVxWrJ/bNj7CTjLBe8KjteEg33tONxLrjINU2eIYqczEBoLiilQ7uGxy+/iF+f8a7nbvFb3vMOnrt9xHN3bvCO528zn2Y8fu0Vzh69yjwTHM1LwvaUIrZMpCc2a84evsrjV1/i+ME9zp884P5Lv8b9L/waL3/ul9k8ecDMCAossTpjovvXshREZ1OAI5dMZEC7hkKCsBYtBG1Tp6AgAW9bjIrQbZiFDau4JqsfE87vI7tTlNsQ2wuUbyhkqqKuiegoCM6jtcHHwXcdycuM4B1ttUVJAUSqriEaRRUsUQsCnhA9RguEDFgcwsg0rr5G5C0BxLEPUQxm804Cy1IjfaDpIr6LTCYFPsLZtsWpEp+tuOgMz/+2382WKf/Bf/x9/K0f+Sm6COvK877nSv7Ev/mHMc0pM+NRsaF6ch+TSYh9X4f+pkUGClBqojOmAz1LGYInYjff+zzp+x/3xyykApWIr14onFA0XlC7SOcC3nmitYSmpa0qmqpOcV6dofoy9Y2XdEEShSEKg1M5QWrAo3yNcRdod45vT6mqM4rlAj1bEbMZ26DpokAZjVGBaCuauqJpLC6kFpmd1NRC0QhFjeTJ+YaoDNPFEqUz6qalbhpCDKi+GENvAOJ6nqSSqZPfRAaK0FGGhgktZazJ/YbcXZB3Z9CeIycasZigp2Vq3GQt0TuyPGdxsMfRO57j4J3PsXrHXZbvuMvq+Tvs3b3Nwa1bHN48IqoA0iGFQ/sWVVf402Pq1+6zvXePcHqCPT2hPT+h25zh2g3BNXjX4LotuYwUou+nHC06NEjfpCZUtkLYCuVrtG8xsSXDUuDI8WSjknOJM5DauF5OqXKMfCqSGxN1DI+kI4tb8rjBsEWJKuUgy5You74AyGVCQpARJ8FKQacEnRR42dfd7F/Ass+aSt3y2LURTQS1tF3KU07pea939fzmlbcEEBPdZXRDx6AYPI/vf4G5DtzIBTkwzwWZicRgMUVJwNBcdJydW5xZwfwOP/rhT/KDP/oRFhPFvhH8od/zzbzv9gp79oBSebr1WSpnr3W6hzvNcNC9BmboM367jQ5jXDRzDJBSCELsHzylUh6wSNViLAJTlCnCrA3z6YQbqyW39lfc2t/j1o1DrLVEoTGTOaqYptqEDoTQRDQIkwr/BwehxdCg3QZfnbA9e0ymFcVsjimn2CCorQch0AS6tqLcWxAXE9xiQjsvOM0ETwyclIrTQvFYecLBHHNzn6aUHNstZ76i0p7ORJApx9xDqmgtAlpBTqRwltw2TEJHQUdOQxZrMrclq88RmzNctUWGwERppkKSW4esakxnKQDhHTE4vLdY22K7Ftu0uG1DUzW0MtCo1KBei0BuW8z5BerRY8T91yhOT1HHTwgnx/j1GcHWBDosHW1oyZTo27BGsugwsUP7BmkrlN0i3Ta9aGIqlZXjyUTouxFeEtwHQEo8ToeKffAkJpdP/7j08cCYaE3CgWyJsiHIBq9anOrolKXTvo/4R1oFnYJWCVotqA1URlDrRGJPkHxZgg5Sn2orJbZvIxp39QhSdtdQrOEZj6CvqrxlgDjmHz6lIQrB6vCIs23NunNoCe947gjlt2yevMpyURBcxf67XkDqjO3DE/bvvJuty/hbP/ITfOILNQYoXeAP/L7vRNqKUksyY3Bti1S9GYbqWyumfEtETKl8Q7+VZySiT20amvAkYEzDY5gAQgj4EHcN1W2MdDF1m54u5mSZQfVE2bmRLIxkkSkWRUZbbWibGmUMxWTS9+SIqelPiH0a1ZBZ5FF4Mpl6LLvNOc3ZMbmC6XyKMQrvLN46hIsE5/EisrY1567mPLQ86rY8CQ0XWWRTCNThHH0wJ04Nm1CzdhusdgTjsKHpezSnp9GLSFQgZUR0LWGzQTcNxtukQwqHEAksMu8wTcP21fu4J8fkdcPEe0pnUVVFd3LM9uEDqocPqR88pH74kOrhI7aPHrF++JD148ecPn6EEynqHWJMz1KITJxjZi1zZ5nZjqJr0F2N9hYjA5lRSC3SO5bUH0TFRFFR0fVVpFt0aDGxSxMOg0cTkCEgvCf6kHzC/ZgQvZ9ORZeqpxN2Udwds6AHxEFTTFV/PUEGvAg4GXAqYmWafN8UfqBdeTFoienvUGBBRvqe4CnzJPZ+at9bU6LvizI8q8Ox7B7SrwF5awCxl4FyM4CjEIIoM2qzZGtmbKICLfjX/pXfx3/0Z//PfNfv/ac5f/XTuPV9jl/8FFo5ls/fBSGRZkrjM/7u3/tQasAeHd/2jS/wLR/4rXRti9IZQmZ4S/JNyXEAJRBxKa8a24PisxPRH9nTqU2Dydz7XWPABt9XmoMuBCwQhaQsMkJXU509YfvkAeuH9zi993lOXvk8J/dfptucs704xduOsijJMpN6CIeIcoHMduQ+9VwRQiGkQUlDFgRZ11E/eJWs27A3VSxLiQoWnEdHQyYLgvd0rkHmksnBgunNFbM7Byy/7iazO/uYvQk+h/P6lOPzR7RuTV5E8jyAr9HOpgouMqZ+JwogEpqG9vQMmhZhHZ6IFeD60l4qKgobMcfnFE9OKS4umDQNU28pXItcX+AfP2bVdRx2jhut54YN7LvIKsJMREqd2AYhSqJPdSmzCEWMTAQsjGIioRAxaXUikgtJJiUGhYoyEbKjIKY6Y8gY0QQyEciEJxMe0xdzUFyOgRAghMiu2L7oXSUpZJayqYY0yR1XNcFjKpoQ+z7H6YU/pD26vum9F2lZJKlxCdASUT69dPupBzoVEtdU96CYmAyJ0UCUyJhK1A15yE+/sr825K0BxP7tlx6CRH9JiwWogh/60Z/mUw8cKhOc28j5Rcd7bxf8O3/kn+cHv/ff5bt+7+/gYCmw5/ex6yecP3wVb1vmyxWvvnKf7bpjr0zlkn7vP/fPU21rnPVM9g5TeTFhECQOXLp/sYcVS9z5GJ+dvM5k7jNo+quG6E1mHyI+xlRCSQiiVCijybUE1+KqNaFeo7otqtugui0mtAjfUp2f0DVbitxQZmaX8ZDFgGlbcufIZKpmHqRBqpxcaMoQEBenZNU5hd1Q0pLLiBYSLQtKOWEaJFMX2M9y7u7vc3O14mAxY55nqJBak9rzU6rHjwhnJ5RtzR6BveiZuZbcWUxMmlAUASSp9Ly12G2N6DwiJDDohKAVkqAypMjIgmJPKBbeYboa6WqEsCjtyUwgl55pdMyCYxEci+CZEihURGcCnanUt9lJpE+DXUQIwRPxMOSWy5DqTvbanbKRzIFORf0IURD6eyZjitIqEdAyTaaP3KbkhJi8SH0XwiASICZJvzGeduZyb90kjuclTUtEjQgGEQ1EA1FD1IiodiBmgiTz9FMk84Gs74GSnrlUcCFNCfx2+cn9/NAJUAWJDJem9dcSKL4lgLij2EDKVBmyVYRA6Cm//Nkn/Kn/6/fxJ//S/5dPfX7D0X6OBmznuGEE//a/8s/wn3zPv8Xv/2d/G6p5THt6H+0rSg0XJ8eszy/wQFVF3vO+Q2aLA7yXCF0kIEQjxGX7y/R+9X1O77MFxOFx2vkOrzxiIoJUKZKXNIL0golSIo3GZCZx0kKLCi0TFVgVmv3SsFcaVmXGvDC4Zktbbci0INdqlzdcCihsxyQEdK9puGjwwaCCovCwFBFx8YTm0cvYs0do35EpjYg5yirK0wrzyiPUKw+R9x4RX3mI+8JrdC/dY/PZF8menFI8OWd1UXGrDTzXRW5vW26uaw6rjpkLu+ZIgeRDFFIk0A2g+9YHXppUAVxorMqJokAETS4glwFtAsE4uszSZhaXe2QJztXEbotot8SuwrqKOtScxy0b36C9IneaLGi00AilcVJQi0ClAhthaZTHqQRCwkV0GyhaKJ1CxaT7IVPD++G+EVLvHRkjRE8Moe8HksAQaRA6I5BcOcNd32luu+fyckl6baQ1kZg0t2BQvkD5HBUylM9Q3mC8IXOa3CkKJygdTGxkYsNuKn3o24KKvmr7pbY4gGCqYSqQoQfEUVvRneX+NSJSQN9Ll95WuXSyy/QJDbuuupHeqYvor0ToyTI2RXSj67WvFIkCenrLZYHY2P+OkAYhC/bf9c2UB+/k0599jT/z576XP/Uf/nV+6pMvITNNBrgI+0bw3b//O/ir3/vv8if+V99N4c/YPPocBVtmWuCqlttzwdwInrt7EyEc9fqUYGsQ4ekoBSSS9hDseSqC9xZLf0FFXxVERF73xpVKInoqxBAUVyo1Es+NRNoG2WwRzQXCbgmuoWkqqu2aZn3Gfi4w7QWmOacMLYVMjnuNJ5eOItQUsSUTqaKOi6mFqAgREzxLHVHbY6qHn6c9fhnVnlNgUcERm4YyWPT2nHh8DMfHyJMTimrLzDtmMSA3FxRdw54UHBnDSkiyLpXDlz4yVGCLAYKPiABGKjIlMCkFFyXSMxOHKZLgIVp8WyGiIxoFuSEqSSAgQocIDZkJqdqZkUitUmRbRIQMSQMMARHSc26kTLQSFfG9H64j4Ej3SQWB9L02T6LLoCRCyZ7qn0BD9FqgiwFP0u6jT78jSR0no5IIowfnbZr6sZU+jR/YARaHx3g012tqA1iKHrR0SER3dvtK39AhdW5UvWaY9iWTN3Lnnwyp8+Ku2EbycyZ2Q2oKJXsi/dNP629uETGGSPD9pVJJJe9XJo+bBVcT8Gycw5eHvBIEf+xP/UWe2Amz2++gbSpcZ4kBVJYjVJ6yELxIHd58TJSR4S0ohrQ9DwFyUyKDJ9MBFbZsn7yMdGt++ze/h+/4tm/h23/ne4bHJfkLgSfrip/6yX/IL/zDH+ff++P/C97zzndzEjS+0PyZv/p3+YmP/TK+2CM/uINzImmIJAIsPiC8Q0SfHgSdpSBLTB3QUhkjlTTLYClFjX/4ObLjX2Flz5jHFuEcLmYIZYhfgZaZ/DdDgc2AEMlMSlHw1I/CBo8wJjVQ8oHGB2SWM1vuMZ0t2aw7bOuwTYNwLXnowS36VHhA5zgSVUeINHBjFDgPMXhKnbqs7AjRcbjavTayM+kGO234lF4qMgoQqXBtSIgOpOWCAeTTQBO9qS5j6jBIX9UmCNEXhUhR18sXaKKj0JcXS3se1qdK4Yl+kqrlQAoUDLQkEVOnQhUjMqam9Ck6Kvq0wgQcDGAwAo6U757M+MGHR3/PBvNS9N/zMqUsDkqA7Okrl+d+Zf994V+GwER/PcdwOEBNWpLeGsNd6ffa738IeKSlQwHhKJL5rmNKeIh9VdAgkg9ztz0htQSOEiWTzzFF/JPv2ntHrg2ZEAnUhUAajReCzqV+6945AIwxCCHw3iOEwBiT3GRc0u+EEMQY8d4TQkBKidZ6971hmRCCEAJZlnF2dsZ8PifLMi4uLijLku12y0/+5E+mk/gqiYghREIqQ4TQqddBfytMBGgJ9SlyWtK6yMMWXqoN/6f/x1/htY3EkjM5OKTZVkQE2WSGkBm2tUiZkc0WuMb2b6Ekyfnc+xJ9AGvxbYNUkJcZ0dW02xMkHYupZjU1/JHv/pf53d9wi9pHYltxZzJFCrh373O8d/8G88mCR03gREj+3F/9r/j4Fx7RZAuqFvRsHylMGkQh/aboq4tEIQjavD0Asa8Oc+lI77ljAoRKOchdDLQ+4IUkywuyfEq0kmADwbYQbMpgkLE/l+SeGIAtCE2UycSzAWKwlCpFbdkNweHYRh+Gdf2y8XbJYriWt6uMWR3jBImr669uN0xd12GMQcpUR1EIgdaaGCPOOYqiwDlH7Ns1hBAS1atvFToA29AnfUgykH32WggBrVMRFrtL2Oh7hff7dM6hlKLrOqy1HB4eUlUVP/IjP7L77ldDegbS0zwn+rdLenMJpMkhRHKdcTBdcWM1YZFpSh05PDqkXtdIMyefHaLMjBgVUmaJcOtS8rfAQ7DgW4SvEaFGxAapGop9iVp4fNZijUfur8jvvoO4d4cnruShWPJ///6/yb/1F/4LPvzLr+InEx7EyIO24rnn3sNZ56kQyEIxyQQuak6PzxFCMz+8mZzDQ7c9SBqqEET55v1gf8Nld60v+f5xN/WaQRSEPoIpIighkTHStQ3biwu6psF1LTE4RIwo0ccGRZoE/QsohJR2FRPoSiLqWZ//tbxl8kYgCKnnsZTJvx77wOdYm8uyDK01SqndX9HXJ1BKsdlsqOt6B1YD6A1TlmVkWUaep8K4xpinANFai7V2B6qDxpjnOUVRsF6vd2BqjGEymdC27U6L/GpKH2UYQPHyU+xNAYQiRKg3FRebNS60dOst+6VGVGecvvJrTCYFeVkQbUdzcky0HeV8ilKC+v49omsJtiEOk+8gJNIpOJrmjGgcZNC6hm1dY9Ho5U3KW+9icxHQe1/Hy487/uL3/uf8+3/hr/Nzn3qJTpc8cZFstc95hIdN5AQolwcsD2+R5TPq03VyDCfnSA+OQ5SuB8Sv8kX99Ui61ilYsjOrBssUANH3LnHpYfMBhcBIhUEiQyC4LoEhKZKZANATQ8o/vZyGzyk7Qkkut7+Wr1kZNL2rIkac4GGbq2A4gN5Yixw0t9hXrZpOp8xmM6bTKVmWPbW9c47NZkPbtjtQGzTItm2pqgpjzM5kDiHQdR1N01BVFVVVcXh4yGQyQUrJbDZDa82DBw+w1qLUVzf1Vn3P9/yZ7+mrqPYm1WWgPymIAqUlJs/Js4KqbthbLPjG3/rbefd73ktUkhdf/DxEj1ERrSUST3Q2gZ7RSJGS6PvwGkDS1qIgikBdXyBzgy6nIAxi6FHoINqINCXeRnQ2oZwuefLohJ//+Y/z8r1T8nLF8mjGeRtpFBgp+NjnL/jUJz9LEDmmXPTBoqEvc49/u4gtiY5DhFEaVdIoJSIGjHDE7QmqPqYIDTkpehhIaWfjl8mvX3pA7vllSXYXKR2NkilTJaTlUiiUUCgpUSI1pldSokUCOBkDIaQMh+A9UogednsHurzMNkg+w+G8nzaFr74mvui6qxtey28qGcBtDIQDGEopcc7tfHvDNHyOvdk8FiHEU9qkc26n9Q3LsyyjLEum0yne+6dM6zF4e+95/PgxT5484eHDh3RdtwPNW7du8Z3f+Z3MZrOnfv8rERGji4QWgChzLKqPESf1UQH1+THTMkNqyXnVQrkgKs02wsubSJ3DB3/sE3zoQz/O8fEZypSobEI2XVDO92haT4yqJ68mM3CHj8IjSwFGpCiiJ3GohEmA6ZITN3QNMjoyI5G+pducYas1wle88NyCP/JHvpv3/5YD7q/hh/72z/JLn/41yr3bXJxcpBzeXdpeotykLJXkLRWo3tf21vsQGcxiEmglYLrMToCIMhrvA8771GMYiZQDjShx4IQQKClSoMjbnfksRTKJQh85jkhQhiiS8zzGgOoDOeloRkd25ZQET2H1Tq59iG9vGQPdWK5qh1fXDZPW+in/H6TMqQH4vPe75QM4Dp+99zvQGzTCqqp2IKmUoqoqyrKkLMsdUM5mM8qyRErJt37rt+5Ab39/n5s3b5JlGdPplBdeeGF3zF8NSYAYGwCCyEhNEZ8GREPA2RrT9/893TacbBpUOWe+KDnzYGQi3/zK/XP+/k98mJ/8mV/geN2SLw4R+YIgCoIsQOYpfxadBqcAVWS4piJ0XfKTaYkafBAq0X+Cddi6xluHNjnFbIbOcghrQvcq24tHLPdv8j/+V/8wmybww//5DxE6weH7v5HtxTpFSmWKsyGuAGJ8doCYgh3pwUym7qCpxd0+Lx/Cnr4RYzruvmpPHN7mAgie6C0EjxSgVfKz+JDahqb0RQV9NBFSkdg31BCvAfFrQsZgN4DgGBzH82NtcZiuBk3oAXHQ+k5PT3ea5KDBDaas9x5rLdPplNVqxWq1Ym9vj4ODA/b395nP5zz33HPMZrOd2T2fz1ksFpR9g7izszNms9kOgAdxzvHw4UPu3r27W/aVSg+IqctY2GVbJiNQ7KoGRprtOblWGGPwCITMcAi2TUWILWU5oUNw0VjIp2x95B/+o8/woz/+M3zm868R1AyyOZgpQk9A5omSg06aow9IBUpLomtwTYUQgSwz2LZFKoPQJqXjudTDQkiDzB2uvcfqYMKT+4+RumS+f4vqvEYXC2xjUcUkDVo5JMT3gCh6QAzPEhCTL3P49NQ7vM9pHt6mUqr0kPZmhSDx30JM5HclRfIbBoeIAa3SGzj2fhvnQ9I9e5J6gsGIGDUKugbErz15M0Acg8zuWRmZyYP/GnjK1yelxBjD/v4+k8mE+XzOfD7nxo0b3Llzh/39fbIs486dO8xmMxaLxc4XOByDEIK6rneBm8Ekpwdd5xyTyWR3PFpr6romxsh8Pk/j4KsYAxAx+kjsekBUJLpuMqBEP8ldBQ7fazA9kxaZSNiy6U1RicXQYWjIqCLUwMc+c8I/+tSv8rO/+Es8PN6QLw4o5wd0NlCtWyIlQmqEBGUESguIluAanOuQRidzV/TZoDHl2wqdgbR4+whii4gJxIh9qh6mh/M+iNIDYpQBRJ+SFZ8tIAI9946nqEn9ChiBT9Ign/4cRzy2YVnKgX19sCQtGfMKBw11NGBG279dAHEYQIPGMswPf40xu22vrqfXUsbrx+agEALn3G4fzjlEz58br+u6bjcgh+3oAeLq8V09zmE9/SAfBvxgMl6dH7YZn8N4Xlzh8Q2/MfjhBk1qPDVNQ9d1u+91Xbc7z/V6/dRvGGOYTqcsl0um0ylHR0cURcFyueTw8JCjoyMODw9ZLpcURcHR0dHONzgGV0am9VciX+n3fz2SABFHhD6lXD3lwUqgOFTy6KOV/cBMaz3IjihCH7RQWDRt1HTC0F7msXB/G/nZj77Eh37qZ/nVz72EkBnTg9swuYH1Ctu1BNcR+h7EqXKmwAdPiAGkQhZlCr5IRWha/PYCUZAip1EmUByAsc8diD2AB5EKCETZd4QTCfafPSAOQZlxeDld+UFEulk9pAEkUnSKTKelg5k9bPv6Y0rmeVp6CZmXEe23JyCO5Y0GxxgsGG0z/B0AiNG24+8k7TsVph37wEIfDc3zHN/z77Is262j164ePXq0Mx8HYJBDdfgeVEXv1hiW0R/XAJ7D8jGgDsc1BtRBxvsbjmUAurZtd6brAIBCCCaTCavVameOTiYTyrLk4OCAO3fu8MILL+y0ujzPWa1WLJfLN7zmY2maZnf+42Mdn9tXIl/p9389ImKMMQyZEf1wGobc5UYJIscVWNJI2mWzgxwG2JCSnhpoOwQ2wrYNWASTPGmfLz5s+Qcf+gl+8iOf4MXjDr24QTFfoIoJXQDrIy7GPu0vIoocjCIGS2jr1BS9zMmLCe2Tc2QYosIS4hBRFru/idqSwJCe8JxG/LP1IfYXcnfF09woM6QXwVDwob/CuyyKvtBDv+0YFN9IxmA4fH67A+IAXmOgGwPLAGJX5er242XDYKUHkvGgG2ggMUastTtNbBj09L85gNZisdjtczDzhimM/Gn0vz8AGT0oZlm2m/fe7zRa1XP1Li4udvsf9kuvnWqtWa/XTCYT9vb2ODw85PDwkP39fZbLJWVZ8k3f9E1orZnNZqxWK2azGXmeP3UuV4F38P35PgLMles+vqZjQL+6/Ksh43vzGy0ixJgCu7thkjTAy+GS3loJONJAEGnM7dY7lTJcEvSkKV2K2ANtypPsQqBzjs6FFNMUklYZfvyXXubDn/gMP/uPfpEnx+cUB7eYHt0hmoK2dahyincdoa0JIfVUiSoScQgvUGGCDEMal9jlTPenmPJGSQAYxZAW15+EEL2Z/WwAcaSn9cgkUjmnEcgNclmyaQyIKU1tvG3a3xeX8baRtz8gXjW7roKb6DUr+kE4Brvh7wBCYzAarxvAaLyt631neZ4zlqvbDvMDGAzfH6Y8z3dAOWhwA1AqpTg+Pt4B0vD9QdPUWnP37t1dUGJvb4/VasXBwQGHh4csFgve9773obV+is83vl5t2z613zgyt0MIlGW5iwCLnlc4dkNcPb/xvge5er15g/v0m0GEj5cFsCSknNCdFzFJ6BuiJ1CUT4FiFJK2LzKZvn85pQHkEQJC9Fjn0Mb0awQueE7qjmYy48xH7j1a85F//Fl+9mOf5nOvPKBxElnM8B70dI6ZTRFK4LoG29YE3yExGLXo/YXD6L5Ctt7Np4O+LK6ZzloIDTw7QByA7hIeE8ANRUPH2w5ZJsOWw3ZPg+cXh8QeCnaf43DJnlrfz1/ZSbqfo/lefqMBcTzI3mjgDZrOIFc1kzGgXh2csdfqfE8rUSoFDgdtKcZI27aoK+TkYTul1A7srLV0XbfTDAd58uQJk8lkBzSyz96YzWZMJhPe+c53flENryiK3fZjc130/j96wBsAdHxOw3W4en0GGV4cY81wkLEWOpYBRIf9xz4KzWh/g3wx8Hw7i/Axxq7/kCLKHhntrmINQhBjKguVppR3K/oBEwErU1Rait7t10/J3xiwXZNunpSEGGjajigERV7iUVRA0481H+EU+MRnH/ETP/dRPvbpX+Vs3YIpEKYgCEUUCqE0yhiUKWlrN2pIfznAxwMdrvrVhvlUhulZAeJQaVlAD3FiVx8vAeJwEul4E4COAZGn6umlv2NAHH//EhCHLeKV6zS+ZG8XQBzLMOjGg28Y0GMNZjwYB/N3DBTDdwdwG0Bs+E4cgeNgUnrvqaqKuq53ADoAitaasiyZz+c7eslisaAoCp5//nlWqxVHR0fs7e1RliV5njOZTFKHwJH2xkgDHfaf5/nrgGUAXCHEDhAHE3t8boxM4jjyJw6aZIyRk5MTdE+WHkB+LNba3fUcNNkvJsNvM/r930wi3BVA1Hhk7HpATEGHZPJqAnooFJbM5yulf+TuwxB8CRBjqlUlJLHvm2FJPsKIQAjIZaJK102DCxGEJkqDE9AK+ODf/yif+JUv8LFPv8hZFZgc3KVY3KBuHJv1BXo16yPIAkZpcDDGgx4Ad2Z/Dwji2QOijskEjiIVjL8ExKRnD9B1qRkOJnO6P74nWSfpoVKk+afl0g85RrbBf9x/vJx/mwDiGKTeTN5oGz2ic4xBdJg2m80OEEMfSPHek2UZk8kEa+3OTF0ul0/NZ1nGt3zLt+wAZSAYl2W50+C22y3GmJ2vkP44xz7GLwU0TZN4wmOAGYP+WHt9MxlM5QH0nHO74xpHngfp+sIOjK7t1b/Dvr7cY3g7i/Ax7lpNK0iDE5vyXvsobALEoRz5CBB3f1Odvl7fGIFP/yYXPfdNKeIIaodHV3hHrnqtMy3BWo/zAouiE3DewK++WvHTH/9lfvqjn+aVB6eoYk55eINNsHjVZ6KIvmgDI7M5Jl9mAsGACD2Y9CAUlSHCrl1pf2YgBNJ3TGgID3+N/MmLLO05MyzBOzoUUmtEn0f830euaojJ/O05gr0p3D9+O61wB4z9tU1lGoaHsd9+FE1O0m/RcxvHEDbQbp56nK+87PgS63+jAZHRYBtAYLxsrP0M01jLGugm4wE8aENaa9797ndjjGG5XHJwcMBqtWJ/f5/bt29zdHRElmXM53Om0yn0QDD89gCy8kqhAT8KSkyn010BA2DHuRuOeQCU2Guwg/Y3AOWw3+F8roLn+HiGfdBfH9mb2MP2w/GOAXG73TKZTJ4CwjcKtHwxGTTrrwlADHF4THYwMIKrOFrag8z4y099GuQNhkZM2uBuKL5uALmdz1KQymElLS0ttRHakMzqJkYen8Mnfvlz/ORP/ywf+eVfo50eEYsFMiuI0hBCAgkhDdJkhLYlW8yAQLc+Q0RLMSnwrmF7foGZrQBF8o6mnw7BEegQdsu+csT7L5E/usfC1eTCYXHUGoSWGGvR8dJn9OsRAU9VnBlI2uNr7QfemkgaNf3dGAa5NtluIMTLdPGnHv7hLgshUFICl+aj7P1gO8L36OG+fDqeHmyM7n9eFHjvcaPI63gwDbSMYRJX6CXDd4qi2FFcBvJtlmW4npQ7+OnG5q3oNZqqqvDe73x1ZVmyv7/P/v4+0+mUg4MDDg4OmM/nO7BbLBZIKdnf39+d0xvJ18JAv5YvT8QID3/D5Ev9RCIluz48kP4lQEzRYnq/YuMiNkI0gqihCXCxdTxpAn/rxz/Cz3/qV3n5C69SLA9YHD2HE4amdoQo0UVJc3xC9JZsOUcZQbe5IEZHPpvTdQ6hsvS7pEKlPnYEaqSv2Bcd4v7L5A9eY+EbjHBY0bHJAlIL8s6h+8ILbyRjEHkjGUCIfvCNAUn0RTPHMt5G9Ez/YV6Oas0N+2jb9ilAHJYPYDRoK+PfHK83xrwOwMb3NPagO/zusGwA3KIodhrbVe1FCMFms9mtCyHQti3W2p1fbrPZ7KKsi8WC2Wy2S++aTqe8//3vR2vN3t4ed+7cYblcIvssiiHtbKz9jDWmL6diypejJV3L14a8LQAx9AGcS0AcnIB9ucY+iu16bbHryd5BJLCsgbMm8ouf+lX+3oc+zKc++xJWlmTzQ4IqiLpkun9EFJrNySm+s+SLGcpkuK7F2g6pNJJE3fHBEmNHkC0y1ixjA/dfonjwKktXkQuLEw2VCb2GmPpSfDF5M0AcBtwARFfnZW/2DNMY3OhNsGHZsH74HHst66qM78lY2xJvoL19qftHb8rpniIyfB60xQHwxgA5yPB7t2/fxhjDYrHY0Ulu3LjBzZs32dvb2+W5LpfLXYBBKbWLbm63W0IIOwDkinZ8dnZGURSoPngSRzm51trdd76YvBlgXsvXjrwlgPilJBLxeGKKXyNIDW929JkoaDuLyQxRJf+jB5oInQdEYKokF80amU+RQvLiqeVH/sGH+fBHf4njytPGjKr2SDNlcngboXOaTUUMkWw+x7ZVilrLjNRRwRHpCMoiaJiFCu5/nuLhK6zsllK0+FjTGJ8KUdihLeMbyxjc3kjGPqQxkA3zY41t/HeQMTXiKnCONbFh/bDvMQCOAXGQ8ecB0MJI0xt8Wm3bokdpWwMQDdHUIXn/xo0b3Lp16ymgM8bw/PPPI0eZDlePb8h1He9/8McN3+MKQTuOUvrGfro3ki/3hXUtX/vyNgJERoBIbzIPoJiKEqS+G6npduw9nQpQ/gyjUmbM4/MKijkmn/D5c8dHP/0iH/7FT/OZz7/Gwydr8vkNitUNrANrU68SR0Qog8JASCWxIo6gLZGaaaiJ91+kfPgyK7tmSk2MFa12CK0QTvWFZ99Y3gwQx/JGt8Nai+z9b2oUURy2bZrmdWYyPdAqpXbVhccDewAS1ee5xlGOa9u2O3ARQuzM16IomEwmu5SvAaQ+8IEPvK6ayaDlFUXBxcXFLgo7Bu+xmWxG3L9BhnXj77yRVFUF/YvBjHp6DKA6BsOx2e77AMwQLLmWa3mbAGLKHnk9IPYbiZSLHGPEx0TRsSFgg0OEjlWpiK5GKAPC0ETBw9MNMZ8xmU5oInz+PPIPPvxJ/t6H/jvu3XtMuXeD+cERXmXUIaQug14ToyJKlfKdlSOGioloiPdfJH/4Mvt2zZQKQoXVFqEV0aUeuF9M3uwSj03mAdDG82MNZtDUxmAyVBAZgGCIbopeOxy0qdhHXAfy8ACSA71ksVg8BWp7e3tMp1Pu3r1Lnuc7s3W5XO5IxfSAPDaZQ09UHuQq3WSsAdJrdgNohd58vwqCoafD0F8v0WeSfDFAG8x239Nnht+7+mK4lmsZy9sAEMHTpwb1UwLEfkmMeNuhtO75jJfx70SmiXRdQ5Gl9KqLzQVSKSblHI/kZLvFovE6x2QCC3zsc+d88L/9EL/wsU9xUrUs3vEeuqCJnUzNvk1B1AonOmKomCqHf/B58ocvs2c3zGKNiBVWdQit3xQQ30zezGSWb+BDHAa3EIInT55gjNlpR/SAOiwbIq97e3vs7+8/5aebz+fcvHmToih23LlBEx3v74tJ6IMgalTU4Kpcpb0MgHfVjB0Amyvd266C41WxfR+P8bUZA/Qg49/nip/xWq6Ftw8gDpSbRCe5BMR+ixB2rG/vHZ21IAVZluojtkhibz5LEmsSPCE4nPcYkzJitj6wbi2qyEHAFx5UfOJzL/FffPDvsXWSUAsQBSKbETJDJxzObpnnEffgZcyje6zclllsEKHGqUFDTBV2vpi8EUiMZRjMg2Y1aDaD2Xd1oJd96fXpdEqe57t6c0PRzTHwzWYzjo6OnvqtAUAG8KuqapciNkg3qpYymOlj7Wp8XKaPQn8x2s0Xk0HLHWuEY82WHux0n1Ux/t7YpH+joFEYFUoYR7nHx/XlAuKbvRSu5WtH3haAmAzmxLzbASIwVI++1Al7EcMy8MJQ9RkwapdtE1HR7XKyY+yzQITCo3AoXB+cqSM89JFPfPpF/tGHP8ZnPvMFziqHzwtsronSMctg/fKL6CcPOJSehQx0zQWd6MjynOgguAQQY+1krPltt1u6rkP1JdcHqsxAKynLkqIoMMYwn885Ojri4OCAPM+5e/fujj+3t7fHYrHYTUPhgC8lbzag32z9tVzLPynytgHEAeBSBsZA+h1YxlcB8XLeC0Pd64Ryl20TUThkdCn0Evuc7L55eQLFBJcdcNptmWdTNPDiKzU/8TMf5Wc+8Um+cHaME55pLonrU8zpCUVbkfkWvKUODc57VBRILiumjDWlsem5WCy4ffs2N27c2FUQXi6X3L179ymf3RCsCH0znel0+pSWNPgA6cHsajWWq/JmgPdm66/lWv5JkWcOiACpHmOSpwFxkCuAOJqPQmKjJo7MZRFTxZ7L7z0Nhh6x0xBjn79ddRvsuWO5OEDmcObhx3/xc/zcx3+Be/de5DO/+AtsX36ZO4s5EyG4OD8jW0x417vfjWsalrP5Uybr4KNbLpe8973v3fnCrlYGkVJyfn6+y9K4KoP5fNXnFq+kj30peTPAe7P113It/6TI2wYQnwa8SyXw9UN12G4EoYOzfLd8vL+UGxx2JrPYcRmH/I+qesyiyJjLBQSBdbCNsM3SNg8vLvg7f/O/5KWPfZxv/a0f4J/79u9gPl/glGS+XLA3n6F731Ts6SuM+IHr9ZrYk4EHX93gHxz8c/TgN/bvDTy6uq5Rb1DNZOzP+1JyDXjXci1fnrwtADHGsEPA1x/Mlx7MMgaIbuRvHL7Sl0IYiiWI1Boh9HCZwi7p9wweQyDzCqwkhkgrYKsFnU7xnAIoiShncVWLkApZThAKok/VfQatbyyxL990VQZAFKMS8OINSNS8QabEEIAZfutqNPWqXAPitVzLlydvH0AcoHBHqxkP4i82nwBRRNtDnBgBYdpH3FXMvvzuEMSJfWusi66m1AVLTELJLjUobQtBUPDw4hGz3LCQmhIFMgMUIcL5ZsPeag5XzNixDMRhenAaNL2xdjgEZN7o+03T7NZ9uRHca7mWa/n1y9sCEIfGVZHXA+IQe/5iIp4C1Kcr6iRJZW2HIlkpyJKCLTF6vNBUYk4A8hYKl9pyosFpaEVEpGqNTAKpqX0Xqc63hCxntloSYhxVGnt9EdKrxOSrdJqiKHaE6THYiZ5LeA1+13Itb428zQEx1fR7Wlu8lGFpSFj4BuY2iF0Vx8FIdsToejPbgcg5k3t4YOagsP0OlcBl4GUk4tBYpG2IdYfSE8hKkKk9pdSX+bSD+FH+7MB/eyPtb5AhMj34Dem/N8gAsGOz+kvt71qu5Vp+/fL2AMRruZZruZa3gVzbYtdyLddyLb1cA+K1XMu1XEsv14B4LddyLdfSyzUgXsu1XMu19HINiNdyLddyLb38/wFu48tM/oDDQgAAAABJRU5ErkJggg=="""


EMBEDDED_DEV_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIC/jCCAeagAwIBAgIIBMxIBLcGgGYwDQYJKoZIhvcNAQELBQAwFDESMBAGA1UE
AxMJbG9jYWxob3N0MB4XDTI2MDUwNjA4NTEzN1oXDTI4MDgwOTA4NTEzN1owFDES
MBAGA1UEAxMJbG9jYWxob3N0MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKC
AQEAor3J9jQD1k9OhUJQkw3mT87+ZGqyzug3LZIxvZL2nYwKhPuL8K26tF3NI33F
N4requttETRqLXdLdDhyWdFRClFjG6lsLarfNhq8FlM/GxE/Rk2nXAnt2WV0/ONi
VdA/qAhDqg0W6LBTwdh+/F0JjQZ511+a7/bjqFUqLt7+IFDMG2PTLgpn7u+ITDDa
Lo2zMksw1KHXWjnOAlE9CkY1MAuxqakndUAMsyHRWegPhWupX8uv2O/sv7z6t1Hj
uKcjnlB7deNs0bFjDzfFcAxDbRmLK0SRO+5/EdkEOaFZPeevos6zgrbSIBe6CegP
lSFJvIAr4PTJRLYqWYWsb58ocQIDAQABo1QwUjAaBgNVHREEEzARgglsb2NhbGhv
c3SHBH8AAAEwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAqQwEwYDVR0l
BAwwCgYIKwYBBQUHAwEwDQYJKoZIhvcNAQELBQADggEBADYioCFb4gW5JyqAwbsK
bgzCkkhu1dBVZ78c8m8TclvGD7c6rLH3rgIgvOhDFu6RoIQC+6LgXSytH9JJN7EG
r+hb1x8R28qKn0ezDEfb+kEGB/jNs3Ce5xkYu/dpmAl8H6ruKuKMEthq8ndrVM5k
YIBBqthV1GCD28YfzE1WGsgWmcjyaJ/8mcnCaVDk/pZtwdkJUvndym/Ev8CsDJjD
xZ0RES2S2oq7m/zXaLR072Y5TQcwSdfIUTfKCwQGrvS6EyKKajg195LsUVgYFQZX
I0Oj6qlKJv2mFKdui7BXx3zOJ9HbgZu7EvQfc5jblOeFhvOUHDS/tPpXGEPhaGsW
v/M=
-----END CERTIFICATE-----
"""


EMBEDDED_DEV_KEY_PEM = """-----BEGIN PRIVATE KEY-----
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
REDACTED-BURNED-DEV-KEY-LINE
yoFkYy+k3LlpZC+vOnuJ60g=
-----END PRIVATE KEY-----
"""


FAVICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="6" fill="#126e82"/>
<path d="M8 10h16v12H8z" fill="#fff"/>
<path d="M11 13h10v2H11zm0 4h7v2h-7z" fill="#126e82"/>
</svg>"""


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <title>NetWorker Backup & Recovery Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef3f6;
      --surface: #ffffff;
      --surface-2: #f7fafb;
      --ink: #172026;
      --muted: #5f6d76;
      --line: #d7e1e7;
      --brand: #126e82;
      --brand-ink: #ffffff;
      --green: #18764a;
      --red: #bd2b3a;
      --amber: #a96800;
      --blue: #2457a6;
      --shadow: 0 14px 32px rgba(18, 41, 52, 0.10);
      --radius: 8px;
    }

    body[data-theme="midnight"] {
      --bg: #101719;
      --surface: #172124;
      --surface-2: #1e2b2f;
      --ink: #edf6f8;
      --muted: #9db1b8;
      --line: #314249;
      --brand: #2aa6b8;
      --brand-ink: #071113;
      --green: #6fcf97;
      --red: #ff6b78;
      --amber: #f2c14e;
      --blue: #7db7ff;
      --shadow: 0 16px 36px rgba(0, 0, 0, 0.30);
    }

    body[data-theme="graphite"] {
      --bg: #f1f2f3;
      --surface: #ffffff;
      --surface-2: #eff1f2;
      --ink: #1f2326;
      --muted: #666f75;
      --line: #d1d6da;
      --brand: #3d5a5f;
      --brand-ink: #ffffff;
      --green: #1d7c55;
      --red: #b93246;
      --amber: #ad7300;
      --blue: #3d64a8;
      --shadow: 0 12px 28px rgba(31, 35, 38, 0.10);
    }

    body[data-theme="contrast"] {
      --bg: #ffffff;
      --surface: #ffffff;
      --surface-2: #f2f2f2;
      --ink: #0b0b0b;
      --muted: #3d3d3d;
      --line: #202020;
      --brand: #005fcc;
      --brand-ink: #ffffff;
      --green: #006b3c;
      --red: #b00020;
      --amber: #8a5a00;
      --blue: #004fb8;
      --shadow: none;
    }

    body[data-theme="ocean"] {
      --bg: #e8f4f6;
      --surface: #ffffff;
      --surface-2: #edf8fa;
      --ink: #102a31;
      --muted: #527179;
      --line: #bfd8de;
      --brand: #087f8c;
      --brand-ink: #ffffff;
      --green: #11845b;
      --red: #c03546;
      --amber: #b27900;
      --blue: #1c6eb8;
      --shadow: 0 12px 28px rgba(8, 72, 86, 0.12);
    }

    body[data-theme="forest"] {
      --bg: #eef5ef;
      --surface: #ffffff;
      --surface-2: #f2f8f1;
      --ink: #17251b;
      --muted: #5f7565;
      --line: #cfddcf;
      --brand: #2f6f45;
      --brand-ink: #ffffff;
      --green: #1f7a45;
      --red: #b83b4b;
      --amber: #a06c00;
      --blue: #3867a8;
      --shadow: 0 12px 28px rgba(25, 66, 36, 0.11);
    }

    body[data-theme="ruby"] {
      --bg: #f8eef1;
      --surface: #ffffff;
      --surface-2: #fff4f6;
      --ink: #2d1720;
      --muted: #7a5d66;
      --line: #e6cbd3;
      --brand: #9f2d55;
      --brand-ink: #ffffff;
      --green: #17794e;
      --red: #b92345;
      --amber: #aa7200;
      --blue: #445ca8;
      --shadow: 0 12px 28px rgba(111, 31, 58, 0.12);
    }

    body[data-theme="steel"] {
      --bg: #edf1f5;
      --surface: #ffffff;
      --surface-2: #f3f6f9;
      --ink: #17202b;
      --muted: #5d6a78;
      --line: #ccd6e1;
      --brand: #425c78;
      --brand-ink: #ffffff;
      --green: #26724a;
      --red: #aa3d45;
      --amber: #9a6b12;
      --blue: #376da9;
      --shadow: 0 12px 28px rgba(47, 67, 91, 0.11);
    }

    body[data-theme="arctic"] {
      --bg: #edf7f8;
      --surface: #ffffff;
      --surface-2: #f4fbfb;
      --ink: #10272d;
      --muted: #5a737a;
      --line: #c8dee3;
      --brand: #0d7891;
      --brand-ink: #ffffff;
      --green: #168059;
      --red: #b83245;
      --amber: #9e7207;
      --blue: #2d68a7;
      --shadow: 0 12px 28px rgba(31, 91, 103, 0.11);
    }

    body[data-theme="citrus"] {
      --bg: #f5f7ec;
      --surface: #ffffff;
      --surface-2: #fbfcf3;
      --ink: #202817;
      --muted: #68705b;
      --line: #dde5ca;
      --brand: #617d18;
      --brand-ink: #ffffff;
      --green: #23733f;
      --red: #b43a47;
      --amber: #a16d00;
      --blue: #3f6fa5;
      --shadow: 0 12px 28px rgba(76, 96, 31, 0.12);
    }

    body[data-theme="harbor"] {
      --bg: #eef3f4;
      --surface: #ffffff;
      --surface-2: #f5f8f9;
      --ink: #17242a;
      --muted: #5e7077;
      --line: #d0dce0;
      --brand: #235f73;
      --brand-ink: #ffffff;
      --green: #24764f;
      --red: #b63548;
      --amber: #9d6e08;
      --blue: #335fa3;
      --shadow: 0 12px 28px rgba(35, 78, 93, 0.11);
    }

    body[data-theme="ember"] {
      --bg: #f6f1ee;
      --surface: #ffffff;
      --surface-2: #fbf7f4;
      --ink: #2a1f1a;
      --muted: #75665f;
      --line: #e2d4cd;
      --brand: #8d4a36;
      --brand-ink: #ffffff;
      --green: #26734a;
      --red: #b23545;
      --amber: #9b6a10;
      --blue: #3c67a2;
      --shadow: 0 12px 28px rgba(96, 59, 43, 0.12);
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      min-height: 100%;
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      letter-spacing: 0;
    }

    body {
      display: flex;
      flex-direction: column;
    }

    button, input, select {
      font: inherit;
      letter-spacing: 0;
    }

    .topbar {
      min-height: 74px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      padding: 18px 28px;
      background: #102832;
      color: #f6fbfd;
      border-bottom: 4px solid #c9b15f;
    }

    .topbar-brand {
      min-width: 0;
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .topbar-logo {
      width: 54px;
      height: 54px;
      flex: 0 0 auto;
      border-radius: 8px;
      padding: 3px;
      background: #ffffff;
      background-image: url("__NETWORKER_LOGO_SRC__");
      background-position: center;
      background-repeat: no-repeat;
      background-size: contain;
      box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.28);
    }

    .topbar-logo img {
      width: 100%;
      height: 100%;
      display: block;
      object-fit: contain;
    }

    body[data-theme="midnight"] .topbar {
      background: #090f10;
      border-bottom-color: #2aa6b8;
    }

    body[data-theme="graphite"] .topbar {
      background: #28363a;
      border-bottom-color: #6a7b80;
    }

    body[data-theme="contrast"] .topbar {
      background: #000000;
      border-bottom-color: #005fcc;
    }

    body[data-theme="ocean"] .topbar {
      background: #075563;
      border-bottom-color: #29a9b7;
    }

    body[data-theme="forest"] .topbar {
      background: #24492f;
      border-bottom-color: #83b56f;
    }

    body[data-theme="ruby"] .topbar {
      background: #64213b;
      border-bottom-color: #d992aa;
    }

    body[data-theme="steel"] .topbar {
      background: #2f4054;
      border-bottom-color: #91a5b8;
    }

    body[data-theme="arctic"] .topbar {
      background: #0b5e72;
      border-bottom-color: #8fd3dc;
    }

    body[data-theme="citrus"] .topbar {
      background: #536d14;
      border-bottom-color: #cedb68;
    }

    body[data-theme="harbor"] .topbar {
      background: #204f61;
      border-bottom-color: #83c0cc;
    }

    body[data-theme="ember"] .topbar {
      background: #7a3e2d;
      border-bottom-color: #d99a78;
    }

    .title-group {
      min-width: 0;
    }

    h1 {
      margin: 0;
      font-size: 23px;
      line-height: 1.2;
      font-weight: 720;
    }

    .subtitle {
      margin: 4px 0 0;
      color: #bfd4dc;
      font-size: 13px;
    }

    .status-pill {
      min-width: 176px;
      min-height: 38px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.10);
      border: 1px solid rgba(255, 255, 255, 0.20);
      color: #ffffff;
      font-size: 13px;
      white-space: nowrap;
    }

    .topbar-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      flex-wrap: wrap;
      gap: 8px;
    }

    .topbar-button {
      min-height: 34px;
      padding: 0 11px;
      border-radius: 999px;
      border: 1px solid rgba(255, 255, 255, 0.28);
      background: rgba(255, 255, 255, 0.12);
      color: #ffffff;
      font-size: 12px;
    }

    .topbar-button.danger {
      border-color: rgba(255, 100, 100, 0.55);
      background: rgba(180, 40, 40, 0.35);
    }
    .topbar-button.danger:hover {
      background: rgba(200, 50, 50, 0.6);
    }

    .shell {
      width: 100%;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 18px;
      padding: 18px;
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }

    .connection {
      display: none;
      position: fixed;
      top: 94px;
      right: 18px;
      z-index: 22;
      width: min(380px, calc(100vw - 36px));
      max-height: calc(100vh - 112px);
      overflow: hidden;
      overflow-y: auto;
    }

    body.connection-open .connection {
      display: block;
    }

    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--surface-2);
    }

    .panel-head h2,
    .section-head h2 {
      margin: 0;
      font-size: 15px;
      line-height: 1.2;
      font-weight: 720;
    }

    form {
      display: grid;
      gap: 13px;
      padding: 16px 18px 18px;
    }

    label {
      display: grid;
      gap: 6px;
      color: var(--ink);
      font-size: 12px;
      font-weight: 680;
    }

    input, select {
      width: 100%;
      min-height: 40px;
      padding: 9px 10px;
      border: 1px solid #bfccd4;
      border-radius: 6px;
      background: var(--surface);
      color: var(--ink);
      outline: none;
    }

    input:focus, select:focus {
      border-color: var(--brand);
      box-shadow: 0 0 0 3px rgba(18, 110, 130, 0.14);
    }

    .row-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .check-row {
      min-height: 34px;
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      align-items: center;
      gap: 9px;
      color: var(--ink);
      font-size: 12px;
      font-weight: 650;
    }

    .check-row input {
      width: 18px;
      min-height: 18px;
      padding: 0;
      accent-color: var(--brand);
    }

    .actions {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr 42px;
      gap: 9px;
      margin-top: 2px;
    }

    button {
      min-height: 42px;
      border: 0;
      border-radius: 6px;
      padding: 9px 13px;
      cursor: pointer;
      font-weight: 720;
      color: var(--ink);
      background: #dfe8ed;
    }

    button.primary {
      background: var(--brand);
      color: var(--brand-ink);
    }

    button.ghost {
      background: var(--surface-2);
      color: var(--ink);
      border: 1px solid #c9d5dc;
    }

    button.icon {
      padding: 0;
      font-size: 19px;
      line-height: 1;
    }

    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }

    .dashboard {
      display: grid;
      gap: 18px;
      min-width: 0;
    }

    .dashboard-toolbar {
      display: none;
      flex-direction: column;
      align-items: stretch;
      gap: 12px;
      padding: 14px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }

    body.connected .dashboard-toolbar {
      display: flex;
    }

    .toolbar-controls {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      flex: 1;
      min-width: 0;
    }

    .custom-date-field[hidden] {
      display: none;
    }

    .toolbar-actions {
      display: flex;
      align-items: end;
      flex-wrap: wrap;
      gap: 8px;
    }

    .toolbar-actions button {
      white-space: nowrap;
    }

    /* Collapsible control groups (clean TV view) */
    .topbar-actions { position: relative; }
    .collapse-bar {
      display: flex; align-items: center; flex-wrap: wrap; gap: 10px;
    }
    .collapse-toggle {
      display: inline-flex; align-items: center; gap: 6px;
      background: var(--surface2); color: var(--ink);
      border: 1px solid var(--line); border-radius: 8px;
      padding: 6px 12px; font-size: 13px; font-weight: 700; cursor: pointer;
    }
    .collapse-toggle:hover { border-color: var(--brand); }
    .collapse-toggle .caret { font-size: 11px; transition: transform .2s ease; }
    .collapse-toggle[aria-expanded="true"] .caret { transform: rotate(90deg); }
    .collapsible {
      overflow: hidden; max-height: 0; opacity: 0;
      transition: max-height .25s ease, opacity .2s ease;
    }
    .collapsible.open { max-height: 1200px; opacity: 1; }
    .account-menu {
      position: absolute; top: calc(100% + 8px); right: 0; z-index: 60;
      display: flex; flex-direction: column; gap: 6px;
      background: var(--surface); border: 1px solid var(--line);
      border-radius: 10px; padding: 10px; box-shadow: var(--shadow);
      min-width: 170px;
    }
    .account-menu .topbar-button {
      width: 100%; justify-content: center;
      color: var(--ink);
      background: var(--surface-2);
      border: 1px solid var(--line);
    }
    .account-menu .topbar-button:hover { background: var(--line); }
    .account-menu .topbar-button.danger {
      color: var(--red);
      border-color: rgba(200, 60, 60, 0.5);
      background: rgba(200, 60, 60, 0.08);
    }
    .account-menu .topbar-button.danger:hover { background: rgba(200, 60, 60, 0.16); }

    .automation-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 12px;
      padding: 14px;
    }

    .automation-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      padding: 0 14px 14px;
    }

    .automation-actions button {
      min-width: 120px;
    }

    .automation-status {
      font-size: 12px;
      color: var(--muted);
      font-weight: 720;
      white-space: pre-wrap;
    }

    .snapshot-controls {
      display: flex;
      align-items: end;
      flex-wrap: wrap;
      gap: 10px;
      padding: 14px 16px 16px;
    }

    .snapshot-controls label {
      min-width: 150px;
    }

    .snapshot-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
      padding: 0 16px 16px;
    }

    .snapshot-cell {
      min-height: 88px;
      display: grid;
      gap: 8px;
      align-content: space-between;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }

    .snapshot-cell span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 760;
    }

    .snapshot-cell strong {
      font-size: 24px;
      line-height: 1.05;
    }

    .snapshot-cell small {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
    }

    .snapshot-header {
      grid-column: 1 / -1;
      font-size: 12px;
      font-weight: 700;
      color: var(--brand);
      padding: 2px 0 6px;
      border-bottom: 1px solid var(--line);
      letter-spacing: 0.02em;
    }

    .snapshot-empty {
      grid-column: 1 / -1;
      background: transparent;
      border: 1.5px dashed var(--line);
    }

    .snap-range-tabs {
      display: flex;
      gap: 4px;
    }

    .snap-tab {
      padding: 4px 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: transparent;
      color: var(--ink);
      font-size: 12px;
      cursor: pointer;
    }

    .snap-tab.active {
      background: var(--brand);
      color: var(--brandInk, #fff);
      border-color: var(--brand);
    }

    .snap-btn-group {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    .snap-auto-label {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--muted);
      cursor: pointer;
    }

    .snapshot-cell[data-trend="good"] { border-left: 3px solid var(--green); }
    .snapshot-cell[data-trend="bad"]  { border-left: 3px solid var(--red); }
    .snapshot-cell[data-trend="neutral"] { border-left: 3px solid var(--line); }

    .snap-badge {
      display: inline-block;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.5;
    }

    .snap-badge.good { background: rgba(46, 158, 107, 0.15); color: var(--green); }
    .snap-badge.bad  { background: rgba(192, 55, 59, 0.15);  color: var(--red); }
    .snap-badge.neutral { background: var(--surface2); color: var(--muted); }

    .snap-bars { margin-top: 4px; display: flex; flex-direction: column; gap: 3px; }

    .snap-bar-row {
      display: grid;
      grid-template-columns: 34px 1fr 36px;
      align-items: center;
      gap: 4px;
      font-size: 10px;
      color: var(--muted);
    }

    .snap-bar-track {
      height: 5px;
      background: var(--surface2);
      border-radius: 999px;
      overflow: hidden;
    }

    .snap-bar {
      height: 100%;
      border-radius: 999px;
      background: var(--brand);
      transition: width 0.4s ease;
    }

    .sparkline { display: block; overflow: visible; }

    .sla-gauge-svg { overflow: visible; }

    .snap-panel-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    .snap-panel-table th {
      text-align: left;
      padding: 6px 8px;
      border-bottom: 2px solid var(--line);
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
    }

    .snap-panel-table td {
      padding: 7px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
    }

    .snap-panel-table tr:last-child td { border-bottom: none; }

    .snap-panel-annotation {
      font-size: 11px;
      color: var(--muted);
      cursor: pointer;
      border: none;
      background: transparent;
      padding: 0;
      text-decoration: underline dotted;
    }

    .snap-panel-annotation:hover { color: var(--brand); }

    .automation-summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 14px 16px 16px;
    }

    .automation-summary p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      font-weight: 650;
    }

    .automation-summary button {
      white-space: nowrap;
    }

    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 22px;
      background: rgba(9, 23, 31, 0.54);
    }

    .modal-backdrop.open {
      display: flex;
    }

    .modal-panel {
      width: min(920px, 100%);
      max-height: min(86vh, 780px);
      overflow: auto;
      background: var(--surface);
      color: var(--ink);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: 0 28px 70px rgba(7, 18, 24, 0.26);
    }

    .modal-head {
      position: sticky;
      top: 0;
      z-index: 1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      background: var(--surface-2);
      border-bottom: 1px solid var(--line);
    }

    .modal-head h2 {
      margin: 0;
      font-size: 16px;
    }

    .modal-close {
      width: 36px;
      height: 36px;
      min-height: 36px;
      border-radius: 8px;
    }

    .management-grid {
      display: none;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
      gap: 12px;
      align-items: stretch;
    }

    body.connected .management-grid {
      display: grid;
    }

    .management-card {
      min-width: 0;
      min-height: 246px;
      padding: 16px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow: hidden;
    }

    .management-card header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
    }

    .management-card h2,
    .management-card h3 {
      margin: 0;
      font-size: 14px;
      line-height: 1.25;
      font-weight: 780;
    }

    .management-card .card-meta {
      color: var(--muted);
      font-size: 12px;
      font-weight: 680;
      text-align: right;
      overflow-wrap: anywhere;
    }

    .brand-card {
      color: #ffffff;
      background:
        linear-gradient(135deg, rgba(11, 32, 42, 0.98), rgba(18, 110, 130, 0.96)),
        var(--brand);
      border-color: rgba(255, 255, 255, 0.16);
    }

    .brand-top {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }

    .networker-mark {
      width: 74px;
      height: 74px;
      box-sizing: border-box;
      flex: 0 0 auto;
      border-radius: 8px;
      background: #f3fbff;
      background-image: url("__NETWORKER_LOGO_SRC__");
      background-position: center;
      background-repeat: no-repeat;
      background-size: contain;
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.42);
      overflow: hidden;
      padding: 3px;
    }

    .networker-mark img {
      width: 100%;
      height: 100%;
      display: block;
      object-fit: contain;
    }

    .brand-copy {
      min-width: 0;
    }

    .brand-copy strong {
      display: block;
      font-size: 18px;
      line-height: 1.15;
      font-weight: 820;
      overflow-wrap: anywhere;
    }

    .brand-copy span {
      display: block;
      margin-top: 3px;
      color: rgba(255, 255, 255, 0.78);
      font-size: 12px;
      font-weight: 720;
    }

    .connection-line {
      min-height: 42px;
      display: flex;
      align-items: center;
      gap: 9px;
      padding: 9px 11px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.12);
      color: #ffffff;
      font-size: 13px;
      font-weight: 760;
      min-width: 0;
      overflow-wrap: anywhere;
    }

    .connection-dot {
      width: 12px;
      height: 12px;
      flex: 0 0 auto;
      border-radius: 999px;
      background: #cbd5dc;
      box-shadow: 0 0 0 4px rgba(203, 213, 220, 0.18);
    }

    .connection-line.ok .connection-dot {
      background: #5ee089;
      box-shadow: 0 0 0 4px rgba(94, 224, 137, 0.18);
    }

    .connection-line.warn .connection-dot {
      background: #f6c955;
      box-shadow: 0 0 0 4px rgba(246, 201, 85, 0.18);
    }

    .connection-line.bad .connection-dot {
      background: #ff7480;
      box-shadow: 0 0 0 4px rgba(255, 116, 128, 0.18);
    }

    .brand-details,
    .summary-list {
      display: grid;
      gap: 9px;
      margin-top: auto;
    }

    .brand-detail,
    .summary-row {
      display: grid;
      grid-template-columns: minmax(88px, auto) minmax(0, 1fr);
      align-items: center;
      gap: 10px;
      min-height: 30px;
      font-size: 12px;
    }

    .brand-detail span,
    .summary-row span {
      color: var(--muted);
      font-weight: 700;
    }

    .brand-card .brand-detail span {
      color: rgba(255, 255, 255, 0.68);
    }

    .brand-detail strong,
    .summary-row strong {
      min-width: 0;
      font-size: 12px;
      text-align: right;
      overflow-wrap: anywhere;
    }

    .brand-signature {
      padding-top: 12px;
      border-top: 1px solid rgba(255, 255, 255, 0.18);
      display: grid;
      gap: 2px;
      color: rgba(255, 255, 255, 0.82);
      font-size: 11px;
      line-height: 1.25;
      font-weight: 650;
    }

    .brand-signature strong {
      color: #ffffff;
      font-size: 12px;
      font-weight: 820;
    }

    .donut-layout {
      display: grid;
      grid-template-columns: minmax(124px, 0.9fr) minmax(132px, 1fr);
      align-items: center;
      gap: 12px;
      margin-top: auto;
      min-width: 0;
    }

    .donut-chart {
      position: relative;
      width: min(172px, 100%);
      justify-self: center;
      aspect-ratio: 1;
      border-radius: 50%;
      background: var(--donut-bg, conic-gradient(#d8e3e8 0 360deg));
      box-shadow: inset 0 0 0 1px var(--line);
    }

    .donut-chart::after {
      content: "";
      position: absolute;
      inset: clamp(22px, 17%, 30px);
      border-radius: 50%;
      background: var(--surface);
      border: 1px solid var(--line);
    }

    .donut-center {
      position: absolute;
      inset: 0;
      z-index: 1;
      display: grid;
      place-items: center;
      text-align: center;
      pointer-events: none;
    }

    .donut-center strong {
      display: block;
      font-size: clamp(22px, 17%, 28px);
      line-height: 1;
      font-weight: 850;
    }

    .donut-center span {
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 780;
      text-transform: uppercase;
    }

    .legend-list {
      display: grid;
      gap: 9px;
      min-width: 0;
    }

    .legend-item {
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr) auto;
      align-items: center;
      gap: 7px;
      color: var(--ink);
      font-size: 12px;
      font-weight: 720;
      min-width: 0;
    }

    .legend-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--dot);
    }

    .legend-item span:nth-child(2) {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .legend-item strong {
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }

    .bar-chart {
      display: grid;
      gap: 12px;
      margin-top: auto;
    }

    .bar-row {
      display: grid;
      grid-template-columns: minmax(82px, 0.65fr) minmax(96px, 1fr) minmax(44px, auto);
      align-items: center;
      gap: 10px;
      min-height: 26px;
      font-size: 12px;
      font-weight: 720;
      min-width: 0;
    }

    .bar-row > span:first-child {
      min-width: 0;
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .bar-track {
      height: 12px;
      border-radius: 999px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      overflow: hidden;
    }

    .bar-fill {
      display: block;
      height: 100%;
      min-width: 2px;
      width: var(--bar-width);
      background: var(--bar-color);
      border-radius: inherit;
    }

    .bar-value {
      text-align: right;
      font-weight: 820;
      white-space: nowrap;
    }

    .summary-band {
      display: grid;
      gap: 7px;
      padding: 12px;
      border-radius: 8px;
      background: var(--surface-2);
      border: 1px solid var(--line);
    }

    .summary-band strong {
      font-size: 24px;
      line-height: 1;
      font-weight: 850;
    }

    .summary-band span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 720;
    }

    .chart-empty {
      display: grid;
      place-items: center;
      min-height: 150px;
      color: var(--muted);
      text-align: center;
      font-size: 13px;
      font-weight: 720;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: var(--surface-2);
    }

    .metric-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
    }

    .metric {
      min-height: 104px;
      padding: 14px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      display: grid;
      align-content: space-between;
      gap: 10px;
    }

    .metric span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 720;
    }

    .metric strong {
      min-height: 36px;
      display: block;
      font-size: 32px;
      line-height: 1.1;
      font-weight: 780;
    }

    .metric[data-tone="green"] strong {
      color: var(--green);
    }

    .metric[data-tone="red"] strong {
      color: var(--red);
    }

    .metric[data-tone="amber"] strong {
      color: var(--amber);
    }

    .metric[data-tone="blue"] strong {
      color: var(--blue);
    }

    .section {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
      min-width: 0;
    }

    .section-head {
      min-height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      background: var(--surface-2);
      border-bottom: 1px solid var(--line);
    }

    .section-head .meta {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .source-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }

    .source-chip {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      min-height: 28px;
      padding: 5px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid #cbd7dd;
      color: #34434b;
      background: #f6fafb;
      overflow-wrap: anywhere;
    }

    .health-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(160px, 1fr));
      gap: 12px;
      padding: 14px 16px 16px;
    }

    .health-item {
      min-height: 92px;
      display: grid;
      gap: 9px;
      align-content: space-between;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }

    .health-item span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 760;
    }

    .health-item strong {
      min-width: 0;
      font-size: 20px;
      line-height: 1.1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .health-item small {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }

    .health-item.ok strong {
      color: var(--green);
    }

    .health-item.warn strong {
      color: var(--amber);
    }

    .health-item.bad strong {
      color: var(--red);
    }

    .health-meter {
      height: 9px;
      overflow: hidden;
      border-radius: 999px;
      background: var(--surface-2);
      border: 1px solid var(--line);
    }

    .health-meter-fill {
      display: block;
      width: var(--meter-width);
      height: 100%;
      border-radius: inherit;
      background: var(--meter-color);
    }

    .source-chip.ok {
      border-color: rgba(24, 118, 74, 0.35);
      color: var(--green);
      background: rgba(24, 118, 74, 0.08);
    }

    .source-chip.bad {
      border-color: rgba(189, 43, 58, 0.28);
      color: var(--red);
      background: rgba(189, 43, 58, 0.08);
    }

    .notice {
      display: none;
      margin: 0;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      color: #593f00;
      background: #fff6df;
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .notice.show {
      display: block;
    }

    .tabs {
      display: flex;
      gap: 6px;
      padding: 10px 16px 0;
      background: var(--surface);
      border-bottom: 1px solid var(--line);
      overflow-x: auto;
    }

    .tab {
      min-height: 38px;
      padding: 8px 12px;
      border-radius: 6px 6px 0 0;
      border: 1px solid transparent;
      border-bottom: 0;
      background: transparent;
      color: #43525b;
      white-space: nowrap;
    }

    .tab.active {
      background: #eef6f7;
      border-color: #c7dadd;
      color: #0d5f71;
    }

    .table-wrap {
      width: 100%;
      overflow-x: auto;
      background: var(--surface);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
    }

    th, td {
      padding: 11px 12px;
      border-bottom: 1px solid #e5edf1;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
      line-height: 1.35;
    }

    th {
      color: var(--muted);
      background: var(--surface-2);
      font-size: 12px;
      font-weight: 760;
    }

    tbody tr:hover {
      background: var(--surface-2);
    }

    .cell-muted {
      color: var(--muted);
    }

    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 760;
      background: #edf2f5;
      color: #35434b;
      white-space: nowrap;
    }

    .badge.success {
      background: rgba(24, 118, 74, 0.10);
      color: var(--green);
    }

    .badge.failed {
      background: rgba(189, 43, 58, 0.10);
      color: var(--red);
    }

    .badge.running {
      background: rgba(36, 87, 166, 0.10);
      color: var(--blue);
    }

    .badge.warning {
      background: rgba(169, 104, 0, 0.12);
      color: var(--amber);
    }

    .empty {
      min-height: 210px;
      display: grid;
      place-items: center;
      padding: 28px;
      color: var(--muted);
      text-align: center;
      font-size: 14px;
    }

    .hidden {
      display: none;
    }

    @media (max-width: 1180px) {
      .management-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .metric-grid {
        grid-template-columns: repeat(3, minmax(150px, 1fr));
      }
    }

    @media (max-width: 860px) {
      .topbar {
        align-items: flex-start;
        flex-direction: column;
      }

      .topbar-brand {
        align-items: flex-start;
      }

      .status-pill {
        width: 100%;
      }

      .topbar-actions {
        width: 100%;
        justify-content: flex-start;
      }

      .shell {
        grid-template-columns: 1fr;
        padding: 12px;
      }

      .connection {
        position: static;
      }

      .metric-grid {
        grid-template-columns: repeat(2, minmax(130px, 1fr));
      }

      .dashboard-toolbar {
        align-items: stretch;
        flex-direction: column;
      }

      .toolbar-controls {
        grid-template-columns: repeat(2, minmax(130px, 1fr));
      }

      .management-grid {
        grid-template-columns: 1fr;
      }

      .donut-layout {
        grid-template-columns: 150px minmax(0, 1fr);
      }

      .donut-chart {
        width: 150px;
      }
    }

    @media (max-width: 540px) {
      .topbar {
        padding: 16px;
      }

      .connection {
        top: 150px;
      }

      h1 {
        font-size: 20px;
      }

      .row-2,
      .actions,
      .metric-grid,
      .donut-layout,
      .toolbar-controls,
      .automation-grid,
      .snapshot-grid {
        grid-template-columns: 1fr;
      }

      .automation-summary {
        align-items: stretch;
        flex-direction: column;
      }

      button.icon {
        min-height: 38px;
      }

      table {
        min-width: 680px;
      }
    }

    /* ── Timeline (Gantt) ────────────────────────────────────── */
    .timeline-wrap {
      overflow-x: auto;
      padding: 4px 0 12px;
    }
    .timeline-svg-container {
      min-width: 700px;
    }
    .tl-axis-label {
      font: 11px/1 system-ui, sans-serif;
      fill: var(--muted);
    }
    .tl-client-label {
      font: 12px/1 system-ui, sans-serif;
      fill: var(--ink);
    }
    .tl-bar {
      rx: 3;
      cursor: pointer;
      opacity: 0.88;
    }
    .tl-bar:hover { opacity: 1; }
    .tl-bar.success { fill: var(--green); }
    .tl-bar.failed  { fill: var(--red); }
    .tl-bar.running { fill: var(--blue); }
    .tl-bar.warning { fill: var(--amber); }
    .tl-bar.unknown { fill: var(--muted); }
    .tl-tooltip {
      position: fixed;
      background: var(--ink);
      color: var(--surface);
      font: 12px/1.5 system-ui, sans-serif;
      padding: 7px 10px;
      border-radius: 6px;
      pointer-events: none;
      z-index: 9999;
      max-width: 320px;
      white-space: pre-wrap;
      display: none;
    }

    /* ── Heatmap ─────────────────────────────────────────────── */
    .heatmap-wrap {
      padding: 8px 0;
    }
    .heatmap-legend {
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      margin-bottom: 12px;
      font-size: 12px;
      color: var(--muted);
      align-items: center;
    }
    .heatmap-legend-dot {
      width: 12px;
      height: 12px;
      border-radius: 3px;
      display: inline-block;
      margin-right: 4px;
      vertical-align: middle;
    }
    .heatmap-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .heatmap-cell {
      width: 36px;
      height: 36px;
      border-radius: 5px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 10px;
      font-weight: 600;
      color: #fff;
      position: relative;
      transition: transform 0.12s;
    }
    .heatmap-cell:hover { transform: scale(1.18); z-index: 2; }
    .heatmap-cell.success { background: var(--green); }
    .heatmap-cell.failed  { background: var(--red); }
    .heatmap-cell.running { background: var(--blue); }
    .heatmap-cell.warning { background: var(--amber); }
    .heatmap-cell.none    { background: var(--line); color: var(--muted); }
    .heatmap-tooltip {
      position: fixed;
      background: var(--ink);
      color: var(--surface);
      font: 12px/1.5 system-ui, sans-serif;
      padding: 7px 10px;
      border-radius: 6px;
      pointer-events: none;
      z-index: 9999;
      max-width: 280px;
      white-space: pre-wrap;
      display: none;
    }

    /* ── Multi-server cards ──────────────────────────────────── */
    .server-cards {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 14px;
      margin-top: 4px;
    }
    .server-card {
      background: var(--surface2);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px 16px;
    }
    .server-card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 8px;
    }
    .server-card-host {
      font-size: 13px;
      font-weight: 600;
      color: var(--ink);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 160px;
    }
    .server-card-badge {
      font-size: 11px;
      font-weight: 600;
      padding: 2px 7px;
      border-radius: 99px;
    }
    .server-card-badge.ok     { background: #d1f0e0; color: var(--green); }
    .server-card-badge.warn   { background: #fef3cd; color: var(--amber); }
    .server-card-badge.bad    { background: #fde2e4; color: var(--red); }
    .server-card-badge.load   { background: var(--line); color: var(--muted); }
    .server-card-stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .server-card-stats strong { color: var(--ink); }
    .server-card-remove {
      margin-top: 10px;
      font-size: 11px;
      color: var(--muted);
      cursor: pointer;
      background: none;
      border: none;
      padding: 0;
      text-decoration: underline;
    }
    .server-card-remove:hover { color: var(--red); }

    /* ── Add-server modal ────────────────────────────────────── */
    .add-server-form {
      display: grid;
      gap: 10px;
    }
    .add-server-form label {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 13px;
      color: var(--muted);
    }
    .add-server-form input, .add-server-form select {
      font-size: 14px;
    }
    .add-server-actions {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
      margin-top: 4px;
    }

    /* ── Share modal ─────────────────────────────────────────── */
    .share-url-row {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-top: 8px;
    }
    .share-url-input {
      flex: 1;
      font-size: 13px;
      font-family: monospace;
      padding: 7px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface2);
      color: var(--ink);
    }
    .share-info {
      font-size: 12px;
      color: var(--muted);
      margin-top: 8px;
      line-height: 1.5;
    }
    .share-token-actions {
      display: flex;
      gap: 8px;
      margin-top: 12px;
    }

    /* ── Connection Profiles ─────────────────────────────────── */
    .profile-bar {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
    }
    .profile-bar select {
      flex: 1;
      font-size: 13px;
    }
    .profile-bar button {
      font-size: 12px;
      padding: 5px 10px;
    }

    /* ── Collapsed connection form ───────────────────────────── */
    .conn-collapsed-bar {
      display: none;
      align-items: center;
      gap: 10px;
      padding: 8px 16px;
      background: var(--surface2);
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      color: var(--muted);
    }
    .conn-collapsed-bar strong { color: var(--ink); }
    body.connected .conn-collapsed-bar { display: flex; }
    body.connected #connectionPanel { display: none; }
    body.connected.connection-open #connectionPanel { display: block; }
    body.connected.connection-open .conn-collapsed-bar { display: none; }

    /* ── Table pagination ────────────────────────────────────── */
    .pagination-bar {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 0 2px;
      font-size: 13px;
      color: var(--muted);
    }
    .pagination-bar button {
      font-size: 12px;
      padding: 4px 12px;
    }

    /* ── Job detail drawer ───────────────────────────────────── */
    .detail-drawer {
      position: fixed;
      top: 0; right: -420px;
      width: 400px;
      height: 100vh;
      background: var(--surface);
      border-left: 1px solid var(--line);
      box-shadow: -4px 0 24px rgba(0,0,0,0.08);
      z-index: 10000;
      display: flex;
      flex-direction: column;
      transition: right 0.22s ease;
      overflow: hidden;
    }
    .detail-drawer.open { right: 0; }
    .detail-drawer-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
    }
    .detail-drawer-head h3 {
      font-size: 15px;
      font-weight: 600;
      color: var(--ink);
      margin: 0;
    }
    .detail-drawer-close {
      background: none;
      border: none;
      font-size: 18px;
      cursor: pointer;
      color: var(--muted);
      padding: 0 4px;
      line-height: 1;
    }
    .detail-drawer-close:hover { color: var(--ink); }
    .detail-drawer-body {
      overflow-y: auto;
      padding: 16px 18px;
      flex: 1;
    }
    .detail-field {
      margin-bottom: 14px;
    }
    .detail-field .detail-label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      margin-bottom: 3px;
    }
    .detail-field .detail-value {
      font-size: 13px;
      color: var(--ink);
      word-break: break-word;
      white-space: pre-wrap;
    }
    .detail-copy-btn {
      font-size: 11px;
      padding: 2px 8px;
      margin-top: 6px;
    }
    .drawer-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.18);
      z-index: 9999;
    }
    .drawer-overlay.open { display: block; }

    /* ── What changed toast ──────────────────────────────────── */
    .change-toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: var(--ink);
      color: var(--surface);
      font-size: 13px;
      padding: 10px 16px;
      border-radius: 8px;
      z-index: 11000;
      max-width: 340px;
      box-shadow: 0 4px 18px rgba(0,0,0,0.18);
      display: none;
      line-height: 1.5;
    }
    .change-toast.show { display: block; animation: slideUp 0.2s ease; }
    @keyframes slideUp {
      from { transform: translateY(12px); opacity: 0; }
      to   { transform: translateY(0);    opacity: 1; }
    }
    .change-toast.ok   { border-left: 4px solid var(--green); }
    .change-toast.warn { border-left: 4px solid var(--amber); }
    .change-toast.bad  { border-left: 4px solid var(--red); }

    /* ── Table row click hint ────────────────────────────────── */
    tbody tr { cursor: pointer; }
    tbody tr:hover td { background: var(--surface2); }
  </style>
</head>
<body class="connection-open">
  <header class="topbar">
    <div class="topbar-brand">
      <div class="topbar-logo" aria-hidden="true">
        <img src="__NETWORKER_LOGO_SRC__" alt="">
      </div>
      <div class="title-group">
        <h1>Backup & Recovery Dashboard</h1>
        <p class="subtitle">Dell NetWorker REST API over HTTPS</p>
      </div>
    </div>
    <div class="topbar-actions">
      <div id="topStatus" class="status-pill">Not connected</div>
      <button class="collapse-toggle" type="button" data-toggle-target="accountMenu" aria-expanded="false">
        <span class="caret">&#9656;</span> Account
      </button>
      <div id="accountMenu" class="account-menu collapsible">
        <button id="showConnectionBtn" class="topbar-button" type="button">Connection</button>
        <button id="addServerBtn" class="topbar-button" type="button">+ Server</button>
        <button id="shareBtn" class="topbar-button hidden" type="button">Share</button>
        <button id="logoutBtn" class="topbar-button danger hidden" type="button">Logout</button>
        <button id="alertConfigBtn" class="topbar-button" type="button">Email</button>
      </div>
    </div>
  </header>

  <!-- Collapsed connection bar (visible when connected) -->
  <div id="connCollapsedBar" class="conn-collapsed-bar">
    <span>Connected to <strong id="collapsedHost">—</strong></span>
    <span id="collapsedRange" style="margin-left:4px"></span>
    <button id="collapsedEditBtn" class="ghost" type="button" style="margin-left:auto;font-size:12px;padding:4px 10px">Edit connection</button>
  </div>

  <main id="mainShell" class="shell">
    <aside id="connectionPanel" class="panel connection">
      <div class="panel-head">
        <h2>Connection</h2>
      </div>
      <!-- Profile bar -->
      <div class="profile-bar">
        <select id="profileSelect" aria-label="Saved connection profiles">
          <option value="">— Select saved profile —</option>
        </select>
        <button id="profileSaveBtn" class="ghost" type="button">Save</button>
        <button id="profileDeleteBtn" class="ghost" type="button">Delete</button>
      </div>
      <form id="connectionForm" autocomplete="off">
        <label>
          API / NWUI server IP/DNS
          <input id="restApiHost" name="restApiHost" placeholder="10.10.10.20" required autocomplete="off" spellcheck="false">
        </label>

        <label>
          API source
          <select id="apiMode" name="apiMode">
            <option value="auto" selected>Auto discover</option>
            <option value="nwui">NWUI API</option>
            <option value="rest">NetWorker REST API</option>
          </select>
        </label>

        <div class="row-2">
          <label>
            API / NWUI port
            <input id="restApiPort" name="restApiPort" value="9090" inputmode="numeric" autocomplete="off">
          </label>
          <label>
            API version
            <select id="apiVersion" name="apiVersion">
              <option value="auto" selected>Auto</option>
              <option value="v3">v3</option>
              <option value="v2">v2</option>
              <option value="v1">v1</option>
            </select>
          </label>
        </div>

        <label>
          Backup server IP/DNS
          <input id="backupServerHost" name="backupServerHost" placeholder="10.10.10.30" autocomplete="off" spellcheck="false">
        </label>

        <div class="row-2">
          <label>
            AuthC port
            <input id="backupServerPort" name="backupServerPort" value="9090" inputmode="numeric" autocomplete="off">
          </label>
          <label>
            Timeout seconds
            <input id="timeoutSeconds" name="timeoutSeconds" value="30" inputmode="numeric" autocomplete="off">
          </label>
        </div>

        <label>
          Report range
          <select id="reportRange" name="reportRange">
            <option value="24h" selected>Last 24 hours</option>
            <option value="7d">Last week</option>
            <option value="30d">Last month</option>
            <option value="custom">Custom dates</option>
          </select>
        </label>

        <div class="row-2 custom-date-field" data-custom-range hidden>
          <label>
            Start date
            <input id="customStartDate" name="customStartDate" placeholder="DD-MM-YYYY" autocomplete="off" inputmode="numeric">
          </label>
          <label>
            End date
            <input id="customEndDate" name="customEndDate" placeholder="DD-MM-YYYY" autocomplete="off" inputmode="numeric">
          </label>
        </div>

        <label>
          Username
          <input id="username" name="username" placeholder="Administrator" required autocomplete="off" spellcheck="false">
        </label>

        <label>
          Password
          <input id="password" name="password" type="password" required autocomplete="new-password">
        </label>

        <label class="check-row">
          <input id="useWmiHealth" name="useWmiHealth" type="checkbox" checked>
          <span>Use WMI for Windows CPU/RAM health</span>
        </label>

        <label>
          WMI username
          <input id="wmiUsername" name="wmiUsername" placeholder="DOMAIN\\svc_networker_health" autocomplete="off" spellcheck="false">
        </label>

        <label>
          WMI password
          <input id="wmiPassword" name="wmiPassword" type="password" autocomplete="new-password">
        </label>

        <label class="check-row">
          <input id="useAuthcHeader" name="useAuthcHeader" type="checkbox" checked>
          <span>Send X-NW-AUTHC-BASE-URL for backup server</span>
        </label>

        <label class="check-row">
          <input id="verifyTls" name="verifyTls" type="checkbox" checked>
          <span>Verify REST API TLS certificate</span>
        </label>

        <div class="actions">
          <button id="connectBtn" class="primary" type="submit">Connect</button>
          <button id="discoverBtn" class="ghost" type="button">Discover</button>
          <button id="refreshBtn" class="ghost" type="button">Refresh</button>
          <button id="clearBtn" class="ghost icon" type="button" title="Clear form">x</button>
        </div>
      </form>
    </aside>

    <section class="dashboard">
      <section id="dashboardToolbar" class="dashboard-toolbar" aria-label="Dashboard controls">
        <div class="collapse-bar">
          <button class="collapse-toggle" type="button" data-toggle-target="viewControls" aria-expanded="false">
            <span class="caret">&#9656;</span> View settings
          </button>
          <div class="toolbar-actions">
            <button id="manualRefreshBtn" class="ghost" type="button">Refresh now</button>
          </div>
        </div>
        <div id="viewControls" class="toolbar-controls collapsible">
          <label>
            Report range
            <select id="dashReportRange">
              <option value="24h" selected>Last 24 hours</option>
              <option value="7d">Last week</option>
              <option value="30d">Last month</option>
              <option value="custom">Custom dates</option>
            </select>
          </label>
          <label class="custom-date-field" data-custom-range hidden>
            Start date
            <input id="dashCustomStartDate" placeholder="DD-MM-YYYY" autocomplete="off" inputmode="numeric">
          </label>
          <label class="custom-date-field" data-custom-range hidden>
            End date
            <input id="dashCustomEndDate" placeholder="DD-MM-YYYY" autocomplete="off" inputmode="numeric">
          </label>
          <label>
            Auto-refresh
            <select id="autoRefreshMode">
              <option value="on" selected>On</option>
              <option value="off">Off</option>
            </select>
          </label>
          <label>
            Interval minutes
            <input id="refreshMinutes" value="5" inputmode="numeric" autocomplete="off">
          </label>
          <label>
            Theme
            <select id="themeSelect">
              <option value="default">Default</option>
              <option value="midnight">Midnight</option>
              <option value="graphite">Graphite</option>
              <option value="contrast">High contrast</option>
              <option value="ocean">Ocean</option>
              <option value="forest">Forest</option>
              <option value="ruby">Ruby</option>
              <option value="steel">Steel</option>
              <option value="arctic">Arctic</option>
              <option value="citrus">Citrus</option>
              <option value="harbor">Harbor</option>
              <option value="ember">Ember</option>
            </select>
          </label>
          <label>
            Export
            <button id="exportBtn" class="primary" type="button">Excel report</button>
          </label>
        </div>
      </section>

      <section class="management-grid" aria-label="Management dashboard">
        <article class="management-card brand-card">
          <div class="brand-top">
            <div class="networker-mark" aria-hidden="true">
              <img src="__NETWORKER_LOGO_SRC__" alt="">
            </div>
            <div class="brand-copy">
              <strong>DELL EMC NetWorker</strong>
              <span>Backup & Recovery Status</span>
            </div>
          </div>
          <div id="mgmtConnection" class="connection-line">
            <span class="connection-dot" aria-hidden="true"></span>
            <strong id="mgmtStatus">Not connected</strong>
          </div>
          <div class="brand-details">
            <div class="brand-detail">
              <span>API source</span>
              <strong id="mgmtApi">--</strong>
            </div>
            <div class="brand-detail">
              <span>Backup server</span>
              <strong id="mgmtBackupServer">--</strong>
            </div>
            <div class="brand-detail">
              <span>Updated</span>
              <strong id="mgmtUpdated">--</strong>
            </div>
          </div>
          <div class="brand-signature">
            <span>Maintained &amp; developed by</span>
            <strong>SHAIKH SHOAIB</strong>
            <span>Sr. Advisor Delivery Specialist</span>
            <span>DELL Technologies</span>
          </div>
        </article>

        <article class="management-card">
          <header>
            <h2>Activity Mix</h2>
            <span id="mgmtRange" class="card-meta">--</span>
          </header>
          <div id="mgmtDonut" class="chart-empty">No backup data yet</div>
        </article>

        <article class="management-card">
          <header>
            <h2>Backup SLA</h2>
            <span id="mgmtSlaMeta" class="card-meta">Jobs ran</span>
          </header>
          <div id="mgmtSlaPie" class="chart-empty">No SLA data yet</div>
        </article>

        <article class="management-card">
          <header>
            <h2>Management Overview</h2>
            <span class="card-meta">Live API</span>
          </header>
          <div id="mgmtBars" class="chart-empty">No management data yet</div>
        </article>

        <article class="management-card">
          <header>
            <h2>Recovery Health</h2>
            <span class="card-meta">Restores</span>
          </header>
          <div id="mgmtRestorePanel" class="summary-list">
            <div class="summary-band">
              <strong>--</strong>
              <span>Restore jobs in selected range</span>
            </div>
          </div>
        </article>

        <article class="management-card">
          <header>
            <h2>Clone Jobs</h2>
            <span class="card-meta">Actions</span>
          </header>
          <div id="mgmtClonePanel" class="summary-list">
            <div class="summary-band">
              <strong>--</strong>
              <span>Clone jobs in selected range</span>
            </div>
          </div>
        </article>
      </section>

      <section class="metric-grid" aria-label="Dashboard metrics">
        <div class="metric" data-tone="blue">
          <span>Clients</span>
          <strong id="metricClients">--</strong>
        </div>
        <div class="metric" data-tone="green">
          <span>Successful Jobs</span>
          <strong id="metricSuccess">--</strong>
        </div>
        <div class="metric" data-tone="red">
          <span>Failed Backups</span>
          <strong id="metricFailed">--</strong>
        </div>
        <div class="metric" data-tone="red">
          <span>Failed Restores</span>
          <strong id="metricFailedRestores">--</strong>
        </div>
        <div class="metric" data-tone="red">
          <span>Failed Clones</span>
          <strong id="metricFailedClones">--</strong>
        </div>
        <div class="metric" data-tone="blue">
          <span>Active Jobs</span>
          <strong id="metricActive">--</strong>
        </div>
        <div class="metric" data-tone="amber">
          <span>Recovery Jobs</span>
          <strong id="metricRecovery">--</strong>
        </div>
        <div class="metric" data-tone="amber">
          <span>Alerts</span>
          <strong id="metricAlerts">--</strong>
        </div>
      </section>

      <section class="section">
        <div class="section-head">
          <h2>NetWorker Server Health</h2>
          <span id="generatedAt" class="meta">Waiting for connection</span>
        </div>
        <p id="notice" class="notice"></p>
        <div id="healthGrid" class="health-grid">
          <div class="health-item">
            <span>Server status</span>
            <strong>Waiting</strong>
            <small>Connect to load server health.</small>
          </div>
          <div class="health-item">
            <span>CPU usage</span>
            <strong>--</strong>
            <small>Awaiting NetWorker health data.</small>
          </div>
          <div class="health-item">
            <span>Memory usage</span>
            <strong>--</strong>
            <small>Awaiting NetWorker health data.</small>
          </div>
          <div class="health-item">
            <span>Server Protection Job</span>
            <strong>--</strong>
            <small>No job data loaded.</small>
          </div>
        </div>
      </section>

      <section class="section" aria-label="Local snapshot growth">
        <div class="section-head">
          <h2>Local Snapshot Growth</h2>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span id="snapshotMeta" class="meta">No local snapshots loaded</span>
            <div id="slaGaugeInline"></div>
          </div>
        </div>
        <button class="collapse-toggle" type="button" data-toggle-target="snapshotControls" aria-expanded="false" style="margin:0 16px 4px">
          <span class="caret">&#9656;</span> Snapshots
        </button>
        <div id="snapshotControls" class="snapshot-controls collapsible">
          <div class="snap-range-tabs" id="snapRangeTabs">
            <button class="snap-tab active" data-range="7d" type="button">7 days</button>
            <button class="snap-tab" data-range="30d" type="button">30 days</button>
            <button class="snap-tab" data-range="90d" type="button">90 days</button>
          </div>
          <div class="snap-btn-group">
            <label class="snap-auto-label">
              <input type="checkbox" id="autoSnapshotToggle"> Auto-save daily
            </label>
            <button id="snapshotSaveBtn" class="primary" type="button" disabled>Save snapshot</button>
            <button id="snapshotCompareBtn" class="ghost" type="button">Compare growth</button>
            <button id="snapshotManageBtn" class="ghost" type="button">Manage</button>
            <button id="snapshotExportBtn" class="ghost" type="button">Export CSV</button>
          </div>
        </div>
        <div id="snapshotGrid" class="snapshot-grid">
          <div class="snapshot-cell snapshot-empty">
            <span>No comparison available</span>
            <strong style="font-size:15px">No local snapshots found.</strong>
            <small>Use <strong>Save snapshot</strong> after each connection to track growth over time.</small>
          </div>
        </div>
      </section>

      <div id="snapshotPanel" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="snapshotPanelTitle" aria-hidden="true">
        <div class="modal-panel" style="max-width:720px">
          <div class="modal-head">
            <h3 id="snapshotPanelTitle">Snapshot History</h3>
            <button id="snapshotPanelCloseBtn" class="modal-close" type="button" aria-label="Close">×</button>
          </div>
          <div id="snapshotPanelBody" style="padding:16px;overflow-y:auto;max-height:60vh"></div>
        </div>
      </div>

      <div id="alertAutomationModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="alertAutomationTitle" aria-hidden="true">
        <div class="modal-panel">
          <div class="modal-head">
            <h2 id="alertAutomationTitle">Email Alert Automation</h2>
            <span id="alertAutomationStatus" class="automation-status">Not scheduled</span>
            <button id="alertModalCloseBtn" class="ghost modal-close" type="button" aria-label="Close email automation popup">x</button>
          </div>
          <div class="automation-grid">
            <label>
              SMTP host
              <input id="smtpHost" placeholder="smtp.company.com" autocomplete="off" spellcheck="false">
            </label>
            <label>
              SMTP port
              <input id="smtpPort" value="587" inputmode="numeric" autocomplete="off">
            </label>
            <label>
              Security
              <select id="smtpSecurity">
                <option value="starttls" selected>STARTTLS</option>
                <option value="ssl">SSL/TLS</option>
                <option value="none">None</option>
              </select>
            </label>
            <label>
              Email type
              <select id="emailScheduleType">
                <option value="alert" selected>Alert check</option>
                <option value="daily_report">Daily backup/SLA report</option>
              </select>
            </label>
            <label>
              Alert interval minutes
              <input id="alertIntervalMinutes" value="60" inputmode="numeric" autocomplete="off">
            </label>
            <label>
              Daily report time
              <input id="dailyReportTime" value="08:00" placeholder="HH:MM" autocomplete="off" inputmode="numeric">
            </label>
            <label>
              SMTP username
              <input id="smtpUsername" autocomplete="off" spellcheck="false">
            </label>
            <label>
              SMTP password
              <input id="smtpPassword" type="password" autocomplete="new-password">
            </label>
            <label>
              From address
              <input id="smtpFrom" placeholder="networker-dashboard@company.com" autocomplete="off" spellcheck="false">
            </label>
            <label>
              To recipients
              <input id="smtpTo" placeholder="ops@company.com; backup@company.com" autocomplete="off" spellcheck="false">
            </label>
            <label>
              Trigger
              <select id="alertTrigger">
                <option value="critical" selected>Critical only</option>
                <option value="warning">Warnings and critical</option>
                <option value="all">Every scheduled check</option>
              </select>
            </label>
          </div>
          <div class="automation-actions">
            <button id="alertScheduleBtn" class="primary" type="button">Schedule selected report</button>
            <button id="emailSaveConfigBtn" class="ghost" type="button">Save configuration</button>
            <button id="alertTestBtn" class="ghost" type="button">Send test</button>
            <button id="alertStopBtn" class="ghost" type="button">Stop selected schedule</button>
          </div>
          <p class="automation-hint" style="margin:0 16px 12px;color:var(--muted);font-size:12px">
            SMTP settings are shared. <strong>Alert check</strong> and
            <strong>Daily backup/SLA report</strong> keep separate recipients and
            settings — switch <em>Email type</em> to edit each. Save configuration
            stores both independently and survives restarts.
          </p>
        </div>
      </div>

      <!-- Share modal -->
      <div id="shareModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="shareModalTitle" aria-hidden="true">
        <div class="modal-panel">
          <div class="modal-head">
            <h2 id="shareModalTitle">Share Read-Only View</h2>
            <button id="shareModalCloseBtn" class="ghost modal-close" type="button" aria-label="Close share popup">x</button>
          </div>
          <p class="share-info">Generate a read-only URL showing the current live dashboard. The link uses a token valid for 24 hours. No credentials are embedded in the URL.</p>
          <div id="shareTokenSection" class="hidden">
            <div class="share-url-row">
              <input id="shareUrlInput" class="share-url-input" readonly type="text" placeholder="Generating...">
              <button id="copyShareUrlBtn" class="primary" type="button">Copy</button>
            </div>
          </div>
          <div class="share-token-actions">
            <button id="generateShareTokenBtn" class="primary" type="button">Generate Link</button>
            <button id="revokeShareTokenBtn" class="ghost hidden" type="button">Revoke</button>
          </div>
          <p id="shareModalStatus" class="share-info"></p>
        </div>
      </div>

      <!-- Add-server modal -->
      <div id="addServerModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="addServerModalTitle" aria-hidden="true">
        <div class="modal-panel">
          <div class="modal-head">
            <h2 id="addServerModalTitle">Add NetWorker Server</h2>
            <button id="addServerModalCloseBtn" class="ghost modal-close" type="button" aria-label="Close add server popup">x</button>
          </div>
          <div class="add-server-form">
            <label>REST API Host<input id="asHost" placeholder="networker.corp.com" autocomplete="off" spellcheck="false"></label>
            <label>REST API Port<input id="asPort" value="9090" inputmode="numeric" autocomplete="off"></label>
            <label>Username<input id="asUsername" autocomplete="off" spellcheck="false"></label>
            <label>Password<input id="asPassword" type="password" autocomplete="new-password"></label>
            <label>API Mode
              <select id="asApiMode">
                <option value="auto">Auto</option>
                <option value="nwui">NWUI</option>
                <option value="rest">REST</option>
              </select>
            </label>
          </div>
          <div class="add-server-actions">
            <button id="addServerCancelBtn" class="ghost" type="button">Cancel</button>
            <button id="addServerConnectBtn" class="primary" type="button">Connect</button>
          </div>
          <p id="addServerStatus" class="share-info"></p>
        </div>
      </div>

      <!-- Tooltip elements -->
      <div id="tlTooltip" class="tl-tooltip"></div>
      <div id="hmTooltip" class="heatmap-tooltip"></div>

      <!-- Job detail drawer -->
      <div id="drawerOverlay" class="drawer-overlay"></div>
      <div id="jobDetailDrawer" class="detail-drawer" role="dialog" aria-label="Job details">
        <div class="detail-drawer-head">
          <h3 id="drawerTitle">Job Details</h3>
          <button id="drawerCloseBtn" class="detail-drawer-close" type="button" aria-label="Close">&#x2715;</button>
        </div>
        <div id="drawerBody" class="detail-drawer-body"></div>
      </div>

      <!-- What-changed toast -->
      <div id="changeToast" class="change-toast" role="status" aria-live="polite"></div>

      <section class="section">
        <div class="section-head">
          <h2 id="tableTitle">Recent Jobs</h2>
          <span id="tableMeta" class="meta">0 rows</span>
        </div>
        <div class="tabs" role="tablist">
          <button class="tab active" type="button" data-table="jobs">Recent Jobs</button>
          <button class="tab" type="button" data-table="failedJobs">Failed Jobs</button>
          <button class="tab" type="button" data-table="recovery">Restores</button>
          <button class="tab" type="button" data-table="cloneJobs">Clone Jobs</button>
          <button class="tab" type="button" data-table="logs">Logs</button>
          <button class="tab" type="button" data-table="alerts">Alerts</button>
          <button class="tab" type="button" data-table="clients">Clients</button>
          <button class="tab" type="button" data-table="timeline">Timeline</button>
          <button class="tab" type="button" data-table="heatmap">Heatmap</button>
        </div>
        <div id="emptyState" class="empty">Enter connection details and connect.</div>
        <div id="tableWrap" class="table-wrap hidden">
          <table>
            <thead id="tableHead"></thead>
            <tbody id="tableBody"></tbody>
          </table>
        </div>
        <div id="paginationBar" class="pagination-bar hidden">
          <span id="paginationMeta"></span>
          <button id="showMoreBtn" class="ghost" type="button">Show more</button>
          <button id="showAllBtn" class="ghost" type="button">Show all</button>
        </div>
        <div id="timelineWrap" class="timeline-wrap hidden"></div>
        <div id="heatmapWrap" class="heatmap-wrap hidden"></div>
      </section>

      <section id="multiServerSection" class="section hidden">
        <div class="section-head">
          <h2>Multi-Server Overview</h2>
          <span id="multiServerMeta" class="meta">0 servers</span>
        </div>
        <div id="serverCards" class="server-cards"></div>
      </section>
    </section>
  </main>

  <script>
    (function(){
      const _fetch = window.fetch;
      window.fetch = async function(...args){
        const resp = await _fetch.apply(this, args);
        try {
          const url = (args[0] && args[0].url) ? args[0].url : String(args[0] || "");
          if (resp.status === 401 && url.indexOf("/api/") !== -1) { location.reload(); }
        } catch (_e) {}
        return resp;
      };
    })();
    function initCollapsibles(){
      var toggles = document.querySelectorAll('[data-toggle-target]');
      for (var i = 0; i < toggles.length; i++){
        (function(btn){
          var panel = document.getElementById(btn.getAttribute('data-toggle-target'));
          if (!panel) return;
          var key = 'collapse:' + btn.getAttribute('data-toggle-target');
          var open = false;
          try { open = localStorage.getItem(key) === 'open'; } catch (_e) {}
          panel.classList.toggle('open', open);
          btn.setAttribute('aria-expanded', open ? 'true' : 'false');
          btn.addEventListener('click', function(){
            var now = !panel.classList.contains('open');
            panel.classList.toggle('open', now);
            btn.setAttribute('aria-expanded', now ? 'true' : 'false');
            try { localStorage.setItem(key, now ? 'open' : 'closed'); } catch (_e) {}
          });
        })(toggles[i]);
      }
    }
    initCollapsibles();
    const form = document.getElementById("connectionForm");
    const topStatus = document.getElementById("topStatus");
    const discoverBtn = document.getElementById("discoverBtn");
    const refreshBtn = document.getElementById("refreshBtn");
    const manualRefreshBtn = document.getElementById("manualRefreshBtn");
    const exportBtn = document.getElementById("exportBtn");
    const showConnectionBtn = document.getElementById("showConnectionBtn");
    const dashReportRange = document.getElementById("dashReportRange");
    const customStartDate = document.getElementById("customStartDate");
    const customEndDate = document.getElementById("customEndDate");
    const dashCustomStartDate = document.getElementById("dashCustomStartDate");
    const dashCustomEndDate = document.getElementById("dashCustomEndDate");
    const autoRefreshMode = document.getElementById("autoRefreshMode");
    const refreshMinutes = document.getElementById("refreshMinutes");
    const themeSelect = document.getElementById("themeSelect");
    const clearBtn = document.getElementById("clearBtn");
    const alertConfigBtn = document.getElementById("alertConfigBtn");
    const alertAutomationModal = document.getElementById("alertAutomationModal");
    const alertModalCloseBtn = document.getElementById("alertModalCloseBtn");
    const alertScheduleBtn = document.getElementById("alertScheduleBtn");
    const alertTestBtn = document.getElementById("alertTestBtn");
    const alertStopBtn = document.getElementById("alertStopBtn");
    const emailSaveConfigBtn = document.getElementById("emailSaveConfigBtn");
    const emailScheduleType = document.getElementById("emailScheduleType");
    const alertAutomationStatus = document.getElementById("alertAutomationStatus");
    const smtpSecurity = document.getElementById("smtpSecurity");
    const smtpPort = document.getElementById("smtpPort");
    const smtpUsername = document.getElementById("smtpUsername");
    const smtpPassword = document.getElementById("smtpPassword");
    const notice = document.getElementById("notice");
    const healthGrid = document.getElementById("healthGrid");
    const generatedAt = document.getElementById("generatedAt");
    const tableTitle = document.getElementById("tableTitle");
    const tableMeta = document.getElementById("tableMeta");
    const emptyState = document.getElementById("emptyState");
    const tableWrap = document.getElementById("tableWrap");
    const tableHead = document.getElementById("tableHead");
    const tableBody = document.getElementById("tableBody");
    const mgmtConnection = document.getElementById("mgmtConnection");
    const mgmtStatus = document.getElementById("mgmtStatus");
    const mgmtApi = document.getElementById("mgmtApi");
    const mgmtBackupServer = document.getElementById("mgmtBackupServer");
    const mgmtUpdated = document.getElementById("mgmtUpdated");
    const mgmtRange = document.getElementById("mgmtRange");
    const mgmtDonut = document.getElementById("mgmtDonut");
    const mgmtSlaPie = document.getElementById("mgmtSlaPie");
    const mgmtSlaMeta = document.getElementById("mgmtSlaMeta");
    const mgmtBars = document.getElementById("mgmtBars");
    const mgmtRestorePanel = document.getElementById("mgmtRestorePanel");
    const mgmtClonePanel = document.getElementById("mgmtClonePanel");
    const snapshotSaveBtn    = document.getElementById("snapshotSaveBtn");
    const snapshotCompareBtn = document.getElementById("snapshotCompareBtn");
    const snapshotMeta       = document.getElementById("snapshotMeta");
    const snapshotGrid       = document.getElementById("snapshotGrid");
    const snapshotManageBtn  = document.getElementById("snapshotManageBtn");
    const snapshotExportBtn  = document.getElementById("snapshotExportBtn");
    const snapshotPanel      = document.getElementById("snapshotPanel");
    const snapshotPanelClose = document.getElementById("snapshotPanelCloseBtn");
    const autoSnapshotToggle = document.getElementById("autoSnapshotToggle");
    let activeSnapshotRange  = "7d";
    let snapshotHistoryCache = null;
    const SERVER_HEALTH_REFRESH_MS = 60000;

    const metrics = {
      clients: document.getElementById("metricClients"),
      success: document.getElementById("metricSuccess"),
      failed: document.getElementById("metricFailed"),
      failedRestores: document.getElementById("metricFailedRestores"),
      failedClones: document.getElementById("metricFailedClones"),
      active: document.getElementById("metricActive"),
      recovery: document.getElementById("metricRecovery"),
      alerts: document.getElementById("metricAlerts"),
    };

    const tableDefs = {
      jobs: {
        title: "Recent Jobs",
        columns: [
          ["client", "Client"],
          ["name", "Job"],
          ["policy", "Policy"],
          ["status", "Status"],
          ["started", "Started"],
          ["duration", "Duration"],
          ["size", "Size"],
          ["message", "Message"],
        ],
      },
      failedJobs: {
        title: "Failed Jobs",
        columns: [
          ["client", "Client"],
          ["name", "Job"],
          ["policy", "Policy"],
          ["started", "Started"],
          ["message", "Message"],
        ],
      },
      recovery: {
        title: "Restores",
        columns: [
          ["client", "Client"],
          ["name", "Restore"],
          ["policy", "Policy"],
          ["status", "Status"],
          ["started", "Started"],
          ["duration", "Duration"],
          ["message", "Message"],
        ],
      },
      cloneJobs: {
        title: "Clone Jobs",
        columns: [
          ["client", "Client"],
          ["name", "Clone Job"],
          ["policy", "Policy"],
          ["status", "Status"],
          ["started", "Started"],
          ["duration", "Duration"],
          ["message", "Message"],
        ],
      },
      logs: {
        title: "Log",
        columns: [
          ["priority", "Priority"],
          ["time", "Time"],
          ["source", "Source"],
          ["category", "Category"],
          ["message", "Message"],
        ],
      },
      alerts: {
        title: "Alerts",
        columns: [
          ["severity", "Severity"],
          ["time", "Time"],
          ["message", "Message"],
          ["resource", "Resource"],
        ],
      },
      clients: {
        title: "Clients",
        columns: [
          ["hostname", "Hostname"],
          ["enabled", "Enabled"],
          ["backupType", "Backup Type"],
          ["saveSets", "Save Sets"],
          ["protectionGroups", "Protection Groups"],
        ],
      },
    };

    let latestDashboard = null;
    let activeTable = "jobs";
    let sessionId = null;
    let refreshTimer = null;
    let healthRefreshTimer = null;

    function text(value) {
      if (value === null || value === undefined || value === "") return "--";
      return String(value);
    }

    function escapeHtml(value) {
      return text(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function numberValue(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }

    function formatNumber(value) {
      try {
        return new Intl.NumberFormat().format(numberValue(value));
      } catch (error) {
        return String(numberValue(value));
      }
    }

    function formatDecimal(value, digits = 1) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "--";
      return new Intl.NumberFormat(undefined, {
        minimumFractionDigits: numeric % 1 === 0 ? 0 : digits,
        maximumFractionDigits: digits,
      }).format(numeric);
    }

    function memoryUsageValue(health) {
      const total = Number(health.ramTotalGb);
      const used = Number(health.ramUsedGb);
      const free = Number(health.ramFreeGb);
      if (Number.isFinite(total) && total > 0) {
        const usedValue = Number.isFinite(used) ? used : (Number.isFinite(free) ? Math.max(0, total - free) : NaN);
        if (Number.isFinite(usedValue)) {
          return `${formatDecimal(usedValue)} / ${formatDecimal(total)} GB`;
        }
        return `${formatDecimal(total)} GB total`;
      }
      const ram = health.ramUsagePercent;
      return ram === null || ram === undefined ? "--" : `${formatNumber(ram)}%`;
    }

    function memoryUsageDetail(health) {
      const percent = health.ramUsagePercent;
      const free = Number(health.ramFreeGb);
      if (Number.isFinite(free) && percent !== null && percent !== undefined) {
        return `${formatDecimal(free)} GB free - ${formatNumber(percent)}% used`;
      }
      if (health.ramDetail) return health.ramDetail;
      return health.source || "No memory metric returned.";
    }

    function percentage(part, total) {
      const denominator = numberValue(total);
      if (!denominator) return 0;
      return Math.round((numberValue(part) / denominator) * 100);
    }

    function rangeLabelFromValue(value) {
      const labels = {
        "24h": "Last 24 Hours",
        "7d": "Last Week",
        "30d": "Last Month",
        "custom": "Custom Dates",
      };
      return labels[value] || text(value);
    }

    function syncCustomDateVisibility() {
      const isCustom = form.reportRange.value === "custom" || dashReportRange.value === "custom";
      document.querySelectorAll("[data-custom-range]").forEach((node) => {
        node.hidden = !isCustom;
      });
    }

    function syncRangeToToolbar() {
      dashReportRange.value = form.reportRange.value;
      dashCustomStartDate.value = customStartDate.value;
      dashCustomEndDate.value = customEndDate.value;
      syncCustomDateVisibility();
    }

    function syncRangeToForm() {
      form.reportRange.value = dashReportRange.value;
      customStartDate.value = dashCustomStartDate.value;
      customEndDate.value = dashCustomEndDate.value;
      syncCustomDateVisibility();
    }

    function setStatus(label, tone) {
      topStatus.textContent = label;
      const colors = {
        neutral: "rgba(255, 255, 255, 0.10)",
        ok: "rgba(24, 118, 74, 0.95)",
        warn: "rgba(169, 104, 0, 0.95)",
        bad: "rgba(189, 43, 58, 0.95)",
      };
      topStatus.style.background = colors[tone] || colors.neutral;
    }

    function setLoading(loading) {
      document.getElementById("connectBtn").disabled = loading;
      discoverBtn.disabled = loading;
      refreshBtn.disabled = loading;
      manualRefreshBtn.disabled = loading;
      exportBtn.disabled = loading;
      if (loading) {
        setStatus("Connecting...", "neutral");
      }
    }

    function getPayload() {
      const selectedRange = dashReportRange?.value || form.reportRange.value;
      const pw  = form.password.value;
      const wpw = form.wmiPassword.value;
      return {
        restApiHost: form.restApiHost.value.trim(),
        restApiPort: form.restApiPort.value.trim(),
        backupServerHost: form.backupServerHost.value.trim(),
        backupServerPort: form.backupServerPort.value.trim(),
        username: form.username.value.trim(),
        password: pw  === "(saved)" ? "__profile_password__" : pw,
        profileName: profileSelect.value || "",
        sessionId,
        apiMode: form.apiMode.value,
        apiVersion: form.apiVersion.value,
        reportRange: selectedRange,
        customStartDate: selectedRange === "custom" ? customStartDate.value : "",
        customEndDate: selectedRange === "custom" ? customEndDate.value : "",
        useWmiHealth: form.useWmiHealth.checked,
        wmiUsername: form.wmiUsername.value.trim(),
        wmiPassword: wpw === "(saved)" ? "__profile_password__" : wpw,
        timeoutSeconds: form.timeoutSeconds.value.trim(),
        useAuthcHeader: form.useAuthcHeader.checked,
        verifyTls: form.verifyTls.checked,
      };
    }

    function clearPassword() {
      form.password.value    = "";
      form.wmiPassword.value = "";
    }

    function statusTone(summary, failed) {
      if (failed) return "bad";
      const health = String(summary?.health || "").toLowerCase();
      if (health === "critical") return "bad";
      if (health === "warning") return "warn";
      return "ok";
    }

    function statusText(summary, failed) {
      if (failed) return "Connection issue";
      const health = String(summary?.health || "").toLowerCase();
      if (health === "critical") return "Connected - action required";
      if (health === "warning") return "Connected with warnings";
      return "Connection established";
    }

    const SNAPSHOT_METRIC_BAD_ON_GROWTH = new Set(["failedJobs", "totalAlerts"]);
    function snapshotColor(value, key) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric) || numeric === 0) return "var(--ink)";
      const badOnGrowth = SNAPSHOT_METRIC_BAD_ON_GROWTH.has(key);
      if (numeric > 0) return badOnGrowth ? "var(--red)" : "var(--green)";
      return badOnGrowth ? "var(--green)" : "var(--red)";
    }

    function renderSparklineSvg(values) {
      if (!values || values.length < 2) return "";
      const nums = values.map(Number);
      const min = Math.min(...nums), max = Math.max(...nums);
      const range = max - min || 1;
      const w = 64, h = 22;
      const step = w / (nums.length - 1);
      const pts = nums.map((v, i) => {
        const x = (i * step).toFixed(1);
        const y = (h - 2 - ((v - min) / range) * (h - 4)).toFixed(1);
        return `${x},${y}`;
      }).join(" ");
      return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" class="sparkline" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--brand)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
    }

    function renderSlaGaugeInline(data) {
      const el = document.getElementById("slaGaugeInline");
      if (!el) return;
      if (!data || !data.ok) { el.innerHTML = ""; return; }
      const slaArr = snapshotHistoryCache?.slaHistory;
      const latestSla = slaArr && slaArr.length ? slaArr[slaArr.length - 1].value : 0;
      const pct = Math.min(100, Math.max(0, Number(latestSla) || 0));
      const r = 30, cx = 38, cy = 38;
      const theta = (180 + (pct / 100) * 180) * Math.PI / 180;
      const ex = (cx + r * Math.cos(theta)).toFixed(2);
      const ey = (cy + r * Math.sin(theta)).toFixed(2);
      const color = pct >= 95 ? "var(--green)" : pct >= 85 ? "var(--amber)" : "var(--red)";
      const bgPath = `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`;
      const fgPath = pct > 0.1 ? `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${ex} ${ey}` : "";
      el.innerHTML = `<svg viewBox="0 0 76 44" width="76" height="44" class="sla-gauge-svg">
        <path d="${bgPath}" fill="none" stroke="var(--line)" stroke-width="5" stroke-linecap="round"/>
        ${fgPath ? `<path d="${fgPath}" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round"/>` : ""}
        <text x="${cx}" y="${cy - 8}" text-anchor="middle" font-size="11" font-weight="800" fill="${color}">${pct.toFixed(1)}%</text>
        <text x="${cx}" y="${cy + 2}" text-anchor="middle" font-size="8" fill="var(--muted)">SLA</text>
      </svg>`;
    }

    function renderSnapshotComparison(data) {
      snapshotMeta.textContent = data.message || (
        data.previousDate
          ? `Comparing ${data.previousDate} to ${data.currentDate}`
          : "No previous snapshot found"
      );
      if (!data.ok || !Array.isArray(data.metrics) || !data.metrics.length) {
        snapshotGrid.innerHTML = `
          <div class="snapshot-cell snapshot-empty">
            <span>No comparison available</span>
            <strong style="font-size:15px">${escapeHtml(data.message || "Save at least two snapshots to compare growth.")}</strong>
            <small>Use <strong>Save snapshot</strong> after each connection to track growth over time.</small>
          </div>`;
        return;
      }
      const prevDate = data.previousDate || "";
      const currDate = data.currentDate || "";
      const rangeLabel = data.range ? ` (${data.range})` : "";
      const history = snapshotHistoryCache?.history || {};
      snapshotGrid.innerHTML = `<div class="snapshot-header">${escapeHtml(prevDate)} &rarr; ${escapeHtml(currDate)}${escapeHtml(rangeLabel)}</div>` +
        data.metrics.map((item) => {
          const delta = Number(item.delta || 0);
          const sign = delta > 0 ? "+" : "";
          const pct = item.deltaPercent === null || item.deltaPercent === undefined ? "--" : `${sign}${formatDecimal(item.deltaPercent)}%`;
          const badOnGrowth = SNAPSHOT_METRIC_BAD_ON_GROWTH.has(item.key);
          const trend = delta === 0 ? "neutral" : (delta > 0 === !badOnGrowth ? "good" : "bad");
          const badgeClass = delta === 0 ? "neutral" : trend;
          const arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "→";
          const sparkVals = (history[item.key] || []).map((h) => h.value);
          const sparkSvg = renderSparklineSvg(sparkVals);
          const maxBar = Math.max(item.previous, item.current, 1);
          const prevW = ((item.previous / maxBar) * 100).toFixed(1);
          const currW = ((item.current  / maxBar) * 100).toFixed(1);
          const currColor = trend === "good" ? "var(--green)" : trend === "bad" ? "var(--red)" : "var(--brand)";
          return `
          <div class="snapshot-cell" data-trend="${trend}">
            <div style="display:flex;justify-content:space-between;align-items:flex-start">
              <span>${escapeHtml(item.label)}</span>
              <span class="snap-badge ${badgeClass}">${arrow} ${pct}</span>
            </div>
            <strong style="color:${snapshotColor(delta, item.key)}">${sign}${formatNumber(delta)}</strong>
            <div class="snap-bars">
              <div class="snap-bar-row"><span>Before</span><div class="snap-bar-track"><div class="snap-bar" style="width:${prevW}%;background:var(--muted);opacity:0.5"></div></div><span>${formatNumber(item.previous)}</span></div>
              <div class="snap-bar-row"><span>After</span><div class="snap-bar-track"><div class="snap-bar" style="width:${currW}%;background:${currColor}"></div></div><span>${formatNumber(item.current)}</span></div>
            </div>
            ${sparkSvg ? `<div style="margin-top:4px">${sparkSvg}</div>` : ""}
          </div>`;
        }).join("");
    }

    async function loadSharedDashboard() {
      try {
        const response = await fetch("/api/current-dashboard", {cache: "no-store"});
        const data = await response.json();
        if (response.ok && data.ok && data.dashboard) {
          sessionId = data.sessionId || data.dashboard.sessionId || sessionId;
          renderDashboard(data.dashboard);
          snapshotMeta.textContent = data.snapshotSummary || "Shared dashboard session restored";
          setStatus("Shared session loaded", "ok");
        }
      } catch (error) {
        if (window.console) console.warn("No shared dashboard session available", error);
      }
    }

    function chartItems(summary, includeClients = false) {
      const rows = [
        {label: "Successful", value: numberValue(summary.successfulJobs), color: "var(--green)"},
        {label: "Failed", value: numberValue(summary.failedJobs), color: "var(--red)"},
        {label: "Running", value: numberValue(summary.activeJobs), color: "var(--blue)"},
        {label: "Restores", value: numberValue(summary.recoveryJobs), color: "var(--amber)"},
        {label: "Clones", value: numberValue(summary.cloneJobs), color: "#8a6fb0"},
        {label: "Alerts", value: numberValue(summary.totalAlerts), color: "#65747c"},
      ];
      if (includeClients) {
        rows.unshift({label: "Clients", value: numberValue(summary.totalClients), color: "#4f8f9e"});
      }
      return rows;
    }

    function conicGradient(items, total) {
      if (!total) return "conic-gradient(#d8e3e8 0deg 360deg)";
      let cursor = 0;
      const segments = [];
      items.forEach((item) => {
        const amount = Math.max(0, numberValue(item.value));
        if (!amount) return;
        const next = cursor + ((amount / total) * 360);
        segments.push(`${item.color} ${cursor.toFixed(2)}deg ${next.toFixed(2)}deg`);
        cursor = next;
      });
      if (segments.length && cursor < 360) {
        segments.push(`${items[0].color} ${cursor.toFixed(2)}deg 360deg`);
      }
      return `conic-gradient(${segments.join(", ")})`;
    }

    function renderDonut(summary) {
      const items = chartItems(summary).slice(0, 4);
      const total = items.reduce((sum, item) => sum + numberValue(item.value), 0);
      if (!total) {
        mgmtDonut.className = "chart-empty";
        mgmtDonut.textContent = "No backup or restore activity in this range";
        return;
      }

      const background = conicGradient(items, total);
      mgmtDonut.className = "";
      mgmtDonut.innerHTML = `
        <div class="donut-layout">
          <div class="donut-chart" style="--donut-bg: ${background}">
            <div class="donut-center">
              <div>
                <strong>${formatNumber(total)}</strong>
                <span>Activity</span>
              </div>
            </div>
          </div>
          <div class="legend-list">
            ${items.map((item) => `
              <div class="legend-item" style="--dot: ${item.color}">
                <span class="legend-dot"></span>
                <span>${escapeHtml(item.label)}</span>
                <strong>${formatNumber(item.value)} (${percentage(item.value, total)}%)</strong>
              </div>
            `).join("")}
          </div>
        </div>
      `;
    }

    function renderSlaPie(summary) {
      const total = numberValue(summary.slaTotalJobs ?? summary.totalJobs);
      const met = numberValue(summary.slaMetJobs ?? summary.successfulJobs);
      const missed = Math.max(0, numberValue(summary.slaMissedJobs ?? (total - met)));
      if (!total) {
        mgmtSlaPie.className = "chart-empty";
        const running = numberValue(summary.activeJobs);
        mgmtSlaPie.textContent = running
          ? `${running} job${running > 1 ? "s" : ""} currently running — SLA pending`
          : "No backup jobs ran in this range";
        mgmtSlaMeta.textContent = "Jobs ran";
        return;
      }

      const percent = numberValue(summary.slaPercent ?? Math.round((met / total) * 100));
      const items = [
        {label: "SLA met", value: met, color: "var(--green)"},
        {label: "Not met", value: missed, color: "var(--red)"},
      ];
      const background = conicGradient(items, total);
      mgmtSlaMeta.textContent = `${formatNumber(total)} jobs`;
      mgmtSlaPie.className = "";
      mgmtSlaPie.innerHTML = `
        <div class="donut-layout">
          <div class="donut-chart" style="--donut-bg: ${background}">
            <div class="donut-center">
              <div>
                <strong>${formatNumber(percent)}%</strong>
                <span>SLA</span>
              </div>
            </div>
          </div>
          <div class="legend-list">
            ${items.map((item) => `
              <div class="legend-item" style="--dot: ${item.color}">
                <span class="legend-dot"></span>
                <span>${escapeHtml(item.label)}</span>
                <strong>${formatNumber(item.value)} (${percentage(item.value, total)}%)</strong>
              </div>
            `).join("")}
          </div>
        </div>
      `;
    }

    function renderBars(summary) {
      const items = chartItems(summary, true);
      const hasData = Object.keys(summary || {}).length > 0;
      if (!hasData) {
        mgmtBars.className = "chart-empty";
        mgmtBars.textContent = "No management data yet";
        return;
      }

      const max = Math.max(1, ...items.map((item) => numberValue(item.value)));
      mgmtBars.className = "bar-chart";
      mgmtBars.innerHTML = items.map((item) => {
        const width = Math.max(2, Math.round((numberValue(item.value) / max) * 100));
        return `
          <div class="bar-row">
            <span>${escapeHtml(item.label)}</span>
            <span class="bar-track">
              <span class="bar-fill" style="--bar-width: ${width}%; --bar-color: ${item.color}"></span>
            </span>
            <span class="bar-value">${formatNumber(item.value)}</span>
          </div>
        `;
      }).join("");
    }

    function renderRecoveryPanel(summary) {
      const rangeLabel = summary.rangeLabel || rangeLabelFromValue(dashReportRange.value);
      const detailRows = summary.recoveryFailed === undefined
        ? `
          <div class="summary-row"><span>Policies</span><strong>${formatNumber(summary.policies)}</strong></div>
          <div class="summary-row"><span>Clients</span><strong>${formatNumber(summary.totalClients)}</strong></div>
          <div class="summary-row"><span>Critical alerts</span><strong>${formatNumber(summary.criticalAlerts)}</strong></div>
        `
        : `
          <div class="summary-row"><span>Failed restores</span><strong>${formatNumber(summary.recoveryFailed)}</strong></div>
          <div class="summary-row"><span>Running restores</span><strong>${formatNumber(summary.recoveryRunning)}</strong></div>
          <div class="summary-row"><span>Clone jobs excluded</span><strong>${formatNumber(summary.cloneJobs)}</strong></div>
        `;

      mgmtRestorePanel.className = "summary-list";
      mgmtRestorePanel.innerHTML = `
        <div class="summary-band">
          <strong>${formatNumber(summary.recoveryJobs)}</strong>
          <span>Restore jobs in ${escapeHtml(rangeLabel)}</span>
        </div>
        ${detailRows}
      `;
    }

    function renderClonePanel(summary) {
      const rangeLabel = summary.rangeLabel || rangeLabelFromValue(dashReportRange.value);
      mgmtClonePanel.className = "summary-list";
      mgmtClonePanel.innerHTML = `
        <div class="summary-band">
          <strong>${formatNumber(summary.cloneJobs)}</strong>
          <span>Clone jobs in ${escapeHtml(rangeLabel)}</span>
        </div>
        <div class="summary-row"><span>Failed clone jobs</span><strong>${formatNumber(summary.cloneFailed)}</strong></div>
        <div class="summary-row"><span>Running clone jobs</span><strong>${formatNumber(summary.cloneRunning)}</strong></div>
        <div class="summary-row"><span>Clone sessions</span><strong>${formatNumber(summary.cloneSessionTotal)}</strong></div>
      `;
    }

    function renderManagement(data, failed = false) {
      const summary = data?.summary || {};
      const target = data?.target || {};
      const tone = statusTone(summary, failed);
      mgmtConnection.className = `connection-line ${tone}`;
      mgmtStatus.textContent = statusText(summary, failed);
      mgmtApi.textContent = text(target.apiMode || form.apiMode.value).toUpperCase();
      mgmtBackupServer.textContent = text(target.backupServer || form.backupServerHost.value.trim());
      mgmtUpdated.textContent = text(data?.generatedAt);
      mgmtRange.textContent = text(summary.rangeLabel || rangeLabelFromValue(dashReportRange.value));
      renderDonut(summary);
      renderSlaPie(summary);
      renderBars(summary);
      renderRecoveryPanel(summary);
      renderClonePanel(summary);
    }

    function resetManagement() {
      mgmtConnection.className = "connection-line";
      mgmtStatus.textContent = "Not connected";
      mgmtApi.textContent = "--";
      mgmtBackupServer.textContent = "--";
      mgmtUpdated.textContent = "--";
      mgmtRange.textContent = "--";
      mgmtDonut.className = "chart-empty";
      mgmtDonut.textContent = "No backup data yet";
      mgmtSlaPie.className = "chart-empty";
      mgmtSlaPie.textContent = "No SLA data yet";
      mgmtSlaMeta.textContent = "Jobs ran";
      mgmtBars.className = "chart-empty";
      mgmtBars.textContent = "No management data yet";
      mgmtRestorePanel.className = "summary-list";
      mgmtRestorePanel.innerHTML = `
        <div class="summary-band">
          <strong>--</strong>
          <span>Restore jobs in selected range</span>
        </div>
      `;
      mgmtClonePanel.className = "summary-list";
      mgmtClonePanel.innerHTML = `
        <div class="summary-band">
          <strong>--</strong>
          <span>Clone jobs in selected range</span>
        </div>
      `;
    }

    function updateMetrics(summary) {
      metrics.clients.textContent = text(summary.totalClients);
      metrics.success.textContent = text(summary.successfulJobs);
      metrics.failed.textContent = text(summary.failedJobs);
      metrics.failedRestores.textContent = text(summary.recoveryFailed);
      metrics.failedClones.textContent = text(summary.cloneFailed);
      metrics.active.textContent = text(summary.activeJobs);
      metrics.recovery.textContent = text(summary.recoveryJobs);
      metrics.alerts.textContent = text(summary.totalAlerts);
    }

    function healthTone(value) {
      const numeric = numberValue(value);
      if (!numeric) return "";
      if (numeric >= 90) return "bad";
      if (numeric >= 75) return "warn";
      return "ok";
    }

    function healthMeter(value, color) {
      const numeric = Math.max(0, Math.min(100, numberValue(value)));
      if (!numeric) return "";
      return `
        <span class="health-meter" aria-hidden="true">
          <span class="health-meter-fill" style="--meter-width: ${numeric}%; --meter-color: ${color}"></span>
        </span>
      `;
    }

    function healthItem(label, value, detail = "", tone = "", meter = "") {
      return `
        <div class="health-item ${tone}">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
          ${meter}
          <small>${escapeHtml(detail)}</small>
        </div>
      `;
    }

    function renderServerHealth(data) {
      const health = data?.serverHealth || {};
      const maintenance = data?.serverProtectionJob || data?.maintenanceBackup || {};
      const cpu = health.cpuUsagePercent;
      const ram = health.ramUsagePercent;
      const statusTone = health.status === "ok" ? "ok" : (health.status === "warning" ? "warn" : (health.status === "critical" ? "bad" : ""));
      const maintenanceTone = maintenance.status === "failed"
        ? "bad"
        : (maintenance.status === "running" || maintenance.status === "queued" || maintenance.status === "warning" ? "warn" : (maintenance.status === "succeeded" ? "ok" : ""));

      healthGrid.innerHTML = [
        healthItem(
          "Server status",
          health.label || "Unavailable",
          health.detail || "CPU/RAM endpoint did not return data.",
          statusTone,
        ),
        healthItem(
          "CPU usage",
          cpu === null || cpu === undefined ? "--" : `${formatNumber(cpu)}%`,
          health.cpuDetail || health.source || "No CPU metric returned.",
          healthTone(cpu),
          healthMeter(cpu, "var(--blue)"),
        ),
        healthItem(
          "Memory usage",
          memoryUsageValue(health),
          memoryUsageDetail(health),
          healthTone(ram),
          healthMeter(ram, "var(--amber)"),
        ),
        healthItem(
          "Server Protection Job",
          maintenance.label || "Not found",
          maintenance.detail || "No Server Protection job found in this range.",
          maintenanceTone,
        ),
      ].join("");
    }

    function sourceNeedsVisibleWarning(item) {
      return item && !item.ok && item.displayWarning !== false && item.severity !== "info";
    }

    function failedSourceSummary(sources) {
      const failed = Object.entries(sources || {}).filter(([, item]) => sourceNeedsVisibleWarning(item));
      if (!failed.length) return "";
      return failed.map(([name, item]) => {
        const path = item.path || name;
        const status = item.status ? `HTTP ${item.status}` : "failed";
        const rawError = String(item.error || "");
        let error = item.userMessage || item.summary || "";
        if (!error) {
          if (path.includes("monitoringactions")) {
            error = "Backup activity source is temporarily unavailable; server health and cached local snapshot data remain visible.";
          } else if (rawError.includes("nwrestapi application was not found") || rawError.includes("HTTP 404")) {
            error = "REST endpoint route is unavailable on the selected host/port.";
          } else {
            error = "Source is temporarily unavailable.";
          }
        }
        return `${path} ${status}: ${error}`;
      }).join(" | ");
    }

    function setActiveTable(tableName) {
      activeTable = tableName;
      document.querySelectorAll(".tab").forEach((node) => {
        node.classList.toggle("active", node.dataset.table === tableName);
      });
    }

    function chooseVisibleTable() {
      const tables = latestDashboard?.tables || {};
      if ((tables[activeTable] || []).length) return;
      const fallback = ["jobs", "failedJobs", "recovery", "cloneJobs", "logs", "alerts", "clients"].find((name) => {
        return (tables[name] || []).length > 0;
      });
      if (fallback) {
        setActiveTable(fallback);
      }
    }

    function badgeClass(value) {
      const status = String(value || "").toLowerCase();
      if (status.includes("success") || status.includes("succeed") || status.includes("complete")) return "success";
      if (status.includes("fail") || status.includes("error") || status.includes("critical")) return "failed";
      if (status.includes("run") || status.includes("active") || status.includes("start")) return "running";
      if (status.includes("warn")) return "warning";
      return "";
    }

    function renderTable() {
      if (activeTable === "timeline" || activeTable === "heatmap") return;
      if (typeof timelineWrap !== "undefined") timelineWrap.classList.add("hidden");
      if (typeof heatmapWrap  !== "undefined") heatmapWrap.classList.add("hidden");
      const def = tableDefs[activeTable] || {title: activeTable, columns: []};
      const rows = latestDashboard?.tables?.[activeTable] || [];
      tableTitle.textContent = def.title;
      tableMeta.textContent = `${rows.length} rows`;
      tableHead.innerHTML = `<tr>${def.columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr>`;

      if (!rows.length) {
        tableWrap.classList.add("hidden");
        paginationBar.classList.add("hidden");
        emptyState.classList.remove("hidden");
        emptyState.textContent = latestDashboard ? "No records returned for this view." : "Enter connection details and connect.";
        return;
      }

      emptyState.classList.add("hidden");
      tableWrap.classList.remove("hidden");
      pageLimit = PAGE_SIZE;
      renderTablePage(rows, def);
    }

    function renderDashboard(data) {
      diffAndNotify(latestDashboard, data);
      latestDashboard = data;
      if (data.sessionId) {
        sessionId = data.sessionId;
      }
      updateMetrics(data.summary || {});
      renderManagement(data);
      renderServerHealth(data);
      // Update collapsed bar
      const host = data.target?.restApiBase || data.target?.backupServerBase || form.restApiHost?.value || "";
      const rl   = data.summary?.rangeLabel || "";
      updateCollapsedBar(host, rl);
      const rangeLabel = data.summary?.rangeLabel ? ` - ${data.summary.rangeLabel}` : "";
      generatedAt.textContent = data.generatedAt ? `Updated ${data.generatedAt}${rangeLabel}` : `Updated now${rangeLabel}`;

      const failedSources = Object.values(data.sources || {}).filter((item) => sourceNeedsVisibleWarning(item));
      if (data.stale && data.reportNotice) {
        notice.textContent = data.reportNotice;
        notice.classList.add("show");
      } else if (failedSources.length) {
        notice.textContent = `Backup data loaded with ${failedSources.length} source warning(s): ${failedSourceSummary(data.sources)}`;
        notice.classList.add("show");
      } else {
        notice.textContent = "";
        notice.classList.remove("show");
      }

      const health = data.summary?.health || "unknown";
      if (data.stale) setStatus("Using cached dashboard", "warn");
      else if (health === "critical") setStatus("Attention required", "bad");
      else if (health === "warning") setStatus("Connected with warnings", "warn");
      else setStatus("Connected", "ok");

      document.body.classList.add("connected");
      document.body.classList.remove("connection-open");
      document.getElementById("shareBtn").classList.remove("hidden");
      document.getElementById("logoutBtn").classList.remove("hidden");
      chooseVisibleTable();
      if (activeTable === "timeline") renderTimeline();
      else if (activeTable === "heatmap") renderHeatmap();
      else renderTable();
      refreshBtn.disabled = false;
      manualRefreshBtn.disabled = false;
      exportBtn.disabled = false;
      snapshotSaveBtn.disabled = false;
      snapshotCompareBtn.disabled = false;
      refreshSnapshotStatus();
      scheduleAutoRefresh();
      scheduleServerHealthRefresh();
    }

    function renderFailure(data, fallbackMessage) {
      latestDashboard = data || null;
      updateMetrics(data?.summary || {});
      renderManagement(data || {}, true);
      renderServerHealth(data || {});
      clearServerHealthRefresh();
      generatedAt.textContent = data?.generatedAt ? `Failed ${data.generatedAt}` : "Connection failed";
      tableWrap.classList.add("hidden");
      emptyState.classList.remove("hidden");
      emptyState.textContent = "REST API data was not returned.";
      tableMeta.textContent = "0 rows";

      const sourceErrors = Object.values(data?.sources || {})
        .filter((item) => !item.ok)
        .map((item) => `${item.path || "REST call"}: ${item.error || "failed"}`);
      notice.textContent = sourceErrors.length
        ? sourceErrors.join(" | ")
        : (fallbackMessage || "Unable to load dashboard.");
      notice.classList.add("show");
      setStatus("REST API failed", "bad");
    }

    function resetDashboard() {
      latestDashboard = null;
      sessionId = null;
      clearAutoRefresh();
      clearServerHealthRefresh();
      Object.values(metrics).forEach((node) => node.textContent = "--");
      resetManagement();
      generatedAt.textContent = "Waiting for connection";
      notice.textContent = "";
      notice.classList.remove("show");
      healthGrid.innerHTML = `
        <div class="health-item"><span>Server status</span><strong>Waiting</strong><small>Connect to load server health.</small></div>
        <div class="health-item"><span>CPU usage</span><strong>--</strong><small>Awaiting NetWorker health data.</small></div>
        <div class="health-item"><span>Memory usage</span><strong>--</strong><small>Awaiting NetWorker health data.</small></div>
        <div class="health-item"><span>Server Protection Job</span><strong>--</strong><small>No job data loaded.</small></div>
      `;
      tableWrap.classList.add("hidden");
      emptyState.classList.remove("hidden");
      emptyState.textContent = "Enter connection details and connect.";
      tableMeta.textContent = "0 rows";
      snapshotMeta.textContent = "No local snapshots loaded";
      snapshotGrid.innerHTML = `
        <div class="snapshot-cell">
          <span>Snapshot status</span>
          <strong>Waiting</strong>
          <small>Connect to NetWorker, then save a local snapshot.</small>
        </div>`;
      snapshotSaveBtn.disabled = true;
      snapshotCompareBtn.disabled = false;
      document.getElementById("shareBtn").classList.add("hidden");
      document.getElementById("logoutBtn").classList.add("hidden");
      setStatus("Not connected", "neutral");
      document.body.classList.remove("connected");
      document.body.classList.add("connection-open");
      refreshBtn.disabled = false;
    }

    function currentRefreshMs() {
      const minutes = Math.max(1, Math.min(1440, parseInt(refreshMinutes.value || "5", 10) || 5));
      refreshMinutes.value = String(minutes);
      return minutes * 60 * 1000;
    }

    function clearAutoRefresh() {
      if (refreshTimer) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
      }
    }

    function clearServerHealthRefresh() {
      if (healthRefreshTimer) {
        clearTimeout(healthRefreshTimer);
        healthRefreshTimer = null;
      }
    }

    function scheduleServerHealthRefresh() {
      clearServerHealthRefresh();
      if (!sessionId || !latestDashboard) return;
      healthRefreshTimer = setTimeout(refreshServerHealth, SERVER_HEALTH_REFRESH_MS);
    }

    async function refreshServerHealth() {
      if (!sessionId) return;
      try {
        const response = await fetch("/api/server-health", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({sessionId}),
          cache: "no-store",
        });
        const data = await response.json();
        if (response.ok && data.serverHealth) {
          latestDashboard = latestDashboard || {};
          latestDashboard.serverHealth = data.serverHealth;
          if (data.serverProtectionJob) {
            latestDashboard.serverProtectionJob = data.serverProtectionJob;
            latestDashboard.maintenanceBackup = data.serverProtectionJob;
          }
          renderServerHealth(data);
          const rangeLabel = latestDashboard.summary?.rangeLabel ? ` - ${latestDashboard.summary.rangeLabel}` : "";
          generatedAt.textContent = data.generatedAt ? `Updated ${data.generatedAt}${rangeLabel}` : generatedAt.textContent;
        }
      } catch (error) {
        if (window.console) console.warn("Server health refresh failed", error);
      } finally {
        scheduleServerHealthRefresh();
      }
    }

    function scheduleAutoRefresh() {
      clearAutoRefresh();
      if (autoRefreshMode.value !== "on" || !latestDashboard) return;
      if (!sessionId) return;
      refreshTimer = setTimeout(() => {
        loadDashboard({silent: true});
      }, currentRefreshMs());
    }

    function applyTheme(theme) {
      const value = theme || "default";
      document.body.dataset.theme = value === "default" ? "" : value;
      themeSelect.value = value;
      try {
        localStorage.setItem("nw_dashboard_theme", value);
      } catch (error) {}
      // Persist the current theme server-side so background-scheduled report
      // emails use the live theme dynamically (fire-and-forget).
      try {
        fetch("/api/ui-theme", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({theme: value}),
          cache: "no-store",
        }).catch(() => {});
      } catch (error) {}
    }

    function syncSmtpSecurityFields() {
      const isPlainSmtp = smtpSecurity.value === "none";
      if (isPlainSmtp) {
        smtpPort.value = "25";
        smtpUsername.value = "";
        smtpPassword.value = "";
      } else if (!smtpPort.value || smtpPort.value === "25") {
        smtpPort.value = smtpSecurity.value === "ssl" ? "465" : "587";
      }
      smtpUsername.disabled = isPlainSmtp;
      smtpPassword.disabled = isPlainSmtp;
      smtpUsername.placeholder = isPlainSmtp ? "Disabled for SMTP without authentication" : "";
      smtpPassword.placeholder = isPlainSmtp ? "Disabled for SMTP without authentication" : "";
    }

    let emailConfigCache = null;

    function applyEmailTypeBlock() {
      const c = emailConfigCache;
      if (!c) return;
      const smtpToEl = document.getElementById("smtpTo");
      if (emailScheduleType.value === "daily_report") {
        smtpToEl.value = c.dailyReport.recipients || "";
        document.getElementById("dailyReportTime").value = c.dailyReport.reportTime || "08:00";
        // NOTE: do NOT touch themeSelect here. The report theme is dynamic and
        // follows the current dashboard theme (persisted server-side); the email
        // modal must never override the shared dashboard theme control.
      } else {
        smtpToEl.value = c.alert.recipients || "";
        document.getElementById("alertTrigger").value = c.alert.trigger || "critical";
        document.getElementById("alertIntervalMinutes").value = c.alert.intervalMinutes || 60;
      }
    }

    function applyEmailConfig() {
      const c = emailConfigCache;
      if (!c) return;
      document.getElementById("smtpHost").value = c.smtp.host || "";
      smtpPort.value = c.smtp.port || "587";
      smtpSecurity.value = c.smtp.security || "starttls";
      smtpUsername.value = c.smtp.username || "";
      document.getElementById("smtpFrom").value = c.smtp.from || "";
      smtpPassword.value = "";
      smtpPassword.placeholder = c.smtp.passwordSaved ? "Saved — leave blank to keep" : "";
      applyEmailTypeBlock();
      syncSmtpSecurityFields();
    }

    async function loadEmailConfigIntoForm() {
      try {
        const r = await fetch("/api/email-config", {cache: "no-store"});
        const d = await r.json();
        if (r.ok && d.ok) {
          emailConfigCache = d;
          applyEmailConfig();
        }
      } catch (_) { /* keep current form values */ }
    }

    function openAlertAutomationModal() {
      alertAutomationModal.classList.add("open");
      alertAutomationModal.setAttribute("aria-hidden", "false");
      syncSmtpSecurityFields();
      loadEmailConfigIntoForm();
      setTimeout(() => document.getElementById("smtpHost").focus(), 0);
    }

    function closeAlertAutomationModal() {
      alertAutomationModal.classList.remove("open");
      alertAutomationModal.setAttribute("aria-hidden", "true");
      smtpPassword.value = "";
      alertConfigBtn.focus();
    }

    function alertAutomationPayload(action) {
      const payload = {
        action,
        sessionId,
        smtpHost: document.getElementById("smtpHost").value.trim(),
        smtpPort: smtpPort.value.trim(),
        smtpSecurity: smtpSecurity.value,
        smtpUsername: smtpSecurity.value === "none" ? "" : smtpUsername.value.trim(),
        smtpPassword: smtpSecurity.value === "none" ? "" : smtpPassword.value,
        smtpFrom: document.getElementById("smtpFrom").value.trim(),
        smtpTo: document.getElementById("smtpTo").value.trim(),
        intervalMinutes: document.getElementById("alertIntervalMinutes").value.trim(),
        trigger: document.getElementById("alertTrigger").value,
        scheduleType: document.getElementById("emailScheduleType").value,
        reportTime: document.getElementById("dailyReportTime").value.trim(),
        theme: themeSelect.value || "default",
      };
      if (action === "test" && payload.scheduleType === "daily_report" && latestDashboard) {
        payload.dashboard = {
          generatedAt: latestDashboard.generatedAt,
          target: latestDashboard.target || {},
          summary: latestDashboard.summary || {},
          serverHealth: latestDashboard.serverHealth || {},
          serverProtectionJob: latestDashboard.serverProtectionJob || latestDashboard.maintenanceBackup || {},
          theme: payload.theme,
        };
      }
      return payload;
    }

    function smtpDebugSummary(debug) {
      if (!debug || typeof debug !== "object") return "";
      const lines = [
        `SMTP stage: ${debug.stage || "unknown"}`,
        `SMTP host: ${debug.host || "--"}:${debug.port || "--"}`,
        `SMTP security: ${debug.security || "none"}`,
        `SMTP auth: ${debug.usernameProvided ? "enabled" : "disabled"}`,
        `Recipients: ${debug.recipientCount || 0}`,
      ];
      if (debug.detail) lines.unshift(debug.detail);
      return lines.join("\\n");
    }

    async function submitAlertAutomation(action) {
      // Saving config does not need a live session; scheduling/testing does.
      if (!sessionId && action !== "stop" && action !== "save") {
        alertAutomationStatus.textContent = "Connect before scheduling email automations";
        setStatus("Connect first", "warn");
        return;
      }
      const payload = alertAutomationPayload(action);
      alertScheduleBtn.disabled = true;
      alertTestBtn.disabled = true;
      alertStopBtn.disabled = true;
      emailSaveConfigBtn.disabled = true;
      try {
        const response = await fetch("/api/alert-automation", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        const data = await response.json();
        if (!response.ok) {
          const debugText = smtpDebugSummary(data.smtpDebug);
          const message = data.error || `Alert automation failed with HTTP ${response.status}`;
          throw new Error(debugText ? `${message}\\n${debugText}` : message);
        }
        // Persisting (save, or start which also saves) returns the refreshed
        // config — update the cache so the per-type recipients stay in sync.
        if (data.config) emailConfigCache = data.config;
        const successDebug = action === "test" ? smtpDebugSummary(data.smtpDebug) : "";
        alertAutomationStatus.textContent = successDebug
          ? `${data.message || "Alert automation updated"}\\n${successDebug}`
          : data.message || "Alert automation updated";
        if (action === "test") setStatus("Test email sent", "ok");
        if (action === "save") setStatus("Email configuration saved", "ok");
        if (action === "start") setStatus(payload.scheduleType === "daily_report" ? "Report scheduled" : "Alerts scheduled", "ok");
        if (action === "stop") setStatus("Schedule stopped", "neutral");
      } catch (error) {
        alertAutomationStatus.textContent = error.message || "Alert automation failed";
        setStatus("Email automation failed", "bad");
      } finally {
        smtpPassword.value = "";
        alertScheduleBtn.disabled = false;
        alertTestBtn.disabled = false;
        alertStopBtn.disabled = false;
        emailSaveConfigBtn.disabled = false;
      }
    }

    // Quietly load saved snapshots and render the growth/status panel. Called on
    // connect and on page load so the panel reflects stored snapshots instead of
    // staying on the disconnected "Waiting" placeholder.
    async function refreshSnapshotStatus() {
      try {
        if (!snapshotHistoryCache) { await loadSnapshotHistory(); }
        const r = await fetch(`/api/snapshots?range=${encodeURIComponent(activeSnapshotRange)}`, {cache: "no-store"});
        const data = await r.json();
        const count = snapshotHistoryCache?.dates?.length
          ?? (snapshotHistoryCache?.history ? Object.keys(snapshotHistoryCache.history).length : 0);
        if (data && data.ok && Array.isArray(data.metrics) && data.metrics.length) {
          renderSnapshotComparison(data);
        } else if (count > 0) {
          snapshotMeta.textContent = `${count} local snapshot(s) saved`;
          snapshotGrid.innerHTML = `
            <div class="snapshot-cell snapshot-empty">
              <span>Snapshot status</span>
              <strong style="font-size:15px">${count} snapshot(s) saved</strong>
              <small>Save another (or wait for tomorrow's auto-save) to compare growth.</small>
            </div>`;
        } else {
          snapshotMeta.textContent = latestDashboard ? "No local snapshots yet" : "No local snapshots loaded";
          snapshotGrid.innerHTML = `
            <div class="snapshot-cell snapshot-empty">
              <span>Snapshot status</span>
              <strong style="font-size:15px">${latestDashboard ? "Ready — no snapshots yet" : "Waiting"}</strong>
              <small>${latestDashboard ? "Use <strong>Save snapshot</strong> or enable Auto-save daily to start tracking growth." : "Connect to NetWorker, then save a local snapshot."}</small>
            </div>`;
        }
      } catch (e) { /* leave existing panel content */ }
    }

    async function saveLocalSnapshot() {
      if (!latestDashboard) {
        setStatus("Connect first", "warn");
        snapshotMeta.textContent = "Load dashboard data before saving a local snapshot.";
        return;
      }
      snapshotSaveBtn.disabled = true;
      try {
        const response = await fetch("/api/snapshots", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            range: activeSnapshotRange,
            dashboard: {
              ok: latestDashboard.ok,
              generatedAt: latestDashboard.generatedAt,
              target: latestDashboard.target || {},
              summary: latestDashboard.summary || {},
              serverHealth: latestDashboard.serverHealth || {},
              serverProtectionJob: latestDashboard.serverProtectionJob || latestDashboard.maintenanceBackup || {},
            },
          }),
          cache: "no-store",
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `Snapshot save failed with HTTP ${response.status}`);
        snapshotMeta.textContent = data.message || "Snapshot saved.";
        snapshotHistoryCache = null;
        await loadSnapshotHistory();
        renderSnapshotComparison(data.comparison || {ok: false, message: "Snapshot saved. Save another snapshot later to compare growth."});
        renderSlaGaugeInline(data.comparison || {});
        setStatus("Snapshot saved", "ok");
      } catch (error) {
        snapshotMeta.textContent = error.message || "Unable to save local snapshot.";
        setStatus("Snapshot failed", "bad");
      } finally {
        snapshotSaveBtn.disabled = false;
      }
    }

    async function loadSnapshotHistory() {
      try {
        const r = await fetch("/api/snapshots?action=history", {cache: "no-store"});
        if (r.ok) snapshotHistoryCache = await r.json();
      } catch (_) {}
    }

    async function compareLocalSnapshots() {
      const originalText = snapshotCompareBtn.textContent;
      snapshotCompareBtn.disabled = true;
      snapshotCompareBtn.textContent = "Comparing...";
      snapshotMeta.textContent = "Comparing local snapshots...";
      snapshotGrid.innerHTML = `
        <div class="snapshot-cell snapshot-empty">
          <span>Comparison running</span>
          <strong style="font-size:15px">Checking saved snapshot history.</strong>
          <small>Comparing the latest saved snapshot with the nearest previous snapshot in the selected range.</small>
        </div>`;
      try {
        const [resp] = await Promise.all([
          fetch(`/api/snapshots?range=${encodeURIComponent(activeSnapshotRange)}`, {cache: "no-store"}),
          snapshotHistoryCache ? Promise.resolve() : loadSnapshotHistory(),
        ]);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `Snapshot compare failed with HTTP ${resp.status}`);
        renderSnapshotComparison(data);
        renderSlaGaugeInline(data);
        if (data.ok) {
          setStatus("Snapshot compared", "ok");
          showToast(data.message || "Snapshot comparison updated");
        } else {
          setStatus("No comparison available", "warn");
          showToast(data.message || "Save at least two snapshots to compare growth");
        }
      } catch (error) {
        snapshotMeta.textContent = error.message || "Unable to compare local snapshots.";
        renderSnapshotComparison({ok: false, message: snapshotMeta.textContent});
        setStatus("Snapshot compare failed", "bad");
        showToast(snapshotMeta.textContent);
      } finally {
        snapshotCompareBtn.disabled = false;
        snapshotCompareBtn.textContent = originalText || "Compare growth";
      }
    }

    async function loadDashboard(options = {}) {
      const payload = getPayload();
      if (!payload.password && !payload.sessionId) {
        setStatus("Password required", "warn");
        notice.textContent = "Enter the password once to connect. Auto-refresh uses a volatile server session after login.";
        notice.classList.add("show");
        return;
      }

      if (!options.silent) {
        setLoading(true);
        notice.textContent = "";
        notice.classList.remove("show");
      }

      try {
        const response = await fetch("/api/dashboard", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        const data = await response.json();
        if (!response.ok) {
          // 401 from server means session expired server-side.
          // If silent refresh, keep showing last good data and note reconnect happening.
          if (response.status === 401 && options.silent && latestDashboard) {
            setStatus("Reconnecting…", "warn");
            notice.textContent = "Session refreshing automatically — no action needed.";
            notice.classList.add("show");
            // Retry with password if available in form, else wait for next cycle
            const retryPayload = getPayload();
            if (retryPayload.password) {
              sessionId = null;
              const retryResp = await fetch("/api/dashboard", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(retryPayload),
                cache: "no-store",
              });
              if (retryResp.ok) {
                const retryData = await retryResp.json();
                renderDashboard(retryData);
                return;
              }
            }
            scheduleAutoRefresh();
            return;
          }
          renderFailure(data, data.error || data.message || `HTTP ${response.status}`);
          return;
        }
        renderDashboard(data);
      } catch (error) {
        if (options.silent && latestDashboard) {
          if (window.console) console.warn("Dashboard auto-refresh failed; keeping last successful data", error);
          scheduleAutoRefresh();
          return;
        }
        setStatus("Connection failed", "bad");
        notice.textContent = error.message || "Unable to load dashboard.";
        notice.classList.add("show");
      } finally {
        payload.password = "";
        clearPassword();
        if (!options.silent) {
          setLoading(false);
        }
      }
    }

    async function exportReport() {
      const payload = getPayload();
      if (!payload.password && !payload.sessionId && latestDashboard) {
        payload.dashboard = latestDashboard;
      }
      if (!payload.password && !payload.sessionId && !payload.dashboard) {
        setStatus("Password required", "warn");
        notice.textContent = "Reconnect before exporting if the in-memory session is no longer available.";
        notice.classList.add("show");
        return;
      }
      setLoading(true);
      try {
        const response = await fetch("/api/export", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.error || `Export failed with HTTP ${response.status}`);
        }
        const blob = await response.blob();
        const disposition = response.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : "networker_dashboard_report.xlsx";
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
        setStatus("Report exported", "ok");
      } catch (error) {
        setStatus("Export failed", "bad");
        notice.textContent = error.message || "Unable to export Excel report.";
        notice.classList.add("show");
      } finally {
        payload.password = "";
        clearPassword();
        setLoading(false);
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      loadDashboard();
    });

    refreshBtn.addEventListener("click", () => {
      loadDashboard();
    });

    manualRefreshBtn.addEventListener("click", () => {
      loadDashboard();
    });

    discoverBtn.addEventListener("click", () => {
      loadDashboard();
    });

    exportBtn.addEventListener("click", () => {
      exportReport();
    });

    alertConfigBtn.addEventListener("click", () => {
      openAlertAutomationModal();
    });

    alertModalCloseBtn.addEventListener("click", () => {
      closeAlertAutomationModal();
    });

    alertAutomationModal.addEventListener("click", (event) => {
      if (event.target === alertAutomationModal) closeAlertAutomationModal();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (alertAutomationModal.classList.contains("open")) closeAlertAutomationModal();
        if (shareModal.classList.contains("open")) closeShareModal();
        if (addServerModal.classList.contains("open")) closeAddServerModal();
      }
    });

    alertScheduleBtn.addEventListener("click", () => {
      submitAlertAutomation("start");
    });

    alertTestBtn.addEventListener("click", () => {
      submitAlertAutomation("test");
    });

    alertStopBtn.addEventListener("click", () => {
      submitAlertAutomation("stop");
    });

    emailSaveConfigBtn.addEventListener("click", () => {
      submitAlertAutomation("save");
    });

    // Switching Email type swaps to that type's separately-saved recipients and
    // settings without losing the other type's values.
    emailScheduleType.addEventListener("change", applyEmailTypeBlock);

    snapshotSaveBtn.addEventListener("click", () => { saveLocalSnapshot(); });
    snapshotCompareBtn.addEventListener("click", () => { compareLocalSnapshots(); });

    snapshotExportBtn.addEventListener("click", async () => {
      try {
        const r = await fetch("/api/snapshots?action=export", {cache: "no-store"});
        const csv = await r.text();
        const blob = new Blob([csv], {type: "text/csv"});
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `networker_snapshots_${new Date().toISOString().slice(0,10)}.csv`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast("Snapshots exported");
      } catch (e) { showToast("Export failed: " + e.message); }
    });

    snapshotManageBtn.addEventListener("click", openSnapshotPanel);
    snapshotPanelClose.addEventListener("click", closeSnapshotPanel);
    snapshotPanel.addEventListener("click", (e) => { if (e.target === snapshotPanel) closeSnapshotPanel(); });

    autoSnapshotToggle.addEventListener("change", async () => {
      const enabled = autoSnapshotToggle.checked;
      try {
        const resp = await fetch("/api/snapshots", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "auto-config", enabled}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!enabled) {
          showToast("Auto-snapshot disabled");
        } else {
          const messages = {
            saved: "Auto-snapshot enabled — snapshot saved now",
            exists: "Auto-snapshot enabled — today already captured",
            "no-dashboard": "Auto-snapshot enabled — will capture once connected",
          };
          showToast(messages[data.result] || "Auto-snapshot enabled");
          if (data.summary) snapshotMeta.textContent = data.summary;
          if (data.result === "saved") { snapshotHistoryCache = null; refreshSnapshotStatus(); }
        }
      } catch (e) { showToast("Failed to update auto-snapshot setting"); }
    });

    async function openSnapshotPanel() {
      snapshotPanel.setAttribute("aria-hidden", "false");
      snapshotPanel.style.display = "flex";
      document.getElementById("snapshotPanelBody").innerHTML = "<p style='color:var(--muted);padding:8px'>Loading…</p>";
      try {
        const r = await fetch("/api/snapshots?action=list", {cache: "no-store"});
        const json = await r.json();
        renderSnapshotPanelList(json.snapshots || []);
      } catch (e) {
        document.getElementById("snapshotPanelBody").innerHTML = `<p style='color:var(--red)'>Failed to load: ${escapeHtml(e.message)}</p>`;
      }
    }

    function closeSnapshotPanel() {
      snapshotPanel.setAttribute("aria-hidden", "true");
      snapshotPanel.style.display = "";
    }

    function renderSnapshotPanelList(snapshots) {
      const body = document.getElementById("snapshotPanelBody");
      if (!snapshots.length) {
        body.innerHTML = "<p style='color:var(--muted);padding:8px'>No snapshots saved yet.</p>";
        return;
      }
      body.innerHTML = `<table class="snap-panel-table">
        <thead><tr><th>Date</th><th>Server</th><th>Health</th><th>SLA %</th><th>Note</th><th></th></tr></thead>
        <tbody>${snapshots.map((s) => `
          <tr data-date="${escapeHtml(s.date)}">
            <td><strong>${escapeHtml(s.date)}</strong></td>
            <td style="color:var(--muted);font-size:12px">${escapeHtml(s.server || "—")}</td>
            <td style="font-size:12px">${escapeHtml(s.health || "—")}</td>
            <td style="font-size:12px">${Number(s.slaPercent || 0).toFixed(1)}%</td>
            <td><button class="snap-panel-annotation" data-date="${escapeHtml(s.date)}" title="Click to edit note">${escapeHtml(s.annotation || "Add note…")}</button></td>
            <td><button class="ghost snap-del-btn" data-date="${escapeHtml(s.date)}" style="color:var(--red);font-size:12px" type="button">Delete</button></td>
          </tr>`).join("")}
        </tbody>
      </table>`;
      body.querySelectorAll(".snap-del-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const date = btn.dataset.date;
          if (!confirm(`Delete snapshot for ${date}?`)) return;
          try {
            const r = await fetch("/api/snapshots", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({action: "delete", date}),
            });
            const j = await r.json();
            renderSnapshotPanelList(j.snapshots || []);
            snapshotHistoryCache = null;
            compareLocalSnapshots();
            showToast(`Snapshot ${date} deleted`);
          } catch (e) { showToast("Delete failed: " + e.message); }
        });
      });
      body.querySelectorAll(".snap-panel-annotation").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const date = btn.dataset.date;
          const note = prompt("Add a note for this snapshot:", btn.textContent === "Add note…" ? "" : btn.textContent);
          if (note === null) return;
          try {
            await fetch("/api/snapshots", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({action: "annotate", date, note}),
            });
            btn.textContent = note || "Add note…";
            showToast("Note saved");
          } catch (e) { showToast("Save failed: " + e.message); }
        });
      });
    }

    // Load auto-snapshot state on init
    fetch("/api/snapshots?action=auto-config", {cache: "no-store"})
      .then((r) => r.json())
      .then((j) => { if (j.ok) autoSnapshotToggle.checked = !!j.enabled; })
      .catch(() => {});

    // Show any already-saved snapshots on load (independent of connection state).
    refreshSnapshotStatus();

    document.getElementById("snapRangeTabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".snap-tab");
      if (!btn) return;
      document.querySelectorAll(".snap-tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      activeSnapshotRange = btn.dataset.range || "7d";
      compareLocalSnapshots();
    });

    showConnectionBtn.addEventListener("click", () => {
      document.body.classList.toggle("connection-open");
    });

    dashReportRange.addEventListener("change", () => {
      syncRangeToForm();
      if (latestDashboard) loadDashboard();
    });

    form.reportRange.addEventListener("change", () => {
      syncRangeToToolbar();
    });

    dashCustomStartDate.addEventListener("change", () => {
      syncRangeToForm();
      if (latestDashboard && dashReportRange.value === "custom") loadDashboard();
    });

    dashCustomEndDate.addEventListener("change", () => {
      syncRangeToForm();
      if (latestDashboard && dashReportRange.value === "custom") loadDashboard();
    });

    customStartDate.addEventListener("change", syncRangeToToolbar);
    customEndDate.addEventListener("change", syncRangeToToolbar);

    autoRefreshMode.addEventListener("change", scheduleAutoRefresh);
    refreshMinutes.addEventListener("change", scheduleAutoRefresh);
    themeSelect.addEventListener("change", () => applyTheme(themeSelect.value));
    smtpSecurity.addEventListener("change", syncSmtpSecurityFields);

    clearBtn.addEventListener("click", () => {
      form.reset();
      form.restApiPort.value = "9090";
      form.backupServerPort.value = "9090";
      form.apiMode.value = "auto";
      form.apiVersion.value = "auto";
      form.reportRange.value = "24h";
      customStartDate.value = "";
      customEndDate.value = "";
      syncRangeToToolbar();
      form.timeoutSeconds.value = "30";
      form.useWmiHealth.checked = true;
      form.useAuthcHeader.checked = true;
      form.verifyTls.checked = true;
      smtpPassword.value = "";
      syncSmtpSecurityFields();
      alertAutomationStatus.textContent = "Not scheduled";
      clearPassword();
      resetDashboard();
    });

    document.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => {
        const tbl = button.dataset.table;
        setActiveTable(tbl);
        if (tbl === "timeline") {
          renderTimeline();
        } else if (tbl === "heatmap") {
          renderHeatmap();
        } else {
          renderTable();
        }
      });
    });

    // ── Timeline (Gantt) ─────────────────────────────────────────────────────
    const timelineWrap = document.getElementById("timelineWrap");
    const tlTooltip    = document.getElementById("tlTooltip");

    function tlStatusClass(status) {
      const s = String(status || "").toLowerCase();
      if (s.includes("success") || s.includes("succeed") || s.includes("complete")) return "success";
      if (s.includes("fail") || s.includes("error") || s.includes("critical")) return "failed";
      if (s.includes("run") || s.includes("active") || s.includes("start")) return "running";
      if (s.includes("warn")) return "warning";
      return "unknown";
    }

    function parseTs(str) {
      if (!str) return NaN;
      const d = new Date(str);
      return isNaN(d.getTime()) ? NaN : d.getTime();
    }

    function parseDurationMs(val) {
      if (!val) return 0;
      const s = String(val);
      let total = 0;
      const d = s.match(/(\d+)d/); if (d) total += parseInt(d[1]) * 86400000;
      const h = s.match(/(\d+)h/); if (h) total += parseInt(h[1]) * 3600000;
      const m = s.match(/(\d+)m/); if (m) total += parseInt(m[1]) * 60000;
      const sec = s.match(/(\d+)s/); if (sec) total += parseInt(sec[1]) * 1000;
      if (total === 0 && /^\d+$/.test(s)) total = parseInt(s) * 1000;
      return total;
    }

    function renderTimeline() {
      tableWrap.classList.add("hidden");
      emptyState.classList.add("hidden");
      heatmapWrap.classList.add("hidden");
      timelineWrap.classList.remove("hidden");

      const jobs = latestDashboard?.tables?.jobs || [];
      if (!jobs.length) {
        timelineWrap.innerHTML = '<p style="padding:16px;color:var(--muted)">No job data to display.</p>';
        return;
      }

      // Build per-client job list
      const clientMap = new Map();
      let minTs = Infinity, maxTs = -Infinity;
      jobs.forEach((job) => {
        const ts = parseTs(job.started);
        if (isNaN(ts)) return;
        const dur = parseDurationMs(job.duration) || 600000;
        const end = ts + dur;
        minTs = Math.min(minTs, ts);
        maxTs = Math.max(maxTs, end);
        const key = job.client || "Unknown";
        if (!clientMap.has(key)) clientMap.set(key, []);
        clientMap.get(key).push({...job, _ts: ts, _end: end});
      });

      if (minTs === Infinity) {
        timelineWrap.innerHTML = '<p style="padding:16px;color:var(--muted)">No parseable timestamps in job data.</p>';
        return;
      }

      const clients = [...clientMap.keys()].sort();
      const ROW_H = 28, ROW_GAP = 6, LABEL_W = 160, AXIS_H = 28, PAD = 12;
      const totalW = Math.max(700, timelineWrap.clientWidth || 900) - PAD * 2;
      const chartW = totalW - LABEL_W;
      const totalH = clients.length * (ROW_H + ROW_GAP) + AXIS_H + PAD;
      const spanMs = maxTs - minTs || 3600000;

      function xOf(ts) { return LABEL_W + ((ts - minTs) / spanMs) * chartW; }

      // Axis ticks (up to 6)
      const tickCount = Math.min(6, Math.floor(chartW / 80));
      const tickLines = [];
      for (let i = 0; i <= tickCount; i++) {
        const ts = minTs + (i / tickCount) * spanMs;
        const x = xOf(ts);
        const label = new Date(ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
        tickLines.push(`<line x1="${x}" y1="${AXIS_H - 6}" x2="${x}" y2="${totalH}" stroke="var(--line)" stroke-width="1"/>`);
        tickLines.push(`<text class="tl-axis-label" x="${x}" y="${AXIS_H - 10}" text-anchor="middle">${escapeHtml(label)}</text>`);
      }

      const bars = [];
      clients.forEach((client, rowIdx) => {
        const y = AXIS_H + PAD / 2 + rowIdx * (ROW_H + ROW_GAP);
        const labelY = y + ROW_H / 2 + 4;
        bars.push(`<text class="tl-client-label" x="${LABEL_W - 8}" y="${labelY}" text-anchor="end">${escapeHtml(client.length > 22 ? client.slice(0, 20) + "…" : client)}</text>`);
        clientMap.get(client).forEach((job) => {
          const x1 = xOf(job._ts);
          const x2 = xOf(job._end);
          const bw = Math.max(4, x2 - x1);
          const cls = tlStatusClass(job.status);
          const tip = `${client}\n${job.name || "Job"}\nStatus: ${job.status}\nStarted: ${job.started}\nDuration: ${job.duration}`;
          bars.push(`<rect class="tl-bar ${cls}" x="${x1.toFixed(1)}" y="${y}" width="${bw.toFixed(1)}" height="${ROW_H}" data-tip="${escapeHtml(tip)}"/>`);
        });
      });

      timelineWrap.innerHTML = `
        <div class="timeline-svg-container">
          <svg width="${totalW}" height="${totalH}" style="display:block">
            ${tickLines.join("")}
            ${bars.join("")}
          </svg>
        </div>`;

      timelineWrap.querySelectorAll(".tl-bar").forEach((el) => {
        el.addEventListener("mousemove", (e) => {
          tlTooltip.style.display = "block";
          tlTooltip.style.left = (e.clientX + 14) + "px";
          tlTooltip.style.top  = (e.clientY - 8) + "px";
          tlTooltip.textContent = el.dataset.tip;
        });
        el.addEventListener("mouseleave", () => { tlTooltip.style.display = "none"; });
      });
    }

    // ── Heatmap ──────────────────────────────────────────────────────────────
    const heatmapWrap = document.getElementById("heatmapWrap");
    const hmTooltip   = document.getElementById("hmTooltip");

    function renderHeatmap() {
      tableWrap.classList.add("hidden");
      emptyState.classList.add("hidden");
      timelineWrap.classList.add("hidden");
      heatmapWrap.classList.remove("hidden");

      const clients = latestDashboard?.tables?.clients || [];
      const jobs    = latestDashboard?.tables?.jobs    || [];

      if (!clients.length) {
        heatmapWrap.innerHTML = '<p style="padding:16px;color:var(--muted)">No client data to display.</p>';
        return;
      }

      // Map client hostname → latest job status
      const statusMap = new Map();
      const lastJobTs = new Map();
      jobs.forEach((job) => {
        const key = (job.client || "").toLowerCase();
        const ts  = parseTs(job.started) || 0;
        if (!lastJobTs.has(key) || ts > lastJobTs.get(key)) {
          lastJobTs.set(key, ts);
          statusMap.set(key, {status: job.status, started: job.started, name: job.name});
        }
      });

      const legend = `
        <div class="heatmap-legend">
          <span><span class="heatmap-legend-dot" style="background:var(--green)"></span>Success</span>
          <span><span class="heatmap-legend-dot" style="background:var(--red)"></span>Failed</span>
          <span><span class="heatmap-legend-dot" style="background:var(--blue)"></span>Running</span>
          <span><span class="heatmap-legend-dot" style="background:var(--amber)"></span>Warning</span>
          <span><span class="heatmap-legend-dot" style="background:var(--line)"></span>No recent job</span>
        </div>`;

      const cells = clients.map((c) => {
        const key  = (c.hostname || "").toLowerCase();
        const info = statusMap.get(key);
        const cls  = info ? tlStatusClass(info.status) : "none";
        const init = (c.hostname || "?").charAt(0).toUpperCase();
        const tip  = info
          ? `${c.hostname}\nLast job: ${info.name || "—"}\nStatus: ${info.status}\nStarted: ${info.started}`
          : `${c.hostname}\nNo backup job in current window`;
        return `<div class="heatmap-cell ${cls}" data-tip="${escapeHtml(tip)}" title="">${escapeHtml(init)}</div>`;
      }).join("");

      heatmapWrap.innerHTML = legend + `<div class="heatmap-grid">${cells}</div>`;

      heatmapWrap.querySelectorAll(".heatmap-cell").forEach((el) => {
        el.addEventListener("mousemove", (e) => {
          hmTooltip.style.display = "block";
          hmTooltip.style.left = (e.clientX + 14) + "px";
          hmTooltip.style.top  = (e.clientY - 8) + "px";
          hmTooltip.textContent = el.dataset.tip;
        });
        el.addEventListener("mouseleave", () => { hmTooltip.style.display = "none"; });
      });
    }

    // ── Multi-server ─────────────────────────────────────────────────────────
    const addServerBtn        = document.getElementById("addServerBtn");
    const addServerModal      = document.getElementById("addServerModal");
    const addServerModalClose = document.getElementById("addServerModalCloseBtn");
    const addServerCancelBtn  = document.getElementById("addServerCancelBtn");
    const addServerConnectBtn = document.getElementById("addServerConnectBtn");
    const addServerStatus     = document.getElementById("addServerStatus");
    const multiServerSection  = document.getElementById("multiServerSection");
    const serverCards         = document.getElementById("serverCards");
    const multiServerMeta     = document.getElementById("multiServerMeta");

    const extraServers = [];  // [{sessionId, host, summary}]

    function openAddServerModal() {
      addServerModal.classList.add("open");
      addServerModal.setAttribute("aria-hidden", "false");
      addServerStatus.textContent = "";
      setTimeout(() => document.getElementById("asHost").focus(), 0);
    }
    function closeAddServerModal() {
      addServerModal.classList.remove("open");
      addServerModal.setAttribute("aria-hidden", "true");
    }

    async function connectExtraServer() {
      const host     = document.getElementById("asHost").value.trim();
      const port     = document.getElementById("asPort").value.trim() || "9090";
      const username = document.getElementById("asUsername").value.trim();
      const password = document.getElementById("asPassword").value;
      const apiMode  = document.getElementById("asApiMode").value;
      if (!host || !username || !password) {
        addServerStatus.textContent = "Host, username, and password are required.";
        return;
      }
      addServerConnectBtn.disabled = true;
      addServerStatus.textContent = "Connecting…";
      try {
        const payload = {
          restApiHost: host, restApiPort: parseInt(port, 10) || 9090,
          backupServerHost: "", backupServerPort: 9090,
          username, password, apiMode, apiVersion: "auto",
          reportRange: "24h", customStartDate: "", customEndDate: "",
          useWmiHealth: false, wmiUsername: "", wmiPassword: "",
          timeoutSeconds: 30, verifyTls: false, useAuthcHeader: true,
        };
        const resp = await fetch("/api/dashboard", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        extraServers.push({sessionId: data.sessionId, host, summary: data.summary || {}});
        document.getElementById("asPassword").value = "";
        closeAddServerModal();
        renderServerCards();
      } catch (err) {
        addServerStatus.textContent = err.message || "Connection failed.";
      } finally {
        addServerConnectBtn.disabled = false;
      }
    }

    function serverCardBadge(summary) {
      const h = String(summary?.health || "unknown").toLowerCase();
      if (h === "ok" || h === "good") return ["ok", "OK"];
      if (h === "critical" || h === "bad") return ["bad", "Critical"];
      if (h === "warning" || h === "warn") return ["warn", "Warning"];
      return ["load", "Unknown"];
    }

    function renderServerCards() {
      if (!extraServers.length) {
        multiServerSection.classList.add("hidden");
        return;
      }
      multiServerSection.classList.remove("hidden");
      multiServerMeta.textContent = `${extraServers.length} server${extraServers.length !== 1 ? "s" : ""}`;
      serverCards.innerHTML = extraServers.map((srv, idx) => {
        const [badgeCls, badgeLabel] = serverCardBadge(srv.summary);
        const s = srv.summary;
        return `<div class="server-card">
          <div class="server-card-head">
            <span class="server-card-host">${escapeHtml(srv.host)}</span>
            <span class="server-card-badge ${badgeCls}">${escapeHtml(badgeLabel)}</span>
          </div>
          <div class="server-card-stats">
            <span>Jobs</span><strong>${numberValue(s.totalJobs)}</strong>
            <span>Failed</span><strong>${numberValue(s.failedJobs)}</strong>
            <span>Active</span><strong>${numberValue(s.activeJobs)}</strong>
            <span>Alerts</span><strong>${numberValue(s.totalAlerts)}</strong>
          </div>
          <button class="server-card-remove" data-idx="${idx}">Remove</button>
        </div>`;
      }).join("");
      serverCards.querySelectorAll(".server-card-remove").forEach((btn) => {
        btn.addEventListener("click", () => {
          extraServers.splice(parseInt(btn.dataset.idx, 10), 1);
          renderServerCards();
        });
      });
    }

    addServerBtn.addEventListener("click", openAddServerModal);
    addServerModalClose.addEventListener("click", closeAddServerModal);
    addServerCancelBtn.addEventListener("click", closeAddServerModal);
    addServerModal.addEventListener("click", (e) => { if (e.target === addServerModal) closeAddServerModal(); });
    addServerConnectBtn.addEventListener("click", connectExtraServer);

    // ── Share ─────────────────────────────────────────────────────────────────
    const shareBtn           = document.getElementById("shareBtn");
    const shareModal         = document.getElementById("shareModal");
    const shareModalClose    = document.getElementById("shareModalCloseBtn");
    const generateShareToken = document.getElementById("generateShareTokenBtn");
    const revokeShareToken   = document.getElementById("revokeShareTokenBtn");
    const shareTokenSection  = document.getElementById("shareTokenSection");
    const shareUrlInput      = document.getElementById("shareUrlInput");
    const copyShareUrlBtn    = document.getElementById("copyShareUrlBtn");
    const shareModalStatus   = document.getElementById("shareModalStatus");

    let currentShareToken = null;

    function openShareModal() {
      shareModal.classList.add("open");
      shareModal.setAttribute("aria-hidden", "false");
      shareModalStatus.textContent = "";
    }
    function closeShareModal() {
      shareModal.classList.remove("open");
      shareModal.setAttribute("aria-hidden", "true");
    }

    async function doGenerateShareToken() {
      if (!sessionId) {
        shareModalStatus.textContent = "Connect to a NetWorker server first.";
        return;
      }
      generateShareToken.disabled = true;
      shareModalStatus.textContent = "Generating…";
      try {
        const resp = await fetch("/api/share", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({sessionId, action: "create"}),
          cache: "no-store",
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        currentShareToken = data.token;
        const viewUrl = `${location.origin}/view/${data.token}`;
        shareUrlInput.value = viewUrl;
        shareTokenSection.classList.remove("hidden");
        revokeShareToken.classList.remove("hidden");
        generateShareToken.textContent = "Regenerate";
        shareModalStatus.textContent = "Link valid for 24 hours.";
      } catch (err) {
        shareModalStatus.textContent = err.message || "Failed to generate link.";
      } finally {
        generateShareToken.disabled = false;
      }
    }

    async function doRevokeShareToken() {
      if (!currentShareToken) return;
      revokeShareToken.disabled = true;
      try {
        await fetch("/api/share", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({token: currentShareToken, action: "revoke"}),
          cache: "no-store",
        });
        currentShareToken = null;
        shareTokenSection.classList.add("hidden");
        revokeShareToken.classList.add("hidden");
        generateShareToken.textContent = "Generate Link";
        shareModalStatus.textContent = "Link revoked.";
        shareUrlInput.value = "";
      } catch (err) {
        shareModalStatus.textContent = "Revoke failed.";
      } finally {
        revokeShareToken.disabled = false;
      }
    }

    document.getElementById("logoutBtn").addEventListener("click", () => {
      stopSSE();
      profileSelect.value = "";
      clearPassword();
      resetDashboard();
    });
    shareBtn.addEventListener("click", openShareModal);
    shareModalClose.addEventListener("click", closeShareModal);
    shareModal.addEventListener("click", (e) => { if (e.target === shareModal) closeShareModal(); });
    generateShareToken.addEventListener("click", doGenerateShareToken);
    revokeShareToken.addEventListener("click", doRevokeShareToken);
    copyShareUrlBtn.addEventListener("click", () => {
      if (!shareUrlInput.value) return;
      navigator.clipboard.writeText(shareUrlInput.value).then(() => {
        copyShareUrlBtn.textContent = "Copied!";
        setTimeout(() => { copyShareUrlBtn.textContent = "Copy"; }, 2000);
      }).catch(() => {
        shareUrlInput.select();
        document.execCommand("copy");
      });
    });

    // ── Connection Profiles ───────────────────────────────────────────────────
    const profileSelect    = document.getElementById("profileSelect");
    const profileSaveBtn   = document.getElementById("profileSaveBtn");
    const profileDeleteBtn = document.getElementById("profileDeleteBtn");
    const PROFILES_KEY     = "nw_dashboard_profiles";

    function loadProfiles() {
      try { return JSON.parse(localStorage.getItem(PROFILES_KEY) || "{}"); }
      catch (e) { return {}; }
    }
    function saveProfiles(profiles) {
      try { localStorage.setItem(PROFILES_KEY, JSON.stringify(profiles)); } catch (e) {}
    }
    async function fetchProfiles() {
      try {
        const r = await fetch("/api/profiles");
        if (r.ok) {
          const j = await r.json();
          if (j.profiles) { saveProfiles(j.profiles); refreshProfileList(); }
        }
      } catch (e) {}
    }
    function refreshProfileList() {
      const profiles = loadProfiles();
      const current = profileSelect.value;
      profileSelect.innerHTML = '<option value="">— Select saved profile —</option>';
      Object.keys(profiles).sort().forEach((name) => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        profileSelect.appendChild(opt);
      });
      if (current && profiles[current]) profileSelect.value = current;
    }
    function profileFormValues() {
      return {
        restApiHost: form.restApiHost.value.trim(),
        restApiPort: form.restApiPort.value.trim(),
        backupServerHost: form.backupServerHost.value.trim(),
        backupServerPort: form.backupServerPort.value.trim(),
        username: form.username.value.trim(),
        apiMode: form.apiMode.value,
        apiVersion: form.apiVersion.value,
        reportRange: form.reportRange.value,
        timeoutSeconds: form.timeoutSeconds.value,
        useWmiHealth: form.useWmiHealth.checked,
        wmiUsername: form.wmiUsername.value.trim(),
        verifyTls: form.verifyTls.checked,
        useAuthcHeader: form.useAuthcHeader.checked,
      };
    }
    function applyProfile(profile) {
      if (!profile) return;
      form.restApiHost.value      = profile.restApiHost      || "";
      form.restApiPort.value      = profile.restApiPort      || "9090";
      form.backupServerHost.value = profile.backupServerHost || "";
      form.backupServerPort.value = profile.backupServerPort || "9090";
      form.username.value         = profile.username         || "";
      form.apiMode.value          = profile.apiMode          || "auto";
      form.apiVersion.value       = profile.apiVersion       || "auto";
      form.reportRange.value      = profile.reportRange      || "24h";
      form.timeoutSeconds.value   = profile.timeoutSeconds   || "30";
      form.useWmiHealth.checked   = !!profile.useWmiHealth;
      form.wmiUsername.value      = profile.wmiUsername      || "";
      form.verifyTls.checked      = !!profile.verifyTls;
      form.useAuthcHeader.checked = profile.useAuthcHeader !== false;
      // "(saved)" means server has encrypted password — keep as-is; getPayload() sends sentinel
      form.password.value    = profile.password    || "";
      form.wmiPassword.value = profile.wmiPassword || "";
      syncRangeToToolbar();
    }
    profileSelect.addEventListener("change", () => {
      const profiles = loadProfiles();
      applyProfile(profiles[profileSelect.value]);
    });
    profileSaveBtn.addEventListener("click", async () => {
      const name = prompt("Profile name:", profileSelect.value || form.restApiHost.value || "My Server");
      if (!name) return;
      const data = profileFormValues();
      const pw  = form.password.value;
      const wpw = form.wmiPassword.value;
      if (pw  && pw  !== "(saved)") data.password    = pw;
      if (wpw && wpw !== "(saved)") data.wmiPassword = wpw;
      try {
        const resp = await fetch("/api/profiles", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ action: "save", name, data }),
        });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || "Save failed");
        saveProfiles(json.profiles || {});
        refreshProfileList();
        profileSelect.value = name;
        showToast(`Profile "${name}" saved`);
      } catch (e) { showToast("Profile save failed: " + e.message); }
    });
    profileDeleteBtn.addEventListener("click", async () => {
      const name = profileSelect.value;
      if (!name) return;
      if (!confirm(`Delete profile "${name}"?`)) return;
      try {
        const resp = await fetch("/api/profiles", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ action: "delete", name }),
        });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || "Delete failed");
        saveProfiles(json.profiles || {});
        refreshProfileList();
        showToast(`Profile "${name}" deleted`);
      } catch (e) { showToast("Profile delete failed: " + e.message); }
    });
    fetchProfiles();

    // ── Collapsed connection bar ──────────────────────────────────────────────
    const connCollapsedBar = document.getElementById("connCollapsedBar");
    const collapsedHost    = document.getElementById("collapsedHost");
    const collapsedRange   = document.getElementById("collapsedRange");
    const collapsedEditBtn = document.getElementById("collapsedEditBtn");

    function updateCollapsedBar(host, rangeLabel) {
      collapsedHost.textContent  = host || "—";
      collapsedRange.textContent = rangeLabel ? `· ${rangeLabel}` : "";
    }
    collapsedEditBtn.addEventListener("click", () => {
      document.body.classList.toggle("connection-open");
    });

    // ── SSE live push ─────────────────────────────────────────────────────────
    let sseSource = null;
    function startSSE() {
      if (sseSource) return;
      if (!window.EventSource) return;
      sseSource = new EventSource("/api/stream");
      sseSource.addEventListener("dashboard", (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data && data.ok && latestDashboard) {
            diffAndNotify(latestDashboard, data);
            renderDashboard(data);
          }
        } catch (_) {}
      });
      sseSource.addEventListener("error", () => {
        sseSource.close();
        sseSource = null;
        setTimeout(startSSE, 15000);
      });
    }
    function stopSSE() {
      if (sseSource) { sseSource.close(); sseSource = null; }
    }

    // ── Browser push notifications ────────────────────────────────────────────
    let notifyPermission = (typeof Notification !== "undefined") ? Notification.permission : "denied";
    function requestNotifyPermission() {
      if (typeof Notification === "undefined" || notifyPermission === "granted") return;
      Notification.requestPermission().then((p) => { notifyPermission = p; });
    }
    function sendBrowserNotification(title, body, tag) {
      if (notifyPermission !== "granted" || typeof Notification === "undefined") return;
      try {
        new Notification(title, {body, tag, icon: "/favicon.ico"});
      } catch (_) {}
    }

    // ── "What changed" toast ──────────────────────────────────────────────────
    const changeToast = document.getElementById("changeToast");
    let toastTimer    = null;

    function showToast(message, tone) {
      changeToast.textContent = message;
      changeToast.className   = `change-toast show ${tone || ""}`;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { changeToast.className = "change-toast"; }, 6000);
    }

    function diffAndNotify(prev, next) {
      if (!prev || !next) return;
      const ps = prev.summary || {}, ns = next.summary || {};
      const prevFailed = Number(ps.failedJobs || 0);
      const nextFailed = Number(ns.failedJobs || 0);
      const prevAlerts = Number(ps.totalAlerts || 0);
      const nextAlerts = Number(ns.totalAlerts || 0);
      const prevHealth = String(ps.health || ""), nextHealth = String(ns.health || "");
      if (nextFailed > prevFailed) {
        const delta = nextFailed - prevFailed;
        const msg = `⚠ ${delta} new failure${delta > 1 ? "s" : ""} detected`;
        showToast(msg, "bad");
        sendBrowserNotification("NetWorker Alert", msg, "failures");
        document.title = `(${nextFailed}) NetWorker Dashboard`;
      } else if (prevFailed > 0 && nextFailed === 0) {
        showToast("✓ All failures resolved", "ok");
        document.title = "NetWorker Dashboard";
      } else if (nextAlerts > prevAlerts) {
        showToast(`⚠ ${nextAlerts - prevAlerts} new alert(s)`, "warn");
      } else if (nextHealth !== prevHealth && nextHealth === "ok" && prevHealth !== "") {
        showToast("✓ Dashboard status is now healthy", "ok");
      }
    }

    // ── Pagination ────────────────────────────────────────────────────────────
    const paginationBar  = document.getElementById("paginationBar");
    const paginationMeta = document.getElementById("paginationMeta");
    const showMoreBtn    = document.getElementById("showMoreBtn");
    const showAllBtn     = document.getElementById("showAllBtn");
    const PAGE_SIZE      = 25;
    let   pageLimit      = PAGE_SIZE;

    function renderTablePage(rows, def) {
      const showing = Math.min(pageLimit, rows.length);
      const visible = rows.slice(0, showing);
      tableBody.innerHTML = visible.map((row) => {
        return `<tr data-row='${escapeHtml(JSON.stringify(row))}'>${def.columns.map(([key]) => {
          const value = row[key];
          if (key === "status" || key === "severity" || key === "priority") {
            return `<td><span class="badge ${badgeClass(value)}">${escapeHtml(value)}</span></td>`;
          }
          const muted = value ? "" : " cell-muted";
          return `<td class="${muted}">${escapeHtml(value)}</td>`;
        }).join("")}</tr>`;
      }).join("");
      // Wire row clicks for drawer
      tableBody.querySelectorAll("tr").forEach((tr) => {
        tr.addEventListener("click", () => {
          try { openJobDrawer(JSON.parse(tr.dataset.row || "{}")); } catch (_) {}
        });
      });
      paginationMeta.textContent = `Showing ${showing} of ${rows.length}`;
      if (showing < rows.length) {
        paginationBar.classList.remove("hidden");
        showMoreBtn.disabled = false;
        showAllBtn.disabled  = false;
      } else {
        paginationBar.classList.add("hidden");
      }
    }

    showMoreBtn.addEventListener("click", () => {
      pageLimit += PAGE_SIZE;
      renderTable();
    });
    showAllBtn.addEventListener("click", () => {
      pageLimit = Infinity;
      renderTable();
    });

    // ── Job detail drawer ─────────────────────────────────────────────────────
    const jobDetailDrawer = document.getElementById("jobDetailDrawer");
    const drawerOverlay   = document.getElementById("drawerOverlay");
    const drawerBody      = document.getElementById("drawerBody");
    const drawerTitle     = document.getElementById("drawerTitle");
    const drawerCloseBtn  = document.getElementById("drawerCloseBtn");

    function openJobDrawer(row) {
      drawerTitle.textContent = row.name || row.client || "Job Details";
      const fields = [
        ["Client",    row.client],
        ["Job Name",  row.name],
        ["Policy",    row.policy],
        ["Status",    row.status],
        ["Started",   row.started],
        ["Duration",  row.duration],
        ["Size",      row.size],
        ["Message",   row.message || "—"],
      ];
      drawerBody.innerHTML = fields.map(([label, value]) => `
        <div class="detail-field">
          <div class="detail-label">${escapeHtml(label)}</div>
          <div class="detail-value">${escapeHtml(value || "—")}</div>
        </div>`).join("") +
        `<button class="ghost detail-copy-btn" id="drawerCopyBtn">Copy all to clipboard</button>`;
      document.getElementById("drawerCopyBtn").addEventListener("click", () => {
        const text = fields.map(([l, v]) => `${l}: ${v || "—"}`).join("\n");
        navigator.clipboard.writeText(text).catch(() => {});
        document.getElementById("drawerCopyBtn").textContent = "Copied!";
        setTimeout(() => { const b = document.getElementById("drawerCopyBtn"); if (b) b.textContent = "Copy all to clipboard"; }, 2000);
      });
      jobDetailDrawer.classList.add("open");
      drawerOverlay.classList.add("open");
    }
    function closeJobDrawer() {
      jobDetailDrawer.classList.remove("open");
      drawerOverlay.classList.remove("open");
    }
    drawerCloseBtn.addEventListener("click", closeJobDrawer);
    drawerOverlay.addEventListener("click", closeJobDrawer);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeJobDrawer(); });

    refreshBtn.disabled = false;
    syncRangeToToolbar();
    applyTheme((() => {
      try { return localStorage.getItem("nw_dashboard_theme") || "default"; }
      catch (error) { return "default"; }
    })());
    syncSmtpSecurityFields();
    exportBtn.disabled = true;
    snapshotSaveBtn.disabled = true;
    requestNotifyPermission();
    fetchProfiles();
    compareLocalSnapshots();
    loadSharedDashboard();
    startSSE();
  </script>
</body>
</html>
"""


def networker_logo_src() -> str:
    if NETWORKER_LOGO_PATH.exists():
        try:
            encoded = base64.b64encode(NETWORKER_LOGO_PATH.read_bytes()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        except OSError:
            pass
    encoded = NETWORKER_LOGO_PNG_BASE64.strip()
    if encoded:
        return f"data:image/png;base64,{encoded}"
    encoded = base64.b64encode(FAVICON_SVG).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def dashboard_html() -> str:
    return HTML_PAGE.replace("__NETWORKER_LOGO_SRC__", networker_logo_src())


@dataclass(frozen=True)
class ApiConfig:
    rest_api_host: str
    rest_api_port: int
    backup_server_host: str
    backup_server_port: int
    username: str
    password: str
    api_mode: str
    api_version: str
    report_range: str
    custom_start_date: str
    custom_end_date: str
    use_wmi_health: bool
    wmi_username: str
    wmi_password: str
    timeout_seconds: int
    verify_tls: bool
    use_authc_header: bool


@dataclass
class DashboardSession:
    config: ApiConfig
    cookie_jar: CookieJar
    auth_headers: dict[str, str]
    encrypted_networker_password: str
    encrypted_wmi_password: str
    created_at: float
    last_used: float
    server_protection_job: dict[str, Any] = field(default_factory=dict)


DASHBOARD_SESSIONS: dict[str, DashboardSession] = {}
SHARED_DASHBOARD_LOCK = threading.Lock()
SHARED_DASHBOARD_STATE: dict[str, Any] = {
    "sessionId": "",
    "dashboard": None,
    "updatedAt": 0.0,
    "lastRefresh": "",
    "lastError": "",
}
SHARED_REFRESH_STOP = threading.Event()


@dataclass
class AlertAutomation:
    automation_id: str
    session_id: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    encrypted_smtp_password: str
    smtp_from: str
    recipients: list[str]
    smtp_security: str
    interval_minutes: int
    trigger: str
    schedule_type: str
    report_time: str
    created_at: float
    theme: str = "default"
    last_run: float = 0.0
    last_result: str = "Scheduled"
    last_signature: str = ""
    timer: threading.Timer | None = None


ALERT_AUTOMATIONS: dict[str, AlertAutomation] = {}

# One reentrant lock guards both global registries. Reentrant so nested calls
# (cleanup -> cancel_session_automations -> cancel_alert_automation) cannot
# self-deadlock. Invariant: never hold REGISTRY_LOCK across network I/O —
# snapshot what you need under the lock, release, then call out.
REGISTRY_LOCK = threading.RLock()


def _get_session(session_id: str) -> "DashboardSession | None":
    with REGISTRY_LOCK:
        return DASHBOARD_SESSIONS.get(session_id)


def _put_session(session_id: str, session: Any) -> None:
    with REGISTRY_LOCK:
        DASHBOARD_SESSIONS[session_id] = session


def _pop_session(session_id: str) -> Any:
    with REGISTRY_LOCK:
        return DASHBOARD_SESSIONS.pop(session_id, None)


def _session_exists(session_id: str) -> bool:
    with REGISTRY_LOCK:
        return session_id in DASHBOARD_SESSIONS


def _session_items_snapshot() -> list[tuple[str, Any]]:
    with REGISTRY_LOCK:
        return list(DASHBOARD_SESSIONS.items())


def _session_ids_snapshot() -> list[str]:
    with REGISTRY_LOCK:
        return list(DASHBOARD_SESSIONS.keys())


def _get_automation(key: str) -> "AlertAutomation | None":
    with REGISTRY_LOCK:
        return ALERT_AUTOMATIONS.get(key)


def _put_automation(key: str, automation: Any) -> None:
    with REGISTRY_LOCK:
        ALERT_AUTOMATIONS[key] = automation


def _pop_automation(key: str) -> Any:
    with REGISTRY_LOCK:
        return ALERT_AUTOMATIONS.pop(key, None)


def _automation_items_snapshot() -> list[tuple[str, Any]]:
    with REGISTRY_LOCK:
        return list(ALERT_AUTOMATIONS.items())


def _automation_keys_snapshot() -> list[str]:
    with REGISTRY_LOCK:
        return list(ALERT_AUTOMATIONS.keys())


# ── SSE clients ──────────────────────────────────────────────────────────────
SSE_CLIENTS: list[Any] = []
SSE_CLIENTS_LOCK = threading.Lock()
DEFAULT_MAX_SSE_CLIENTS = 50
MAX_SSE_CLIENTS = DEFAULT_MAX_SSE_CLIENTS


def _sse_register(wfile: Any) -> bool:
    """Register an SSE client if under cap. Returns False when full."""
    with SSE_CLIENTS_LOCK:
        if len(SSE_CLIENTS) >= MAX_SSE_CLIENTS:
            return False
        SSE_CLIENTS.append(wfile)
        return True


def sse_broadcast(event: str, data: str) -> None:
    payload = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
    with SSE_CLIENTS_LOCK:
        clients = list(SSE_CLIENTS)
    dead = []
    for wfile in clients:
        try:
            wfile.write(payload)
            wfile.flush()
        except OSError:
            dead.append(wfile)
    if dead:
        with SSE_CLIENTS_LOCK:
            for wfile in dead:
                try:
                    SSE_CLIENTS.remove(wfile)
                except ValueError:
                    pass


# ── Connection profiles ───────────────────────────────────────────────────────
PROFILES_LOCK = threading.Lock()
SNAPSHOTS_LOCK = threading.Lock()


def load_profiles() -> dict[str, Any]:
    try:
        if PROFILES_FILE.exists():
            return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_profiles(profiles: dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, separators=(",", ":")), encoding="utf-8")
        tmp.replace(PROFILES_FILE)
    except OSError:
        pass


def _mask_profiles(profiles: dict[str, Any]) -> dict[str, Any]:
    """Return profiles safe to send to browser — encrypted blobs replaced with sentinel."""
    masked = {}
    for name, prof in profiles.items():
        m = dict(prof)
        if m.pop("_enc_password", ""):
            m["password"] = _PROFILE_PW_SAVED
        if m.pop("_enc_wmiPassword", ""):
            m["wmiPassword"] = _PROFILE_PW_SAVED
        masked[name] = m
    return masked


def _resolve_profile_password(payload: dict[str, Any]) -> dict[str, Any]:
    """If password is sentinel, look up and decrypt from saved profile."""
    profile_name = str(payload.get("profileName") or "").strip()
    pw   = str(payload.get("password")    or "")
    wpw  = str(payload.get("wmiPassword") or "")
    if not profile_name:
        return payload
    if pw not in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED) and wpw not in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
        return payload
    with PROFILES_LOCK:
        profiles = load_profiles()
    prof = profiles.get(profile_name, {})
    result = dict(payload)
    if pw in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
        result["password"] = decrypt_profile_secret(prof.get("_enc_password", ""))
    if wpw in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
        result["wmiPassword"] = decrypt_profile_secret(prof.get("_enc_wmiPassword", ""))
    return result


# token → {session_id, created_at}
SHARE_TOKENS: dict[str, dict[str, Any]] = {}
SHARE_TOKEN_LOCK = threading.Lock()
SHARE_TOKEN_TTL_SECONDS = 86400  # 24 h


def create_share_token(session_id: str) -> str:
    token = str(uuid.uuid4()).replace("-", "")
    now = time.time()
    with SHARE_TOKEN_LOCK:
        # Purge expired tokens first
        expired = [t for t, v in SHARE_TOKENS.items() if now - v["created_at"] > SHARE_TOKEN_TTL_SECONDS]
        for t in expired:
            del SHARE_TOKENS[t]
        # Revoke any existing token for this session
        stale = [t for t, v in SHARE_TOKENS.items() if v["session_id"] == session_id]
        for t in stale:
            del SHARE_TOKENS[t]
        SHARE_TOKENS[token] = {"session_id": session_id, "created_at": now}
    return token


def revoke_share_token(token: str) -> bool:
    with SHARE_TOKEN_LOCK:
        if token in SHARE_TOKENS:
            del SHARE_TOKENS[token]
            return True
    return False


def validate_share_token(token: str) -> str | None:
    """Return session_id if token valid and not expired, else None."""
    with SHARE_TOKEN_LOCK:
        entry = SHARE_TOKENS.get(token)
    if not entry:
        return None
    if time.time() - entry["created_at"] > SHARE_TOKEN_TTL_SECONDS:
        revoke_share_token(token)
        return None
    return entry["session_id"]


def automation_key(session_id: str, schedule_type: str) -> str:
    return f"{session_id}:{schedule_type}"


def session_automation_keys(session_id: str) -> list[str]:
    prefix = f"{session_id}:"
    return [
        key
        for key, automation in _automation_items_snapshot()
        if key == session_id or key.startswith(prefix) or automation.session_id == session_id
    ]


def active_automation_summary(session_id: str) -> str:
    labels: list[str] = []
    for key in session_automation_keys(session_id):
        automation = _get_automation(key)
        if not automation:
            continue
        if automation.schedule_type == "daily_report":
            labels.append(f"Daily dashboard report at {automation.report_time}")
        else:
            labels.append(f"Alerts every {automation.interval_minutes} minute(s)")
    return "; ".join(labels)


def existing_smtp_automation(session_id: str, schedule_type: str) -> AlertAutomation | None:
    same_type = _get_automation(automation_key(session_id, schedule_type))
    if same_type:
        return same_type
    for key in session_automation_keys(session_id):
        automation = _get_automation(key)
        if automation and automation.encrypted_smtp_password:
            return automation
    return None


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


def persist_last_good_dashboard(session_id: str, dashboard: dict[str, Any]) -> None:
    if not session_id or not isinstance(dashboard, dict) or dashboard.get("stale"):
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "sessionId": session_id,
            "dashboard": json_clone(dashboard),
            "updatedAt": time.time(),
            "lastRefresh": generated_at(),
        }
        tmp = LAST_GOOD_DASHBOARD_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")
        tmp.replace(LAST_GOOD_DASHBOARD_FILE)
    except (OSError, TypeError, ValueError):
        pass


def load_last_good_dashboard_record() -> dict[str, Any] | None:
    try:
        record = json.loads(LAST_GOOD_DASHBOARD_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dashboard = record.get("dashboard") if isinstance(record, dict) else None
    session_id = str(record.get("sessionId") or "") if isinstance(record, dict) else ""
    if not session_id or not isinstance(dashboard, dict) or not dashboard.get("ok"):
        return None
    if not dashboard_backup_source_available(dashboard):
        return None
    return {
        "sessionId": session_id,
        "dashboard": json_clone(dashboard),
        "updatedAt": float(record.get("updatedAt") or 0),
        "lastRefresh": str(record.get("lastRefresh") or ""),
    }


def cached_reliable_dashboard_for_session(session_id: str) -> dict[str, Any] | None:
    cached = shared_reliable_dashboard_for_session(session_id)
    if cached:
        return cached
    record = load_last_good_dashboard_record()
    if record and record.get("sessionId") == session_id:
        return record["dashboard"]
    return None


def stale_dashboard_from_cache(
    session_id: str,
    refresh_body: dict[str, Any],
    refresh_error: str = "",
) -> dict[str, Any] | None:
    cached = cached_reliable_dashboard_for_session(session_id)
    if not cached:
        return None
    stale = json_clone(cached)
    stale["sessionId"] = session_id
    stale["stale"] = True
    stale["liveRefreshFailedAt"] = generated_at()
    detail = refresh_error or dashboard_backup_source_error(refresh_body)
    stale["reportNotice"] = (
        "Live backup activity refresh is temporarily unavailable. "
        "Showing the last successful dashboard snapshot until NetWorker returns current backup activity."
    )
    sources = stale.get("sources") if isinstance(stale.get("sources"), dict) else {}
    stale["sources"] = {
        **sources,
        "liveRefresh": {
            "ok": False,
            "path": "live-refresh",
            "error": safe_log_text(detail, 500),
            "userMessage": stale["reportNotice"],
            "severity": "info",
            "displayWarning": False,
            "diagnosticOnly": True,
        },
    }
    summary = stale.get("summary") if isinstance(stale.get("summary"), dict) else {}
    if summary.get("health") not in ("critical", "warning"):
        summary["health"] = "warning"
    stale["summary"] = summary
    return stale


def set_shared_dashboard(session_id: str, dashboard: dict[str, Any]) -> None:
    if not session_id or not isinstance(dashboard, dict) or not dashboard.get("ok"):
        return
    shared = json_clone(dashboard)
    shared["sessionId"] = session_id
    if shared.get("stale"):
        with SHARED_DASHBOARD_LOCK:
            SHARED_DASHBOARD_STATE["lastError"] = str(shared.get("reportNotice") or "Live refresh is using cached data.")
        try:
            sse_broadcast("dashboard", json.dumps(shared, separators=(",", ":")))
        except Exception:
            pass
        return
    if not dashboard_backup_source_available(shared):
        with SHARED_DASHBOARD_LOCK:
            SHARED_DASHBOARD_STATE["lastError"] = dashboard_backup_source_error(shared)
        return
    with SHARED_DASHBOARD_LOCK:
        SHARED_DASHBOARD_STATE.update(
            {
                "sessionId": session_id,
                "dashboard": shared,
                "updatedAt": time.time(),
                "lastRefresh": generated_at(),
                "lastError": "",
            }
        )
    persist_last_good_dashboard(session_id, shared)
    # Push to SSE subscribers
    try:
        sse_broadcast("dashboard", json.dumps(shared, separators=(",", ":")))
    except Exception:
        pass


def shared_dashboard_payload() -> dict[str, Any]:
    with SHARED_DASHBOARD_LOCK:
        dashboard = SHARED_DASHBOARD_STATE.get("dashboard")
        if not isinstance(dashboard, dict):
            record = load_last_good_dashboard_record()
            if record:
                return {
                    "ok": True,
                    "sessionId": record["sessionId"],
                    "dashboard": record["dashboard"],
                    "updatedAt": record.get("lastRefresh") or "",
                    "lastError": "Loaded the last successful dashboard snapshot from local disk.",
                    "snapshotSummary": snapshot_summary_text(),
                }
            return {
                "ok": False,
                "message": "No shared NetWorker dashboard session is active.",
                "snapshotSummary": snapshot_summary_text(),
            }
        return {
            "ok": True,
            "sessionId": SHARED_DASHBOARD_STATE.get("sessionId") or dashboard.get("sessionId") or "",
            "dashboard": json_clone(dashboard),
            "updatedAt": SHARED_DASHBOARD_STATE.get("lastRefresh") or "",
            "lastError": SHARED_DASHBOARD_STATE.get("lastError") or "",
            "snapshotSummary": snapshot_summary_text(),
        }


def _shared_dashboard_refresh_once() -> None:
    with SHARED_DASHBOARD_LOCK:
        session_id = str(SHARED_DASHBOARD_STATE.get("sessionId") or "")
    if not session_id:
        return

    status, dashboard = build_dashboard_from_session(session_id)

    if status < 400 and dashboard.get("ok") and dashboard_backup_source_available(dashboard):
        set_shared_dashboard(session_id, dashboard)
        return

    # Session expired or auth failure — attempt silent reauth then retry once
    if status in (401, 403) or not _get_session(session_id):
        session = _get_session(session_id)
        if session:
            config = session_config_with_secrets(session)
            debug_log(f"shared_refresh: session {session_id[:8]}… auth failure, attempting reauth")
            if reauthenticate_dashboard_session(session, config):
                status, dashboard = build_dashboard_from_session(session_id)
                if status < 400 and dashboard.get("ok") and dashboard_backup_source_available(dashboard):
                    set_shared_dashboard(session_id, dashboard)
                    debug_log(f"shared_refresh: reauth succeeded for session {session_id[:8]}…")
                    return
        debug_log(f"shared_refresh: reauth failed or session missing for {session_id[:8]}…")

    with SHARED_DASHBOARD_LOCK:
        SHARED_DASHBOARD_STATE["lastError"] = str(
            dashboard_backup_source_error(dashboard)
            if status < 400 and dashboard.get("ok")
            else dashboard.get("error") or dashboard.get("message") or f"Refresh failed with HTTP {status}"
        )


def shared_dashboard_refresh_loop() -> None:
    while not SHARED_REFRESH_STOP.wait(SHARED_REFRESH_SECONDS):
        try:
            _shared_dashboard_refresh_once()
        except Exception as exc:  # noqa: BLE001 — loop must never die.
            debug_log(f"shared_dashboard_refresh_loop iteration failed: {exc}")


class BadRequest(ValueError):
    pass


class RestApiError(RuntimeError):
    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.body = body


class SmtpDeliveryError(RuntimeError):
    def __init__(self, stage: str, detail: str, diagnostics: dict[str, Any]) -> None:
        self.stage = stage
        self.detail = safe_log_text(detail, 900)
        self.diagnostics = dict(diagnostics)
        self.diagnostics["stage"] = stage
        self.diagnostics["detail"] = self.detail
        super().__init__(f"SMTP {stage} failed: {self.detail}")


def parse_port(value: Any, default: int, field_name: str) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        port = int(str(value).strip())
    except ValueError as exc:
        raise BadRequest(f"{field_name} must be a number.") from exc
    if not 1 <= port <= 65535:
        raise BadRequest(f"{field_name} must be between 1 and 65535.")
    return port


ALLOWED_HOST_NAMES: set[str] = set()
ALLOWED_NETWORKS: list[Any] = []
ALLOWED_PINNED_IPS: set[str] = set()
ALLOWLIST_ENABLED = False


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().strip("[]")


def _resolve_ips(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError, UnicodeError):
        return set()
    ips: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            ips.add(sockaddr[0])
    return ips


def configure_allowed_hosts(raw: str) -> None:
    """Parse a comma-separated allowlist of hostnames / IPs / CIDRs.
    Hostname entries are resolved once here and their IPs pinned (rebinding guard).
    """
    global ALLOWLIST_ENABLED
    ALLOWED_HOST_NAMES.clear()
    ALLOWED_NETWORKS.clear()
    ALLOWED_PINNED_IPS.clear()
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            ALLOWED_NETWORKS.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        name = _normalize_host(entry)
        if name:
            ALLOWED_HOST_NAMES.add(name)
            for ip in _resolve_ips(name):
                ALLOWED_PINNED_IPS.add(ip)
    ALLOWLIST_ENABLED = bool(ALLOWED_HOST_NAMES or ALLOWED_NETWORKS)


def _ip_in_networks(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in ALLOWED_NETWORKS)


def _host_allowed(host: str) -> bool:
    if not ALLOWLIST_ENABLED:
        return True
    h = _normalize_host(host)
    if not h:
        return False
    try:
        literal_ip = ipaddress.ip_address(h)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        return _ip_in_networks(h)
    if h not in ALLOWED_HOST_NAMES:
        return False
    resolved = _resolve_ips(h)
    if not resolved:
        return False
    return all(ip in ALLOWED_PINNED_IPS or _ip_in_networks(ip) for ip in resolved)


def _assert_host_allowed(config: "ApiConfig") -> None:
    for host in {config.rest_api_host, config.backup_server_host or config.rest_api_host}:
        if host and not _host_allowed(host):
            raise BadRequest(f"Host '{host}' is not in the configured allow-list.")


def parse_host(value: Any, field_name: str) -> tuple[str, int | None]:
    raw = str(value or "").strip()
    if not raw:
        raise BadRequest(f"{field_name} is required.")

    embedded_port: int | None = None
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme.lower() != "https":
            raise BadRequest(f"{field_name} must use HTTPS.")
        host = parsed.hostname or ""
        embedded_port = parsed.port
    else:
        if "/" in raw or "\\" in raw:
            raise BadRequest(f"{field_name} must be a host or IP address, not a path.")
        if raw.startswith("[") and "]" in raw:
            host_part, _, port_part = raw.partition("]")
            host = host_part.strip("[]")
            if port_part.startswith(":") and port_part[1:].isdigit():
                embedded_port = int(port_part[1:])
        elif raw.count(":") == 1 and raw.rsplit(":", 1)[1].isdigit():
            host, port_part = raw.rsplit(":", 1)
            embedded_port = int(port_part)
        else:
            host = raw

    host = host.strip().strip("[]")
    if not host or not HOST_PATTERN.match(host):
        raise BadRequest(f"{field_name} contains unsupported characters.")
    return host, embedded_port


def validate_payload(payload: dict[str, Any]) -> ApiConfig:
    rest_api_host, embedded_rest_port = parse_host(payload.get("restApiHost"), "REST API server")
    rest_api_port = parse_port(
        payload.get("restApiPort") or embedded_rest_port,
        DEFAULT_API_PORT,
        "REST API port",
    )

    backup_value = str(payload.get("backupServerHost") or "").strip()
    if backup_value:
        backup_server_host, embedded_backup_port = parse_host(backup_value, "Backup server")
    else:
        backup_server_host = rest_api_host
        embedded_backup_port = None

    backup_server_port = parse_port(
        payload.get("backupServerPort") or embedded_backup_port,
        DEFAULT_API_PORT,
        "AuthC port",
    )

    if ALLOWLIST_ENABLED:
        for host in {rest_api_host, backup_server_host}:
            if host and not _host_allowed(host):
                raise BadRequest(f"Host '{host}' is not in the configured allow-list.")

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username:
        raise BadRequest("Username is required.")
    if not password:
        raise BadRequest("Password is required.")

    api_mode = str(payload.get("apiMode") or "auto").strip().lower()
    if api_mode not in {"auto", "nwui", "rest"}:
        raise BadRequest("API source must be auto, nwui, or rest.")

    api_version = str(payload.get("apiVersion") or "auto").strip().lower()
    if api_version != "auto" and not API_VERSION_PATTERN.match(api_version):
        raise BadRequest("API version must be auto or look like v3.")

    report_range = str(payload.get("reportRange") or DEFAULT_REPORT_RANGE).strip().lower()
    if report_range not in REPORT_RANGES and report_range != CUSTOM_REPORT_RANGE:
        raise BadRequest("Report range must be 24h, 7d, 30d, or custom.")
    custom_start_date = str(payload.get("customStartDate") or "").strip()
    custom_end_date = str(payload.get("customEndDate") or "").strip()
    if report_range == CUSTOM_REPORT_RANGE:
        parse_custom_date_window(custom_start_date, custom_end_date)
    use_wmi_health = bool(payload.get("useWmiHealth", True))
    wmi_username = str(payload.get("wmiUsername") or "").strip()
    wmi_password = str(payload.get("wmiPassword") or "")
    if use_wmi_health and bool(wmi_username) != bool(wmi_password):
        raise BadRequest("WMI username and WMI password must be provided together.")

    timeout_seconds = parse_port(
        payload.get("timeoutSeconds"),
        DEFAULT_TIMEOUT_SECONDS,
        "Timeout seconds",
    )
    timeout_seconds = min(max(timeout_seconds, 5), 120)

    return ApiConfig(
        rest_api_host=rest_api_host,
        rest_api_port=rest_api_port,
        backup_server_host=backup_server_host,
        backup_server_port=backup_server_port,
        username=username,
        password=password,
        api_mode=api_mode,
        api_version=api_version,
        report_range=report_range,
        custom_start_date=custom_start_date,
        custom_end_date=custom_end_date,
        use_wmi_health=use_wmi_health,
        wmi_username=wmi_username,
        wmi_password=wmi_password,
        timeout_seconds=timeout_seconds,
        verify_tls=bool(payload.get("verifyTls", True)),
        use_authc_header=bool(payload.get("useAuthcHeader", False)),
    )


def host_for_url(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def authc_header_value(host: str, port: int) -> str:
    return f"{host_for_url(host)}:{port}"


def report_range_label(report_range: str) -> str:
    return REPORT_RANGES.get(report_range, REPORT_RANGES[DEFAULT_REPORT_RANGE])[0]


def report_range_days(report_range: str) -> int:
    return REPORT_RANGES.get(report_range, REPORT_RANGES[DEFAULT_REPORT_RANGE])[1]


def parse_dashboard_date(value: str) -> datetime:
    text = str(value or "").strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise BadRequest("Custom dates must use DD-MM-YYYY format.")


def display_date(value: str) -> str:
    try:
        return parse_dashboard_date(value).strftime("%d-%m-%Y")
    except BadRequest:
        return str(value or "")


def parse_custom_date_window(start_date: str, end_date: str) -> tuple[float, float]:
    if not start_date or not end_date:
        raise BadRequest("Custom date range requires both start and end dates.")
    start_dt = parse_dashboard_date(start_date)
    end_dt = parse_dashboard_date(end_date) + timedelta(days=1)
    if end_dt <= start_dt:
        raise BadRequest("Custom end date must be on or after the start date.")
    return start_dt.timestamp(), end_dt.timestamp()


def report_window(config: ApiConfig) -> tuple[float, float, str]:
    if config.report_range == CUSTOM_REPORT_RANGE:
        start_ts, end_ts = parse_custom_date_window(
            config.custom_start_date,
            config.custom_end_date,
        )
        return start_ts, end_ts, f"{display_date(config.custom_start_date)} to {display_date(config.custom_end_date)}"
    end_ts = time.time()
    start_ts = end_ts - (report_range_days(config.report_range) * 24 * 60 * 60)
    return start_ts, end_ts, report_range_label(config.report_range)


def in_report_window(value: Any, config: ApiConfig) -> bool:
    ts = timestamp(value)
    start_ts, end_ts, _ = report_window(config)
    return bool(ts and start_ts <= ts <= end_ts)


def in_report_range(value: Any, report_range: str) -> bool:
    ts = timestamp(value)
    end_ts = time.time()
    start_ts = end_ts - (report_range_days(report_range) * 24 * 60 * 60)
    return bool(ts and start_ts <= ts <= end_ts)


def display_datetime(value: Any) -> str:
    ts = timestamp(value)
    if ts:
        try:
            return datetime.fromtimestamp(ts).astimezone().strftime("%d-%m-%Y %H:%M:%S %Z")
        except (OSError, OverflowError, ValueError):
            pass
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = timestamp(text)
    if parsed:
        try:
            return datetime.fromtimestamp(parsed).astimezone().strftime("%d-%m-%Y %H:%M:%S %Z")
        except (OSError, OverflowError, ValueError):
            pass
    return text


def generated_at() -> str:
    return datetime.now().astimezone().strftime("%d-%m-%Y %H:%M:%S %Z")


def add_sla_summary(summary: dict[str, Any]) -> dict[str, Any]:
    met = int(summary.get("successfulJobs") or 0)
    missed = int(summary.get("failedJobs") or 0)
    total = met + missed
    summary["slaTotalJobs"] = total
    summary["slaMetJobs"] = met
    summary["slaMissedJobs"] = missed
    summary["slaPercent"] = round((met / total) * 100, 2) if total else 0
    return summary


def unavailable_server_health(detail: str = "CPU/RAM metrics were not exposed by the tested endpoints.") -> dict[str, Any]:
    return {
        "status": "unknown",
        "label": "Unavailable",
        "detail": detail,
        "source": "",
        "cpuUsagePercent": None,
        "ramUsagePercent": None,
        "ramUsedGb": None,
        "ramFreeGb": None,
        "ramTotalGb": None,
        "cpuDetail": "",
        "ramDetail": "",
    }


def percent_from_any(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        match = re.search(r"(\d+(?:\.\d+)?)", value)
        if not match:
            return None
        value = match.group(1)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 1:
        number *= 100
    return max(0, min(100, round(number)))


def find_metric_value(data: Any, names: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        lowered = {str(key).lower(): value for key, value in data.items()}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
        for value in data.values():
            found = find_metric_value(value, names)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_metric_value(item, names)
            if found not in (None, ""):
                return found
    return None


def server_health_from_payload(data: Any, source: str) -> dict[str, Any] | None:
    cpu = percent_from_any(
        find_metric_value(
            data,
            (
                "cpuUsagePercent",
                "cpuPercent",
                "cpuUsage",
                "processorUsage",
                "systemCpuUsage",
                "cpu",
            ),
        )
    )
    ram = percent_from_any(
        find_metric_value(
            data,
            (
                "ramUsagePercent",
                "memoryUsagePercent",
                "memoryPercent",
                "memoryUsage",
                "usedMemoryPercent",
                "ram",
            ),
        )
    )
    if cpu is None and ram is None:
        return None

    status = "ok"
    if (cpu is not None and cpu >= 90) or (ram is not None and ram >= 90):
        status = "critical"
    elif (cpu is not None and cpu >= 75) or (ram is not None and ram >= 75):
        status = "warning"

    return {
        "status": status,
        "label": "Critical" if status == "critical" else ("Warning" if status == "warning" else "Healthy"),
        "detail": f"Metrics loaded from {source}.",
        "source": source,
        "cpuUsagePercent": cpu,
        "ramUsagePercent": ram,
        "ramUsedGb": None,
        "ramFreeGb": None,
        "ramTotalGb": None,
        "cpuDetail": "CPU utilization",
        "ramDetail": "Memory utilization",
    }


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


def wmi_target_host(config: ApiConfig) -> str:
    return config.backup_server_host or config.rest_api_host


def is_local_wmi_target(target: str) -> bool:
    host = str(target or "").strip().strip("[]").lower()
    if host in ("", ".", "localhost", "127.0.0.1", "::1"):
        return True
    local_names = {
        socket.gethostname().lower(),
        socket.getfqdn().lower(),
    }
    if host in local_names:
        return True
    try:
        target_ips = {info[4][0] for info in socket.getaddrinfo(host, None)}
        local_ips = {"127.0.0.1", "::1"}
        for name in local_names:
            try:
                local_ips.update(info[4][0] for info in socket.getaddrinfo(name, None))
            except socket.gaierror:
                continue
        return bool(target_ips & local_ips)
    except socket.gaierror:
        return False


def wmi_connectivity_hint(target: str) -> str:
    return (
        f"Check WMI/DCOM access to {target}: Windows Firewall WMI rules, RPC port 135 "
        "and dynamic RPC ports, and Remote WMI/DCOM permissions for the service account."
    )


def wmi_failure_hint(target: str, detail: str = "") -> str:
    lowered = str(detail or "").lower()
    if "access is denied" in lowered or "0x80070005" in lowered or "unauthorizedaccess" in lowered:
        return (
            f"WMI reached {target}, but Windows denied the account. Use DOMAIN\\user or {target}\\localadmin, "
            f"add the account to local Administrators on {target}, or grant DCOM Remote Launch/Activation and "
            r"WMI root\cimv2 Remote Enable, Execute Methods, and Enable Account permissions. "
            "For non-domain local accounts, Remote UAC filtering may also block WMI; use a domain service account "
            "or configure LocalAccountTokenFilterPolicy on the backup server."
        )
    if "user credentials cannot be used for local connections" in lowered:
        return (
            "Windows rejected explicit credentials for a local WMI target. Use localhost/the server hostname from "
            "the dashboard host and leave WMI username/password blank, or run the dashboard under the account that "
            "has local WMI access."
        )
    return wmi_connectivity_hint(target)


def clean_powershell_error(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    text = text.replace("#< CLIXML", "").strip()
    if "<Objs" in text:
        xml_text = text[text.find("<Objs") :]
        try:
            root = ET.fromstring(xml_text)
            messages = []
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1]
                stream = element.attrib.get("S", "")
                name = element.attrib.get("N", "")
                value_text = (element.text or "").strip()
                if not value_text:
                    continue
                if stream == "progress" or value_text == "Preparing modules for first use.":
                    continue
                if tag in {"S", "AV"} or name in {"Message", "ErrorRecord", "FullyQualifiedErrorId"}:
                    messages.append(value_text)
            if messages:
                return safe_log_text(" ".join(dict.fromkeys(messages)), 700)
        except ET.ParseError:
            pass
    text = re.sub(r"<Obj\b[^>]*S=\"progress\".*?</Obj>", " ", text, flags=re.DOTALL)
    text = re.sub(r"Preparing modules for first use\.", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return safe_log_text(text, 700)


def load_server_health_wmi(config: ApiConfig) -> dict[str, Any] | None:
    if not config.use_wmi_health:
        return None
    target = wmi_target_host(config)
    is_local_target = is_local_wmi_target(target)
    if not is_local_target and (not config.wmi_username or not config.wmi_password):
        return unavailable_server_health("WMI credentials were not provided.")

    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    executable = str(powershell) if powershell.exists() else "powershell.exe"
    script = r'''
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
$DebugPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"
$inputJson = [Console]::In.ReadToEnd()
$payload = $inputJson | ConvertFrom-Json
$target = $payload.host
if ($payload.isLocal) {
  $wmi = @{}
} elseif ($payload.useCredential) {
  $securePassword = ConvertTo-SecureString -String $payload.password -AsPlainText -Force
  $credential = New-Object System.Management.Automation.PSCredential($payload.username, $securePassword)
  $wmi = @{
    ComputerName = $target
    Credential = $credential
    Authentication = "PacketPrivacy"
    Impersonation = "Impersonate"
  }
} else {
  $wmi = @{
    ComputerName = $target
  }
}
$cpuSampleSeconds = 1
$processorStart = Get-WmiObject -Class Win32_PerfRawData_PerfOS_Processor @wmi -Filter "Name='_Total'"
Start-Sleep -Seconds $cpuSampleSeconds
$processorEnd = Get-WmiObject -Class Win32_PerfRawData_PerfOS_Processor @wmi -Filter "Name='_Total'"
$os = Get-WmiObject -Class Win32_OperatingSystem @wmi
$system = Get-WmiObject -Class Win32_PerfFormattedData_PerfOS_System @wmi
$totalKb = [double]$os.TotalVisibleMemorySize
$freeKb = [double]$os.FreePhysicalMemory
$cpuCounterDelta = [double]$processorEnd.PercentProcessorTime - [double]$processorStart.PercentProcessorTime
$cpuTimeDelta = [double]$processorEnd.Timestamp_Sys100NS - [double]$processorStart.Timestamp_Sys100NS
$cpuPercent = if ($cpuTimeDelta -gt 0) { [math]::Round((1 - ($cpuCounterDelta / $cpuTimeDelta)) * 100) } else { $null }
if ($cpuPercent -ne $null) {
  if ($cpuPercent -lt 0) { $cpuPercent = 0 }
  if ($cpuPercent -gt 100) { $cpuPercent = 100 }
}
$ramPercent = if ($totalKb -gt 0) { [math]::Round((($totalKb - $freeKb) / $totalKb) * 100) } else { $null }
[pscustomobject]@{
  host = $target
  cpuUsagePercent = if ($cpuPercent -ne $null) { [int]$cpuPercent } else { $null }
  cpuSampleSeconds = $cpuSampleSeconds
  ramUsagePercent = $ramPercent
  totalMemoryMb = if ($totalKb -gt 0) { [math]::Round($totalKb / 1024) } else { $null }
  freeMemoryMb = if ($freeKb -gt 0) { [math]::Round($freeKb / 1024) } else { $null }
  uptimeSeconds = [int64]$system.SystemUpTime
  osCaption = [string]$os.Caption
  lastBoot = [string]$os.LastBootUpTime
} | ConvertTo-Json -Compress
'''
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    payload = json.dumps(
        {
            "host": target,
            "username": config.wmi_username,
            "password": config.wmi_password,
            "isLocal": is_local_target,
            "useCredential": bool(config.wmi_username and config.wmi_password and not is_local_target),
        },
        ensure_ascii=True,
    )
    wmi_timeout = max(10, min(config.timeout_seconds, 120))
    try:
        completed = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded_script,
            ],
            input=payload,
            text=True,
            capture_output=True,
            timeout=wmi_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return unavailable_server_health(
            f"WMI query timed out after {wmi_timeout}s. {wmi_failure_hint(target)}"
        )
    except OSError as exc:
        return unavailable_server_health(f"WMI query could not start PowerShell: {safe_log_text(exc)}")

    if completed.returncode != 0:
        detail = clean_powershell_error(completed.stderr) or clean_powershell_error(completed.stdout) or "PowerShell WMI command failed."
        return unavailable_server_health(f"WMI query failed: {detail} {wmi_failure_hint(target, detail)}")

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return unavailable_server_health("WMI query returned non-JSON output.")

    health = server_health_from_payload(data, f"WMI {wmi_target_host(config)}")
    if not health:
        return unavailable_server_health("WMI query returned no CPU/RAM metrics.")
    health["detail"] = f"{data.get('osCaption') or 'Windows'} via WMI."
    sample_seconds = data.get("cpuSampleSeconds") or 1
    health["cpuDetail"] = f"Real-time WMI sample from {data.get('host') or wmi_target_host(config)} over {sample_seconds}s"
    total = data.get("totalMemoryMb")
    free = data.get("freeMemoryMb")
    if total is not None and free is not None:
        total_gb = gb_from_mb(total)
        free_gb = gb_from_mb(free)
        if total_gb is not None:
            health["ramTotalGb"] = total_gb
        if free_gb is not None:
            health["ramFreeGb"] = free_gb
        if total_gb is not None and free_gb is not None:
            used_gb = round(max(0.0, total_gb - free_gb), 1)
            health["ramUsedGb"] = used_gb
            health["ramDetail"] = f"{used_gb:g} GB used of {total_gb:g} GB ({health.get('ramUsagePercent')}%)"
    return health


def format_number_for_detail(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value or "")


def gb_from_mb(value: Any) -> float | None:
    try:
        mb = float(value)
    except (TypeError, ValueError):
        return None
    return round(mb / 1024, 1)


def maintenance_backup_status(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    housekeeping_keywords = ("expiration", "expire", "retention cleanup", "cleanup", "staging", "recycle")
    server_backup_names = {
        "server backup",
        "nmc server backup",
        "nmc server backup vm",
        "bootstrap maintenance backup",
    }

    def clean(value: Any) -> str:
        return " ".join(str(value or "").lower().split())

    def is_server_protection_job(job: dict[str, Any]) -> bool:
        identity_values = [
            clean(job.get(key))
            for key in (
                "policy",
                "policyName",
                "protectionPolicyName",
                "workflowName",
                "_workflow",
                "groupName",
                "_group",
            )
        ]
        action_values = [
            clean(job.get(key))
            for key in ("client", "clientHostname", "name", "actionName", "policyActionName", "_save_set")
        ]
        if any("server protection" in value for value in identity_values):
            return True
        if any(value == "server protection" for value in action_values):
            return True
        return any(value in server_backup_names for value in action_values + identity_values)

    matches = []
    for job in jobs:
        action_text = " ".join(
            clean(job.get(key))
            for key in ("name", "actionName", "policyActionName", "_workflow", "_group")
        )
        is_housekeeping = any(keyword in action_text for keyword in housekeeping_keywords)
        if is_server_protection_job(job) and not is_housekeeping:
            matches.append(job)

    if not matches:
        return {
            "status": "unknown",
            "label": "Not found",
            "detail": "No Server Protection job found in this range.",
            "count": 0,
        }

    latest = matches[0]
    raw_status = str(latest.get("status") or "unknown").lower()
    if any(word in raw_status for word in ("success", "succeed", "complete", "ok")):
        status = "succeeded"
    elif any(word in raw_status for word in ("fail", "error", "critical")):
        status = "failed"
    elif any(word in raw_status for word in ("run", "active", "start")):
        status = "running"
    elif any(word in raw_status for word in ("queue", "wait", "pending")):
        status = "queued"
    elif "warn" in raw_status:
        status = "warning"
    else:
        status = raw_status
    label = status.title() if status else "Unknown"
    return {
        "status": status,
        "label": label,
        "detail": (
            f"{latest.get('name') or 'Maintenance job'} on {latest.get('client') or 'server'}"
            f" at {latest.get('started') or 'unknown time'}"
        ),
        "count": len(matches),
    }


def api_base_url(config: ApiConfig) -> str:
    version = "v3" if config.api_version == "auto" else config.api_version
    return (
        f"https://{host_for_url(config.rest_api_host)}:{config.rest_api_port}"
        f"/nwrestapi/{version}"
    )


def api_base_url_for_version(config: ApiConfig, version: str) -> str:
    return (
        f"https://{host_for_url(config.rest_api_host)}:{config.rest_api_port}"
        f"/nwrestapi/{version}"
    )


def nwui_api_base_url(config: ApiConfig) -> str:
    return f"https://{host_for_url(config.rest_api_host)}:{config.rest_api_port}/nwui/api"


def origin_url(config: ApiConfig) -> str:
    return f"https://{host_for_url(config.rest_api_host)}:{config.rest_api_port}"


def endpoint(path: str, query: dict[str, str] | None = None) -> str:
    if query:
        return f"{path}?{urlencode(query)}"
    return path


# NetWorker /global/jobs field list (the `fl` query param). NetWorker rejects
# unknown fields with HTTP 400 ("The <field> field is not valid"), so this set
# is limited to fields the jobs resource actually exposes. Fields such as
# elapsedTime/policyName/saveBytes/transferredBytes are NOT valid job query
# fields on NetWorker and were removed. nwui_rest_fallback_items() still
# auto-strips any field a given NetWorker version rejects, as a safety net.
#
# `message` is deliberately EXCLUDED from the bulk list: it carries multi-KB of
# job-log text per record and on a busy server the jobs DB holds tens of
# thousands of jobs (observed: 36,031 jobs / 11.5 MB, almost entirely message
# text). Dropping it cuts the response by ~10x and removes the per-record log
# cleaning that was making each refresh time out. Failure detail still comes
# from the small, completionStatus:"Failed"-filtered failedJobs query below.
JOB_QUERY_FIELDS = (
    "clientHostname",
    "startTime",
    "completionStatus",
    "name",
    "policyActionName",
    "workflowName",
    "level",
)


def dashboard_endpoints(config: ApiConfig | None = None) -> dict[str, str]:
    # NOTE: NetWorker Query Language (the `q` param) supports only field:value
    # equality — it has NO range/comparison operators, so the report-time window
    # CANNOT be applied server-side (a startTime>=... query is rejected with
    # HTTP 400). The jobs database is naturally bounded by NetWorker's completed-
    # job retention, and the exact report window is enforced client-side by
    # in_report_window(). `config` is accepted for signature stability.
    job_fields = ",".join(JOB_QUERY_FIELDS)
    # The failed set is small (filtered to completionStatus:"Failed"), so it can
    # afford to include the verbose `message` field for failure detail.
    failed_fields = ",".join((*JOB_QUERY_FIELDS, "message"))
    return {
        "clients": endpoint(
            "/global/clients",
            {
                "fl": "hostname,backupType,saveSets,protectionGroups,enabled,aliases",
            },
        ),
        "jobs": endpoint("/global/jobs", {"fl": job_fields}),
        "failedJobs": endpoint(
            "/global/jobs",
            {
                "q": 'completionStatus:"Failed"',
                "fl": failed_fields,
            },
        ),
        "alerts": endpoint("/global/alerts"),
        "policies": endpoint("/global/protectionpolicies"),
    }


def build_headers(config: ApiConfig) -> dict[str, str]:
    token = base64.b64encode(f"{config.username}:{config.password}".encode("utf-8")).decode(
        "ascii"
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Basic {token}",
        "User-Agent": f"networker-dashboard/{APP_VERSION}",
    }
    if config.use_authc_header and config.backup_server_host:
        headers["X-NW-AUTHC-BASE-URL"] = authc_header_value(
            config.backup_server_host,
            config.backup_server_port,
        )
    return headers


def ssl_context_for_api(verify_tls: bool) -> ssl.SSLContext:
    if verify_tls:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def read_limited(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RestApiError(502, "REST API response exceeded dashboard safety limit.")
    return data


def compact_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    query_keys = ",".join(sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}))
    compact = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if query_keys:
        compact += f"?queryKeys={query_keys}"
    return compact


def compact_path_for_log(path: str) -> str:
    parsed = urlparse(path)
    query_keys = ",".join(sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}))
    compact = parsed.path or path
    if query_keys:
        compact += f"?queryKeys={query_keys}"
    return compact


def strip_html_for_error(body: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", body)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def looks_like_missing_nwrestapi(body: str) -> bool:
    lowered = html_lib.unescape(body).lower()
    return (
        "/nwrestapi/" in lowered
        and "is not available" in lowered
        and ("apache tomcat" in lowered or "http status 404" in lowered)
    )


def describe_http_error(status_code: int, reason: str, body: str, url: str) -> str:
    if status_code == 404 and looks_like_missing_nwrestapi(body):
        base = compact_url_for_log(url).split("?queryKeys=", 1)[0]
        return (
            f"HTTP 404 from NetWorker/Tomcat: the nwrestapi application was not found at {base}. "
            "Check that REST API server IP/port points to the NetWorker REST API host, not only an "
            "AuthC/Tomcat host. On the NetWorker REST API host verify the nwrestapi webapp exists "
            "and restapi.log is updating: Linux /nsr/authc/webapps/nwrestapi/ and "
            "/nsr/logs/restapi/restapi.log; Windows C:\\Program Files\\EMC NetWorker\\nsr\\authc-server\\tomcat\\webapps\\nwrestapi "
            "and C:\\Program Files\\EMC NetWorker\\nsr\\logs\\restapi\\restapi.log."
        )

    message = f"HTTP {status_code} {reason}".strip()
    clean_body = strip_html_for_error(body)
    if clean_body:
        message = f"{message}: {clean_body[:260]}"
    return message


def invalid_rest_query_field(message: str, body: str = "") -> str:
    text = f"{message}\n{body}"
    match = re.search(r"The\s+([A-Za-z0-9_.-]+)\s+field\s+is\s+not\s+valid", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"field\s+error:\s*([A-Za-z0-9_.-]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def remove_rest_field_from_path(path: str, field_name: str) -> str:
    if not field_name:
        return path
    parsed = urlparse(path)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    changed = False
    updated: list[tuple[str, str]] = []
    for key, value in query:
        if key != "fl":
            updated.append((key, value))
            continue
        fields = [field for field in value.split(",") if field and field.lower() != field_name.lower()]
        if len(fields) != len([field for field in value.split(",") if field]):
            changed = True
        updated.append((key, ",".join(fields)))
    if not changed:
        return path
    return parsed._replace(query=urlencode(updated)).geturl()


def strip_query_param(path: str, param_name: str) -> str:
    """Remove a single query parameter (e.g. the NQL `q` time filter) from a
    path, leaving the rest of the query string intact."""
    parsed = urlparse(path)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != param_name]
    return parsed._replace(query=urlencode(query)).geturl()


def describe_url_error(exc: BaseException) -> str:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            "REST API TLS certificate verification failed. Import the NetWorker REST API "
            "CA certificate into this host trust store, use a CA-signed certificate, or "
            "turn off 'Verify REST API TLS certificate' for lab/self-signed testing."
        )
    if isinstance(reason, ssl.SSLError):
        return f"REST API TLS handshake failed: {reason}"
    if isinstance(reason, TimeoutError) or isinstance(exc, (TimeoutError, socket.timeout)):
        return "REST API connection timed out. Check the REST API host, port, firewall, and routing."
    if isinstance(reason, ConnectionRefusedError):
        return "REST API connection refused. Check that NetWorker REST API is listening on the selected host and port."
    if isinstance(reason, OSError):
        return f"REST API network error: {reason}"
    return f"REST API connection failed: {reason}"


def fetch_json(
    url: str,
    headers: dict[str, str],
    timeout: int,
    context: ssl.SSLContext,
    label: str,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> Any:
    request = Request(url, headers=headers, method="GET")
    started = time.monotonic()
    debug_log(f"REST GET start source={label} url={compact_url_for_log(url)} timeout={timeout}s")
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            try:
                raw = read_limited(response, max_bytes)
            except RestApiError:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                debug_log(
                    f"REST GET too-large source={label} limitBytes={max_bytes} "
                    f"elapsedMs={elapsed_ms}"
                )
                raise
            elapsed_ms = int((time.monotonic() - started) * 1000)
            debug_log(
                f"REST GET ok source={label} status={response.status} "
                f"bytes={len(raw)} elapsedMs={elapsed_ms}"
            )
            if not raw:
                return None
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, "replace")
            return json.loads(text)
    except HTTPError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = exc.read(8192).decode("utf-8", "replace")
        message = describe_http_error(exc.code, exc.reason, body, url)
        debug_log(
            f"REST GET http-error source={label} status={exc.code} "
            f"elapsedMs={elapsed_ms} error={message}"
        )
        raise RestApiError(exc.code, message, body[:8192]) from exc
    except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        message = describe_url_error(exc)
        debug_log(
            f"REST GET network-error source={label} elapsedMs={elapsed_ms} "
            f"error={message}"
        )
        raise RestApiError(502, message) from exc
    except json.JSONDecodeError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        debug_log(
            f"REST GET json-error source={label} elapsedMs={elapsed_ms} "
            f"error={exc}"
        )
        raise RestApiError(502, f"REST API did not return JSON: {exc}") from exc


def collection_from(data: Any, preferred_key: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (preferred_key, "items", "results", "resources", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def stringify(value: Any, max_len: int = 220) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        text = ", ".join(stringify(item, 80) for item in value[:8])
        if len(value) > 8:
            text += f", +{len(value) - 8} more"
    elif isinstance(value, dict):
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    else:
        text = str(value)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def networker_group_name_from_output(text: str) -> str:
    patterns = (
        r"\bGroup\s+(.+?)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d{5,}",
        r"\bfor workflow '([^']+)'",
        r"\bStarting workflow '([^']+)'",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip(" .")
        if value and "%" not in value:
            return value
    return ""


def clean_networker_record_body(body: str, group_name: str = "") -> str:
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return ""

    waiting_match = re.search(
        r"\bwaiting\s+for\s+(\d+)\s+jobs\s+\((\d+)\s+awaiting\s+restart\)\s+to\s+complete\b",
        body,
        flags=re.IGNORECASE,
    )
    if waiting_match:
        prefix = f"Group {group_name} " if group_name else ""
        return (
            f"{prefix}waiting for {waiting_match.group(1)} jobs "
            f"({waiting_match.group(2)} awaiting restart) to complete."
        ).strip()

    nested_message = re.search(
        r"\bUnable to handle job (?:add|monitor) message:\s+(?:\d+\s+){3,}(.+?)(?=\s+\d+\s+\d+\s+\d+\s+\S+\s+NSR\b|$)",
        body,
        flags=re.IGNORECASE,
    )
    if nested_message:
        nested = re.sub(r"\s+", " ", nested_message.group(1)).strip(" .")
        nested = re.sub(r"\s+\d+\s+\d+\s+\d+\s+\S+.*$", "", nested).strip(" .")
        if nested:
            return f"{nested}."

    sentence_match = re.match(r"(.+?\.)\s+(.+)$", body)
    sentence = sentence_match.group(1) if sentence_match else body
    arg_tail = sentence_match.group(2) if sentence_match else ""
    sentence = re.sub(r"\\[rn]", " ", sentence)
    sentence = re.sub(r"\s+", " ", sentence).strip(" .")
    if not sentence:
        return ""

    if "%" in sentence:
        placeholders = re.findall(r"%[sdu]", sentence)
        arg_tokens = arg_tail.split()
        if arg_tokens and arg_tokens[0].isdigit() and int(arg_tokens[0]) == len(placeholders):
            arg_tokens = arg_tokens[1:]
        numeric_needed = sum(1 for placeholder in placeholders if placeholder in ("%d", "%u"))
        string_needed = sum(1 for placeholder in placeholders if placeholder == "%s")
        numeric_values: list[str] = []
        while arg_tokens and len(numeric_values) < numeric_needed and re.fullmatch(r"\d+", arg_tokens[0]):
            numeric_values.append(arg_tokens.pop(0))
        while arg_tokens and re.fullmatch(r"\d+", arg_tokens[-1]):
            arg_tokens.pop()
        string_values: list[str] = []
        if string_needed == 1 and arg_tokens:
            string_values.append(" ".join(arg_tokens))
        elif string_needed > 1:
            string_values.extend(arg_tokens[:string_needed])
        if len(numeric_values) >= numeric_needed and len(string_values) >= string_needed:
            numeric_index = 0
            string_index = 0

            def replace_record_placeholder(match: re.Match[str]) -> str:
                nonlocal numeric_index, string_index
                placeholder = match.group(0)
                if placeholder in ("%d", "%u"):
                    value = numeric_values[numeric_index]
                    numeric_index += 1
                    return value
                value = string_values[string_index]
                string_index += 1
                return value

            rendered = re.sub(r"%[sdu]", replace_record_placeholder, sentence)
            rendered = re.sub(r"\s+", " ", rendered).strip(" .")
            if rendered:
                return f"{rendered}."

        generic_templates = (
            (r"^Started\s+''\s+job\s+with\s+jobid\s+\[%u\]", "Backup job started."),
            (r"^Action\s+''\s+has\s+initialized", "Action initialized."),
        )
        for pattern, replacement in generic_templates:
            if re.search(pattern, sentence, flags=re.IGNORECASE):
                return replacement
        sentence = re.sub(r"\s+with job id %u.*$", "", sentence, flags=re.IGNORECASE).strip(" .")
        sentence = re.sub(r"\s+with jobid \[%u\].*$", "", sentence, flags=re.IGNORECASE).strip(" .")
        sentence = re.sub(r"%[sdu]", "", sentence)
        sentence = re.sub(r"\s+", " ", sentence).strip(" .'")

    if re.fullmatch(r"\d{1,2}:\d{1,2}\s+\S+", sentence):
        return ""
    return f"{sentence}." if sentence and not sentence.endswith(".") else sentence


def extract_networker_record_messages(text: str, group_name: str = "") -> list[str]:
    marker_pattern = re.compile(
        r"\b(?P<source>[A-Za-z0-9_.-]+)\s+NSR\s+(?P<level>info|notice|warning|error|critical)\s+(?P<code>\d+)\s+",
        flags=re.IGNORECASE,
    )
    matches = list(marker_pattern.finditer(text))
    messages: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        message = clean_networker_record_body(text[start:end], group_name)
        if message:
            messages.append(message)
    return messages


def clean_networker_job_message(value: Any, fallback: str = "", group_name: str = "") -> str:
    text = stringify(value, 12000)
    if not text:
        return fallback
    had_suppressed_prefix = bool(re.search(r"\bsuppressed\s+\d+\s+bytes?\s+of\s+output\b", text, re.IGNORECASE))
    text = re.sub(r"\bsuppressed\s+\d+\s+bytes?\s+of\s+output\.?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return fallback

    record_messages = extract_networker_record_messages(text, group_name)
    if record_messages:
        preferred = [message for message in record_messages if "waiting for" in message.lower()]
        return (preferred or record_messages)[-1]

    rendered_waiting_match = re.search(
        r"\bGroup\s+(.+?)\s+waiting\s+for\s+(\d+)\s+jobs\s+\((\d+)\s+awaiting\s+restart\)\s+to\s+complete\b",
        text,
        flags=re.IGNORECASE,
    )
    if rendered_waiting_match:
        group = re.sub(r"\s+", " ", rendered_waiting_match.group(1)).strip(" .")
        if len(group) <= 80 and not re.search(r"\b(?:NSR|savegrp|Program Files)\b", group, re.IGNORECASE):
            return (
                f"Group {group} waiting for {rendered_waiting_match.group(2)} jobs "
                f"({rendered_waiting_match.group(3)} awaiting restart) to complete."
            )

    waiting_match = re.search(
        r"\bwaiting\s+for\s+(\d+)\s+jobs\s+\((\d+)\s+awaiting\s+restart\)\s+to\s+complete\b",
        text,
        flags=re.IGNORECASE,
    )
    if waiting_match:
        group = group_name or networker_group_name_from_output(text)
        prefix = f"Group {group} " if group else ""
        return (
            f"{prefix}waiting for {waiting_match.group(1)} jobs "
            f"({waiting_match.group(2)} awaiting restart) to complete."
        ).strip().capitalize() if not prefix else (
            f"{prefix}waiting for {waiting_match.group(1)} jobs "
            f"({waiting_match.group(2)} awaiting restart) to complete."
        )

    catalog_match = re.search(
        r"\bNSR\s+(?:info|notice|warning|error|critical)\s+\d+\s+(.+?)\.\s+(\d+)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if catalog_match:
        template = catalog_match.group(1).strip()
        arg_count = int(catalog_match.group(2) or 0)
        arg_tokens = catalog_match.group(3).split()
        placeholders = re.findall(r"%[sd]", template)
        numeric_needed = sum(1 for placeholder in placeholders if placeholder == "%d")
        string_needed = sum(1 for placeholder in placeholders if placeholder == "%s")
        numeric_values = []
        while arg_tokens and len(numeric_values) < numeric_needed and re.fullmatch(r"\d+", arg_tokens[0]):
            numeric_values.append(arg_tokens.pop(0))
        while arg_tokens and re.fullmatch(r"\d+", arg_tokens[-1]):
            arg_tokens.pop()
        string_values: list[str] = []
        if string_needed == 1 and arg_tokens:
            string_values.append(" ".join(arg_tokens))
        elif string_needed > 1:
            string_values.extend(arg_tokens[:string_needed])

        if len(placeholders) == arg_count and len(numeric_values) >= numeric_needed and len(string_values) >= string_needed:
            numeric_index = 0
            string_index = 0

            def replace_placeholder(match: re.Match[str]) -> str:
                nonlocal numeric_index, string_index
                placeholder = match.group(0)
                if placeholder == "%d":
                    value = numeric_values[numeric_index]
                    numeric_index += 1
                    return value
                value = string_values[string_index]
                string_index += 1
                return value

            rendered = re.sub(r"%[sd]", replace_placeholder, template)
            rendered = re.sub(r"\s+", " ", rendered).strip(" .")
            if rendered:
                return f"{rendered}."
        compact_template = re.sub(r"%[sd]", "", template)
        compact_template = re.sub(r"\s+", " ", compact_template).strip(" .")
        if compact_template:
            return f"{compact_template}."

    tokens = text.split()
    numeric_tokens = sum(1 for token in tokens if re.fullmatch(r"\d+", token))
    if had_suppressed_prefix or (len(tokens) >= 18 and numeric_tokens / max(1, len(tokens)) > 0.45):
        return fallback or "Verbose NetWorker job output suppressed."
    return stringify(text, 260)


def first_value(item: Any, *keys: str) -> Any:
    if not isinstance(item, dict):
        return ""
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return ""


def timestamp(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1000 if numeric > 100000000000 else numeric

    text = str(value).strip()
    if not text:
        return 0.0
    if text.isdigit():
        numeric = float(text)
        return numeric / 1000 if numeric > 100000000000 else numeric

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass

    try:
        dt = email.utils.parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, IndexError):
        pass

    for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def format_duration_seconds(total_seconds: Any) -> str:
    try:
        seconds = int(float(total_seconds))
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not days and not hours:
        parts.append(f"{seconds}s")
    return " ".join(parts[:3]) or "0s"


def _format_bytes(value: Any) -> str:
    """Convert raw byte count to human-readable string (KB/MB/GB/TB)."""
    if value in (None, "", [], {}):
        return ""
    try:
        b = float(value)
    except (TypeError, ValueError):
        return ""
    if b <= 0:
        return ""
    for unit, threshold in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= threshold:
            return f"{b / threshold:.1f} {unit}"
    return f"{int(b)} B"


def format_duration_value(value: Any) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, (int, float)):
        return format_duration_seconds(value)
    text = stringify(value, 80).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return format_duration_seconds(float(text))
    colon_match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
    if colon_match:
        hours = int(colon_match.group(1) or 0)
        minutes = int(colon_match.group(2))
        seconds = int(colon_match.group(3))
        return format_duration_seconds((hours * 3600) + (minutes * 60) + seconds)
    return text


def status_text(job: Any) -> str:
    return stringify(first_value(job, "completionStatus", "state", "status"), 80)


def is_failed_job(job: Any) -> bool:
    status = status_text(job).lower()
    return any(word in status for word in ("fail", "error", "critical"))


def is_success_job(job: Any) -> bool:
    status = status_text(job).lower()
    return any(word in status for word in ("success", "succeed", "completed"))


def is_active_job(job: Any) -> bool:
    status = status_text(job).lower()
    return any(word in status for word in ("run", "active", "start", "queued"))


def is_clone_job(job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    fields = [
        first_value(job, "name"),
        first_value(job, "policyActionName"),
        first_value(job, "actionName"),
        first_value(job, "workflowName"),
        first_value(job, "jobType"),
        first_value(job, "type"),
        first_value(job, "recoverType"),
        first_value(job, "policyActionName"),
        first_value(job, "message"),
        first_value(job, "jobCmd"),
        job.get("_action_type"),
        job.get("_workflow"),
        job.get("_group"),
        job.get("_save_set"),
    ]
    text = " ".join(stringify(field, 140).lower() for field in fields)
    return any(token in text for token in ("clone", "nsrclone"))


def is_recovery_job(job: Any) -> bool:
    if is_clone_job(job):
        return False
    fields = [
        first_value(job, "name"),
        first_value(job, "policyActionName"),
        first_value(job, "actionName"),
        first_value(job, "workflowName"),
        first_value(job, "message"),
    ]
    text = " ".join(stringify(field, 120).lower() for field in fields)
    return any(word in text for word in ("recover", "recovery", "restore"))


def alert_severity(alert: Any) -> str:
    return stringify(first_value(alert, "severity", "level", "priority", "type", "status"), 80)


def is_critical_alert(alert: Any) -> bool:
    return any(word in alert_severity(alert).lower() for word in ("critical", "severe", "fatal"))


def is_warning_alert(alert: Any) -> bool:
    return any(word in alert_severity(alert).lower() for word in ("warn", "minor", "medium"))


def networker_log_priority(status: Any, message: Any = "") -> str:
    text = f"{stringify(status, 80)} {stringify(message, 140)}".lower()
    if any(word in text for word in ("critical", "fatal", "failed", "failure", "error")):
        return "error"
    if any(word in text for word in ("warn", "waiting", "queued", "awaiting restart")):
        return "warning"
    return "info"


def networker_log_category(message: Any, default: str = "policy") -> str:
    text = stringify(message, 260).lower()
    if any(word in text for word in ("device", "volume", "save set", "saveset", "media", "ddboost", "ddclone")):
        return "media"
    if any(word in text for word in ("workflow", "action", "group", "policy")):
        return "policy"
    if any(word in text for word in ("client", "host")):
        return "client"
    return default or "event"


def networker_log_row(
    message: Any,
    time_value: Any = "",
    status: Any = "",
    category: str = "",
    source: str = "event",
) -> dict[str, str]:
    clean_message = clean_networker_job_message(message)
    return {
        "priority": networker_log_priority(status, clean_message),
        "time": display_datetime(time_value) if time_value else "",
        "source": source or "event",
        "category": networker_log_category(clean_message, category or "policy"),
        "message": clean_message,
    }


def project_job_log(job: Any) -> dict[str, str]:
    projected = project_job(job)
    return networker_log_row(
        projected.get("message") or projected.get("name"),
        first_value(job, "startTime", "started", "start"),
        projected.get("status"),
        "policy",
        "event",
    )


def project_job(job: Any) -> dict[str, str]:
    group_name = stringify(
        first_value(job, "workflowName", "groupName", "policyName", "policy", "protectionPolicyName"),
        140,
    )
    raw_message = first_value(job, "jobOutput", "message", "messages", "statusMessage", "errorMessage")
    raw_bytes = first_value(job, "saveBytes", "transferredBytes", "bytesTransferred", "savedSize", "dataTransferred")
    return {
        "client": stringify(first_value(job, "clientHostname", "client", "hostname"), 120),
        "name": stringify(first_value(job, "name", "policyActionName", "actionName"), 140),
        "policy": group_name,
        "status": status_text(job),
        "started": display_datetime(first_value(job, "startTime", "started", "start")),
        "duration": format_duration_value(first_value(job, "elapsedTime", "duration", "elapsed")),
        "size": _format_bytes(raw_bytes),
        "message": clean_networker_job_message(raw_message, "", group_name),
    }


def project_failed_job(job: Any) -> dict[str, str]:
    projected = project_job(job)
    return {
        "client": projected["client"],
        "name": projected["name"],
        "policy": projected["policy"],
        "started": projected["started"],
        "message": projected["message"],
    }


def project_alert(alert: Any) -> dict[str, str]:
    return {
        "severity": alert_severity(alert),
        "time": display_datetime(first_value(alert, "time", "timestamp", "date", "createdTime")),
        "message": stringify(first_value(alert, "message", "summary", "description"), 260),
        "resource": stringify(first_value(alert, "resource", "resourceName", "source", "name"), 160),
    }


def project_client(client: Any) -> dict[str, str]:
    return {
        "hostname": stringify(first_value(client, "hostname", "name", "clientHostname"), 160),
        "enabled": stringify(first_value(client, "enabled", "active", "status"), 80),
        "backupType": stringify(first_value(client, "backupType", "type"), 120),
        "saveSets": stringify(first_value(client, "saveSets", "savesets"), 260),
        "protectionGroups": stringify(first_value(client, "protectionGroups", "groups"), 260),
    }


def sort_jobs(items: list[Any]) -> list[Any]:
    return sorted(
        items,
        key=lambda item: timestamp(first_value(item, "startTime", "started", "start")),
        reverse=True,
    )


def load_server_health_rest(
    config: ApiConfig,
    base_url: str,
    headers: dict[str, str],
    context: ssl.SSLContext,
) -> dict[str, Any]:
    wmi_health = load_server_health_wmi(config)
    if config.use_wmi_health:
        return wmi_health or unavailable_server_health("WMI health collection did not return CPU/memory metrics.")
    if wmi_health and (
        wmi_health.get("cpuUsagePercent") is not None
        or wmi_health.get("ramUsagePercent") is not None
    ):
        return wmi_health
    wmi_detail = wmi_health.get("detail") if wmi_health else ""
    candidates = (
        "/global/serverstatistics",
        "/global/serverstatus",
        "/global/health",
        "/global/status",
        "/server/health",
        "/server/status",
    )
    errors = []
    for path in candidates:
        try:
            data = fetch_json(base_url + path, headers, config.timeout_seconds, context, "serverHealth")
            health = server_health_from_payload(data, path)
            if health:
                return health
        except RestApiError as exc:
            errors.append(f"{path}: HTTP {exc.status_code}")
    detail = "No CPU/RAM metric found."
    if errors:
        detail = "Health endpoint unavailable: " + "; ".join(errors[:3])
    if wmi_detail:
        detail = f"{wmi_detail} NetWorker fallback: {detail}"
    return unavailable_server_health(detail)


def load_server_health_nwui(config: ApiConfig, opener: Any, auth_headers: dict[str, str]) -> dict[str, Any]:
    wmi_health = load_server_health_wmi(config)
    if config.use_wmi_health:
        return wmi_health or unavailable_server_health("WMI health collection did not return CPU/memory metrics.")
    if wmi_health and (
        wmi_health.get("cpuUsagePercent") is not None
        or wmi_health.get("ramUsagePercent") is not None
    ):
        return wmi_health
    wmi_detail = wmi_health.get("detail") if wmi_health else ""
    candidates = (
        "serverstatistics",
        "serverstatus",
        "system/status",
        "system/health",
        "monitoring/serverhealth",
        "monitoring/system",
        "health",
    )
    errors = []
    for path in candidates:
        try:
            data = nwui_get_json(config, opener, auth_headers, path)
            health = server_health_from_payload(data, f"/nwui/api/{path}")
            if health:
                return health
        except RestApiError as exc:
            errors.append(f"/nwui/api/{path}: HTTP {exc.status_code}")
    detail = "No CPU/RAM metric found."
    if errors:
        detail = "Health endpoint unavailable: " + "; ".join(errors[:3])
    if wmi_detail:
        detail = f"{wmi_detail} NetWorker fallback: {detail}"
    return unavailable_server_health(detail)


def build_dashboard_rest(config: ApiConfig) -> tuple[int, dict[str, Any]]:
    base_url = api_base_url(config)
    paths = dashboard_endpoints(config)
    headers = build_headers(config)
    context = ssl_context_for_api(config.verify_tls)
    debug_log(
        "Dashboard request "
        f"restApiBase={base_url} "
        f"backupServer={authc_header_value(config.backup_server_host, config.backup_server_port)} "
        f"authcHeaderEnabled={config.use_authc_header} "
        f"verifyTls={config.verify_tls} "
        f"timeout={config.timeout_seconds}s"
    )

    raw_results: dict[str, Any] = {}
    sources: dict[str, dict[str, Any]] = {}

    def load(name: str, path: str) -> tuple[str, Any]:
        is_jobs = name in ("jobs", "failedJobs")
        load_timeout = max(config.timeout_seconds, 120) if is_jobs else config.timeout_seconds
        load_max_bytes = MAX_JOBS_RESPONSE_BYTES if is_jobs else MAX_RESPONSE_BYTES
        return name, fetch_json(
            base_url + path, headers, load_timeout, context, name, max_bytes=load_max_bytes
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(paths))) as executor:
        futures = {
            executor.submit(load, name, path): (name, path)
            for name, path in paths.items()
        }
        for future in concurrent.futures.as_completed(futures):
            name, path = futures[future]
            try:
                _, data = future.result()
                raw_results[name] = data
                preferred = "jobs" if name == "failedJobs" else name
                sources[name] = {
                    "ok": True,
                    "path": path,
                    "count": len(collection_from(data, preferred)),
                }
                debug_log(
                    f"Dashboard source ok source={name} "
                    f"path={compact_path_for_log(path)} count={sources[name]['count']}"
                )
            except RestApiError as exc:
                sources[name] = {
                    "ok": False,
                    "path": path,
                    "status": exc.status_code,
                    "error": exc.message,
                }
                debug_log(
                    f"Dashboard source failed source={name} "
                    f"path={compact_path_for_log(path)} status={exc.status_code} error={exc.message}"
                )

    clients = collection_from(raw_results.get("clients"), "clients")
    _, _, range_label = report_window(config)
    jobs = [
        job for job in sort_jobs(collection_from(raw_results.get("jobs"), "jobs"))
        if in_report_window(first_value(job, "startTime", "started", "start"), config)
    ]
    failed_jobs_from_query = [
        job for job in sort_jobs(collection_from(raw_results.get("failedJobs"), "jobs"))
        if in_report_window(first_value(job, "startTime", "started", "start"), config)
    ]
    clone_jobs = [job for job in jobs if is_clone_job(job)]
    backup_jobs = [job for job in jobs if not is_clone_job(job)]
    failed_jobs_from_query = [job for job in failed_jobs_from_query if not is_clone_job(job)]
    failed_jobs = failed_jobs_from_query or [job for job in backup_jobs if is_failed_job(job)]
    alerts = [
        alert for alert in collection_from(raw_results.get("alerts"), "alerts")
        if not first_value(alert, "time", "timestamp", "date", "createdTime")
        or in_report_window(first_value(alert, "time", "timestamp", "date", "createdTime"), config)
    ]
    policies = collection_from(raw_results.get("policies"), "policies")
    recovery_jobs = [job for job in backup_jobs if is_recovery_job(job)]

    critical_alerts = sum(1 for alert in alerts if is_critical_alert(alert))
    warning_alerts = sum(1 for alert in alerts if is_warning_alert(alert))
    failed_count = len(failed_jobs)
    clone_failed = sum(1 for job in clone_jobs if is_failed_job(job))
    clone_running = sum(1 for job in clone_jobs if is_active_job(job))
    source_errors = sum(1 for item in sources.values() if not item.get("ok"))

    if failed_count or clone_failed or critical_alerts:
        health = "critical"
    elif warning_alerts or source_errors:
        health = "warning"
    else:
        health = "ok"

    summary = add_sla_summary({
        "totalClients": len(clients),
        "totalJobs": len(backup_jobs),
        "successfulJobs": sum(1 for job in backup_jobs if is_success_job(job)),
        "failedJobs": failed_count,
        "activeJobs": sum(1 for job in backup_jobs if is_active_job(job)),
        "recoveryJobs": len(recovery_jobs),
        "recoveryFailed": sum(1 for job in recovery_jobs if is_failed_job(job)),
        "recoveryRunning": sum(1 for job in recovery_jobs if is_active_job(job)),
        "cloneJobs": len(clone_jobs),
        "cloneFailed": clone_failed,
        "cloneRunning": clone_running,
        "cloneSessionTotal": 0,
        "cloneSessionFailed": 0,
        "cloneSessionRunning": 0,
        "totalAlerts": len(alerts),
        "criticalAlerts": critical_alerts,
        "warningAlerts": warning_alerts,
        "policies": len(policies),
        "range": config.report_range,
        "rangeLabel": range_label,
        "health": health,
    })

    tables = {
        "jobs": [project_job(job) for job in backup_jobs[:TABLE_LIMIT]],
        "failedJobs": [project_failed_job(job) for job in failed_jobs[:TABLE_LIMIT]],
        "recovery": [project_job(job) for job in recovery_jobs[:TABLE_LIMIT]],
        "cloneJobs": [project_job(job) for job in clone_jobs[:TABLE_LIMIT]],
        "logs": [
            row
            for row in (project_job_log(job) for job in (backup_jobs + clone_jobs)[:TABLE_LIMIT])
            if row.get("message")
        ],
        "alerts": [project_alert(alert) for alert in alerts[:TABLE_LIMIT]],
        "clients": [project_client(client) for client in clients[:TABLE_LIMIT]],
    }
    server_health = load_server_health_rest(config, base_url, headers, context)
    maintenance_backup = maintenance_backup_status(tables["jobs"] + tables["failedJobs"])

    any_success = any(item.get("ok") for item in sources.values())
    statuses = {item.get("status") for item in sources.values() if not item.get("ok")}
    response_status = 200
    if not any_success:
        if len(statuses) == 1 and next(iter(statuses)) in (401, 403):
            response_status = int(next(iter(statuses)))
        else:
            response_status = 502

    body = {
        "ok": any_success,
        "generatedAt": generated_at(),
        "target": {
            "restApiBase": base_url,
            "apiMode": "rest",
            "backupServer": authc_header_value(config.backup_server_host, config.backup_server_port),
            "authcHeaderEnabled": config.use_authc_header,
            "verifyTls": config.verify_tls,
            "reportRange": config.report_range,
        },
        "summary": summary,
        "serverHealth": server_health,
        "serverProtectionJob": maintenance_backup,
        "maintenanceBackup": maintenance_backup,
        "sources": sources,
        "tables": tables,
    }
    if not any_success:
        first_error = next((item.get("error") for item in sources.values() if item.get("error")), "")
        body["error"] = first_error or "All REST API calls failed."
    return response_status, body


def json_status_request(
    opener: Any,
    url: str,
    method: str,
    headers: dict[str, str],
    timeout: int,
    payload: Any | None = None,
) -> tuple[int, Any, str]:
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = read_limited(response, MAX_RESPONSE_BYTES)
            text = raw.decode(response.headers.get_content_charset() or "utf-8", "replace")
            if not text:
                return response.status, None, ""
            try:
                return response.status, json.loads(text), text
            except json.JSONDecodeError:
                return response.status, text, text
    except HTTPError as exc:
        raw = exc.read(8192)
        text = raw.decode("utf-8", "replace")
        try:
            data_obj: Any = json.loads(text)
        except json.JSONDecodeError:
            data_obj = text
        return exc.code, data_obj, text
    except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        raise RestApiError(502, describe_url_error(exc)) from exc


def nwui_headers(config: ApiConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    origin = origin_url(config)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": f"{origin}/nwui/",
        "User-Agent": f"networker-dashboard/{APP_VERSION}",
    }
    if extra:
        headers.update(extra)
    return headers


def extract_nwui_list(data: Any, *keys: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    nested = data.get("data")
    if isinstance(nested, dict):
        return extract_nwui_list(nested, *keys)
    if isinstance(nested, list):
        return nested
    for value in data.values():
        if isinstance(value, list):
            return value
    return []


def unwrap_nwui_data(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data and len(data) <= 3:
        return data["data"]
    return data


def nwui_login(config: ApiConfig, opener: Any) -> tuple[dict[str, str], dict[str, Any]]:
    origin = origin_url(config)
    login_url = f"{origin}/nwui/api/login"
    headers = nwui_headers(config, {"Content-Type": "application/json"})
    auth_headers: dict[str, str] = {}
    payloads = [
        {"username": config.username, "pwd": config.password, "server": None, "port": None},
        {
            "username": config.username,
            "pwd": config.password,
            "server": config.backup_server_host,
            "port": config.backup_server_port,
        },
        {"username": config.username, "pwd": config.password},
    ]
    last_error = "NWUI login did not return a successful response."
    last_auth_status = 0
    auth_failures: list[str] = []
    for payload in payloads:
        keys = ",".join(payload.keys())
        debug_log(f"NWUI login try url={login_url} payloadKeys={keys}")
        status, data, body = json_status_request(
            opener,
            login_url,
            "POST",
            headers,
            config.timeout_seconds,
            payload,
        )
        debug_log(f"NWUI login result status={status} payloadKeys={keys}")
        if status in (200, 201):
            data_obj = data if isinstance(data, dict) else {}
            token = ""
            for key in ("token", "access_token", "Token", "accessToken", "auth_token", "authToken"):
                if data_obj.get(key):
                    token = str(data_obj[key])
                    break
            if token:
                auth_headers["Authorization"] = f"Bearer {token}"
            if not data_obj.get("errorCode") and not data_obj.get("errorMessage"):
                return auth_headers, {"status": status, "hasToken": bool(token)}
            last_error = stringify(data_obj.get("errorMessage") or data_obj.get("errorCode") or body, 260)
        elif status in (401, 403):
            detail = data.get("errorMessage") if isinstance(data, dict) else ""
            last_auth_status = status
            auth_failures.append(
                f"{keys}: {stringify(detail or 'NWUI login rejected this payload shape.', 180)}"
            )
            last_error = detail or "NWUI login failed. Check username/password and account access."
            continue
        elif status == 404:
            last_error = (
                "NWUI login endpoint /nwui/api/login was not found. Check that REST API server IP/port "
                "points to the NWUI host, or use NetWorker REST API mode."
            )
        else:
            last_error = describe_http_error(status, "NWUI login failed", body, login_url)
    if last_auth_status:
        detail = (
            f"{last_error} Tried {len(auth_failures)} NWUI login payload variant(s): "
            + " | ".join(auth_failures)
        )
        raise RestApiError(last_auth_status, safe_log_text(detail, 700))
    raise RestApiError(502, last_error)


def nwui_get_json(config: ApiConfig, opener: Any, auth_headers: dict[str, str], path: str) -> Any:
    url = f"{nwui_api_base_url(config)}/{path.lstrip('/')}"
    headers = nwui_headers(config, auth_headers)
    debug_log(f"NWUI GET path=/{path.lstrip('/')}")
    status, data, body = json_status_request(opener, url, "GET", headers, config.timeout_seconds)
    if status not in (200, 201):
        raise RestApiError(status, describe_http_error(status, "NWUI GET failed", body, url))
    return unwrap_nwui_data(data)


def nwui_post_json(
    config: ApiConfig,
    opener: Any,
    auth_headers: dict[str, str],
    path: str,
    payload: dict[str, Any],
) -> Any:
    url = f"{nwui_api_base_url(config)}/{path.lstrip('/')}"
    headers = nwui_headers(config, {**auth_headers, "Content-Type": "application/json"})
    debug_log(
        f"NWUI POST path=/{path.lstrip('/')} page={payload.get('pageNumber', '')} "
        f"limit={payload.get('pageLimit', '')}"
    )
    status, data, body = json_status_request(opener, url, "POST", headers, config.timeout_seconds, payload)
    if status not in (200, 201):
        raise RestApiError(status, describe_http_error(status, "NWUI POST failed", body, url))
    return unwrap_nwui_data(data)


def monitoring_payload(
    page: int,
    page_limit: int = 200,
    start_ts: float | None = None,
    end_ts: float | None = None,
    include_window: bool = True,
) -> dict[str, Any]:
    now = datetime.now().timestamp()
    start = start_ts if start_ts is not None else now - (30 * 24 * 60 * 60)
    end = end_ts if end_ts is not None else now
    payload = {
        "lastRun": False,
        "noRun": False,
        "pageNumber": page,
        "pageLimit": page_limit,
    }
    if include_window:
        payload["startTime"] = int(start * 1000)
        payload["endTime"] = int(end * 1000)
    return payload


def item_in_report_window(item: Any, start_ts: float | None, end_ts: float | None) -> bool:
    if start_ts is None and end_ts is None:
        return True
    if not isinstance(item, dict):
        return True
    value = first_value(item, "startTime", "started", "start", "timestamp", "time", "createdTime", "lastRunTime")
    item_ts = timestamp(value)
    if not item_ts:
        return True
    if start_ts is not None and item_ts < start_ts:
        return False
    if end_ts is not None and item_ts > end_ts:
        return False
    return True


def filter_items_to_report_window(items: list[Any], start_ts: float | None, end_ts: float | None) -> list[Any]:
    return [item for item in items if item_in_report_window(item, start_ts, end_ts)]


def nwui_monitoring_pages_with_strategy(
    config: ApiConfig,
    opener: Any,
    auth_headers: dict[str, str],
    endpoint_name: str,
    start_ts: float | None = None,
    end_ts: float | None = None,
    page_limit: int = 200,
    include_window: bool = True,
) -> list[Any]:
    all_items: list[Any] = []
    page = 1
    while page <= 50:
        data = nwui_post_json(
            config,
            opener,
            auth_headers,
            endpoint_name,
            monitoring_payload(page, page_limit, start_ts, end_ts, include_window=include_window),
        )
        items = extract_nwui_list(
            data,
            "policies",
            "workflows",
            "actions",
            "sessions",
            "alerts",
            "recoveries",
            "items",
            "data",
            "results",
            "rows",
            "content",
        )
        if not items:
            break
        all_items.extend(items)
        total = 0
        if isinstance(data, dict):
            total = int(data.get("totalCount") or data.get("total") or data.get("totalItems") or 0)
        if total and len(all_items) >= total:
            break
        if len(items) < page_limit:
            break
        page += 1
    return all_items


def nwui_monitoring_all_pages(
    config: ApiConfig,
    opener: Any,
    auth_headers: dict[str, str],
    endpoint_name: str,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> list[Any]:
    attempts: list[str] = []
    strategies = [
        (200, True),
        (100, True),
        (50, True),
        (100, False),
        (50, False),
    ]
    last_error: RestApiError | None = None
    for page_limit, include_window in strategies:
        strategy_name = f"pageLimit={page_limit},window={'on' if include_window else 'off'}"
        try:
            items = nwui_monitoring_pages_with_strategy(
                config,
                opener,
                auth_headers,
                endpoint_name,
                start_ts=start_ts,
                end_ts=end_ts,
                page_limit=page_limit,
                include_window=include_window,
            )
            if not include_window:
                items = filter_items_to_report_window(items, start_ts, end_ts)
                debug_log(
                    f"NWUI monitoring fallback succeeded endpoint={endpoint_name} "
                    f"strategy={strategy_name} filteredCount={len(items)}"
                )
            elif attempts:
                debug_log(f"NWUI monitoring retry succeeded endpoint={endpoint_name} strategy={strategy_name}")
            return items
        except RestApiError as exc:
            last_error = exc
            attempts.append(f"{strategy_name}: {exc.message}")
            if exc.status_code < 500:
                break
            debug_log(f"NWUI monitoring retry endpoint={endpoint_name} strategy={strategy_name} error={exc.message}")
    if last_error:
        detail = " | ".join(attempts)
        raise RestApiError(last_error.status_code, f"{last_error.message} (retry attempts: {detail})") from last_error
    return []


# Short-TTL cache for the completed-job history pulled from the NetWorker jobs
# database. The /global/jobs response is large (NetWorker has no server-side
# time filter, so the whole retained set is returned — ~11 MB / thousands of
# jobs on a busy server) and barely changes between rapid refreshes. Without
# caching, every dashboard build — for every restored session and the shared
# refresh loop — re-downloads and re-parses it, starving the request workers
# and causing unrelated endpoints to time out. Cache keyed by server+range.
_JOBS_HISTORY_CACHE: dict[tuple[Any, ...], tuple[float, list[Any], str]] = {}
_JOBS_HISTORY_LOCK = threading.Lock()
JOBS_HISTORY_TTL_SECONDS = 180
JOBS_HISTORY_CACHE_MAX = 16


def cached_nwui_job_history(
    config: ApiConfig, context: ssl.SSLContext
) -> tuple[list[Any], str, bool]:
    """Return (items, path, from_cache) for the NetWorker completed-job history,
    served from a process-wide short-TTL cache shared across sessions and the
    shared refresh loop."""
    key = (
        str(config.backup_server_host or "").lower(),
        int(config.backup_server_port or 0),
        str(config.rest_api_host or "").lower(),
        int(config.rest_api_port or 0),
        str(config.username or ""),
        str(config.report_range or ""),
        str(config.custom_start_date or ""),
        str(config.custom_end_date or ""),
    )
    now = time.time()
    with _JOBS_HISTORY_LOCK:
        entry = _JOBS_HISTORY_CACHE.get(key)
        if entry and now - entry[0] < JOBS_HISTORY_TTL_SECONDS:
            return entry[1], entry[2], True
    items, path = nwui_rest_fallback_items(config, "actions", context)
    with _JOBS_HISTORY_LOCK:
        _JOBS_HISTORY_CACHE[key] = (now, items, path)
        if len(_JOBS_HISTORY_CACHE) > JOBS_HISTORY_CACHE_MAX:
            for old_key in sorted(
                _JOBS_HISTORY_CACHE, key=lambda k: _JOBS_HISTORY_CACHE[k][0]
            )[:-JOBS_HISTORY_CACHE_MAX]:
                _JOBS_HISTORY_CACHE.pop(old_key, None)
    return items, path, False


def cmd_flag_value(command: str, flag: str) -> str:
    if not command:
        return ""
    match = re.search(r"\s-" + re.escape(flag) + r'\s+("[^"]+"|\S+)', " " + command)
    if not match:
        return ""
    return match.group(1).strip('"')


def parse_nwui_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return display_datetime(value)
    text = str(value)
    if text.isdigit() and len(text) >= 10:
        return display_datetime(text)
    return display_datetime(text).replace(".000", "")


def normalize_nwui_status(value: Any, failed_sessions: int = 0) -> str:
    raw = str(value or "unknown").strip().lower().replace(" ", "_")
    mapping = {
        "completed": "succeeded",
        "success": "succeeded",
        "successful": "succeeded",
        "ok": "succeeded",
        "done": "succeeded",
        "finished": "succeeded",
        "in_progress": "running",
        "active": "running",
        "started": "running",
        "waiting": "queued",
        "pending": "queued",
        "scheduled": "queued",
        "error": "failed",
        "aborted": "failed",
        "failure": "failed",
        "warnings": "warning",
        "interrupted": "warning",
        "missed": "warning",
        "missedtheschedule": "warning",
        "missed_the_schedule": "warning",
        "skipped": "warning",
        "never_started": "warning",
        "notstarted": "warning",
    }
    status = mapping.get(raw, raw)
    if status not in ("succeeded", "failed", "warning", "running", "queued"):
        if "succ" in raw or "complet" in raw:
            status = "succeeded"
        elif "fail" in raw or "error" in raw or "abort" in raw:
            status = "failed"
        elif "warn" in raw or "miss" in raw or "skip" in raw or "interrupt" in raw:
            status = "warning"
        elif "run" in raw or "progress" in raw:
            status = "running"
        elif "wait" in raw or "pend" in raw or "queue" in raw:
            status = "queued"
        else:
            status = raw or "unknown"
    if status == "succeeded" and failed_sessions > 0:
        return "warning"
    return status


def project_nwui_job(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    job_data = item.get("jobData") if isinstance(item.get("jobData"), dict) else {}
    success = int(job_data.get("successfulInputCount") or 0)
    failed = int(job_data.get("failedInputCount") or 0)
    waiting = int(job_data.get("waitingInputCount") or 0)
    running = int(job_data.get("runningInputCount") or 0)
    canceled = int(job_data.get("canceledInputCount") or 0)
    total_sessions = success + failed + waiting + running + canceled
    command = str(item.get("jobCmd") or "")
    server_from_cmd = cmd_flag_value(command, "s")
    pool_from_cmd = cmd_flag_value(command, "b")
    group_from_cmd = cmd_flag_value(command, "g")
    start = parse_nwui_time(item.get("startTime"))
    try:
        duration_ms = int(float(item.get("duration") or 0))
    except (TypeError, ValueError):
        duration_ms = 0
    duration_seconds = int(duration_ms / 1000) if duration_ms else 0
    status = normalize_nwui_status(item.get("status"), failed)
    workflow = str(item.get("workflowName") or item.get("groupName") or server_from_cmd or "")
    action = str(item.get("actionName") or item.get("jobType") or item.get("name") or "")
    session_summary = (
        f"{total_sessions} sessions ({success} ok, {failed} failed, {running} running, {waiting} waiting, {canceled} canceled)"
        if total_sessions
        else ""
    )
    return {
        "client": workflow,
        "name": action,
        "policy": str(item.get("policyName") or ""),
        "status": status,
        "started": start,
        "duration": format_duration_seconds(duration_seconds),
        "message": clean_networker_job_message(
            first_value(item, "jobOutput", "message", "statusMessage", "errorMessage"),
            session_summary,
            workflow,
        ),
        "_workflow": workflow,
        "_group": str(item.get("groupName") or group_from_cmd or ""),
        "_pool": pool_from_cmd,
        "_server": server_from_cmd,
        "_sessions": {
            "success": success,
            "failed": failed,
            "waiting": waiting,
            "running": running,
            "canceled": canceled,
            "total": total_sessions,
        },
        "_save_set": session_summary,
        "_action_type": str(item.get("policyActionName") or item.get("jobType") or action or ""),
    }


def nwui_job_table_row(job: dict[str, Any]) -> dict[str, str]:
    return {
        "client": str(job.get("client") or ""),
        "name": str(job.get("name") or ""),
        "policy": str(job.get("policy") or ""),
        "status": str(job.get("status") or ""),
        "started": str(job.get("started") or ""),
        "duration": str(job.get("duration") or ""),
        "message": str(job.get("message") or job.get("_save_set") or ""),
    }


def nwui_job_log_row(job: dict[str, Any]) -> dict[str, str]:
    message = str(job.get("message") or job.get("_save_set") or job.get("name") or "")
    return networker_log_row(
        message,
        job.get("started"),
        job.get("status"),
        networker_log_category(message, "policy"),
        "event",
    )


def project_nwui_recovery(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        return {
            "client": "",
            "name": "Restore",
            "policy": "",
            "status": "unknown",
            "started": "",
            "duration": "",
            "message": "",
        }
    return {
        "client": str(item.get("clientName") or item.get("client") or item.get("hostname") or ""),
        "name": str(item.get("recoverType") or item.get("type") or "Restore"),
        "policy": str(item.get("policyName") or item.get("policy") or ""),
        "status": normalize_nwui_status(item.get("status") or item.get("state")),
        "started": parse_nwui_time(item.get("startTime") or item.get("start")),
        "duration": str(item.get("durationSeconds") or item.get("duration") or ""),
        "message": str(item.get("saveSet") or item.get("path") or item.get("message") or ""),
    }


def rest_job_as_nwui_action(job: Any) -> dict[str, Any]:
    status = status_text(job)
    status_lower = status.lower()
    success = 1 if is_success_job(job) else 0
    failed = 1 if is_failed_job(job) else 0
    running = 1 if is_active_job(job) and "queue" not in status_lower else 0
    waiting = 1 if "queue" in status_lower or "wait" in status_lower or "pending" in status_lower else 0
    return {
        "startTime": first_value(job, "startTime", "started", "start"),
        "duration": first_value(job, "elapsedTime", "duration", "elapsed"),
        "status": status,
        "workflowName": first_value(job, "clientHostname", "client", "hostname", "workflowName"),
        # policyActionName is the NetWorker action TYPE (backup/clone/...), the
        # same thing the live monitoringactions feed exposes as actionName. It
        # must take priority over the job `name` (often a save-set string) so
        # clone/recovery jobs are classified correctly after projection.
        "actionName": first_value(job, "policyActionName", "actionName", "name"),
        "policyActionName": first_value(job, "policyActionName"),
        "policyName": first_value(job, "policyName", "policy", "workflowName", "protectionPolicyName"),
        "message": first_value(job, "message", "messages", "statusMessage", "errorMessage"),
        "jobData": {
            "successfulInputCount": success,
            "failedInputCount": failed,
            "runningInputCount": running,
            "waitingInputCount": waiting,
            "canceledInputCount": 0,
        },
    }


def action_dedup_key(item: Any) -> tuple[str, str, int] | None:
    """Stable identity for a workflow-action run, used to merge the live
    monitoringactions feed with completed jobs from the NetWorker jobs DB.
    Normalizes startTime to epoch seconds so ISO/epoch format differences
    between the two sources collapse to the same key."""
    if not isinstance(item, dict):
        return None
    workflow = str(item.get("workflowName") or item.get("groupName") or "").strip().lower()
    action = str(item.get("actionName") or item.get("jobType") or item.get("name") or "").strip().lower()
    start = int(timestamp(first_value(item, "startTime", "started", "start")) or 0)
    if not workflow and not action and not start:
        return None
    return (workflow, action, start)


def merge_action_history(live: list[Any], history: list[Any]) -> list[Any]:
    """Merge live monitoringactions (running set) with completed job history.
    When the same run appears in both, prefer the terminal (completed) record
    over the live "running" one so finished jobs are counted correctly."""
    by_key: dict[tuple[str, str, int], Any] = {}
    extras: list[Any] = []
    for item in live:
        key = action_dedup_key(item)
        if key is None:
            extras.append(item)
            continue
        by_key[key] = item
    for item in history:
        key = action_dedup_key(item)
        if key is None:
            extras.append(item)
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            continue
        existing_running = normalize_nwui_status(existing.get("status")) in ("running", "queued")
        incoming_running = normalize_nwui_status(item.get("status")) in ("running", "queued")
        if existing_running and not incoming_running:
            by_key[key] = item
    return list(by_key.values()) + extras


def rest_fallback_versions(config: ApiConfig) -> tuple[str, ...]:
    if config.api_version != "auto":
        return (config.api_version,)
    return API_VERSION_CANDIDATES


def nwui_rest_fallback_configs(config: ApiConfig) -> list[ApiConfig]:
    candidates: list[ApiConfig] = []
    seen: set[tuple[str, int]] = set()

    def add(host: str, port: int) -> None:
        key = (str(host or "").lower(), int(port))
        if not key[0] or key in seen:
            return
        seen.add(key)
        candidates.append(replace(config, rest_api_host=host, rest_api_port=port))

    # In NWUI mode the login/API host can be the NWUI front end, while
    # /nwrestapi is often exposed by the actual NetWorker/AuthC server.
    add(config.backup_server_host, config.backup_server_port)
    add(config.rest_api_host, config.rest_api_port)
    return candidates


def nwui_rest_fallback_items(
    config: ApiConfig,
    target: str,
    context: ssl.SSLContext,
) -> tuple[list[Any], str]:
    if not config.password:
        raise RestApiError(502, "Direct REST fallback needs the current login password; reconnect to refresh this source.")
    paths = dashboard_endpoints(config)
    source_name = "jobs" if target == "actions" else "policies"
    original_path = paths["jobs" if target == "actions" else "policies"]
    last_error: RestApiError | None = None
    attempts: list[str] = []
    for fallback_config in nwui_rest_fallback_configs(config):
        headers = build_headers(fallback_config)
        endpoint_host = authc_header_value(fallback_config.rest_api_host, fallback_config.rest_api_port)
        for version in rest_fallback_versions(fallback_config):
            path = original_path
            removed_fields: set[str] = set()
            query_stripped = False
            url = api_base_url_for_version(fallback_config, version) + path
            while True:
                try:
                    # The jobs database has no server-side time filter and can be
                    # large on busy servers; allow a higher response ceiling and a
                    # longer read timeout for it than for small resources.
                    is_jobs = target == "actions"
                    fetch_timeout = max(fallback_config.timeout_seconds, 120) if is_jobs else fallback_config.timeout_seconds
                    fetch_max_bytes = MAX_JOBS_RESPONSE_BYTES if is_jobs else MAX_RESPONSE_BYTES
                    data = fetch_json(
                        url,
                        headers,
                        fetch_timeout,
                        context,
                        f"nwuiFallback:{source_name}:{endpoint_host}:{version}",
                        max_bytes=fetch_max_bytes,
                    )
                    preferred_key = "jobs" if target == "actions" else "policies"
                    items = collection_from(data, preferred_key)
                    if target == "actions":
                        if APP_DEBUG:
                            total_raw = len(items)
                            raw_completion = Counter(
                                str(job.get("completionStatus") or "").lower()
                                for job in items
                                if isinstance(job, dict)
                            )
                            debug_log(
                                f"REST jobs raw diagnostic source={source_name} version={version} "
                                f"totalRaw={total_raw} completionStatus={dict(raw_completion)}"
                            )
                            for idx, job in enumerate(items[:3]):
                                if isinstance(job, dict):
                                    fields = {k: job.get(k) for k in sorted(job.keys())}
                                    debug_log(
                                        f"REST jobs raw sample[{idx}] keys={sorted(job.keys())} "
                                        f"values={safe_log_text(json.dumps(fields, default=str), 900)}"
                                    )
                        # Filter to the report window FIRST (cheap timestamp
                        # check), then sort and project only the survivors. The
                        # jobs DB can hold tens of thousands of records; sorting
                        # and converting the whole set wastes CPU on a busy server.
                        in_window = [
                            job
                            for job in items
                            if in_report_window(first_value(job, "startTime", "started", "start"), config)
                        ]
                        items = [rest_job_as_nwui_action(job) for job in sort_jobs(in_window)]
                    return items, f"https://{endpoint_host}/nwrestapi/{version}{compact_path_for_log(path)}"
                except RestApiError as exc:
                    invalid_field = invalid_rest_query_field(exc.message, exc.body)
                    if (
                        target == "actions"
                        and exc.status_code == 400
                        and invalid_field
                        and invalid_field not in removed_fields
                    ):
                        next_path = remove_rest_field_from_path(path, invalid_field)
                        if next_path != path:
                            removed_fields.add(invalid_field)
                            attempts.append(
                                f"{endpoint_host}/{version}: removed unsupported field {invalid_field}"
                            )
                            debug_log(
                                f"NWUI REST fallback retry source={source_name} host={endpoint_host} "
                                f"version={version} removedField={invalid_field}"
                            )
                            path = next_path
                            url = api_base_url_for_version(fallback_config, version) + path
                            continue
                    # If the server rejected the server-side time-window query
                    # (NQL syntax not supported on this version), drop the `q`
                    # filter once and retry unfiltered so smaller deployments
                    # still return data. On busy servers this may then hit the
                    # response-size guard, which is reported as a normal error.
                    if (
                        exc.status_code == 400
                        and not query_stripped
                        and "q=" in path
                    ):
                        next_path = strip_query_param(path, "q")
                        if next_path != path:
                            query_stripped = True
                            attempts.append(
                                f"{endpoint_host}/{version}: dropped time-window query after HTTP 400"
                            )
                            debug_log(
                                f"NWUI REST fallback retry source={source_name} host={endpoint_host} "
                                f"version={version} droppedTimeWindowQuery=1"
                            )
                            path = next_path
                            url = api_base_url_for_version(fallback_config, version) + path
                            continue
                    last_error = exc
                    attempts.append(f"{endpoint_host}/{version}: {safe_log_text(exc.message, 180)}")
                    if exc.status_code in (401, 403):
                        break
                    break
    if last_error:
        if attempts:
            raise RestApiError(
                last_error.status_code,
                f"{last_error.message} (direct REST fallback attempts: {' | '.join(attempts)})",
                last_error.body,
            ) from last_error
        raise last_error
    raise RestApiError(502, f"Direct REST fallback for {source_name} did not return data.")


def session_counts_for_jobs(jobs: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"total": 0, "failed": 0, "running": 0}
    for job in jobs:
        counts = job.get("_sessions") or {}
        totals["total"] += int(counts.get("total") or 0)
        totals["failed"] += int(counts.get("failed") or 0)
        totals["running"] += int(counts.get("running") or 0)
    return totals


def nwui_backup_activity_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "completed": 0,
        "successful": 0,
        "failed": 0,
        "active": 0,
        "warnings": 0,
    }
    for job in jobs:
        counts = job.get("_sessions") or {}
        success = int(counts.get("success") or 0)
        failed = int(counts.get("failed") or 0)
        canceled = int(counts.get("canceled") or 0)
        running = int(counts.get("running") or 0)
        waiting = int(counts.get("waiting") or 0)
        session_total = success + failed + canceled + running + waiting
        if session_total:
            failed_total = failed + canceled
            totals["successful"] += success
            totals["failed"] += failed_total
            totals["active"] += running + waiting
            totals["completed"] += success + failed_total
            if str(job.get("status") or "").lower() == "warning" and not failed_total:
                totals["warnings"] += 1
            continue

        status = str(job.get("status") or "").lower()
        if status == "succeeded":
            totals["successful"] += 1
            totals["completed"] += 1
        elif status == "failed":
            totals["failed"] += 1
            totals["completed"] += 1
        elif status == "warning":
            totals["warnings"] += 1
            totals["completed"] += 1
        elif status in ("running", "queued"):
            totals["active"] += 1
    return totals


def project_nwui_alert(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        return {"severity": "info", "time": "", "message": "", "resource": ""}
    return {
        "severity": str(item.get("severity") or item.get("level") or "info").lower(),
        "time": str(item.get("timestamp") or item.get("time") or ""),
        "message": str(item.get("message") or item.get("description") or "")[:260],
        "resource": str(item.get("source") or item.get("category") or item.get("name") or ""),
    }


def build_nwui_clients(jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_workflow: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = job.get("_workflow") or job.get("client") or ""
        if not name:
            continue
        row = by_workflow.setdefault(
            name,
            {
                "hostname": name,
                "enabled": "Yes",
                "backupType": job.get("name", ""),
                "saveSets": 0,
                "protectionGroups": set(),
                "_failed": 0,
            },
        )
        sessions = job.get("_sessions") or {}
        row["saveSets"] += int(sessions.get("total") or 0)
        row["_failed"] += int(sessions.get("failed") or 0)
        if job.get("_group"):
            row["protectionGroups"].add(job["_group"])
    output = []
    for row in by_workflow.values():
        groups = ", ".join(sorted(row["protectionGroups"]))
        output.append(
            {
                "hostname": str(row["hostname"]),
                "enabled": "Warnings" if row["_failed"] else "Yes",
                "backupType": str(row["backupType"]),
                "saveSets": f"{row['saveSets']} sessions",
                "protectionGroups": groups,
            }
        )
    return sorted(output, key=lambda item: item["hostname"])


def build_nwui_policies(policy_items: list[Any], jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
    stats: dict[str, dict[str, Any]] = {}
    for job in jobs:
        policy = job.get("policy") or ""
        if not policy:
            continue
        row = stats.setdefault(policy, {"actions": 0, "workflows": set(), "last": "", "status": "unknown"})
        row["actions"] += 1
        if job.get("_workflow"):
            row["workflows"].add(job["_workflow"])
        if str(job.get("started") or "") > row["last"]:
            row["last"] = str(job.get("started") or "")
            row["status"] = job.get("status") or "unknown"

    output = []
    for item in policy_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("policyName") or "")
        stat = stats.get(name, {})
        output.append(
            {
                "severity": str(item.get("status") or stat.get("status") or "active").lower(),
                "time": str(item.get("lastRun") or item.get("lastRunTime") or stat.get("last") or ""),
                "message": (
                    f"Policy {name}: {item.get('workflowCount') or len(stat.get('workflows', [])) or 0} workflows, "
                    f"{item.get('actionCount') or stat.get('actions') or 0} actions"
                ),
                "resource": name,
            }
        )
    if not output:
        for name, stat in stats.items():
            output.append(
                {
                    "severity": str(stat.get("status") or "unknown"),
                    "time": str(stat.get("last") or ""),
                    "message": f"Policy {name}: {len(stat.get('workflows', []))} workflows, {stat.get('actions', 0)} actions",
                    "resource": name,
                }
            )
    return sorted(output, key=lambda item: item["resource"])


def base_server_protection_detail(detail: Any) -> str:
    text = str(detail or "Last known Server Protection job")
    marker = " (last known"
    if marker in text:
        text = text.split(marker, 1)[0]
    return text.strip() or "Last known Server Protection job"


def last_known_server_protection(
    previous: dict[str, Any],
    refresh_error: str = "",
) -> dict[str, Any]:
    detail = f"{base_server_protection_detail(previous.get('detail'))} (last known)"
    return {
        **previous,
        "detail": detail,
        "_baseDetail": base_server_protection_detail(previous.get("detail") or previous.get("_baseDetail")),
        "_lastRefreshError": refresh_error,
    }


def refresh_server_protection_job_nwui(
    config: ApiConfig,
    cookie_jar: CookieJar,
    auth_headers: dict[str, str],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = ssl_context_for_api(config.verify_tls)
    import urllib.request as _urllib_request

    opener = _urllib_request.build_opener(
        _urllib_request.HTTPCookieProcessor(cookie_jar),
        _urllib_request.HTTPSHandler(context=context),
    )
    now = time.time()
    lookback_start = now - (7 * 24 * 60 * 60)
    try:
        items = nwui_monitoring_all_pages(
            replace(config, api_mode="nwui"),
            opener,
            auth_headers,
            "monitoringactions",
            start_ts=lookback_start,
            end_ts=now,
        )
        jobs = [job for job in (project_nwui_job(item) for item in items) if job]
        jobs = sorted(jobs, key=lambda item: item.get("started") or "", reverse=True)
        status = maintenance_backup_status(jobs)
        if status.get("count"):
            status["_baseDetail"] = base_server_protection_detail(status.get("detail"))
            status["_lastRefreshError"] = ""
            return status
        if previous and previous.get("count"):
            return last_known_server_protection(previous)
        return status
    except RestApiError as exc:
        if previous and previous.get("count"):
            return last_known_server_protection(previous, exc.message)
        return {
            "status": "unknown",
            "label": "Unavailable",
            "detail": f"Server Protection refresh failed: {exc.message}",
            "count": 0,
        }


def session_config_with_secrets(session: DashboardSession) -> ApiConfig:
    config = session.config
    networker_password = decrypt_process_secret(session.encrypted_networker_password)
    wmi_password = decrypt_wmi_password(session.encrypted_wmi_password)
    if networker_password or wmi_password:
        config = replace(
            config,
            password=networker_password or config.password,
            wmi_password=wmi_password or config.wmi_password,
        )
    return config


def sanitize_session_config(config: ApiConfig) -> ApiConfig:
    return replace(config, password="", wmi_password="")


def dashboard_needs_reauth(status: int, body: dict[str, Any]) -> bool:
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return True
    sources = body.get("sources") if isinstance(body.get("sources"), dict) else {}
    for item in sources.values():
        if isinstance(item, dict) and not item.get("ok") and item.get("status") in (401, 403):
            return True
    return False


def reauthenticate_dashboard_session(session: DashboardSession, config: ApiConfig) -> bool:
    """Re-establish NetWorker auth using the stored encrypted password.
    Handles both NWUI and REST modes. Persists updated session to disk.
    """
    password = decrypt_process_secret(session.encrypted_networker_password)
    if not password:
        debug_log("reauthenticate: no stored password available — cannot reauth")
        return False

    context = ssl_context_for_api(config.verify_tls)
    import urllib.request as _urllib_request

    try:
        if config.api_mode in ("auto", "nwui", ""):
            cookie_jar = CookieJar()
            opener = _urllib_request.build_opener(
                _urllib_request.HTTPCookieProcessor(cookie_jar),
                _urllib_request.HTTPSHandler(context=context),
            )
            auth_headers, _ = nwui_login(replace(config, password=password), opener)
            session.cookie_jar = cookie_jar
            session.auth_headers = dict(auth_headers)
        else:
            # REST mode: rebuild Basic auth header (stateless, no cookie needed)
            session.auth_headers = build_headers(replace(config, password=password))
            session.cookie_jar = CookieJar()

        session.last_used = time.time()
        debug_log(f"reauthenticate: success for host={config.rest_api_host} mode={config.api_mode}")
        persist_sessions()
        return True
    except Exception as exc:
        debug_log(f"reauthenticate: failed for host={config.rest_api_host} — {exc}")
        return False


def _session_to_dict(session_id: str, session: "DashboardSession") -> dict[str, Any]:
    """Serialize a session to a JSON-safe dict for disk persistence."""
    c = session.config
    return {
        "session_id": session_id,
        "created_at": session.created_at,
        "last_used": session.last_used,
        "encrypted_networker_password": session.encrypted_networker_password,
        "encrypted_wmi_password": session.encrypted_wmi_password,
        "config": {
            "rest_api_host": c.rest_api_host,
            "rest_api_port": c.rest_api_port,
            "backup_server_host": c.backup_server_host,
            "backup_server_port": c.backup_server_port,
            "username": c.username,
            "api_mode": c.api_mode,
            "api_version": c.api_version,
            "report_range": c.report_range,
            "custom_start_date": c.custom_start_date,
            "custom_end_date": c.custom_end_date,
            "use_wmi_health": c.use_wmi_health,
            "wmi_username": c.wmi_username,
            "timeout_seconds": c.timeout_seconds,
            "verify_tls": c.verify_tls,
            "use_authc_header": c.use_authc_header,
        },
    }


def persist_sessions() -> None:
    """Write all current sessions to disk (encrypted passwords already safe)."""
    if not WMI_CIPHER:
        return  # without stable key, persistence is pointless
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = {
            sid: _session_to_dict(sid, s)
            for sid, s in _session_items_snapshot()
        }
        tmp = SESSION_PERSISTENCE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
        tmp.replace(SESSION_PERSISTENCE_FILE)
    except OSError:
        pass


def restore_sessions_from_disk() -> int:
    """Re-establish sessions saved by a previous process run.
    Returns count of sessions successfully restored.
    """
    if not WMI_CIPHER:
        return 0
    if not SESSION_PERSISTENCE_FILE.exists():
        return 0
    try:
        records: dict[str, Any] = json.loads(SESSION_PERSISTENCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    restored = 0
    now = time.time()
    import urllib.request as _urllib_request

    for session_id, rec in records.items():
        try:
            if now - float(rec.get("last_used", 0)) > SESSION_TTL_SECONDS:
                continue
            enc_pw = str(rec.get("encrypted_networker_password") or "")
            password = decrypt_process_secret(enc_pw)
            if not password:
                continue  # can't reauth without password

            cfg_raw = rec.get("config", {})
            config = ApiConfig(
                rest_api_host=str(cfg_raw.get("rest_api_host") or ""),
                rest_api_port=int(cfg_raw.get("rest_api_port") or DEFAULT_API_PORT),
                backup_server_host=str(cfg_raw.get("backup_server_host") or ""),
                backup_server_port=int(cfg_raw.get("backup_server_port") or DEFAULT_API_PORT),
                username=str(cfg_raw.get("username") or ""),
                password=password,
                api_mode=str(cfg_raw.get("api_mode") or "nwui"),
                api_version=str(cfg_raw.get("api_version") or "auto"),
                report_range=str(cfg_raw.get("report_range") or DEFAULT_REPORT_RANGE),
                custom_start_date=str(cfg_raw.get("custom_start_date") or ""),
                custom_end_date=str(cfg_raw.get("custom_end_date") or ""),
                use_wmi_health=bool(cfg_raw.get("use_wmi_health", False)),
                wmi_username=str(cfg_raw.get("wmi_username") or ""),
                wmi_password=decrypt_wmi_password(str(rec.get("encrypted_wmi_password") or "")),
                timeout_seconds=int(cfg_raw.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
                verify_tls=bool(cfg_raw.get("verify_tls", False)),
                use_authc_header=bool(cfg_raw.get("use_authc_header", True)),
            )
            if not config.rest_api_host or not config.username:
                continue
            if ALLOWLIST_ENABLED and not (
                _host_allowed(config.rest_api_host)
                and _host_allowed(config.backup_server_host or config.rest_api_host)
            ):
                continue

            context = ssl_context_for_api(config.verify_tls)
            cookie_jar = CookieJar()
            opener = _urllib_request.build_opener(
                _urllib_request.HTTPCookieProcessor(cookie_jar),
                _urllib_request.HTTPSHandler(context=context),
            )
            auth_headers, _ = nwui_login(config, opener)

            _put_session(session_id, DashboardSession(
                config=replace(config, password="", wmi_password=""),
                cookie_jar=cookie_jar,
                auth_headers=dict(auth_headers),
                encrypted_networker_password=enc_pw,
                encrypted_wmi_password=str(rec.get("encrypted_wmi_password") or ""),
                created_at=float(rec.get("created_at") or now),
                last_used=now,
            ))
            restored += 1
        except Exception:
            continue

    return restored


def create_dashboard_session(
    config: ApiConfig,
    cookie_jar: CookieJar,
    auth_headers: dict[str, str],
    server_protection_job: dict[str, Any] | None = None,
) -> str:
    cleanup_dashboard_sessions()
    session_id = uuid.uuid4().hex
    _put_session(session_id, DashboardSession(
        config=replace(config, password="", wmi_password="", api_mode="nwui"),
        cookie_jar=cookie_jar,
        auth_headers=dict(auth_headers),
        encrypted_networker_password=encrypt_process_secret(config.password),
        encrypted_wmi_password=encrypt_wmi_password(config.wmi_password),
        created_at=time.time(),
        last_used=time.time(),
        server_protection_job=server_protection_job or maintenance_backup_status([]),
    ))
    persist_sessions()
    return session_id


def cleanup_dashboard_sessions() -> None:
    now = time.time()
    stale = [
        session_id
        for session_id, session in _session_items_snapshot()
        if now - session.last_used > SESSION_TTL_SECONDS
    ]
    for session_id in stale:
        _pop_session(session_id)
        cancel_session_automations(session_id)
    if stale:
        persist_sessions()


def build_dashboard_from_session(
    session_id: str,
    report_range: str | None = None,
    custom_start_date: str = "",
    custom_end_date: str = "",
) -> tuple[int, dict[str, Any]]:
    cleanup_dashboard_sessions()
    session = _get_session(session_id)
    if not session:
        return 401, {
            "ok": False,
            "error": "Dashboard session expired. Reconnect with the password.",
            "summary": {"health": "critical"},
            "serverHealth": unavailable_server_health("Dashboard session expired before server health could be checked."),
            "serverProtectionJob": maintenance_backup_status([]),
            "maintenanceBackup": maintenance_backup_status([]),
            "sources": {},
            "tables": {"jobs": [], "failedJobs": [], "recovery": [], "cloneJobs": [], "logs": [], "alerts": [], "clients": []},
        }
    session.last_used = time.time()
    config = session_config_with_secrets(session)
    if report_range and (report_range in REPORT_RANGES or report_range == CUSTOM_REPORT_RANGE):
        if report_range == CUSTOM_REPORT_RANGE:
            parse_custom_date_window(custom_start_date, custom_end_date)
            config = replace(
                config,
                report_range=report_range,
                custom_start_date=custom_start_date,
                custom_end_date=custom_end_date,
            )
        else:
            config = replace(
                config,
                report_range=report_range,
                custom_start_date="",
                custom_end_date="",
            )
        session.config = sanitize_session_config(config)
    status, body = build_dashboard_nwui(
        config,
        cookie_jar=session.cookie_jar,
        auth_headers=session.auth_headers,
        create_session=False,
    )
    reauthed = False
    if dashboard_needs_reauth(status, body):
        try:
            if reauthenticate_dashboard_session(session, config):
                reauthed = True
                status, body = build_dashboard_nwui(
                    config,
                    cookie_jar=session.cookie_jar,
                    auth_headers=session.auth_headers,
                    create_session=False,
                )
        except RestApiError as exc:
            body.setdefault("sources", {})["nwuiRelogin"] = {
                "ok": False,
                "path": "/nwui/api/login",
                "status": exc.status_code,
                "error": exc.message,
            }
    if status == 200 and body.get("ok") and not dashboard_backup_source_available(body):
        if not reauthed and reauthenticate_dashboard_session(session, config):
            status, body = build_dashboard_nwui(
                config,
                cookie_jar=session.cookie_jar,
                auth_headers=session.auth_headers,
                create_session=False,
            )
        if status == 200 and body.get("ok") and not dashboard_backup_source_available(body):
            stale_body = stale_dashboard_from_cache(session_id, body)
            if stale_body:
                body = stale_body
    if status == 200:
        body["sessionId"] = session_id
        persist_sessions()
    session.config = sanitize_session_config(config)
    return status, body


def build_server_health_from_session(session_id: str) -> tuple[int, dict[str, Any]]:
    cleanup_dashboard_sessions()
    session = _get_session(session_id)
    if not session:
        return HTTPStatus.UNAUTHORIZED, {
            "ok": False,
            "error": "Dashboard session expired. Reconnect with the password.",
            "generatedAt": generated_at(),
            "serverHealth": unavailable_server_health("Dashboard session expired before server health could be refreshed."),
        }

    session.last_used = time.time()
    config = session_config_with_secrets(session)

    if config.use_wmi_health:
        health = load_server_health_wmi(config) or unavailable_server_health(
            "WMI health collection did not return CPU/memory metrics."
        )
    elif config.api_mode in ("auto", "nwui"):
        config = replace(config, api_mode="nwui")
        health = load_server_health_nwui(config, session.cookie_jar, session.auth_headers)
    else:
        health = unavailable_server_health("Real-time server health refresh requires a dashboard session with stored authentication.")

    if session.auth_headers or any(True for _ in session.cookie_jar):
        try:
            server_protection = refresh_server_protection_job_nwui(
                config,
                session.cookie_jar,
                session.auth_headers,
                session.server_protection_job,
            )
        except RestApiError as exc:
            if exc.status_code in (401, 403) and reauthenticate_dashboard_session(session, config):
                server_protection = refresh_server_protection_job_nwui(
                    config,
                    session.cookie_jar,
                    session.auth_headers,
                    session.server_protection_job,
                )
            else:
                raise
    else:
        server_protection = session.server_protection_job or maintenance_backup_status([])
    session.server_protection_job = server_protection
    session.config = sanitize_session_config(config)
    return HTTPStatus.OK, {
        "ok": True,
        "generatedAt": generated_at(),
        "serverHealth": health,
        "serverProtectionJob": server_protection,
        "maintenanceBackup": server_protection,
    }


def build_dashboard_nwui(
    config: ApiConfig,
    cookie_jar: CookieJar | None = None,
    auth_headers: dict[str, str] | None = None,
    create_session: bool = True,
) -> tuple[int, dict[str, Any]]:
    context = ssl_context_for_api(config.verify_tls)
    sources: dict[str, dict[str, Any]] = {}
    backup_target = authc_header_value(config.backup_server_host, config.backup_server_port)
    debug_log(
        "NWUI dashboard request "
        f"apiBase={nwui_api_base_url(config)} "
        f"networkerServer={backup_target} "
        f"verifyTls={config.verify_tls} timeout={config.timeout_seconds}s"
    )

    # urllib opener does not accept an SSL context directly through build_opener;
    # use a custom HTTPS handler by installing the context into requests below.
    import urllib.request as _urllib_request

    cookie_jar = cookie_jar or CookieJar()
    opener = _urllib_request.build_opener(
        _urllib_request.HTTPCookieProcessor(cookie_jar),
        _urllib_request.HTTPSHandler(context=context),
    )

    should_login = auth_headers is None
    login_info: dict[str, Any] = {"status": "reused", "hasToken": bool(auth_headers)}
    if auth_headers is None:
        auth_headers = {}
    try:
        if should_login:
            auth_headers, login_info = nwui_login(config, opener)
            source_path = "/nwui/api/login"
        else:
            source_path = "volatile-session"
        sources["nwuiLogin"] = {
            "ok": True,
            "path": source_path,
            "count": 1,
            "detail": f"Networker server target {backup_target}",
        }
    except RestApiError as exc:
        sources["nwuiLogin"] = {
            "ok": False,
            "path": "/nwui/api/login",
            "status": exc.status_code,
            "error": exc.message,
        }
        body = {
            "ok": False,
            "generatedAt": generated_at(),
            "target": {
                "restApiBase": nwui_api_base_url(config),
                "apiMode": "nwui",
                "backupServer": backup_target,
                "authcHeaderEnabled": False,
                "verifyTls": config.verify_tls,
                "reportRange": config.report_range,
            },
            "summary": {"health": "critical"},
            "serverHealth": unavailable_server_health("NWUI login failed before server health could be checked."),
            "serverProtectionJob": maintenance_backup_status([]),
            "maintenanceBackup": maintenance_backup_status([]),
            "sources": sources,
            "tables": {"jobs": [], "failedJobs": [], "recovery": [], "cloneJobs": [], "logs": [], "alerts": [], "clients": []},
            "error": exc.message,
        }
        return exc.status_code if exc.status_code in (401, 403) else 502, body

    raw_actions: list[Any] = []
    raw_policies: list[Any] = []
    raw_alerts: list[Any] = []
    raw_recoveries: list[Any] = []
    start_ts, end_ts, range_label = report_window(config)
    for source_name, endpoint_name, target in (
        ("monitoringActions", "monitoringactions", "actions"),
        ("monitoringPolicies", "monitoringpolicies", "policies"),
        ("monitoringAlerts", "monitoringalerts", "alerts"),
        ("monitoringRecoveries", "monitoringrecoveries", "recoveries"),
    ):
        try:
            items = nwui_monitoring_all_pages(
                config,
                opener,
                auth_headers,
                endpoint_name,
                start_ts=start_ts,
                end_ts=end_ts,
            )
            sources[source_name] = {"ok": True, "path": f"/nwui/api/{endpoint_name}", "count": len(items)}
            if target == "actions":
                raw_actions = items
            elif target == "policies":
                raw_policies = items
            elif target == "alerts":
                raw_alerts = items
            elif target == "recoveries":
                raw_recoveries = items
        except RestApiError as exc:
            if target in ("actions", "policies"):
                try:
                    items, fallback_path = nwui_rest_fallback_items(config, target, context)
                    sources[source_name] = {
                        "ok": True,
                        "path": fallback_path,
                        "count": len(items),
                        "detail": f"Used direct REST fallback after /nwui/api/{endpoint_name} returned HTTP {exc.status_code}.",
                    }
                    if target == "actions":
                        raw_actions = items
                    else:
                        raw_policies = items
                    continue
                except RestApiError as fallback_exc:
                    if target == "policies":
                        sources[source_name] = {
                            "ok": True,
                            "path": f"/nwui/api/{endpoint_name}",
                            "count": 0,
                            "detail": (
                                "Optional policy summary unavailable; dashboard continues without policy rows. "
                                f"NWUI error: {safe_log_text(exc.message, 220)}; REST fallback: {safe_log_text(fallback_exc.message, 220)}"
                            ),
                        }
                        raw_policies = []
                        continue
                    exc = RestApiError(
                        exc.status_code,
                        f"{exc.message}; direct REST fallback also failed: {fallback_exc.message}",
                    )
            sources[source_name] = {
                "ok": False,
                "path": f"/nwui/api/{endpoint_name}",
                "status": exc.status_code,
                "error": exc.message,
                "userMessage": (
                    "Backup activity source is temporarily unavailable; server health and cached local snapshot data remain visible."
                    if target == "actions"
                    else "Source is temporarily unavailable."
                ),
            }

    # /nwui/api/monitoringactions is the LIVE activity monitor: it returns only
    # the currently-active workflow actions (status="Running"), not completed
    # historical runs, and it ignores the requested time window. Completed jobs
    # for the selected range live in the NetWorker jobs database, reachable via
    # nwrestapi /global/jobs. Merge that history in so finished backups show up.
    # Best-effort: a failure here must never break the live dashboard.
    live_action_count = len(raw_actions)
    history_action_count = 0
    history_from_cache = False
    if config.password:
        try:
            rest_history, history_path, history_from_cache = cached_nwui_job_history(config, context)
            # Keep only completed/terminal runs (succeeded/failed/warning) from
            # the jobs DB. This drops running/queued (the live monitor already
            # provides those) and status-less records (empty completionStatus),
            # which are not real completed backups and would otherwise inflate
            # the totals as "unknown".
            rest_history = [
                item
                for item in rest_history
                if normalize_nwui_status(item.get("status")) in ("succeeded", "failed", "warning")
            ]
            history_action_count = len(rest_history)
            raw_actions = merge_action_history(raw_actions, rest_history)
            sources["monitoringActionsHistory"] = {
                "ok": True,
                "path": history_path,
                "count": history_action_count,
                "cached": history_from_cache,
                "detail": (
                    "Completed job history merged from the NetWorker jobs database"
                    + (" (cached)." if history_from_cache else ".")
                ),
            }
        except RestApiError as exc:
            sources["monitoringActionsHistory"] = {
                "ok": False,
                "path": "/nwrestapi/global/jobs",
                "status": exc.status_code,
                "error": safe_log_text(exc.message, 300),
                "userMessage": "Completed job history is unavailable; showing live backup activity only.",
                "severity": "info",
                "displayWarning": False,
                "diagnosticOnly": True,
            }

    jobs = [job for job in (project_nwui_job(item) for item in raw_actions) if job]
    jobs = sorted(jobs, key=lambda item: item.get("started") or "", reverse=True)
    clone_jobs = [job for job in jobs if is_clone_job(job)]
    backup_jobs = [job for job in jobs if not is_clone_job(job)]
    if APP_DEBUG:
        debug_log(
            "NWUI action merge: "
            f"liveActions={live_action_count} historyActions={history_action_count} "
            f"mergedActions={len(raw_actions)} historyCached={history_from_cache}"
        )
        raw_status = Counter(
            str(item.get("status") or "").lower()
            for item in raw_actions
            if isinstance(item, dict)
        )
        norm_status = Counter(str(job.get("status") or "unknown") for job in jobs)
        debug_log(
            "NWUI monitoringactions diagnostic: "
            f"window={display_datetime(start_ts)}..{display_datetime(end_ts)} "
            f"rawActions={len(raw_actions)} jobs={len(jobs)} "
            f"backup={len(backup_jobs)} clone={len(clone_jobs)} "
            f"rawStatus={dict(raw_status)} normalizedStatus={dict(norm_status)}"
        )
        for idx, sample in enumerate(raw_actions[:3]):
            if isinstance(sample, dict):
                debug_log(
                    f"NWUI raw action sample[{idx}] keys={sorted(sample.keys())} "
                    f"status={sample.get('status')!r} "
                    f"startTime={sample.get('startTime')!r} "
                    f"completionTime={sample.get('completionTime')!r} "
                    f"actionName={sample.get('actionName')!r} "
                    f"workflowName={sample.get('workflowName')!r}"
                )
    failed_jobs = [job for job in backup_jobs if str(job.get("status", "")).lower() in ("failed", "warning")]
    clients = build_nwui_clients(backup_jobs)
    alerts = [project_nwui_alert(item) for item in raw_alerts if isinstance(item, dict)]
    if not alerts:
        alerts = [
            {
                "severity": "critical" if job.get("status") == "failed" else "warning",
                "time": str(job.get("started") or ""),
                "message": f"{str(job.get('status')).title()} backup: {job.get('client', '')} / {job.get('name', '')}",
                "resource": str(job.get("policy") or "backup"),
            }
            for job in failed_jobs[:50]
        ]
    policy_alert_rows = build_nwui_policies(raw_policies, backup_jobs)
    recovery_jobs = [item for item in raw_recoveries if isinstance(item, dict) and not is_clone_job(item)]
    clone_recoveries = [item for item in raw_recoveries if isinstance(item, dict) and is_clone_job(item)]
    recovery_rows = [project_nwui_recovery(item) for item in recovery_jobs]
    clone_recovery_rows = [project_nwui_recovery(item) for item in clone_recoveries]

    clone_session_counts = session_counts_for_jobs(clone_jobs)
    backup_activity = nwui_backup_activity_counts(backup_jobs)
    successful_jobs = backup_activity["successful"]
    failed_count = backup_activity["failed"]
    warning_count = backup_activity["warnings"]
    active_jobs = backup_activity["active"]
    recovery_failed = sum(1 for row in recovery_rows if row.get("status") == "failed")
    recovery_running = sum(1 for row in recovery_rows if row.get("status") in ("running", "queued"))
    clone_failed = sum(1 for job in clone_jobs if job.get("status") == "failed") + sum(
        1 for row in clone_recovery_rows if row.get("status") == "failed"
    )
    clone_running = sum(1 for job in clone_jobs if job.get("status") in ("running", "queued")) + sum(
        1 for row in clone_recovery_rows if row.get("status") in ("running", "queued")
    )
    critical_source_errors = sum(
        1
        for name, item in sources.items()
        if name in {"nwuiLogin", "monitoringActions"} and not item.get("ok")
    )
    warning_source_errors = sum(
        1
        for name, item in sources.items()
        if name not in {"nwuiLogin", "monitoringActions"}
        and not item.get("ok")
        and not item.get("diagnosticOnly")
    )
    health = (
        "critical"
        if failed_count or recovery_failed or clone_failed or critical_source_errors
        else ("warning" if warning_count or warning_source_errors else "ok")
    )

    tables = {
        "jobs": [nwui_job_table_row(job) for job in backup_jobs[:TABLE_LIMIT]],
        "failedJobs": [
            {
                "client": job.get("client", ""),
                "name": job.get("name", ""),
                "policy": job.get("policy", ""),
                "started": job.get("started", ""),
                "message": job.get("message") or job.get("_save_set", ""),
            }
            for job in failed_jobs[:TABLE_LIMIT]
        ],
        "recovery": recovery_rows[:TABLE_LIMIT],
        "cloneJobs": ([nwui_job_table_row(job) for job in clone_jobs] + clone_recovery_rows)[:TABLE_LIMIT],
        "logs": [
            row
            for row in (nwui_job_log_row(job) for job in (backup_jobs + clone_jobs)[:TABLE_LIMIT])
            if row.get("message")
        ],
        "alerts": (alerts + policy_alert_rows)[:TABLE_LIMIT],
        "clients": clients[:TABLE_LIMIT],
    }
    server_health = load_server_health_nwui(config, opener, auth_headers)
    maintenance_backup = maintenance_backup_status(tables["jobs"] + backup_jobs)

    any_success = sources.get("nwuiLogin", {}).get("ok") and any(
        item.get("ok") for key, item in sources.items() if key != "nwuiLogin"
    )
    body = {
        "ok": bool(any_success),
        "generatedAt": generated_at(),
        "target": {
            "restApiBase": nwui_api_base_url(config),
            "apiMode": "nwui",
            "backupServer": backup_target,
            "authcHeaderEnabled": False,
            "verifyTls": config.verify_tls,
            "reportRange": config.report_range,
            "login": login_info,
        },
        "summary": add_sla_summary({
            "totalClients": len(clients),
            "totalJobs": backup_activity["completed"] + backup_activity["active"],
            "completedJobs": backup_activity["completed"],
            "successfulJobs": successful_jobs,
            "failedJobs": failed_count,
            "activeJobs": active_jobs,
            "recoveryJobs": len(recovery_jobs),
            "recoveryFailed": recovery_failed,
            "recoveryRunning": recovery_running,
            "cloneJobs": len(clone_jobs) + len(clone_recoveries),
            "cloneFailed": clone_failed,
            "cloneRunning": clone_running,
            "cloneSessionTotal": clone_session_counts["total"],
            "cloneSessionFailed": clone_session_counts["failed"],
            "cloneSessionRunning": clone_session_counts["running"],
            "totalAlerts": len(alerts),
            "criticalAlerts": sum(1 for item in alerts if item.get("severity") == "critical"),
            "warningAlerts": sum(1 for item in alerts if item.get("severity") == "warning"),
            "policies": len(raw_policies),
            "range": config.report_range,
            "rangeLabel": range_label,
            "health": health,
        }),
        "serverHealth": server_health,
        "serverProtectionJob": maintenance_backup,
        "maintenanceBackup": maintenance_backup,
        "sources": sources,
        "tables": tables,
    }
    if not any_success:
        first_error = next((item.get("error") for item in sources.values() if item.get("error")), "")
        body["error"] = first_error or "NWUI login worked, but no monitoring endpoints returned data."
        return 502, body
    if create_session:
        body["sessionId"] = create_dashboard_session(config, cookie_jar, auth_headers, maintenance_backup)
    return 200, body


def parse_email_recipients(value: Any) -> list[str]:
    raw = str(value or "").replace(";", ",")
    recipients = [address.strip() for _, address in email.utils.getaddresses([raw]) if address.strip()]
    clean = []
    for address in recipients:
        if "\r" in address or "\n" in address or "@" not in address:
            raise BadRequest("Email recipients must be valid email addresses.")
        clean.append(address)
    if not clean:
        raise BadRequest("At least one email recipient is required.")
    return clean


SNAPSHOT_METRICS: tuple[tuple[str, str], ...] = (
    ("totalJobs", "Total backup jobs"),
    ("successfulJobs", "Successful jobs"),
    ("failedJobs", "Failed jobs"),
    ("activeJobs", "Active jobs"),
    ("recoveryJobs", "Restore jobs"),
    ("cloneJobs", "Clone jobs"),
    ("totalAlerts", "Alerts"),
    ("totalClients", "Clients"),
)


def snapshot_date_key(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d")


def snapshot_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def dashboard_snapshot_record(dashboard: dict[str, Any], when: datetime | None = None) -> dict[str, Any]:
    summary = dashboard.get("summary") if isinstance(dashboard.get("summary"), dict) else {}
    target = dashboard.get("target") if isinstance(dashboard.get("target"), dict) else {}
    health = dashboard.get("serverHealth") if isinstance(dashboard.get("serverHealth"), dict) else {}
    protection = dashboard.get("serverProtectionJob") if isinstance(dashboard.get("serverProtectionJob"), dict) else {}
    generated = when or datetime.now()
    return {
        "date": snapshot_date_key(generated),
        "savedAt": generated.astimezone().isoformat(),
        "generatedAt": str(dashboard.get("generatedAt") or ""),
        "apiMode": str(target.get("apiMode") or ""),
        "backupServer": str(target.get("backupServer") or ""),
        "server": str(target.get("restApiHost") or target.get("restApiBase") or target.get("backupServer") or ""),
        "range": str(summary.get("range") or target.get("reportRange") or DEFAULT_REPORT_RANGE),
        "rangeLabel": str(summary.get("rangeLabel") or ""),
        "health": str(summary.get("health") or ""),
        "serverStatus": str(health.get("label") or health.get("status") or ""),
        "serverProtectionStatus": str(protection.get("label") or protection.get("status") or ""),
        "slaPercent": float(summary.get("slaPercent") or 0),
        "annotation": "",
        "metrics": {key: snapshot_int(summary.get(key)) for key, _ in SNAPSHOT_METRICS},
    }


def load_dashboard_snapshots() -> dict[str, Any]:
    if not DASHBOARD_SNAPSHOT_FILE.exists():
        return {}
    try:
        raw = DASHBOARD_SNAPSHOT_FILE.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_dashboard_snapshots(snapshots: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DASHBOARD_SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(snapshots, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(DASHBOARD_SNAPSHOT_FILE)
    try:
        DASHBOARD_SNAPSHOT_FILE.chmod(0o600)
    except OSError:
        pass


def save_dashboard_snapshot(dashboard: dict[str, Any], when: datetime | None = None) -> dict[str, Any]:
    if not isinstance(dashboard, dict) or not dashboard.get("ok"):
        raise BadRequest("A successful dashboard result is required before saving a snapshot.")
    record = dashboard_snapshot_record(dashboard, when)
    snapshots = load_dashboard_snapshots()
    snapshots[record["date"]] = record
    for old_date in sorted(snapshots)[:-180]:
        snapshots.pop(old_date, None)
    write_dashboard_snapshots(snapshots)
    return record


def snapshot_range_days(value: Any) -> int:
    raw = str(value or "7d").strip().lower()
    return {"7d": 7, "30d": 30, "90d": 90}.get(raw, 7)


def list_snapshot_summary() -> list[dict[str, Any]]:
    snapshots = load_dashboard_snapshots()
    return [
        {
            "date": date,
            "server": snap.get("server", ""),
            "health": snap.get("health", ""),
            "slaPercent": snap.get("slaPercent", 0),
            "savedAt": snap.get("savedAt", ""),
            "annotation": snap.get("annotation", ""),
            "metricsSnapshot": snap.get("metrics", {}),
        }
        for date, snap in sorted(snapshots.items(), reverse=True)
    ]


def delete_snapshot_by_date(date: str) -> None:
    snapshots = load_dashboard_snapshots()
    snapshots.pop(date, None)
    write_dashboard_snapshots(snapshots)


def annotate_snapshot(date: str, note: str) -> None:
    snapshots = load_dashboard_snapshots()
    if date in snapshots:
        snapshots[date]["annotation"] = str(note)[:500]
        write_dashboard_snapshots(snapshots)


def snapshot_history_all() -> dict[str, Any]:
    snapshots = load_dashboard_snapshots()
    dates = sorted(snapshots.keys())
    history: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in SNAPSHOT_METRICS}
    sla_history: list[dict[str, Any]] = []
    for date in dates:
        snap = snapshots[date]
        metrics = snap.get("metrics", {})
        for key, _ in SNAPSHOT_METRICS:
            history[key].append({"date": date, "value": snapshot_int(metrics.get(key))})
        sla_history.append({"date": date, "value": float(snap.get("slaPercent") or 0)})
    return {"ok": True, "dates": dates, "history": history, "slaHistory": sla_history}


def snapshots_to_csv() -> str:
    import io as _io
    snapshots = load_dashboard_snapshots()
    metric_keys = [k for k, _ in SNAPSHOT_METRICS]
    buf = _io.StringIO()
    headers = ["date", "server", "slaPercent", "health", "annotation"] + metric_keys
    buf.write(",".join(headers) + "\n")

    def _esc(v: str) -> str:
        if "," in v or '"' in v or "\n" in v:
            return '"' + v.replace('"', '""') + '"'
        return v

    for date in sorted(snapshots.keys(), reverse=True):
        snap = snapshots[date]
        m = snap.get("metrics", {})
        row = [
            date,
            snap.get("server", ""),
            str(snap.get("slaPercent", "")),
            snap.get("health", ""),
            snap.get("annotation", ""),
        ] + [str(snapshot_int(m.get(k))) for k in metric_keys]
        buf.write(",".join(_esc(v) for v in row) + "\n")
    return buf.getvalue()


def load_auto_snapshot_config() -> bool:
    try:
        raw = AUTO_SNAPSHOT_FILE.read_text(encoding="utf-8").strip()
        return bool(json.loads(raw).get("enabled", False))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def save_auto_snapshot_config(enabled: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTO_SNAPSHOT_FILE.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")


# Current dashboard theme, persisted server-side so background-scheduled report
# emails use whatever theme the dashboard is currently set to (dynamic), rather
# than the theme frozen at schedule time.
UI_THEME_LOCK = threading.Lock()


def load_ui_theme() -> str:
    try:
        raw = UI_PREFS_FILE.read_text(encoding="utf-8").strip()
        return parse_theme(json.loads(raw).get("theme"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def save_ui_theme(theme: str) -> str:
    resolved = parse_theme(theme)
    with UI_THEME_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UI_PREFS_FILE.write_text(json.dumps({"theme": resolved}), encoding="utf-8")
    return resolved


def _auto_snapshot_once() -> str:
    """Save today's snapshot if auto-save is on and not already captured.
    Returns a status code for observability: disabled / exists / no-dashboard /
    saved."""
    if not load_auto_snapshot_config():
        return "disabled"
    today = snapshot_date_key()
    with SNAPSHOTS_LOCK:
        existing = load_dashboard_snapshots()
    if today in existing:
        debug_log(f"auto_snapshot: snapshot for {today} already exists; skipping")
        return "exists"
    with SHARED_DASHBOARD_LOCK:
        dashboard = dict(SHARED_DASHBOARD_STATE.get("dashboard") or {})
    if not isinstance(dashboard, dict) or not dashboard.get("ok"):
        debug_log("auto_snapshot: no shared dashboard available yet; will retry")
        return "no-dashboard"
    with SNAPSHOTS_LOCK:
        save_dashboard_snapshot(dashboard)
    debug_log(f"auto_snapshot: saved snapshot for {today}")
    return "saved"


def auto_snapshot_worker() -> None:
    while not SHARED_REFRESH_STOP.is_set():
        SHARED_REFRESH_STOP.wait(600)
        if SHARED_REFRESH_STOP.is_set():
            break
        try:
            _auto_snapshot_once()
        except Exception as exc:  # noqa: BLE001 — loop must never die.
            debug_log(f"auto_snapshot_worker iteration failed: {exc}")


def compare_dashboard_snapshots(range_value: Any = "7d") -> dict[str, Any]:
    days = snapshot_range_days(range_value)
    snapshots = load_dashboard_snapshots()
    dates = sorted(snapshots)
    if not dates:
        return {"ok": False, "message": "No local snapshots found.", "metrics": []}
    current_date = dates[-1]
    current = snapshots[current_date]
    current_dt = datetime.strptime(current_date, "%Y-%m-%d")
    target_dt = current_dt - timedelta(days=days)
    window_start = (current_dt - timedelta(days=max(days + 1, int(days * 1.5)))).strftime("%Y-%m-%d")
    candidates = [date for date in dates if window_start <= date < current_date]
    if not candidates:
        return {
            "ok": False,
            "currentDate": current_date,
            "message": f"No previous snapshot found for the last {days} days.",
            "metrics": [],
        }
    previous_date = min(candidates, key=lambda date: abs((datetime.strptime(date, "%Y-%m-%d") - target_dt).days))
    previous = snapshots[previous_date]
    current_metrics = current.get("metrics") if isinstance(current.get("metrics"), dict) else {}
    previous_metrics = previous.get("metrics") if isinstance(previous.get("metrics"), dict) else {}
    rows = []
    for key, label in SNAPSHOT_METRICS:
        current_value = snapshot_int(current_metrics.get(key))
        previous_value = snapshot_int(previous_metrics.get(key))
        delta = current_value - previous_value
        delta_percent = round((delta / previous_value) * 100, 2) if previous_value else None
        rows.append(
            {
                "key": key,
                "label": label,
                "previous": previous_value,
                "current": current_value,
                "delta": delta,
                "deltaPercent": delta_percent,
                "previousDate": previous_date,
                "currentDate": current_date,
            }
        )
    return {
        "ok": True,
        "range": f"{days}d",
        "targetDays": days,
        "previousDate": previous_date,
        "currentDate": current_date,
        "message": f"Comparing {previous_date} to {current_date}.",
        "metrics": rows,
    }


def snapshot_summary_text() -> str:
    snapshots = load_dashboard_snapshots()
    if not snapshots:
        return "No local snapshots saved"
    dates = sorted(snapshots)
    return f"{len(dates)} local snapshot(s), latest {dates[-1]}"


def parse_report_time(value: Any) -> str:
    raw = str(value or "08:00").strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    if not match:
        raise BadRequest("Daily report time must use HH:MM in 24-hour format.")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def parse_theme(value: Any) -> str:
    theme = str(value or "default").strip().lower()
    return theme if theme in THEME_PALETTES else "default"


def report_theme_palette(value: Any) -> dict[str, str]:
    return THEME_PALETTES.get(parse_theme(value), THEME_PALETTES["default"])


def parse_smtp_settings(payload: dict[str, Any]) -> dict[str, Any]:
    host = str(payload.get("smtpHost") or "").strip()
    if not host:
        raise BadRequest("SMTP host is required.")
    if not HOST_PATTERN.match(host):
        raise BadRequest("SMTP host contains unsupported characters.")
    port = parse_port(payload.get("smtpPort"), 587, "SMTP port")
    security = str(payload.get("smtpSecurity") or "starttls").strip().lower()
    if security not in ("starttls", "ssl", "none"):
        raise BadRequest("SMTP security must be starttls, ssl, or none.")
    mail_from = str(payload.get("smtpFrom") or "").strip()
    if "\r" in mail_from or "\n" in mail_from or "@" not in mail_from:
        raise BadRequest("From address must be a valid email address.")
    interval = parse_port(payload.get("intervalMinutes"), 60, "Schedule minutes")
    interval = max(ALERT_AUTOMATION_MIN_INTERVAL_MINUTES, min(ALERT_AUTOMATION_MAX_INTERVAL_MINUTES, interval))
    trigger = str(payload.get("trigger") or "critical").strip().lower()
    if trigger not in ("critical", "warning", "all"):
        raise BadRequest("Alert trigger must be critical, warning, or all.")
    schedule_type = str(payload.get("scheduleType") or "alert").strip().lower()
    if schedule_type not in ("alert", "daily_report"):
        raise BadRequest("Email type must be alert or daily_report.")
    return {
        "smtp_host": host,
        "smtp_port": port,
        "smtp_security": security,
        "smtp_username": str(payload.get("smtpUsername") or "").strip(),
        "smtp_password": str(payload.get("smtpPassword") or ""),
        "smtp_from": mail_from,
        "recipients": parse_email_recipients(payload.get("smtpTo")),
        "interval_minutes": interval,
        "trigger": trigger,
        "schedule_type": schedule_type,
        "report_time": parse_report_time(payload.get("reportTime")),
        "theme": parse_theme(payload.get("theme")),
    }


# Persisted email-notification configuration. The SMTP transport is shared, but
# the "alert" and "daily_report" notification types keep SEPARATE recipient
# lists and per-type settings so configuring one never overwrites the other.
EMAIL_CONFIG_LOCK = threading.Lock()


def load_email_config() -> dict[str, Any]:
    try:
        raw = json.loads(EMAIL_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_email_config(cfg: dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = EMAIL_CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, separators=(",", ":")), encoding="utf-8")
        tmp.replace(EMAIL_CONFIG_FILE)
    except OSError:
        pass


def email_config_public() -> dict[str, Any]:
    """UI-facing config. Never returns the SMTP password — only whether one is
    saved. Recipients are returned per-type as a '; '-joined string."""
    cfg = load_email_config()
    smtp = cfg.get("smtp") if isinstance(cfg.get("smtp"), dict) else {}
    types = cfg.get("types") if isinstance(cfg.get("types"), dict) else {}
    alert = types.get("alert") if isinstance(types.get("alert"), dict) else {}
    daily = types.get("daily_report") if isinstance(types.get("daily_report"), dict) else {}
    return {
        "ok": True,
        "smtp": {
            "host": str(smtp.get("host") or ""),
            "port": int(smtp.get("port") or 587),
            "security": str(smtp.get("security") or "starttls"),
            "username": str(smtp.get("username") or ""),
            "from": str(smtp.get("from") or ""),
            "passwordSaved": bool(smtp.get("encrypted_password")),
        },
        "alert": {
            "recipients": "; ".join(alert.get("recipients") or []),
            "trigger": str(alert.get("trigger") or "critical"),
            "intervalMinutes": int(alert.get("interval_minutes") or 60),
        },
        "dailyReport": {
            "recipients": "; ".join(daily.get("recipients") or []),
            "reportTime": str(daily.get("report_time") or "08:00"),
            "theme": str(daily.get("theme") or "default"),
        },
    }


def saved_email_smtp_password() -> str:
    smtp = load_email_config().get("smtp")
    if isinstance(smtp, dict) and smtp.get("encrypted_password"):
        return decrypt_process_secret(str(smtp.get("encrypted_password")))
    return ""


def save_email_config_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the shared SMTP transport plus the selected notification type's
    recipients/settings, preserving the OTHER type's saved recipients."""
    settings = parse_smtp_settings(payload)
    schedule_type = settings["schedule_type"]
    with EMAIL_CONFIG_LOCK:
        cfg = load_email_config()
        prev_smtp = cfg.get("smtp") if isinstance(cfg.get("smtp"), dict) else {}
        # Keep the previously saved password if the form left it blank.
        encrypted = str(prev_smtp.get("encrypted_password") or "")
        if settings["smtp_password"]:
            encrypted = encrypt_process_secret(settings["smtp_password"])
        smtp = {
            "host": settings["smtp_host"],
            "port": settings["smtp_port"],
            "security": settings["smtp_security"],
            "username": settings["smtp_username"],
            "from": settings["smtp_from"],
            "encrypted_password": encrypted,
        }
        types = cfg.get("types") if isinstance(cfg.get("types"), dict) else {}
        if not isinstance(types, dict):
            types = {}
        if schedule_type == "daily_report":
            types["daily_report"] = {
                "recipients": settings["recipients"],
                "report_time": settings["report_time"],
                "theme": settings["theme"],
            }
        else:
            types["alert"] = {
                "recipients": settings["recipients"],
                "trigger": settings["trigger"],
                "interval_minutes": settings["interval_minutes"],
            }
        new_cfg = {"smtp": smtp, "types": types}
        write_email_config(new_cfg)
    return email_config_public()


def dashboard_alert_lines(dashboard: dict[str, Any]) -> tuple[str, list[str]]:
    summary = dashboard.get("summary") or {}
    protection = dashboard.get("serverProtectionJob") or {}
    lines: list[str] = []
    failed = int(summary.get("failedJobs") or 0)
    critical = int(summary.get("criticalAlerts") or 0)
    warnings = int(summary.get("warningAlerts") or 0)
    active = int(summary.get("activeJobs") or 0)
    protection_status = str(protection.get("status") or "unknown").lower()
    if failed:
        lines.append(f"Failed backup jobs: {failed}")
    if critical:
        lines.append(f"Critical alerts: {critical}")
    if warnings:
        lines.append(f"Warning alerts: {warnings}")
    if protection_status and protection_status not in ("succeeded", "success", "completed", "ok"):
        lines.append(f"Server Protection Job: {protection.get('label') or protection_status} - {protection.get('detail') or ''}")
    lines.append(f"Active jobs: {active}")
    lines.append(f"SLA: {summary.get('slaPercent', 0)}% ({summary.get('slaMetJobs', 0)} met / {summary.get('slaTotalJobs', 0)} total)")
    lines.append(f"Generated: {dashboard.get('generatedAt') or generated_at()}")
    severity = "critical" if failed or critical or protection_status in ("failed", "critical") else ("warning" if warnings or protection_status in ("running", "queued", "warning", "unknown") else "ok")
    return severity, lines


def dashboard_backup_source_item(dashboard: dict[str, Any]) -> dict[str, Any] | None:
    sources = dashboard.get("sources") if isinstance(dashboard.get("sources"), dict) else {}
    for name in ("monitoringActions", "jobs"):
        item = sources.get(name)
        if isinstance(item, dict):
            return item
    return None


def dashboard_backup_source_available(dashboard: dict[str, Any]) -> bool:
    if not isinstance(dashboard, dict) or not dashboard.get("ok", True):
        return False
    item = dashboard_backup_source_item(dashboard)
    if item is None:
        return True
    return bool(item.get("ok"))


def dashboard_backup_source_error(dashboard: dict[str, Any]) -> str:
    item = dashboard_backup_source_item(dashboard)
    if not item:
        return "Backup job source status is unavailable."
    detail = item.get("error") or item.get("detail") or item.get("message") or ""
    status = item.get("status")
    path = item.get("path") or "backup job source"
    if status:
        return f"{path} failed with HTTP {status}: {safe_log_text(detail, 500)}"
    return f"{path} failed: {safe_log_text(detail, 500)}"


def shared_reliable_dashboard_for_session(session_id: str) -> dict[str, Any] | None:
    with SHARED_DASHBOARD_LOCK:
        shared_session_id = str(SHARED_DASHBOARD_STATE.get("sessionId") or "")
        dashboard = SHARED_DASHBOARD_STATE.get("dashboard")
        if shared_session_id != session_id or not isinstance(dashboard, dict):
            return None
        candidate = json_clone(dashboard)
    if dashboard_backup_source_available(candidate):
        return candidate
    return None


def dashboard_report_rows(dashboard: dict[str, Any]) -> list[tuple[str, str]]:
    summary = dashboard.get("summary") or {}
    protection = dashboard.get("serverProtectionJob") or {}
    health = dashboard.get("serverHealth") or {}
    rows = [
        ("Report range", str(summary.get("rangeLabel") or summary.get("range") or "--")),
        ("Total backup jobs", str(summary.get("totalJobs", 0))),
        ("Successful jobs", str(summary.get("successfulJobs", 0))),
        ("Failed jobs", str(summary.get("failedJobs", 0))),
        ("Running/queued jobs", str(summary.get("activeJobs", 0))),
        ("Recovery jobs", str(summary.get("recoveryJobs", 0))),
        ("Clone jobs", str(summary.get("cloneJobs", 0))),
        ("Alerts", str(summary.get("totalAlerts", 0))),
        ("Backup SLA", f"{summary.get('slaPercent', 0)}% ({summary.get('slaMetJobs', 0)} met / {summary.get('slaTotalJobs', 0)} total)"),
        ("SLA not met", str(summary.get("slaMissedJobs", 0))),
        ("Server status", str(health.get("label") or "--")),
        ("CPU usage", "--" if health.get("cpuUsagePercent") is None else f"{health.get('cpuUsagePercent')}%"),
        ("Memory usage", report_memory_value(health)),
        ("Server Protection Job", f"{protection.get('label') or 'Not found'} - {protection.get('detail') or ''}".strip()),
        ("Generated", str(dashboard.get("generatedAt") or generated_at())),
    ]
    notice = str(dashboard.get("reportNotice") or "").strip()
    if notice:
        rows.insert(1, ("Report notice", safe_log_text(notice, 420)))
    return rows


def report_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def report_percent(part: int, total: int) -> int:
    return round((part / total) * 100) if total else 0


def report_bar(label: str, value: int, max_value: int, color: str, palette: dict[str, str] | None = None) -> str:
    palette = palette or THEME_PALETTES["default"]
    width = max(2, min(100, round((value / max(1, max_value)) * 100)))
    return (
        '<tr>'
        f'<td style="padding:7px 10px 7px 0;width:92px;font-size:12px;color:{palette["ink"]};">{html_lib.escape(label)}</td>'
        '<td style="padding:7px 8px;width:150px;">'
        f'<div style="height:9px;background:{palette["surface2"]};border:1px solid {palette["line"]};border-radius:6px;overflow:hidden;">'
        f'<div style="height:9px;width:{width}%;background:{color};border-radius:6px;"></div>'
        '</div>'
        '</td>'
        f'<td style="padding:7px 0 7px 6px;width:42px;text-align:right;font-size:12px;font-weight:700;color:{palette["ink"]};">{value:,}</td>'
        '</tr>'
    )


def report_metric_card(label: str, value: int, color: str, palette: dict[str, str] | None = None) -> str:
    palette = palette or THEME_PALETTES["default"]
    return (
        '<td style="padding:0 8px 8px 0;width:16.66%;">'
        f'<div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px 14px 12px;">'
        f'<div style="font-size:12px;color:{palette["ink"]};font-weight:700;margin-bottom:20px;">{html_lib.escape(label)}</div>'
        f'<div style="font-size:28px;line-height:1;font-weight:800;color:{color};">{value:,}</div>'
        '</div>'
        '</td>'
    )


def report_donut_card(
    title: str,
    center_value: str,
    center_label: str,
    legend: list[tuple[str, int, str]],
    meta: str,
    width: str = "16.66%",
    min_height: str = "252px",
    donut_size: int = 138,
    inner_size: int = 82,
    palette: dict[str, str] | None = None,
) -> str:
    palette = palette or THEME_PALETTES["default"]
    total = sum(max(0, value) for _, value, _ in legend)
    cursor = 0.0
    segments = []
    for _, value, color in legend:
        if not value or total <= 0:
            continue
        end = cursor + ((value / total) * 360)
        segments.append(f"{color} {cursor:.2f}deg {end:.2f}deg")
        cursor = end
    gradient = ", ".join(segments) if segments else f"{palette['line']} 0deg 360deg"
    legend_rows = "".join(
        '<tr>'
        f'<td style="padding:4px 6px 4px 0;"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};"></span></td>'
        f'<td style="padding:4px 8px 4px 0;font-size:12px;font-weight:700;color:{palette["ink"]};">{html_lib.escape(label)}</td>'
        f'<td style="padding:4px 0;text-align:right;font-size:12px;font-weight:800;color:{palette["ink"]};">{value:,} ({report_percent(value, total)}%)</td>'
        '</tr>'
        for label, value, color in legend
    )
    return f"""
      <td style="padding:0 8px 12px 0;width:{width};min-width:280px;vertical-align:top;">
        <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:{min_height};">
          <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:10px;">
            <tr>
              <td style="font-size:14px;font-weight:800;color:{palette["ink"]};">{html_lib.escape(title)}</td>
              <td style="font-size:12px;color:{palette["muted"]};text-align:right;">{html_lib.escape(meta)}</td>
            </tr>
          </table>
          <table role="presentation" style="width:100%;border-collapse:collapse;">
            <tr>
              <td style="width:{donut_size}px;vertical-align:middle;">
                <div style="width:{donut_size}px;height:{donut_size}px;border-radius:50%;background:conic-gradient({gradient});display:table;text-align:center;">
                  <div style="display:table-cell;vertical-align:middle;">
                    <div style="width:{inner_size}px;height:{inner_size}px;margin:0 auto;border-radius:50%;background:{palette["surface"]};border:1px solid {palette["line"]};display:table;">
                      <div style="display:table-cell;vertical-align:middle;text-align:center;">
                        <div style="font-size:26px;font-weight:850;color:{palette["ink"]};line-height:1;">{html_lib.escape(center_value)}</div>
                        <div style="font-size:10px;text-transform:uppercase;color:{palette["muted"]};font-weight:800;margin-top:5px;">{html_lib.escape(center_label)}</div>
                      </div>
                    </div>
                  </div>
                </div>
              </td>
              <td style="vertical-align:middle;padding-left:14px;">
                <table role="presentation" style="width:100%;border-collapse:collapse;">{legend_rows}</table>
              </td>
            </tr>
          </table>
        </div>
      </td>
    """


def report_decimal(value: Any, digits: int = 1) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "--"
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.{digits}f}"


def report_connection_label(summary: dict[str, Any]) -> tuple[str, str]:
    health = str(summary.get("health") or "").lower()
    if health == "critical":
        return "Connected - action required", "#f36c7d"
    if health == "warning":
        return "Connected with warnings", "#e0a11b"
    return "Connection established", "#4fd17b"


def report_memory_value(health: dict[str, Any]) -> str:
    total = health.get("ramTotalGb")
    used = health.get("ramUsedGb")
    free = health.get("ramFreeGb")
    try:
        total_float = float(total)
    except (TypeError, ValueError):
        total_float = 0.0
    if total_float > 0:
        try:
            used_float = float(used)
        except (TypeError, ValueError):
            try:
                used_float = max(0.0, total_float - float(free))
            except (TypeError, ValueError):
                used_float = 0.0
        return f"{report_decimal(used_float)} / {report_decimal(total_float)} GB"
    ram = health.get("ramUsagePercent")
    return "--" if ram is None else f"{report_int(ram)}%"


def report_memory_detail(health: dict[str, Any]) -> str:
    free = health.get("ramFreeGb")
    percent = health.get("ramUsagePercent")
    try:
        free_float = float(free)
    except (TypeError, ValueError):
        free_float = -1.0
    if free_float >= 0 and percent is not None:
        return f"{report_decimal(free_float)} GB free - {report_int(percent)}% used"
    return str(health.get("ramDetail") or health.get("source") or "No memory metric returned.")


def report_health_card(
    label: str,
    value: str,
    detail: str,
    color: str,
    meter_percent: Any = None,
    palette: dict[str, str] | None = None,
) -> str:
    palette = palette or THEME_PALETTES["default"]
    meter = ""
    if meter_percent is not None:
        width = max(0, min(100, report_int(meter_percent)))
        meter = (
            f'<div style="height:7px;background:{palette["surface2"]};border:1px solid {palette["line"]};'
            'border-radius:6px;overflow:hidden;margin:8px 0 6px;">'
            f'<div style="height:7px;width:{width}%;background:{color};border-radius:6px;"></div>'
            '</div>'
        )
    return (
        '<td style="padding:0 10px 10px 0;width:25%;min-width:260px;vertical-align:top;">'
        f'<div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:92px;">'
        f'<div style="font-size:12px;color:{palette["ink"]};font-weight:700;margin-bottom:12px;">{html_lib.escape(label)}</div>'
        f'<div style="font-size:18px;line-height:1.1;font-weight:850;color:{color};">{html_lib.escape(value)}</div>'
        f'{meter}'
        f'<div style="font-size:11px;line-height:1.35;color:{palette["muted"]};margin-top:10px;">{html_lib.escape(detail)}</div>'
        '</div>'
        '</td>'
    )


def report_color_for_server_status(status: Any, palette: dict[str, str]) -> str:
    return palette["green"] if str(status or "").lower() == "ok" else palette["red"]


def report_color_for_protection_status(status: Any, palette: dict[str, str]) -> str:
    normalized = str(status or "").lower()
    if normalized == "failed":
        return palette["red"]
    if normalized in ("running", "queued", "warning"):
        return palette["amber"]
    return palette["green"]


def report_status_model(dashboard: dict[str, Any]) -> dict[str, Any]:
    summary = dashboard.get("summary") or {}
    target = dashboard.get("target") or {}
    health = dashboard.get("serverHealth") or {}
    protection = dashboard.get("serverProtectionJob") or dashboard.get("maintenanceBackup") or {}
    palette = report_theme_palette(dashboard.get("theme") or target.get("theme"))
    successful = report_int(summary.get("successfulJobs"))
    failed = report_int(summary.get("failedJobs"))
    active = report_int(summary.get("activeJobs"))
    recovery = report_int(summary.get("recoveryJobs"))
    clones = report_int(summary.get("cloneJobs"))
    alerts = report_int(summary.get("totalAlerts"))
    clients = report_int(summary.get("totalClients"))
    sla_total = report_int(summary.get("slaTotalJobs", summary.get("totalJobs")))
    sla_met = report_int(summary.get("slaMetJobs"))
    sla_missed = report_int(summary.get("slaMissedJobs"))
    range_label = str(summary.get("rangeLabel") or summary.get("range") or "Selected range")
    generated = str(dashboard.get("generatedAt") or generated_at())
    connection_label, connection_color = report_connection_label(summary)
    return {
        "summary": summary,
        "target": target,
        "health": health,
        "protection": protection,
        "palette": palette,
        "brand_background": palette["brand"],
        "brand_ink": palette["brandInk"],
        "successful": successful,
        "failed": failed,
        "active": active,
        "recovery": recovery,
        "clones": clones,
        "alerts": alerts,
        "clients": clients,
        "sla_total": sla_total,
        "sla_met": sla_met,
        "sla_missed": sla_missed,
        "sla_percent": report_decimal(summary.get("slaPercent"), 2),
        "range_label": range_label,
        "generated": generated,
        "backup_server": str(target.get("backupServer") or "--"),
        "api_mode": str(target.get("apiMode") or "--").upper(),
        "connection_label": connection_label,
        "connection_color": connection_color,
        "server_status": str(health.get("label") or "Unavailable"),
        "server_status_color": report_color_for_server_status(health.get("status"), palette),
        "cpu_value": "--" if health.get("cpuUsagePercent") is None else f"{report_int(health.get('cpuUsagePercent'))}%",
        "cpu_detail": str(health.get("cpuDetail") or health.get("source") or "No CPU metric returned."),
        "ram_value": report_memory_value(health),
        "ram_detail": report_memory_detail(health),
        "ram_percent": health.get("ramUsagePercent"),
        "protection_color": report_color_for_protection_status(protection.get("status"), palette),
        "protection_label": str(protection.get("label") or "Not found"),
        "protection_detail": str(protection.get("detail") or "No Server Protection job found in this range."),
    }


def snapshot_donut(title: str, value: str, label: str, rows: list[tuple[str, int, str]], meta: str) -> str:
    total = sum(max(0, row_value) for _, row_value, _ in rows)
    cursor = 0.0
    segments = []
    for _, row_value, color in rows:
        if not row_value or total <= 0:
            continue
        end = cursor + ((row_value / total) * 360)
        segments.append(f"{color} {cursor:.2f}deg {end:.2f}deg")
        cursor = end
    gradient = ", ".join(segments) if segments else "#d7e1e7 0deg 360deg"
    legend = "".join(
        f'<div class="legend-item"><i style="background:{color}"></i><span>{html_lib.escape(name)}</span><b>{row_value:,} ({report_percent(row_value, total)}%)</b></div>'
        for name, row_value, color in rows
    )
    return (
        '<article class="card">'
        f'<header><h2>{html_lib.escape(title)}</h2><span>{html_lib.escape(meta)}</span></header>'
        '<div class="donut-layout">'
        f'<div class="donut" style="background:conic-gradient({gradient});"><div class="donut-hole"><strong>{html_lib.escape(value)}</strong><small>{html_lib.escape(label)}</small></div></div>'
        f'<div class="legend-list">{legend}</div>'
        '</div>'
        '</article>'
    )


def snapshot_bar(label: str, value: int, max_value: int, color: str) -> str:
    width = max(2, min(100, round((value / max(1, max_value)) * 100)))
    return (
        '<div class="bar-row">'
        f'<span>{html_lib.escape(label)}</span>'
        f'<div class="bar-track"><i style="width:{width}%;background:{color};"></i></div>'
        f'<strong>{value:,}</strong>'
        '</div>'
    )


def snapshot_metric(label: str, value: int, color: str) -> str:
    return (
        '<article class="metric">'
        f'<span>{html_lib.escape(label)}</span>'
        f'<strong style="color:{color};">{value:,}</strong>'
        '</article>'
    )


def snapshot_health_card(label: str, value: str, detail: str, color: str, percent: Any = None) -> str:
    meter = ""
    if percent is not None:
        width = max(0, min(100, report_int(percent)))
        meter = f'<div class="health-meter"><i style="width:{width}%;background:{color};"></i></div>'
    return (
        '<article class="health-card">'
        f'<span>{html_lib.escape(label)}</span>'
        f'<strong style="color:{color};">{html_lib.escape(value)}</strong>'
        f'{meter}'
        f'<small>{html_lib.escape(detail)}</small>'
        '</article>'
    )


def dashboard_snapshot_html(dashboard: dict[str, Any]) -> str:
    model = report_status_model(dashboard)
    palette = model["palette"]
    # Match the live dashboard brand card exactly (fixed navy->teal gradient).
    # Chrome renders this PNG, so the CSS gradient works here.
    model["brand_background"] = BRAND_CARD_GRADIENT
    model["brand_ink"] = BRAND_CARD_INK
    successful = model["successful"]
    failed = model["failed"]
    active = model["active"]
    recovery = model["recovery"]
    clones = model["clones"]
    alerts = model["alerts"]
    clients = model["clients"]
    summary = model["summary"]
    top_activity = successful + failed + active + recovery
    overview = [
        ("Clients", clients, palette["blue"]),
        ("Successful", successful, palette["green"]),
        ("Failed", failed, palette["red"]),
        ("Running", active, palette["blue"]),
        ("Restores", recovery, palette["amber"]),
        ("Clones", clones, palette["brand"]),
        ("Alerts", alerts, palette["muted"]),
    ]
    max_overview = max([value for _, value, _ in overview] + [1])
    overview_rows = "".join(snapshot_bar(label, value, max_overview, color) for label, value, color in overview)
    activity = snapshot_donut(
        "Activity Mix",
        f"{top_activity:,}",
        "Activity",
        [
            ("Successful", successful, palette["green"]),
            ("Failed", failed, palette["red"]),
            ("Running", active, palette["blue"]),
            ("Restores", recovery, palette["amber"]),
        ],
        model["range_label"],
    )
    sla = snapshot_donut(
        "Backup SLA",
        f'{model["sla_percent"]}%',
        "SLA",
        [
            ("SLA met", model["sla_met"], palette["green"]),
            ("Not met", model["sla_missed"], palette["red"]),
        ],
        f'{model["sla_total"]:,} jobs',
    )
    metrics = "".join(
        [
            snapshot_metric("Clients", clients, palette["blue"]),
            snapshot_metric("Successful Jobs", successful, palette["green"]),
            snapshot_metric("Failed Jobs", failed, palette["red"]),
            snapshot_metric("Active Jobs", active, palette["blue"]),
            snapshot_metric("Recovery Jobs", recovery, palette["amber"]),
            snapshot_metric("Alerts", alerts, palette["amber"]),
        ]
    )
    return f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    :root {{
      --bg:{palette["bg"]}; --surface:{palette["surface"]}; --surface2:{palette["surface2"]};
      --ink:{palette["ink"]}; --muted:{palette["muted"]}; --line:{palette["line"]};
      --green:{palette["green"]}; --red:{palette["red"]}; --amber:{palette["amber"]}; --blue:{palette["blue"]};
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font-family:"Segoe UI",Arial,sans-serif; }}
    .snapshot {{ width:1880px; padding:6px; }}
    .top-grid {{ display:grid; grid-template-columns:repeat(6, 1fr); gap:12px; }}
    .card, .metric, .health-section {{ background:var(--surface); border:1px solid var(--line); border-radius:8px; box-shadow:0 1px 2px rgba(15,23,42,.08); }}
    .card {{ min-height:338px; padding:16px; display:flex; flex-direction:column; gap:14px; }}
    .brand-card {{ background:{model["brand_background"]}; background-color:{model["brand_background"]}; color:{model["brand_ink"]}; border-color:rgba(255,255,255,.18); }}
    .brand-top {{ display:flex; align-items:center; gap:14px; }}
    .logo {{ width:68px; height:68px; border-radius:7px; background:#f3fbff; padding:4px; object-fit:contain; }}
    h2 {{ margin:0; font-size:14px; line-height:1.25; font-weight:800; }}
    header {{ display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }}
    header span {{ color:var(--muted); font-size:12px; font-weight:700; white-space:nowrap; }}
    .brand-title strong {{ display:block; font-size:18px; line-height:1.15; font-weight:850; }}
    .brand-title span {{ display:block; margin-top:4px; font-size:12px; font-weight:720; color:rgba(255,255,255,.82); }}
    .connection-line {{ min-height:38px; display:flex; align-items:center; gap:8px; padding:9px 11px; margin-top:12px; border-radius:8px; background:rgba(255,255,255,.14); font-size:13px; font-weight:800; }}
    .dot {{ width:12px; height:12px; border-radius:50%; background:{model["connection_color"]}; box-shadow:0 0 0 4px rgba(255,255,255,.14); }}
    .brand-details {{ display:grid; gap:9px; margin-top:auto; }}
    .brand-detail {{ display:flex; justify-content:space-between; gap:12px; font-size:12px; min-height:26px; }}
    .brand-detail span {{ color:rgba(255,255,255,.7); font-weight:700; }}
    .brand-detail strong {{ font-weight:820; text-align:right; }}
    .signature {{ border-top:1px solid rgba(255,255,255,.2); padding-top:11px; display:grid; gap:2px; font-size:11px; line-height:1.25; color:rgba(255,255,255,.85); font-weight:650; }}
    .signature strong {{ color:#fff; font-size:12px; font-weight:850; }}
    .donut-layout {{ display:grid; grid-template-columns:172px minmax(0,1fr); align-items:center; gap:14px; margin-top:auto; }}
    .donut {{ width:172px; height:172px; border-radius:50%; display:grid; place-items:center; box-shadow:inset 0 0 0 1px var(--line); }}
    .donut-hole {{ width:112px; height:112px; border-radius:50%; background:var(--surface); border:1px solid var(--line); display:grid; place-items:center; align-content:center; text-align:center; }}
    .donut-hole strong {{ display:block; font-size:28px; line-height:1; font-weight:850; }}
    .donut-hole small {{ display:block; margin-top:5px; color:var(--muted); font-size:11px; font-weight:800; text-transform:uppercase; }}
    .legend-list {{ display:grid; gap:9px; min-width:0; }}
    .legend-item {{ display:grid; grid-template-columns:12px minmax(0,1fr) auto; align-items:center; gap:8px; font-size:12px; font-weight:730; }}
    .legend-item i {{ width:10px; height:10px; border-radius:50%; }}
    .legend-item span {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
    .legend-item b {{ font-size:12px; font-weight:850; }}
    .bar-chart {{ display:grid; gap:12px; margin-top:auto; }}
    .bar-row {{ display:grid; grid-template-columns:92px minmax(120px,1fr) 54px; gap:10px; align-items:center; min-height:26px; font-size:12px; font-weight:720; }}
    .bar-row > span {{ color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
    .bar-track {{ height:12px; border-radius:999px; background:var(--surface2); border:1px solid var(--line); overflow:hidden; }}
    .bar-track i {{ display:block; height:100%; border-radius:inherit; }}
    .bar-row strong {{ text-align:right; font-weight:850; }}
    .summary-band {{ padding:12px; border:1px solid var(--line); border-radius:8px; background:var(--surface2); margin-top:auto; }}
    .summary-band strong {{ display:block; font-size:24px; line-height:1; font-weight:850; }}
    .summary-band span {{ display:block; margin-top:8px; color:var(--muted); font-size:12px; font-weight:720; }}
    .summary-row {{ display:flex; justify-content:space-between; gap:12px; padding:7px 0; font-size:12px; font-weight:730; }}
    .summary-row span {{ color:var(--muted); }}
    .metric-grid {{ display:grid; grid-template-columns:repeat(6, 1fr); gap:12px; margin-top:16px; }}
    .metric {{ min-height:92px; padding:14px; display:grid; align-content:space-between; gap:10px; }}
    .metric span {{ color:var(--muted); font-size:12px; font-weight:720; }}
    .metric strong {{ font-size:30px; line-height:1; font-weight:820; }}
    .health-section {{ margin-top:16px; overflow:hidden; }}
    .health-head {{ display:flex; justify-content:space-between; gap:12px; align-items:center; min-height:52px; padding:14px 16px; background:var(--surface2); border-bottom:1px solid var(--line); }}
    .health-head strong {{ font-size:14px; font-weight:850; }}
    .health-head span {{ color:var(--muted); font-size:12px; }}
    .health-grid {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; padding:14px 16px 16px; }}
    .health-card {{ min-height:104px; padding:12px; border:1px solid var(--line); border-radius:8px; background:var(--surface); display:grid; gap:8px; align-content:space-between; }}
    .health-card span {{ color:var(--muted); font-size:12px; font-weight:760; }}
    .health-card strong {{ font-size:20px; line-height:1.1; font-weight:850; }}
    .health-card small {{ color:var(--muted); font-size:11px; line-height:1.35; overflow-wrap:anywhere; }}
    .health-meter {{ height:9px; border-radius:999px; background:var(--surface2); border:1px solid var(--line); overflow:hidden; }}
    .health-meter i {{ display:block; height:100%; border-radius:inherit; }}
  </style>
</head>
<body>
  <main class="snapshot">
    <section class="top-grid">
      <article class="card brand-card">
        <div class="brand-top">
          <img class="logo" alt="NetWorker" width="68" height="68" src="{networker_logo_src()}">
          <div class="brand-title"><strong>DELL EMC NetWorker</strong><span>Backup &amp; Recovery Status</span></div>
        </div>
        <div class="connection-line"><i class="dot"></i>{html_lib.escape(model["connection_label"])}</div>
        <div class="brand-details">
          <div class="brand-detail"><span>API source</span><strong>{html_lib.escape(model["api_mode"])}</strong></div>
          <div class="brand-detail"><span>Backup server</span><strong>{html_lib.escape(model["backup_server"])}</strong></div>
          <div class="brand-detail"><span>Updated</span><strong>{html_lib.escape(model["generated"])}</strong></div>
        </div>
        <div class="signature"><span>Maintained &amp; developed by</span><strong>SHAIKH SHOAIB</strong><span>Sr. Advisor Delivery Specialist</span><span>DELL Technologies</span></div>
      </article>
      {activity}
      {sla}
      <article class="card">
        <header><h2>Management Overview</h2><span>Live API</span></header>
        <div class="bar-chart">{overview_rows}</div>
      </article>
      <article class="card">
        <header><h2>Recovery Health</h2><span>Restores</span></header>
        <div class="summary-band"><strong>{recovery:,}</strong><span>Restore jobs in {html_lib.escape(model["range_label"])}</span></div>
        <div class="summary-row"><span>Failed restores</span><strong>{report_int(summary.get("recoveryFailed")):,}</strong></div>
        <div class="summary-row"><span>Running restores</span><strong>{report_int(summary.get("recoveryRunning")):,}</strong></div>
        <div class="summary-row"><span>Clone jobs excluded</span><strong>{clones:,}</strong></div>
      </article>
      <article class="card">
        <header><h2>Clone Jobs</h2><span>Actions</span></header>
        <div class="summary-band"><strong>{clones:,}</strong><span>Clone jobs in {html_lib.escape(model["range_label"])}</span></div>
        <div class="summary-row"><span>Failed clone jobs</span><strong>{report_int(summary.get("cloneFailed")):,}</strong></div>
        <div class="summary-row"><span>Running clone jobs</span><strong>{report_int(summary.get("cloneRunning")):,}</strong></div>
        <div class="summary-row"><span>Clone sessions</span><strong>{report_int(summary.get("cloneSessionTotal")):,}</strong></div>
      </article>
    </section>
    <section class="metric-grid">{metrics}</section>
    <section class="health-section">
      <div class="health-head"><strong>NetWorker Server Health</strong><span>Updated {html_lib.escape(model["generated"])} - {html_lib.escape(model["range_label"])}</span></div>
      <div class="health-grid">
        {snapshot_health_card("Server status", model["server_status"], str(model["health"].get("detail") or "CPU/RAM endpoint did not return data."), model["server_status_color"])}
        {snapshot_health_card("CPU usage", model["cpu_value"], model["cpu_detail"], palette["blue"], model["health"].get("cpuUsagePercent"))}
        {snapshot_health_card("Memory usage", model["ram_value"], model["ram_detail"], palette["amber"], model["ram_percent"])}
        {snapshot_health_card("Server Protection Job", model["protection_label"], model["protection_detail"], model["protection_color"])}
      </div>
    </section>
  </main>
</body>
</html>
"""


def headless_browser_path() -> str:
    candidates = [
        shutil.which("msedge"),
        shutil.which("microsoft-edge"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return ""


def render_dashboard_snapshot_png(dashboard: dict[str, Any]) -> bytes | None:
    browser = headless_browser_path()
    if not browser:
        debug_log("Dashboard email snapshot skipped: no Edge/Chrome browser found.")
        return None
    with tempfile.TemporaryDirectory(prefix="networker-dashboard-report-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        html_path = tmp_path / "dashboard-report.html"
        png_path = tmp_path / "dashboard-report.png"
        html_path.write_text(dashboard_snapshot_html(dashboard), encoding="utf-8")
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--disable-dev-shm-usage",
            "--window-size=1880,760",
            f"--screenshot={png_path}",
            html_path.as_uri(),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not png_path.exists():
            debug_log(f"Dashboard email snapshot failed: {result.stderr or result.stdout or result.returncode}")
            return None
        try:
            return png_path.read_bytes()
        except OSError as exc:
            debug_log(f"Dashboard email snapshot read failed: {exc}")
            return None


def dashboard_report_email(dashboard: dict[str, Any], snapshot_cid: str = "") -> tuple[str, str]:
    summary = dashboard.get("summary") or {}
    target = dashboard.get("target") or {}
    health = dashboard.get("serverHealth") or {}
    protection = dashboard.get("serverProtectionJob") or dashboard.get("maintenanceBackup") or {}
    palette = report_theme_palette(dashboard.get("theme") or target.get("theme"))
    # Match the live dashboard brand card. It uses a fixed navy->teal gradient,
    # not the theme brand color. Email clients (Outlook) ignore CSS gradients, so
    # every bgcolor uses the solid dark-teal fallback; the outer brand container
    # additionally carries the gradient via background-image for modern clients.
    brand_background = BRAND_CARD_SOLID
    brand_ink = BRAND_CARD_INK
    rows = dashboard_report_rows(dashboard)
    plain = "\n".join(f"{label}: {value}" for label, value in rows)
    total_jobs = report_int(summary.get("slaTotalJobs", summary.get("totalJobs")))
    successful = report_int(summary.get("successfulJobs"))
    failed = report_int(summary.get("failedJobs"))
    active = report_int(summary.get("activeJobs"))
    recovery = report_int(summary.get("recoveryJobs"))
    clones = report_int(summary.get("cloneJobs"))
    alerts = report_int(summary.get("totalAlerts"))
    clients = report_int(summary.get("totalClients"))
    sla_percent = report_decimal(summary.get("slaPercent"), 2)
    sla_met = report_int(summary.get("slaMetJobs"))
    sla_missed = report_int(summary.get("slaMissedJobs"))
    range_label = str(summary.get("rangeLabel") or summary.get("range") or "Selected range")
    generated = str(dashboard.get("generatedAt") or generated_at())
    backup_server = str((target.get("backupServer") or "--"))
    api_mode = str((target.get("apiMode") or "--")).upper()
    connection_label, connection_color = report_connection_label(summary)
    server_status = str(health.get("label") or "Unavailable")
    server_status_color = palette["green"] if str(health.get("status") or "").lower() == "ok" else palette["red"]
    cpu_value = "--" if health.get("cpuUsagePercent") is None else f"{report_int(health.get('cpuUsagePercent'))}%"
    cpu_detail = str(health.get("cpuDetail") or health.get("source") or "No CPU metric returned.")
    ram_value = report_memory_value(health)
    ram_detail = report_memory_detail(health)
    ram_percent = health.get("ramUsagePercent")
    protection_status = str(protection.get("status") or "").lower()
    protection_color = (
        palette["red"]
        if protection_status == "failed"
        else (palette["amber"] if protection_status in ("running", "queued", "warning") else palette["green"])
    )
    protection_label = str(protection.get("label") or "Not found")
    protection_detail = str(protection.get("detail") or "No Server Protection job found in this range.")
    overview = [
        ("Clients", clients, palette["blue"]),
        ("Successful", successful, palette["green"]),
        ("Failed", failed, palette["red"]),
        ("Running", active, palette["blue"]),
        ("Restores", recovery, palette["amber"]),
        ("Clones", clones, palette["brand"]),
        ("Alerts", alerts, palette["muted"]),
    ]
    max_overview = max([value for _, value, _ in overview] + [1])
    top_activity = successful + failed + active + recovery
    table_rows = "\n".join(
        "<tr>"
        f"<td style=\"padding:8px 10px;border:1px solid {palette['line']};font-weight:700;color:{palette['ink']};\">{html_lib.escape(label)}</td>"
        f"<td style=\"padding:8px 10px;border:1px solid {palette['line']};color:{palette['ink']};\">{html_lib.escape(value)}</td>"
        "</tr>"
        for label, value in rows
    )
    metric_rows = (
        "<tr>"
        + report_metric_card("Clients", clients, palette["blue"], palette)
        + report_metric_card("Successful Jobs", successful, palette["green"], palette)
        + report_metric_card("Failed Jobs", failed, palette["red"], palette)
        + report_metric_card("Active Jobs", active, palette["blue"], palette)
        + report_metric_card("Recovery Jobs", recovery, palette["amber"], palette)
        + report_metric_card("Alerts", alerts, palette["amber"], palette)
        + "</tr>"
    )
    overview_rows = "".join(report_bar(label, value, max_overview, color, palette) for label, value, color in overview)
    recovery_rows = (
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Failed restores <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("recoveryFailed")):,}</strong></div>'
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Running restores <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("recoveryRunning")):,}</strong></div>'
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Clone jobs excluded <strong style="float:right;color:{palette["ink"]};">{clones:,}</strong></div>'
    )
    clone_rows = (
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Failed clone jobs <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("cloneFailed")):,}</strong></div>'
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Running clone jobs <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("cloneRunning")):,}</strong></div>'
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Clone sessions <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("cloneSessionTotal")):,}</strong></div>'
    )
    snapshot_block = ""
    if snapshot_cid:
        escaped_cid = html_lib.escape(snapshot_cid, quote=True)
        snapshot_block = (
            f'<div style="margin:0 0 14px;background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:10px;">'
            f'<img alt="NetWorker dashboard snapshot" src="cid:{escaped_cid}" '
            'style="display:block;width:100%;max-width:1880px;height:auto;border:0;border-radius:6px;">'
            '</div>'
        )
    html_body = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:{palette["bg"]};font-family:Segoe UI,Arial,sans-serif;color:{palette["ink"]};">
    <div style="padding:18px;background:{palette["bg"]};">
      {snapshot_block}
      <table role="presentation" style="width:100%;min-width:1680px;border-collapse:collapse;">
        <tr>
          <td bgcolor="{brand_background}" style="padding:0 8px 12px 0;width:16.66%;min-width:280px;vertical-align:top;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
            <div style="background:{brand_background};background-color:{brand_background};background-image:{BRAND_CARD_GRADIENT};border-radius:8px;padding:16px;color:{brand_ink};min-height:252px;">
              <table role="presentation" bgcolor="{brand_background}" style="width:100%;border-collapse:collapse;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                <tr>
                  <td bgcolor="{brand_background}" style="width:68px;vertical-align:top;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                    <img alt="NetWorker" width="60" height="60" src="{networker_logo_src()}" style="display:block;width:60px;height:60px;max-width:60px;max-height:60px;object-fit:contain;background:{palette["surface"]};border-radius:6px;padding:4px;">
                  </td>
                  <td bgcolor="{brand_background}" style="vertical-align:top;padding-left:10px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                    <div style="font-size:16px;font-weight:850;color:{brand_ink};">DELL EMC NetWorker</div>
                    <div style="font-size:12px;font-weight:700;margin-top:3px;color:{brand_ink};">Backup &amp; Recovery Status</div>
                  </td>
                </tr>
              </table>
              <div style="margin-top:14px;background:{brand_background};background-color:{brand_background};border-radius:7px;padding:10px;font-size:13px;font-weight:800;color:{brand_ink};"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:{connection_color};vertical-align:-1px;margin-right:6px;"></span>{html_lib.escape(connection_label)}</div>
              <table role="presentation" bgcolor="{brand_background}" style="width:100%;border-collapse:collapse;margin-top:18px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                <tr><td bgcolor="{brand_background}" style="padding:6px 0;font-size:12px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">API source</td><td bgcolor="{brand_background}" style="padding:6px 0;text-align:right;font-size:12px;font-weight:800;background:{brand_background};background-color:{brand_background};color:{brand_ink};">{html_lib.escape(api_mode)}</td></tr>
                <tr><td bgcolor="{brand_background}" style="padding:6px 0;font-size:12px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">Backup server</td><td bgcolor="{brand_background}" style="padding:6px 0;text-align:right;font-size:12px;font-weight:800;background:{brand_background};background-color:{brand_background};color:{brand_ink};">{html_lib.escape(backup_server)}</td></tr>
                <tr><td bgcolor="{brand_background}" style="padding:6px 0;font-size:12px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">Updated</td><td bgcolor="{brand_background}" style="padding:6px 0;text-align:right;font-size:12px;font-weight:800;background:{brand_background};background-color:{brand_background};color:{brand_ink};">{html_lib.escape(generated)}</td></tr>
              </table>
              <div style="margin-top:12px;border-top:1px solid rgba(255,255,255,0.22);padding-top:10px;font-size:11px;line-height:1.35;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                <div>Maintained &amp; developed by</div>
                <div style="font-weight:850;">SHAIKH SHOAIB</div>
                <div>Sr. Advisor Delivery Specialist</div>
                <div>DELL Technologies</div>
              </div>
            </div>
          </td>
          {report_donut_card(
              "Activity Mix",
              f"{top_activity:,}",
              "Activity",
              [
                  ("Successful", successful, palette["green"]),
                  ("Failed", failed, palette["red"]),
                  ("Running", active, palette["blue"]),
                  ("Restores", recovery, palette["amber"]),
              ],
              range_label,
              palette=palette,
          )}
          {report_donut_card(
              "Backup SLA",
              f"{sla_percent}%",
              "SLA",
              [
                  ("SLA met", sla_met, palette["green"]),
                  ("Not met", sla_missed, palette["red"]),
              ],
              f"{total_jobs:,} jobs",
              palette=palette,
          )}
          <td style="padding:0 8px 12px 0;width:16.66%;min-width:280px;vertical-align:top;">
            <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:252px;">
              <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:12px;">
                <tr><td style="font-size:14px;font-weight:800;color:{palette["ink"]};">Management Overview</td><td style="font-size:12px;text-align:right;color:{palette["muted"]};">Live API</td></tr>
              </table>
              <table role="presentation" style="width:100%;border-collapse:collapse;">{overview_rows}</table>
            </div>
          </td>
          <td style="padding:0 8px 12px 0;width:16.66%;min-width:280px;vertical-align:top;">
            <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:252px;">
              <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:18px;">
                <tr><td style="font-size:14px;font-weight:800;color:{palette["ink"]};">Recovery Health</td><td style="font-size:12px;text-align:right;color:{palette["muted"]};">Restores</td></tr>
              </table>
              <div style="background:{palette["surface2"]};border:1px solid {palette["line"]};border-radius:7px;padding:12px;margin-bottom:13px;">
                <div style="font-size:24px;font-weight:850;color:{palette["ink"]};">{recovery:,}</div>
                <div style="font-size:12px;font-weight:700;color:{palette["muted"]};">Restore jobs in {html_lib.escape(range_label)}</div>
              </div>
              {recovery_rows}
            </div>
          </td>
          <td style="padding:0 8px 12px 0;width:16.66%;min-width:280px;vertical-align:top;">
            <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:252px;">
              <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:18px;">
                <tr><td style="font-size:14px;font-weight:800;color:{palette["ink"]};">Clone Jobs</td><td style="font-size:12px;text-align:right;color:{palette["muted"]};">Actions</td></tr>
              </table>
              <div style="background:{palette["surface2"]};border:1px solid {palette["line"]};border-radius:7px;padding:12px;margin-bottom:13px;">
                <div style="font-size:24px;font-weight:850;color:{palette["ink"]};">{clones:,}</div>
                <div style="font-size:12px;font-weight:700;color:{palette["muted"]};">Clone jobs in {html_lib.escape(range_label)}</div>
              </div>
              {clone_rows}
            </div>
          </td>
        </tr>
      </table>

      <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:12px;">{metric_rows}</table>

      <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;margin-bottom:12px;">
        <table role="presentation" style="width:100%;border-collapse:collapse;border-bottom:1px solid {palette["line"]};">
          <tr>
            <td style="padding:14px 16px;font-size:14px;font-weight:850;color:{palette["ink"]};">NetWorker Server Health</td>
            <td style="padding:14px 16px;font-size:12px;text-align:right;color:{palette["muted"]};">Updated {html_lib.escape(generated)} - {html_lib.escape(range_label)}</td>
          </tr>
        </table>
        <table role="presentation" style="width:100%;border-collapse:collapse;padding:12px;">
          <tr>
            {report_health_card("Server status", server_status, str(health.get("detail") or "CPU/RAM endpoint did not return data."), server_status_color, palette=palette)}
            {report_health_card("CPU usage", cpu_value, cpu_detail, palette["blue"], health.get("cpuUsagePercent"), palette)}
            {report_health_card("Memory usage", ram_value, ram_detail, palette["amber"], ram_percent, palette)}
            {report_health_card("Server Protection Job", protection_label, protection_detail, protection_color, palette=palette)}
          </tr>
        </table>
      </div>

      <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;">
        <h3 style="margin:0 0 12px;font-size:14px;color:{palette["ink"]};">Report Details</h3>
        <table style="border-collapse:collapse;border:1px solid {palette["line"]};min-width:520px;width:100%;">
          {table_rows}
        </table>
      </div>
    </div>
  </body>
</html>
"""
    return plain, html_body


def should_send_alert(trigger: str, severity: str) -> bool:
    if trigger == "all":
        return True
    if trigger == "warning":
        return severity in ("warning", "critical")
    return severity == "critical"


def smtp_debug_snapshot(settings: AlertAutomation, smtp_password: str, stage: str = "prepare") -> dict[str, Any]:
    return {
        "stage": stage,
        "host": settings.smtp_host,
        "port": settings.smtp_port,
        "security": settings.smtp_security,
        "usernameProvided": bool(settings.smtp_username),
        "passwordProvided": bool(smtp_password),
        "recipientCount": len(settings.recipients),
    }


def smtp_exception_detail(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        smtp_error = exc.smtp_error.decode("utf-8", errors="replace") if isinstance(exc.smtp_error, bytes) else exc.smtp_error
        return f"authentication rejected by SMTP server: code={exc.smtp_code} response={smtp_error}"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"all recipients were refused by SMTP server: {exc.recipients}"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"sender was refused by SMTP server: code={exc.smtp_code} sender={exc.sender} response={exc.smtp_error}"
    if isinstance(exc, smtplib.SMTPDataError):
        smtp_error = exc.smtp_error.decode("utf-8", errors="replace") if isinstance(exc.smtp_error, bytes) else exc.smtp_error
        return f"SMTP data command failed: code={exc.smtp_code} response={smtp_error}"
    if isinstance(exc, smtplib.SMTPConnectError):
        smtp_error = exc.smtp_error.decode("utf-8", errors="replace") if isinstance(exc.smtp_error, bytes) else exc.smtp_error
        return f"SMTP connection rejected: code={exc.smtp_code} response={smtp_error}"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return f"SMTP server disconnected: {exc}"
    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout):
        return "SMTP connection timed out."
    if isinstance(exc, ssl.SSLError):
        return f"TLS/SSL error: {exc}"
    if isinstance(exc, OSError):
        return f"network error: {exc}"
    return str(exc) or exc.__class__.__name__


def send_smtp_email(
    settings: AlertAutomation,
    subject: str,
    body: str,
    smtp_password: str,
    html_body: str = "",
    inline_images: dict[str, tuple[bytes, str, str]] | None = None,
    attachments: dict[str, tuple[bytes, str, str]] | None = None,
) -> dict[str, Any]:
    stage = "prepare_message"
    diagnostics = smtp_debug_snapshot(settings, smtp_password, stage)
    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.smtp_from
        message["To"] = ", ".join(settings.recipients)
        message["Date"] = email.utils.formatdate(localtime=True)
        message.set_content(body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
            if inline_images:
                html_part = message.get_payload()[-1]
                for cid, (image_bytes, mime_type, filename) in inline_images.items():
                    maintype, _, subtype = mime_type.partition("/")
                    html_part.add_related(
                        image_bytes,
                        maintype=maintype or "image",
                        subtype=subtype or "png",
                        cid=f"<{cid}>",
                        filename=filename,
                    )
        if attachments:
            for _, (attachment_bytes, mime_type, filename) in attachments.items():
                maintype, _, subtype = mime_type.partition("/")
                message.add_attachment(
                    attachment_bytes,
                    maintype=maintype or "application",
                    subtype=subtype or "octet-stream",
                    filename=filename,
                )

        if settings.smtp_security == "ssl":
            stage = "connect_ssl"
            diagnostics["stage"] = stage
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                if settings.smtp_username:
                    stage = "login"
                    diagnostics["stage"] = stage
                    smtp.login(settings.smtp_username, smtp_password)
                stage = "send_message"
                diagnostics["stage"] = stage
                smtp.send_message(message)
        else:
            stage = "connect"
            diagnostics["stage"] = stage
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                stage = "ehlo"
                diagnostics["stage"] = stage
                smtp.ehlo()
                if settings.smtp_security == "starttls":
                    stage = "starttls"
                    diagnostics["stage"] = stage
                    smtp.starttls()
                    stage = "ehlo_after_starttls"
                    diagnostics["stage"] = stage
                    smtp.ehlo()
                if settings.smtp_username:
                    stage = "login"
                    diagnostics["stage"] = stage
                    smtp.login(settings.smtp_username, smtp_password)
                stage = "send_message"
                diagnostics["stage"] = stage
                smtp.send_message(message)
        diagnostics["stage"] = "sent"
        diagnostics["detail"] = "Email accepted by SMTP server."
        return diagnostics
    except (
        smtplib.SMTPException,
        TimeoutError,
        socket.timeout,
        OSError,
        ssl.SSLError,
    ) as exc:
        detail = smtp_exception_detail(exc)
        debug_log(f"SMTP delivery failed at {stage}: {detail}")
        raise SmtpDeliveryError(stage, detail, diagnostics) from exc


def cancel_alert_automation(automation_id: str) -> bool:
    automation = _pop_automation(automation_id)
    if not automation:
        return False
    if automation.timer:
        automation.timer.cancel()
    return True


def cancel_session_automations(session_id: str) -> int:
    count = 0
    for key in list(session_automation_keys(session_id)):
        if cancel_alert_automation(key):
            count += 1
    return count


def seconds_until_daily_report(report_time: str, now: datetime | None = None) -> float:
    now = now or datetime.now().astimezone()
    hour, minute = (int(part) for part in report_time.split(":", 1))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def schedule_alert_automation(automation: AlertAutomation) -> None:
    if _get_automation(automation.automation_id) is None:
        return
    delay = (
        seconds_until_daily_report(automation.report_time)
        if automation.schedule_type == "daily_report"
        else automation.interval_minutes * 60
    )
    timer = threading.Timer(delay, run_alert_automation, args=(automation.automation_id,))
    timer.daemon = True
    automation.timer = timer
    timer.start()


def scheduled_dashboard_email_payload(dashboard: dict[str, Any]) -> tuple[str, str, dict[str, tuple[bytes, str, str]]]:
    snapshot_png = render_dashboard_snapshot_png(dashboard)
    attachments: dict[str, tuple[bytes, str, str]] = {}
    plain, html_body = dashboard_report_email(dashboard)
    if snapshot_png:
        attachments["networker-dashboard.png"] = (snapshot_png, "image/png", "networker-dashboard.png")
    return plain, html_body, attachments


def run_alert_automation(automation_id: str) -> None:
    automation = _get_automation(automation_id)
    if not automation:
        return
    try:
        status, dashboard = build_dashboard_from_session(automation.session_id)
        if status != HTTPStatus.OK:
            raise RuntimeError(dashboard.get("error") or "Dashboard session refresh failed.")
        if automation.schedule_type == "daily_report":
            if not dashboard_backup_source_available(dashboard):
                source_error = dashboard_backup_source_error(dashboard)
                last_good_dashboard = shared_reliable_dashboard_for_session(automation.session_id)
                if not last_good_dashboard:
                    raise RuntimeError(
                        f"Daily report skipped because backup job data was unavailable: {source_error}"
                    )
                dashboard = last_good_dashboard
                dashboard["reportNotice"] = (
                    "Live scheduled refresh could not load backup job data; this email uses the last successful "
                    f"dashboard snapshot. Refresh error: {source_error}"
                )
            # Use the current dashboard theme (dynamic) so the report matches
            # whatever theme is set now, falling back to the theme captured when
            # the schedule was created.
            dashboard["theme"] = load_ui_theme() or automation.theme
            dashboard["scheduledReport"] = True
            plain, html_body, attachments = scheduled_dashboard_email_payload(dashboard)
            report_password = decrypt_process_secret(automation.encrypted_smtp_password)
            smtp_debug = send_smtp_email(
                automation,
                "NetWorker daily backup status and SLA report",
                plain,
                report_password,
                html_body,
                attachments=attachments,
            ) or smtp_debug_snapshot(automation, report_password, "sent")
            automation.last_signature = dashboard.get("generatedAt") or generated_at()
            automation.last_result = (
                f"Sent daily backup/SLA report at {generated_at()} "
                f"via {smtp_debug.get('host')}:{smtp_debug.get('port')}"
            )
            automation.last_run = time.time()
            return
        severity, lines = dashboard_alert_lines(dashboard)
        signature = "|".join(lines)
        cooldown_ok = (time.time() - automation.last_run) >= (automation.interval_minutes * 60) if automation.last_run else True
        if should_send_alert(automation.trigger, severity) and signature != automation.last_signature and cooldown_ok:
            subject = f"NetWorker dashboard alert: {severity.title()}"
            alert_password = decrypt_process_secret(automation.encrypted_smtp_password)
            smtp_debug = send_smtp_email(
                automation,
                subject,
                "\n".join(lines),
                alert_password,
            ) or smtp_debug_snapshot(automation, alert_password, "sent")
            automation.last_signature = signature
            automation.last_result = (
                f"Sent {severity} alert at {generated_at()} "
                f"via {smtp_debug.get('host')}:{smtp_debug.get('port')}"
            )
        else:
            automation.last_result = f"No matching alert at {generated_at()}"
        automation.last_run = time.time()
    except SmtpDeliveryError as exc:
        automation.last_result = f"SMTP failed at {exc.stage}: {exc.detail}"
    except Exception as exc:
        automation.last_result = f"Alert automation failed: {exc}"
    finally:
        schedule_alert_automation(automation)


def handle_alert_automation(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    action = str(payload.get("action") or "").strip().lower()
    session_id = str(payload.get("sessionId") or "").strip()
    if action == "stop":
        raw_schedule_type = str(payload.get("scheduleType") or "").strip().lower()
        if raw_schedule_type in ("alert", "daily_report"):
            schedule_type = raw_schedule_type
            stopped = cancel_alert_automation(automation_key(session_id, schedule_type))
            kind = "Daily dashboard report" if schedule_type == "daily_report" else "Alert automation"
            summary = active_automation_summary(session_id)
            return HTTPStatus.OK, {
                "ok": True,
                "message": (
                    f"{kind} stopped. Active schedules: {summary}."
                    if stopped and summary
                    else (f"{kind} stopped." if stopped else f"No {kind.lower()} was scheduled.")
                ),
                "activeAutomations": summary,
            }
        stopped_count = cancel_session_automations(session_id)
        return HTTPStatus.OK, {
            "ok": True,
            "message": (
                f"Stopped {stopped_count} email automation(s)."
                if stopped_count
                else "No email automation was scheduled."
            ),
            "activeAutomations": active_automation_summary(session_id),
        }
    if action == "save":
        config = save_email_config_from_payload(payload)
        return HTTPStatus.OK, {
            "ok": True,
            "message": "Email notification configuration saved.",
            "config": config,
        }
    if action not in ("start", "test"):
        raise BadRequest("Alert automation action must be start, test, save, or stop.")
    if not session_id or not _session_exists(session_id):
        raise BadRequest("A live dashboard session is required before scheduling email alerts.")
    settings = parse_smtp_settings(payload)
    automation_id = automation_key(session_id, settings["schedule_type"])
    existing = existing_smtp_automation(session_id, settings["schedule_type"])
    smtp_password = settings["smtp_password"] or (
        decrypt_process_secret(existing.encrypted_smtp_password) if existing else ""
    ) or saved_email_smtp_password()
    # Scheduling a notification also persists its configuration so it survives a
    # restart and pre-fills the form next time.
    if action == "start":
        try:
            save_email_config_from_payload(payload)
        except BadRequest:
            pass
    automation = AlertAutomation(
        automation_id=automation_id,
        session_id=session_id,
        smtp_host=settings["smtp_host"],
        smtp_port=settings["smtp_port"],
        smtp_username=settings["smtp_username"],
        encrypted_smtp_password=encrypt_process_secret(smtp_password),
        smtp_from=settings["smtp_from"],
        recipients=settings["recipients"],
        smtp_security=settings["smtp_security"],
        interval_minutes=settings["interval_minutes"],
        trigger=settings["trigger"],
        schedule_type=settings["schedule_type"],
        report_time=settings["report_time"],
        created_at=time.time(),
        theme=settings["theme"],
    )
    if action == "test":
        subject = "NetWorker dashboard test email"
        plain_body = f"Test email from {APP_NAME} at {generated_at()}."
        html_body = ""
        attachments: dict[str, tuple[bytes, str, str]] = {}
        if automation.schedule_type == "daily_report":
            dashboard = payload.get("dashboard") if isinstance(payload.get("dashboard"), dict) else None
            if dashboard:
                dashboard["theme"] = automation.theme
                dashboard["scheduledReport"] = True
                plain_body, html_body, attachments = scheduled_dashboard_email_payload(dashboard)
                subject = "NetWorker daily backup status and SLA report - test"
            else:
                status, dashboard = build_dashboard_from_session(session_id)
                if status == HTTPStatus.OK:
                    dashboard["theme"] = automation.theme
                    dashboard["scheduledReport"] = True
                    plain_body, html_body, attachments = scheduled_dashboard_email_payload(dashboard)
                    subject = "NetWorker daily backup status and SLA report - test"
        try:
            smtp_debug = send_smtp_email(
                automation,
                subject,
                plain_body,
                smtp_password,
                html_body,
                attachments=attachments,
            ) or smtp_debug_snapshot(automation, smtp_password, "sent")
        except SmtpDeliveryError as exc:
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "error": str(exc),
                "message": str(exc),
                "smtpDebug": exc.diagnostics,
            }
        return HTTPStatus.OK, {"ok": True, "message": "Test email sent.", "smtpDebug": smtp_debug}

    cancel_alert_automation(automation_id)
    _put_automation(automation_id, automation)
    schedule_alert_automation(automation)
    active_summary = active_automation_summary(session_id)
    message = (
        f"Daily backup/SLA report scheduled for {automation.report_time}."
        if automation.schedule_type == "daily_report"
        else f"Alert automation scheduled every {automation.interval_minutes} minute(s)."
    )
    if active_summary:
        message = f"{message} Active schedules: {active_summary}."
    return HTTPStatus.OK, {
        "ok": True,
        "message": message,
        "activeAutomations": active_summary,
    }


def build_dashboard_rest_auto(config: ApiConfig) -> tuple[int, dict[str, Any]]:
    if config.api_version != "auto":
        return build_dashboard_rest(config)
    last_status = 502
    last_body: dict[str, Any] = {}
    for version in API_VERSION_CANDIDATES:
        status, body = build_dashboard_rest(replace(config, api_version=version))
        body.setdefault("target", {})["apiVersionTried"] = version
        if status == 200:
            return status, body
        last_status, last_body = status, body
        if status in (401, 403):
            return status, body
    return last_status, last_body


def build_dashboard(config: ApiConfig) -> tuple[int, dict[str, Any]]:
    if config.api_mode in ("auto", "nwui"):
        nwui_status, nwui_body = build_dashboard_nwui(config)
        nwui_login_ok = bool(
            isinstance(nwui_body.get("sources"), dict)
            and isinstance(nwui_body["sources"].get("nwuiLogin"), dict)
            and nwui_body["sources"]["nwuiLogin"].get("ok")
        )
        if nwui_status == 200 or config.api_mode == "nwui" or nwui_status in (401, 403) or nwui_login_ok:
            return nwui_status, nwui_body
        debug_log(f"NWUI mode failed with status={nwui_status}; trying direct REST fallback")
    return build_dashboard_rest_auto(config)


def build_multi_server_dashboard(session_ids: list[str]) -> tuple[int, dict[str, Any]]:
    """Fetch dashboard data for each session in parallel and return aggregated summary."""
    if not session_ids:
        return 400, {"ok": False, "error": "No session IDs provided."}

    def fetch_one(sid: str) -> dict[str, Any]:
        status, body = build_dashboard_from_session(sid, DEFAULT_REPORT_RANGE, "", "")
        summary = body.get("summary", {}) if isinstance(body.get("summary"), dict) else {}
        target = body.get("target", {}) if isinstance(body.get("target"), dict) else {}
        return {
            "sessionId": sid,
            "host": target.get("restApiBase", sid),
            "ok": status < 400,
            "status": status,
            "summary": summary,
            "error": body.get("error", "") if status >= 400 else "",
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(session_ids), 8)) as pool:
        results = list(pool.map(fetch_one, session_ids))

    totals: dict[str, int] = {}
    for key in ("totalJobs", "successfulJobs", "failedJobs", "activeJobs", "recoveryJobs", "cloneJobs", "totalAlerts"):
        totals[key] = sum(int(r["summary"].get(key) or 0) for r in results if r["ok"])

    any_critical = any(str(r["summary"].get("health", "")).lower() == "critical" for r in results if r["ok"])
    any_warning  = any(str(r["summary"].get("health", "")).lower() in ("warning", "warn") for r in results if r["ok"])
    agg_health = "critical" if any_critical else ("warning" if any_warning else "ok")

    return 200, {
        "ok": True,
        "servers": results,
        "aggregate": {**totals, "health": agg_health, "serverCount": len(session_ids)},
        "generatedAt": generated_at(),
    }


def login_page_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NetWorker Dashboard — Sign in</title>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#eef3f6;color:#172026;min-height:100vh;display:flex;align-items:center;justify-content:center}
  .card{background:#fff;border:1px solid #d7e1e7;border-radius:12px;padding:32px;width:340px;box-shadow:0 4px 16px rgba(0,0,0,.06)}
  h1{font-size:18px;margin-bottom:6px}
  p{font-size:13px;color:#5f6d76;margin-bottom:20px}
  label{display:block;font-size:13px;margin-bottom:6px}
  input{width:100%;padding:10px 12px;border:1px solid #d7e1e7;border-radius:8px;font-size:14px;margin-bottom:16px}
  button{width:100%;padding:10px;border:0;border-radius:8px;background:#126e82;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  .err{background:#fde2e4;border:1px solid #f0b8bc;color:#bd2b3a;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:16px;display:none}
</style>
</head>
<body>
<div class="card">
  <h1>NetWorker Dashboard</h1>
  <p>Enter the dashboard access password to continue.</p>
  <div class="err" id="err"></div>
  <form id="loginForm">
    <label for="pw">Password</label>
    <input type="password" id="pw" autocomplete="current-password" autofocus>
    <button type="submit" id="btn">Sign in</button>
  </form>
</div>
<script>
  const form=document.getElementById('loginForm');
  const err=document.getElementById('err');
  const btn=document.getElementById('btn');
  form.addEventListener('submit',async(e)=>{
    e.preventDefault();
    btn.disabled=true;err.style.display='none';
    try{
      const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
      if(r.ok){location.reload();return;}
      const d=await r.json().catch(()=>({}));
      err.textContent=d.error||'Sign in failed.';err.style.display='block';
    }catch(_){err.textContent='Network error.';err.style.display='block';}
    btn.disabled=false;
  });
</script>
</body>
</html>"""


def read_only_view_html(token: str) -> str:
    """Minimal read-only dashboard page served at /view/{token}."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NetWorker Dashboard – Read Only</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: #eef3f6; color: #172026; min-height: 100vh; }}
  .topbar {{ background: #126e82; color: #fff; padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }}
  .topbar h1 {{ font-size: 18px; font-weight: 600; }}
  .topbar .meta {{ font-size: 13px; opacity: 0.8; }}
  .shell {{ max-width: 1200px; margin: 0 auto; padding: 24px 20px; }}
  .status-bar {{ background: #fff; border: 1px solid #d7e1e7; border-radius: 10px; padding: 14px 18px; margin-bottom: 20px;
                 display: flex; align-items: center; gap: 12px; font-size: 14px; color: #5f6d76; }}
  .pill {{ padding: 3px 10px; border-radius: 99px; font-size: 12px; font-weight: 600; }}
  .pill.ok   {{ background: #d1f0e0; color: #18764a; }}
  .pill.warn {{ background: #fef3cd; color: #a96800; }}
  .pill.bad  {{ background: #fde2e4; color: #bd2b3a; }}
  .pill.load {{ background: #f0f4f6; color: #5f6d76; }}
  .metric-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .metric-card {{ background: #fff; border: 1px solid #d7e1e7; border-radius: 10px; padding: 16px 18px; }}
  .metric-card .label {{ font-size: 12px; color: #5f6d76; margin-bottom: 4px; }}
  .metric-card .value {{ font-size: 28px; font-weight: 700; color: #172026; }}
  .metric-card .value.red {{ color: #bd2b3a; }}
  .metric-card .value.green {{ color: #18764a; }}
  .metric-card .value.amber {{ color: #a96800; }}
  .readonly-badge {{ background: rgba(255,255,255,0.2); border-radius: 6px; padding: 3px 10px; font-size: 12px; }}
  .refresh-hint {{ font-size: 12px; color: #5f6d76; text-align: right; margin-top: 12px; }}
  .error-box {{ background: #fde2e4; border: 1px solid #f0b8bc; border-radius: 10px; padding: 16px 18px; color: #bd2b3a; font-size: 14px; }}
</style>
</head>
<body>
<div class="topbar">
  <h1>Backup &amp; Recovery Dashboard</h1>
  <span class="readonly-badge">Read-only view</span>
</div>
<div class="shell">
  <div id="content"><div class="status-bar"><span class="pill load">Loading…</span><span id="statusText">Fetching dashboard data…</span></div></div>
</div>
<script>
  async function load() {{
    try {{
      const r = await fetch('/api/view/{token}', {{cache: 'no-store'}});
      const data = await r.json();
      render(data);
    }} catch(e) {{
      document.getElementById('content').innerHTML = '<div class="error-box">Failed to load dashboard data: ' + (e.message || 'unknown error') + '</div>';
    }}
  }}

  function esc(s) {{
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }}

  function render(data) {{
    const s = data.dashboard?.summary || {{}};
    const health = String(s.health || 'unknown').toLowerCase();
    const pillCls = health === 'ok' ? 'ok' : health === 'critical' ? 'bad' : health === 'warning' ? 'warn' : 'load';
    const pillLabel = health === 'ok' ? 'Healthy' : health === 'critical' ? 'Critical' : health === 'warning' ? 'Warning' : 'Unknown';

    const metrics = [
      ['Total Jobs',   s.totalJobs   || 0, ''],
      ['Succeeded',    s.successfulJobs || 0, 'green'],
      ['Failed',       s.failedJobs  || 0, 'red'],
      ['Active',       s.activeJobs  || 0, ''],
      ['Restores',     s.recoveryJobs || 0, ''],
      ['Alerts',       s.totalAlerts || 0, s.totalAlerts > 0 ? 'amber' : ''],
    ];

    const cards = metrics.map(([lbl, val, cls]) =>
      '<div class="metric-card"><div class="label">' + esc(lbl) + '</div><div class="value ' + cls + '">' + val + '</div></div>'
    ).join('');

    const ts = data.dashboard?.generatedAt || data.updatedAt || '';
    document.getElementById('content').innerHTML =
      '<div class="status-bar"><span class="pill ' + pillCls + '">' + pillLabel + '</span>' +
      '<span id="statusText">Server: ' + esc(data.dashboard?.target?.restApiBase || 'unknown') + '</span>' +
      '<span style="margin-left:auto;font-size:12px">' + esc(ts) + '</span></div>' +
      '<div class="metric-grid">' + cards + '</div>' +
      '<p class="refresh-hint">Auto-refreshes every 60 s · Token: {token}</p>';
  }}

  load();
  setInterval(load, 60000);
</script>
</body>
</html>"""


def excel_col(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def excel_ref(row: int, col: int) -> str:
    return f"{excel_col(col)}{row}"


def xml_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def xlsx_cell(row: int, col: int, value: Any, style: int = 0) -> str:
    ref = excel_ref(row, col)
    style_attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{style_attr} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{xml_escape(value)}</t></is></c>'


def worksheet_xml(
    rows: list[list[Any]],
    sheet_name: str,
    drawing_id: str | None = None,
    column_widths: list[int] | None = None,
) -> str:
    max_cols = max((len(row) for row in rows), default=1)
    widths = column_widths or [18] * max_cols
    cols_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(widths[:max_cols], start=1)
    )
    row_xml = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx in range(1, max_cols + 1):
            value = row[c_idx - 1] if c_idx <= len(row) else ""
            style = 1 if r_idx == 1 else 0
            if sheet_name == "Dashboard" and r_idx in (1, 3, 11):
                style = 2 if r_idx == 1 else 1
            cells.append(xlsx_cell(r_idx, c_idx, value, style))
        row_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    drawing_xml = f'<drawing r:id="{drawing_id}"/>' if drawing_id else ""
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <cols>{cols_xml}</cols>
 <sheetData>{"".join(row_xml)}</sheetData>
 {drawing_xml}
</worksheet>'''


def workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, name in enumerate(sheet_names, start=1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets>{sheets}</sheets>
</workbook>'''


def workbook_rels_xml(sheet_count: int) -> str:
    rels = []
    for idx in range(1, sheet_count + 1):
        rels.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )
    rels.append(
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 {"".join(rels)}
</Relationships>'''


def styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <fonts count="3">
  <font><sz val="11"/><color rgb="FF172026"/><name val="Calibri"/></font>
  <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
  <font><b/><sz val="18"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
 </fonts>
 <fills count="4">
  <fill><patternFill patternType="none"/></fill>
  <fill><patternFill patternType="gray125"/></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FF126E82"/><bgColor indexed="64"/></patternFill></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FF102832"/><bgColor indexed="64"/></patternFill></fill>
 </fills>
 <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
 <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
 <cellXfs count="3">
  <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
  <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/>
  <xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyFill="1" applyFont="1"/>
 </cellXfs>
</styleSheet>'''


def content_types_xml(sheet_count: int) -> str:
    overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
        '<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>',
        '<Override PartName="/xl/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>',
    ]
    for idx in range(1, sheet_count + 1):
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 {"".join(overrides)}
</Types>'''


def package_rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''


def drawing_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <xdr:twoCellAnchor>
  <xdr:from><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
  <xdr:to><xdr:col>9</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>17</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>
  <xdr:graphicFrame macro="">
   <xdr:nvGraphicFramePr><xdr:cNvPr id="2" name="Backup Status Pie Chart"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr>
   <xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>
   <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
    <c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="rId1"/>
   </a:graphicData></a:graphic>
  </xdr:graphicFrame>
  <xdr:clientData/>
 </xdr:twoCellAnchor>
</xdr:wsDr>'''


def chart_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <c:chart>
  <c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Backup Status Distribution</a:t></a:r></a:p></c:rich></c:tx></c:title>
  <c:plotArea>
   <c:layout/>
   <c:pieChart>
    <c:varyColors val="1"/>
    <c:ser>
     <c:idx val="0"/><c:order val="0"/>
     <c:cat><c:strRef><c:f>Dashboard!$A$12:$A$16</c:f></c:strRef></c:cat>
     <c:val><c:numRef><c:f>Dashboard!$B$12:$B$16</c:f></c:numRef></c:val>
    </c:ser>
    <c:firstSliceAng val="270"/>
   </c:pieChart>
  </c:plotArea>
  <c:legend><c:legendPos val="r"/><c:layout/></c:legend>
  <c:plotVisOnly val="1"/>
 </c:chart>
 <c:printSettings><c:headerFooter/><c:pageMargins b="0.75" l="0.7" r="0.7" t="0.75" header="0.3" footer="0.3"/><c:pageSetup/></c:printSettings>
</c:chartSpace>'''


def simple_rels_xml(target: str, rel_type: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="{rel_type}" Target="{target}"/>
</Relationships>'''


def core_props_xml() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <dc:title>NetWorker Backup and Restore Dashboard Report</dc:title>
 <dc:creator>{APP_NAME}</dc:creator>
 <cp:lastModifiedBy>{APP_NAME}</cp:lastModifiedBy>
 <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
 <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>'''


def app_props_xml(sheet_names: list[str]) -> str:
    titles = "".join(f'<vt:lpstr>{xml_escape(name)}</vt:lpstr>' for name in sheet_names)
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
 <Application>NetWorker Dashboard</Application>
 <TitlesOfParts><vt:vector size="{len(sheet_names)}" baseType="lpstr">{titles}</vt:vector></TitlesOfParts>
</Properties>'''


def rows_from_table(items: list[dict[str, Any]], columns: list[tuple[str, str]]) -> list[list[Any]]:
    rows = [[label for _, label in columns]]
    for item in items:
        rows.append([item.get(key, "") for key, _ in columns])
    return rows


def build_excel_report(dashboard: dict[str, Any]) -> bytes:
    summary = dashboard.get("summary", {}) if isinstance(dashboard.get("summary"), dict) else {}
    tables = dashboard.get("tables", {}) if isinstance(dashboard.get("tables"), dict) else {}
    target = dashboard.get("target", {}) if isinstance(dashboard.get("target"), dict) else {}
    status_rows = [
        ["Succeeded", int(summary.get("successfulJobs") or 0)],
        ["Failed", int(summary.get("failedJobs") or 0)],
        ["Running / Queued", int(summary.get("activeJobs") or 0)],
        ["Restores", int(summary.get("recoveryJobs") or 0)],
        ["Clones", int(summary.get("cloneJobs") or 0)],
        ["Alerts", int(summary.get("totalAlerts") or 0)],
    ]
    dashboard_rows = [
        ["NetWorker Backup and Restore Dashboard Report", ""],
        ["Generated", dashboard.get("generatedAt", "")],
        ["Range", summary.get("rangeLabel", "")],
        ["API Source", target.get("apiMode", "")],
        ["API Base", target.get("restApiBase", "")],
        ["Total Jobs", int(summary.get("totalJobs") or 0)],
        ["Successful Jobs", int(summary.get("successfulJobs") or 0)],
        ["Failed Jobs", int(summary.get("failedJobs") or 0)],
        ["Active Jobs", int(summary.get("activeJobs") or 0)],
        ["Restore Jobs", int(summary.get("recoveryJobs") or 0)],
        ["Clone Jobs", int(summary.get("cloneJobs") or 0)],
        ["Status", "Count"],
        *status_rows,
    ]
    jobs_cols = [
        ("client", "Client"),
        ("name", "Job"),
        ("policy", "Policy"),
        ("status", "Status"),
        ("started", "Started"),
        ("duration", "Duration"),
        ("message", "Message"),
    ]
    failed_cols = [
        ("client", "Client"),
        ("name", "Job"),
        ("policy", "Policy"),
        ("started", "Started"),
        ("message", "Message"),
    ]
    log_cols = [
        ("priority", "Priority"),
        ("time", "Time"),
        ("source", "Source"),
        ("category", "Category"),
        ("message", "Message"),
    ]
    alert_cols = [("severity", "Severity"), ("time", "Time"), ("message", "Message"), ("resource", "Resource")]
    client_cols = [
        ("hostname", "Hostname"),
        ("enabled", "Enabled"),
        ("backupType", "Backup Type"),
        ("saveSets", "Save Sets"),
        ("protectionGroups", "Protection Groups"),
    ]
    sheets = [
        ("Dashboard", dashboard_rows, [28, 18, 18, 18, 18, 18, 18, 18, 18]),
        ("Backup Jobs", rows_from_table(tables.get("jobs", []), jobs_cols), [22, 24, 24, 16, 24, 14, 48]),
        ("Failed Jobs", rows_from_table(tables.get("failedJobs", []), failed_cols), [22, 24, 24, 24, 58]),
        ("Restores", rows_from_table(tables.get("recovery", []), jobs_cols), [22, 24, 24, 16, 24, 14, 48]),
        ("Clone Jobs", rows_from_table(tables.get("cloneJobs", []), jobs_cols), [22, 24, 24, 16, 24, 14, 48]),
        ("Logs", rows_from_table(tables.get("logs", []), log_cols), [14, 24, 16, 18, 80]),
        ("Alerts", rows_from_table(tables.get("alerts", []), alert_cols), [16, 24, 64, 28]),
        ("Clients", rows_from_table(tables.get("clients", []), client_cols), [28, 16, 20, 24, 40]),
    ]
    sheet_names = [name for name, _, _ in sheets]
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml(len(sheets)))
        zf.writestr("_rels/.rels", package_rels_xml())
        zf.writestr("docProps/core.xml", core_props_xml())
        zf.writestr("docProps/app.xml", app_props_xml(sheet_names))
        zf.writestr("xl/workbook.xml", workbook_xml(sheet_names))
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml(len(sheets)))
        zf.writestr("xl/styles.xml", styles_xml())
        for idx, (name, rows, widths) in enumerate(sheets, start=1):
            drawing_id = "rId1" if idx == 1 else None
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", worksheet_xml(rows, name, drawing_id, widths))
        zf.writestr(
            "xl/worksheets/_rels/sheet1.xml.rels",
            simple_rels_xml("../drawings/drawing1.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"),
        )
        zf.writestr("xl/drawings/drawing1.xml", drawing_xml())
        zf.writestr(
            "xl/drawings/_rels/drawing1.xml.rels",
            simple_rels_xml("../charts/chart1.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"),
        )
        zf.writestr("xl/charts/chart1.xml", chart_xml())
    return output.getvalue()


def safe_log_text(value: Any, max_len: int = 600) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def debug_log(message: str) -> None:
    LOG.debug(safe_log_text(message, 520))


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = f"NetWorkerDashboard/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_SECONDS
    request_id = "-"

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info(
            format % args,
            extra={"request_id": getattr(self, "request_id", "-"), "client": self.client_address[0]},
        )

    def log_dashboard_failure(self, status: int, body: dict[str, Any]) -> None:
        target = body.get("target") if isinstance(body.get("target"), dict) else {}
        sources = body.get("sources") if isinstance(body.get("sources"), dict) else {}
        rest_base = safe_log_text(target.get("restApiBase", "unknown"))
        api_mode = safe_log_text(target.get("apiMode", "rest"))
        authc_header = "enabled" if target.get("authcHeaderEnabled") else "disabled"
        rid = getattr(self, "request_id", "-")
        LOG.warning(
            f"NetWorker dashboard upstream failure: apiMode={api_mode} apiBase={rest_base} authcHeader={authc_header}",
            extra={"request_id": rid, "status": status},
        )
        for name, item in sources.items():
            if isinstance(item, dict) and not item.get("ok"):
                path = safe_log_text(item.get("path", name))
                error = safe_log_text(item.get("error", "failed"))
                upstream_status = item.get("status", "n/a")
                LOG.warning(
                    f"  source={safe_log_text(name)} upstreamStatus={upstream_status} path={path} error={error}",
                    extra={"request_id": rid, "status": status},
                )

    def _is_https(self) -> bool:
        return isinstance(self.request, ssl.SSLSocket)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError as exc:
            # Client disconnected mid-response (TLS EOF / reset / broken pipe).
            # ssl.SSLError is an OSError subclass, so this covers SSLEOFError too.
            # Nothing to recover; abort this connection quietly instead of letting
            # the failure bubble into a second (also-failing) error write.
            self.close_connection = True
            LOG.debug(
                f"client disconnected during response: {exc}",
                extra={"request_id": getattr(self, "request_id", "-"), "client": self.client_address[0]},
            )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise BadRequest("Invalid Content-Length.") from exc
        if length <= 0 or length > MAX_POST_BYTES:
            raise BadRequest("Request body size is invalid.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise BadRequest("JSON body must be an object.")
        return payload

    def _require_https(self) -> bool:
        if self._is_https():
            return True
        self._send_error_json(HTTPStatus.FORBIDDEN, "HTTPS is required.")
        return False

    def _authenticated(self) -> bool:
        if not AUTH_ENABLED:
            return True
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 — malformed cookie header
            return False
        morsel = jar.get(COOKIE_NAME)
        if not morsel:
            return False
        return _verify_auth_cookie(morsel.value)

    def _send_json_with_cookie(self, status: int, payload: dict[str, Any], cookie_value: str, max_age: int) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        cookie = (
            f"{COOKIE_NAME}={cookie_value}; HttpOnly; Secure; SameSite=Strict; "
            f"Path=/; Max-Age={max_age}"
        )
        self._send_bytes(status, body, "application/json; charset=utf-8", {"Set-Cookie": cookie})

    def _handle_login(self) -> None:
        ip = self.client_address[0]
        if _login_rate_limited(ip):
            self._send_error_json(HTTPStatus.TOO_MANY_REQUESTS, "Too many login attempts. Wait and try again.")
            return
        try:
            payload = self._read_json_body()
        except (BadRequest, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid login request.")
            return
        password = str(payload.get("password") or "")
        if not AUTH_ENABLED:
            self._send_json(HTTPStatus.OK, {"ok": True, "authDisabled": True})
            return
        if verify_auth_password(password):
            _clear_login_failures(ip)
            self._send_json_with_cookie(HTTPStatus.OK, {"ok": True}, _make_auth_cookie(), AUTH_TTL_SECONDS)
        else:
            _record_login_failure(ip)
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid password.")

    def _handle_token_dashboard(self, path: str) -> None:
        token = path[len("/api/view/"):].strip("/")
        if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        session_id = validate_share_token(token)
        if not session_id:
            self._send_error_json(HTTPStatus.GONE, "This share link has expired or been revoked.")
            return
        dashboard = cached_reliable_dashboard_for_session(session_id)
        if not isinstance(dashboard, dict):
            with SHARED_DASHBOARD_LOCK:
                if SHARED_DASHBOARD_STATE.get("sessionId") == session_id:
                    candidate = SHARED_DASHBOARD_STATE.get("dashboard")
                    dashboard = candidate if isinstance(candidate, dict) else None
        if not isinstance(dashboard, dict):
            self._send_json(HTTPStatus.OK, {"ok": False, "message": "No dashboard data available for this share link yet."})
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "dashboard": json_clone(dashboard), "updatedAt": dashboard.get("generatedAt", "")},
        )

    def do_GET(self) -> None:
        if not self._require_https():
            return
        self.request_id = uuid.uuid4().hex[:8]
        try:
            path = urlparse(self.path).path

            # --- Always-open routes (no auth) ---
            if path == "/favicon.ico":
                self._send_bytes(HTTPStatus.OK, FAVICON_SVG, "image/svg+xml")
                return
            if path == "/networker-logo.png":
                if NETWORKER_LOGO_PATH.exists():
                    self._send_bytes(HTTPStatus.OK, NETWORKER_LOGO_PATH.read_bytes(), "image/png")
                else:
                    self._send_bytes(HTTPStatus.OK, FAVICON_SVG, "image/svg+xml")
                return
            if path == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "app": APP_NAME,
                        "version": APP_VERSION,
                        "https": True,
                        "debug": APP_DEBUG,
                        "time": datetime.now().astimezone().isoformat(),
                    },
                )
                return

            # --- Token-gated share routes (capability URL, no cookie) ---
            if path.startswith("/view/"):
                token = path[6:].strip("/")
                if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
                    self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
                    return
                session_id = validate_share_token(token)
                if not session_id:
                    self._send_bytes(
                        HTTPStatus.GONE,
                        b"<html><body><p>This share link has expired or been revoked.</p></body></html>",
                        "text/html; charset=utf-8",
                    )
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    read_only_view_html(token).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if path.startswith("/api/view/"):
                self._handle_token_dashboard(path)
                return

            # --- Root: login page when auth required and not authenticated ---
            if path in ("/", "/index.html"):
                if AUTH_ENABLED and not self._authenticated():
                    self._send_bytes(HTTPStatus.OK, login_page_html().encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send_bytes(HTTPStatus.OK, dashboard_html().encode("utf-8"), "text/html; charset=utf-8")
                return

            # --- Everything below requires authentication ---
            if AUTH_ENABLED and not self._authenticated():
                self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required.")
                return

            if path == "/api/status":
                with SHARED_DASHBOARD_LOCK:
                    updated = float(SHARED_DASHBOARD_STATE.get("updatedAt") or 0)
                    last_refresh = SHARED_DASHBOARD_STATE.get("lastRefresh") or ""
                    last_error = SHARED_DASHBOARD_STATE.get("lastError") or ""
                with SSE_CLIENTS_LOCK:
                    sse_count = len(SSE_CLIENTS)
                age = int(time.time() - updated) if updated else None
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "version": APP_VERSION,
                        "uptimeSeconds": int(time.time() - PROCESS_START_TIME),
                        "threads": threading.active_count(),
                        "sessions": len(_session_ids_snapshot()),
                        "automations": len(_automation_keys_snapshot()),
                        "sseClients": sse_count,
                        "sharedDashboard": {
                            "lastRefresh": last_refresh,
                            "lastRefreshAgeSeconds": age,
                            "lastError": last_error,
                        },
                        "authEnabled": AUTH_ENABLED,
                        "allowlistEnabled": ALLOWLIST_ENABLED,
                    },
                )
                return
            if path == "/api/current-dashboard":
                self._send_json(HTTPStatus.OK, shared_dashboard_payload())
                return
            if path == "/api/profiles":
                with PROFILES_LOCK:
                    self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(load_profiles())})
                return
            if path == "/api/email-config":
                self._send_json(HTTPStatus.OK, email_config_public())
                return
            if path == "/api/ui-theme":
                self._send_json(HTTPStatus.OK, {"ok": True, "theme": load_ui_theme() or "default"})
                return
            if path == "/api/stream":
                wfile = self.wfile
                if not _sse_register(wfile):
                    self._send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "Too many live viewers connected. Try again shortly.")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                self.end_headers()
                payload = shared_dashboard_payload()
                dash = payload.get("dashboard")
                if isinstance(dash, dict):
                    try:
                        wfile.write(f"event: dashboard\ndata: {json.dumps(dash, separators=(',', ':'))}\n\n".encode("utf-8"))
                        wfile.flush()
                    except OSError:
                        pass
                while not SHARED_REFRESH_STOP.is_set():
                    try:
                        wfile.write(b": heartbeat\n\n")
                        wfile.flush()
                        SHARED_REFRESH_STOP.wait(25)
                    except OSError:
                        break
                with SSE_CLIENTS_LOCK:
                    try:
                        SSE_CLIENTS.remove(wfile)
                    except ValueError:
                        pass
                return
            if path == "/api/snapshots":
                query = dict(parse_qsl(urlparse(self.path).query, keep_blank_values=True))
                action = query.get("action", "compare")
                if action == "list":
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, {"ok": True, "snapshots": list_snapshot_summary()})
                elif action == "history":
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, snapshot_history_all())
                elif action == "export":
                    with SNAPSHOTS_LOCK:
                        csv_data = snapshots_to_csv()
                    self._send_bytes(HTTPStatus.OK, csv_data.encode("utf-8"), "text/csv; charset=utf-8")
                elif action == "auto-config":
                    self._send_json(HTTPStatus.OK, {"ok": True, "enabled": load_auto_snapshot_config()})
                else:
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, compare_dashboard_snapshots(query.get("range", "7d")))
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        except Exception as exc:  # noqa: BLE001
            ref = getattr(self, "request_id", "-")
            LOG.error(f"do_GET unhandled error: {safe_log_text(exc)}", extra={"request_id": ref}, exc_info=True)
            try:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
            except Exception:  # noqa: BLE001 — headers may already be sent
                pass

    def do_POST(self) -> None:
        if not self._require_https():
            return
        self.request_id = uuid.uuid4().hex[:8]
        path = urlparse(self.path).path
        # Always-open auth endpoints
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._send_json_with_cookie(HTTPStatus.OK, {"ok": True}, "", 0)
            return
        # Auth gate for all other POST routes
        if AUTH_ENABLED and not self._authenticated():
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required.")
            return
        allowed = {"/api/dashboard", "/api/export", "/api/server-health",
                   "/api/alert-automation", "/api/snapshots",
                   "/api/share", "/api/multi-server", "/api/profiles",
                   "/api/ui-theme"}
        if path not in allowed:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return

        try:
            payload = self._read_json_body()
            if path == "/api/ui-theme":
                theme = save_ui_theme(payload.get("theme"))
                self._send_json(HTTPStatus.OK, {"ok": True, "theme": theme})
                return
            if path == "/api/snapshots":
                snap_action = str(payload.get("action") or "save").strip().lower()
                if snap_action == "delete":
                    date_str = str(payload.get("date") or "").strip()
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                        raise BadRequest("Invalid date format. Use YYYY-MM-DD.")
                    with SNAPSHOTS_LOCK:
                        delete_snapshot_by_date(date_str)
                        summaries = list_snapshot_summary()
                    self._send_json(HTTPStatus.OK, {"ok": True, "snapshots": summaries})
                    return
                if snap_action == "annotate":
                    date_str = str(payload.get("date") or "").strip()
                    note = str(payload.get("note") or "").strip()
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                        raise BadRequest("Invalid date format. Use YYYY-MM-DD.")
                    with SNAPSHOTS_LOCK:
                        annotate_snapshot(date_str, note)
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                if snap_action == "auto-config":
                    enabled = bool(payload.get("enabled", False))
                    save_auto_snapshot_config(enabled)
                    # Capture immediately on enable so the first snapshot does not
                    # wait up to 10 minutes for the next worker tick, and report
                    # the outcome so the UI can confirm it actually saved.
                    result = _auto_snapshot_once() if enabled else "disabled"
                    self._send_json(HTTPStatus.OK, {
                        "ok": True,
                        "enabled": enabled,
                        "result": result,
                        "summary": snapshot_summary_text(),
                    })
                    return
                # default: save
                dashboard = payload.get("dashboard") if isinstance(payload.get("dashboard"), dict) else None
                if not dashboard:
                    with SHARED_DASHBOARD_LOCK:
                        dashboard = SHARED_DASHBOARD_STATE.get("dashboard")
                if not isinstance(dashboard, dict):
                    raise BadRequest("No dashboard data is available to save as a snapshot.")
                with SNAPSHOTS_LOCK:
                    record = save_dashboard_snapshot(dashboard)
                    comparison = compare_dashboard_snapshots(payload.get("range", "7d"))
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "message": f"Snapshot saved for {record['date']}.",
                        "snapshot": record,
                        "summary": snapshot_summary_text(),
                        "comparison": comparison,
                    },
                )
                return
            if path == "/api/profiles":
                action = str(payload.get("action") or "").strip().lower()
                with PROFILES_LOCK:
                    profiles = load_profiles()
                    if action == "save":
                        name = str(payload.get("name") or "").strip()
                        data = payload.get("data")
                        if not name:
                            raise BadRequest("Profile name required.")
                        if not isinstance(data, dict):
                            raise BadRequest("Profile data must be an object.")
                        # Encrypt password fields; keep all other fields as-is
                        safe: dict[str, Any] = {}
                        for k, v in data.items():
                            if k in ("password", "wmiPassword"):
                                raw = str(v or "").strip()
                                if raw and raw not in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
                                    safe[f"_enc_{k}"] = encrypt_profile_secret(raw)
                                elif k in profiles.get(name, {}):
                                    # Keep existing encrypted value if no new one provided
                                    safe[f"_enc_{k}"] = profiles[name].get(f"_enc_{k}", "")
                            else:
                                safe[k] = v
                        # Preserve existing encrypted passwords not touched this save
                        existing = profiles.get(name, {})
                        for enc_key in ("_enc_password", "_enc_wmiPassword"):
                            if enc_key not in safe and enc_key in existing:
                                safe[enc_key] = existing[enc_key]
                        profiles[name] = safe
                        save_profiles(profiles)
                        self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(profiles)})
                    elif action == "delete":
                        name = str(payload.get("name") or "").strip()
                        profiles.pop(name, None)
                        save_profiles(profiles)
                        self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(profiles)})
                    else:
                        self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(profiles)})
                return

            if path == "/api/share":
                action = str(payload.get("action") or "").strip().lower()
                if action == "create":
                    session_id = str(payload.get("sessionId") or "").strip()
                    if not session_id or not _session_exists(session_id):
                        raise BadRequest("Valid session ID required to create share link.")
                    token = create_share_token(session_id)
                    self._send_json(HTTPStatus.OK, {"ok": True, "token": token})
                elif action == "revoke":
                    token = str(payload.get("token") or "").strip()
                    revoke_share_token(token)
                    self._send_json(HTTPStatus.OK, {"ok": True})
                else:
                    raise BadRequest("action must be 'create' or 'revoke'.")
                return

            if path == "/api/multi-server":
                raw_ids = payload.get("sessionIds")
                if not isinstance(raw_ids, list) or not raw_ids:
                    raise BadRequest("sessionIds must be a non-empty array.")
                session_ids = [str(s).strip() for s in raw_ids if str(s).strip()]
                ms_status, ms_body = build_multi_server_dashboard(session_ids)
                self._send_json(ms_status, ms_body)
                return

            if path == "/api/alert-automation":
                status, body = handle_alert_automation(payload)
                self._send_json(status, body)
                return

            if path == "/api/server-health":
                session_id = str(payload.get("sessionId") or "").strip()
                if not session_id:
                    raise BadRequest("Dashboard session id is required for server health refresh.")
                status, body = build_server_health_from_session(session_id)
                self._send_json(status, body)
                return

            cached_dashboard = payload.get("dashboard") if path == "/api/export" else None
            if isinstance(cached_dashboard, dict) and cached_dashboard.get("ok"):
                status, body = HTTPStatus.OK, cached_dashboard
            else:
                session_id = str(payload.get("sessionId") or "").strip()
                if session_id and not payload.get("password"):
                    report_range = str(payload.get("reportRange") or DEFAULT_REPORT_RANGE).strip().lower()
                    status, body = build_dashboard_from_session(
                        session_id,
                        report_range,
                        str(payload.get("customStartDate") or "").strip(),
                        str(payload.get("customEndDate") or "").strip(),
                    )
                else:
                    payload = _resolve_profile_password(payload)
                    config = validate_payload(payload)
                    status, body = build_dashboard(config)

            if path == "/api/export":
                if status >= 400:
                    self._send_json(status, body)
                else:
                    report_bytes = build_excel_report(body)
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"networker_dashboard_{body.get('summary', {}).get('range', DEFAULT_REPORT_RANGE)}_{stamp}.xlsx"
                    self._send_bytes(
                        HTTPStatus.OK,
                        report_bytes,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        {"Content-Disposition": f'attachment; filename="{filename}"'},
                    )
            else:
                if path == "/api/dashboard" and status < 400 and body.get("ok"):
                    set_shared_dashboard(str(body.get("sessionId") or session_id or ""), body)
                self._send_json(status, body)
            if status >= 400:
                self.log_dashboard_failure(status, body)
        except BadRequest as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.")
        except Exception as exc:
            ref = getattr(self, "request_id", "-")
            LOG.error(f"do_POST unhandled error: {safe_log_text(exc)}", extra={"request_id": ref}, exc_info=True)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")


def write_embedded_dev_certificate(cert_path: Path, key_path: Path) -> None:
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_text(EMBEDDED_DEV_CERT_PEM, encoding="ascii")
    key_path.write_text(EMBEDDED_DEV_KEY_PEM, encoding="ascii")
    try:
        key_path.chmod(0o600)
    except OSError:
        pass


def ensure_certificate(cert_path: Path, key_path: Path) -> bool:
    if cert_path.exists() and key_path.exists():
        return False

    write_embedded_dev_certificate(cert_path, key_path)
    return True


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, *args: Any, max_connections: int = DEFAULT_MAX_CONNECTIONS, **kwargs: Any) -> None:
        self._conn_semaphore = threading.BoundedSemaphore(max(1, int(max_connections)))
        super().__init__(*args, **kwargs)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def _acquire_slot(self) -> bool:
        return self._conn_semaphore.acquire(blocking=False)

    def _release_slot(self) -> None:
        try:
            self._conn_semaphore.release()
        except ValueError:
            # Over-release means a slot was released without a matching acquire —
            # a logic bug. Log it rather than crash the worker thread.
            debug_log("ExclusiveThreadingHTTPServer._release_slot over-release ignored")

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._acquire_slot():
            # At connection cap — refuse by closing the socket. A fronting
            # proxy/LB will retry; we avoid spawning an unbounded thread.
            self.shutdown_request(request)
            return
        try:
            # super() spawns the worker thread, which releases the slot in
            # process_request_thread's finally. If thread creation itself fails
            # (e.g. OS thread exhaustion), release here so the slot is not leaked.
            super().process_request(request, client_address)
        except Exception:
            self._release_slot()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_slot()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Replace socketserver's raw stderr traceback dump. Client-side transport
        # errors (disconnects, TLS EOF, resets) are benign -> DEBUG; anything else
        # is a real server fault -> ERROR with traceback, as structured JSON.
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):
            LOG.debug(f"client {client_address} connection error: {exc}")
        else:
            LOG.error(f"request error from {client_address}: {exc}", extra={"client": client_address[0] if client_address else "-"}, exc_info=True)


def local_port_probe_hosts(bind_host: str) -> list[str]:
    normalized = (bind_host or "").strip().lower()
    hosts = ["127.0.0.1", "localhost"]
    if normalized in ("", "0.0.0.0", "::"):
        hosts.extend(local_ipv4_addresses())
    elif normalized not in ("127.0.0.1", "localhost", "::1"):
        hosts.append(bind_host)
    seen: set[str] = set()
    result: list[str] = []
    for host in hosts:
        if host and host not in seen:
            seen.add(host)
            result.append(host)
    return result


def port_has_active_listener(bind_host: str, port: int) -> bool:
    for host in local_port_probe_hosts(bind_host):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            continue
    return False


def can_exclusively_bind_port(bind_host: str, port: int) -> bool:
    if port == 0:
        return True
    if port_has_active_listener(bind_host, port):
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((bind_host, port))
        return True
    except OSError:
        return False


def bind_dashboard_server(
    bind_host: str, requested_port: int, max_connections: int = DEFAULT_MAX_CONNECTIONS
) -> tuple[ThreadingHTTPServer, int, bool]:
    if requested_port != 0 and not can_exclusively_bind_port(bind_host, requested_port):
        server = ExclusiveThreadingHTTPServer((bind_host, 0), DashboardHandler, max_connections=max_connections)
        return server, int(server.server_address[1]), True
    try:
        server = ExclusiveThreadingHTTPServer((bind_host, requested_port), DashboardHandler, max_connections=max_connections)
        return server, int(server.server_address[1]), False
    except OSError as exc:
        if requested_port == 0:
            raise
        try:
            server = ExclusiveThreadingHTTPServer((bind_host, 0), DashboardHandler, max_connections=max_connections)
        except OSError:
            raise exc
        return server, int(server.server_address[1]), True


def port_self_test_script_block(bind_host: str, requested_port: int) -> str:
    if requested_port == 0:
        return (
            f"Port self-test script block: request an OS-selected random HTTPS port on {bind_host}; "
            "validate the selected listener before launch."
        )
    return (
        f"Port self-test script block: validate {bind_host}:{requested_port}; "
        "if unavailable, bind to an OS-selected random HTTPS port."
    )


def preferred_launch_url(urls: list[tuple[str, str]]) -> str:
    for label, url in urls:
        if label == "Localhost":
            return url
    return urls[0][1] if urls else ""


def self_test_dashboard_listener(url: str, timeout_seconds: float = 8.0) -> tuple[bool, str]:
    health_url = f"{url.rstrip('/')}/api/health"
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    tls_context = ssl._create_unverified_context()
    last_error = "listener did not respond"

    while time.monotonic() < deadline:
        try:
            request = Request(health_url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=1.5, context=tls_context) as response:
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            if payload.get("ok") is True and payload.get("https") is True:
                port = urlparse(url).port or DEFAULT_PORT
                return True, f"HTTPS listener is healthy on port {port}."
            last_error = f"health endpoint returned unexpected payload: {payload!r}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.2)

    return False, f"HTTPS listener self-test failed at {health_url}: {last_error}"


def auto_launch_dashboard(url: str, enabled: bool = True) -> tuple[bool, str]:
    if not enabled:
        return False, "Auto-launch disabled by --no-launch."
    if not url:
        return False, "No dashboard URL available to launch."
    try:
        opened = webbrowser.open_new_tab(url)
    except Exception as exc:
        return False, f"Unable to auto-launch browser: {exc}"
    if opened:
        return True, f"Opened dashboard page: {url}"
    return False, f"Browser auto-launch was requested but no browser accepted: {url}"


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if address and not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    for name in {socket.gethostname(), socket.getfqdn()}:
        try:
            for family, _, _, _, sockaddr in socket.getaddrinfo(name, None, socket.AF_INET):
                if family == socket.AF_INET:
                    address = sockaddr[0]
                    if address and not address.startswith("127."):
                        addresses.add(address)
        except socket.gaierror:
            continue
    return sorted(addresses)


def service_access_urls(bind_host: str, port: int) -> list[tuple[str, str]]:
    normalized = (bind_host or "").strip().lower()
    urls: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, host: str) -> None:
        url = f"https://{host}:{port}/"
        if url not in seen:
            seen.add(url)
            urls.append((label, url))

    if normalized in ("", "0.0.0.0", "::"):
        add("Localhost", "localhost")
        for address in local_ipv4_addresses():
            add("Local server IP", address)
    elif normalized in ("127.0.0.1", "localhost", "::1"):
        add("Localhost", "localhost")
    else:
        add("Configured bind address", bind_host)
        if not normalized.startswith("127."):
            add("Localhost", "localhost")
    return urls


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Interface to bind. Defaults to 0.0.0.0 (all interfaces, reachable on the server IP for publishing). Use 127.0.0.1 to restrict to local only.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="HTTPS port to listen on. If busy, an available random port is selected.",
    )
    parser.add_argument(
        "--cert",
        default=".certs/networker_dashboard.crt",
        help="TLS certificate path in PEM format.",
    )
    parser.add_argument(
        "--key",
        default=".certs/networker_dashboard.key",
        help="TLS private key path in PEM format.",
    )
    parser.add_argument(
        "--no-auto-cert",
        action="store_true",
        help="Require an existing certificate/key instead of writing the embedded development certificate.",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Do not automatically open the dashboard HTML page in the default browser after startup self-test.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose REST API diagnostics without logging passwords or Authorization headers.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Per-request socket timeout in seconds. Drops idle/slowloris connections.",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=DEFAULT_MAX_CONNECTIONS,
        help="Maximum concurrent connections before new ones are refused.",
    )
    parser.add_argument(
        "--max-sse",
        type=int,
        default=DEFAULT_MAX_SSE_CLIENTS,
        help="Maximum concurrent live-stream (SSE) viewers.",
    )
    parser.add_argument(
        "--auth-password",
        default="",
        help="Dashboard access password. May also be supplied via the DASHBOARD_AUTH_PASSWORD environment variable. Stored only as a salted hash.",
    )
    parser.add_argument(
        "--allowed-hosts",
        default="",
        help="Comma-separated allow-list of NetWorker hosts/IPs/CIDRs the server may connect to (e.g. nw1.corp.local,10.0.0.0/24). May also be set via DASHBOARD_ALLOWED_HOSTS. If unset, any host is permitted.",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    global APP_DEBUG, REQUEST_TIMEOUT_SECONDS, MAX_CONNECTIONS, MAX_SSE_CLIENTS, AUTH_ENABLED
    args = parse_args(argv or sys.argv[1:])
    APP_DEBUG = bool(args.debug)
    configure_logging(APP_DEBUG)
    LOG.info(f"{APP_NAME} {APP_VERSION} starting", extra={"event": "startup"})
    REQUEST_TIMEOUT_SECONDS = max(5, int(args.request_timeout))
    MAX_CONNECTIONS = max(1, int(args.max_connections))
    MAX_SSE_CLIENTS = max(1, int(args.max_sse))
    DashboardHandler.timeout = REQUEST_TIMEOUT_SECONDS
    auth_password = args.auth_password or os.environ.get("DASHBOARD_AUTH_PASSWORD") or ""
    if auth_password:
        set_auth_password(auth_password)
    AUTH_ENABLED = auth_password_configured()
    configure_allowed_hosts(args.allowed_hosts or os.environ.get("DASHBOARD_ALLOWED_HOSTS") or "")
    cert_path = Path(args.cert).expanduser().resolve()
    key_path = Path(args.key).expanduser().resolve()

    used_embedded_cert = False
    if args.no_auto_cert:
        if not cert_path.exists() or not key_path.exists():
            raise SystemExit("Certificate/key missing and --no-auto-cert was provided.")
    else:
        used_embedded_cert = ensure_certificate(cert_path, key_path)

    if not 0 <= int(args.port) <= 65535:
        raise SystemExit("HTTPS port must be between 0 and 65535.")

    print(port_self_test_script_block(args.bind, int(args.port)))
    server, selected_port, used_random_port = bind_dashboard_server(
        args.bind, int(args.port), max_connections=MAX_CONNECTIONS
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    access_urls = service_access_urls(args.bind, selected_port)
    launch_url = preferred_launch_url(access_urls)

    print(f"{APP_NAME} {APP_VERSION}")
    if int(args.port) == 0:
        print(f"Requested automatic random HTTPS port; selected {selected_port}.")
    elif used_random_port:
        print(f"Requested HTTPS port {args.port} is not available; selected {selected_port} instead.")
    else:
        print(f"Requested HTTPS port {selected_port} is available and bound.")
    print("Serving HTTPS only at:")
    for label, url in access_urls:
        print(f"  {label}: {url}")
    if used_embedded_cert:
        print(
            "Using the embedded self-signed development certificate. "
            "Use --cert and --key with --no-auto-cert for production."
        )
    if APP_DEBUG:
        print("Debug logging is enabled. Passwords and Authorization headers are not logged.")
    print("Credentials are encrypted at rest in the data directory and are never stored in plaintext or placed in URLs.")
    if ALLOWLIST_ENABLED:
        print(f"NetWorker host allow-list ENABLED ({len(ALLOWED_HOST_NAMES) + len(ALLOWED_NETWORKS)} entr(y/ies)).")
    else:
        print("NetWorker host allow-list is disabled (the server may connect to any host you enter). Set --allowed-hosts to restrict.")
    if AUTH_ENABLED:
        print("Dashboard authentication is ENABLED (gateway password required).")
        LOG.info("authentication enabled", extra={"event": "auth"})
    elif not _is_loopback_bind(args.bind):
        print("=" * 72)
        print("WARNING: Bound to a non-loopback address with NO authentication.")
        print("Anyone who can reach this port can view all backup data.")
        print("Set DASHBOARD_AUTH_PASSWORD (or --auth-password), or bind to 127.0.0.1.")
        print("=" * 72)
        LOG.warning("exposed to non-loopback with no authentication", extra={"event": "auth"})
    else:
        print("Dashboard authentication is disabled (local loopback bind).")
        LOG.info("authentication disabled (loopback bind)", extra={"event": "auth"})

    def _handle_term(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    for _sig_name in ("SIGTERM", "SIGINT"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _handle_term)
            except (ValueError, OSError, RuntimeError):
                pass

    server_thread = threading.Thread(
        target=server.serve_forever,
        name="networker-dashboard-https",
        daemon=True,
    )
    server_thread.start()
    SHARED_REFRESH_STOP.clear()
    shared_refresh_thread = threading.Thread(
        target=shared_dashboard_refresh_loop,
        name="networker-dashboard-shared-refresh",
        daemon=True,
    )
    shared_refresh_thread.start()

    # Restore sessions from previous run in background — non-blocking
    def _restore_sessions_bg() -> None:
        count = restore_sessions_from_disk()
        if count:
            print(f"Restored {count} dashboard session(s) from previous run.")
            # Re-prime shared dashboard state with first restored session
            with SHARED_DASHBOARD_LOCK:
                ids = _session_ids_snapshot()
                if not SHARED_DASHBOARD_STATE.get("sessionId") and ids:
                    SHARED_DASHBOARD_STATE["sessionId"] = ids[0]
        else:
            print("No previous sessions to restore (connect via browser to begin monitoring).")

    threading.Thread(target=_restore_sessions_bg, name="session-restore", daemon=True).start()
    threading.Thread(target=auto_snapshot_worker, name="auto-snapshot", daemon=True).start()

    try:
        self_test_ok, self_test_message = self_test_dashboard_listener(launch_url)
        print(f"Startup self-test: {self_test_message}")
        if not self_test_ok:
            raise SystemExit("Dashboard HTTPS listener did not pass startup self-test.")

        launch_ok, launch_message = auto_launch_dashboard(launch_url, enabled=not args.no_launch)
        print(launch_message)

        while server_thread.is_alive():
            server_thread.join(1.0)
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        try:
            for automation_id in _automation_keys_snapshot():
                cancel_alert_automation(automation_id)
            SHARED_REFRESH_STOP.set()
            if server_thread.is_alive():
                server.shutdown()
                server_thread.join(3.0)
            shared_refresh_thread.join(3.0)
            server.server_close()
        except Exception:
            pass
        # Daemon worker/background threads may still be mid-write to stderr (via
        # logging) when the interpreter finalizes — a normal shutdown can then
        # deadlock on the stderr buffer lock (Fatal Python error:
        # _enter_buffered_busy). Flush, then exit hard to skip finalization and
        # avoid that race.
        logging.shutdown()
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
