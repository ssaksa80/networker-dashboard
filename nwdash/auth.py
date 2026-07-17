"""Password hashing, auth cookies, CSRF tokens, and login rate limiting.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import base64
import json
import hashlib
import hmac
import os
import threading
import time
from typing import Any

from .config import (
    AUTH_CONFIG_FILE,
    AUTH_TTL_SECONDS,
    DATA_DIR,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    PBKDF2_ITERATIONS,
)
from .secrets import AUTH_SECRET_KEY

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


def _make_csrf_token(cookie_value: str) -> str:
    # Stateless synchronizer token bound to the signed session cookie: an
    # attacker without the HttpOnly cookie value cannot derive it, and a new
    # login (new cookie) automatically rotates it.
    payload, _, _ = cookie_value.rpartition(".")
    return base64.urlsafe_b64encode(
        hmac.new(AUTH_SECRET_KEY, b"csrf:" + payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def _verify_csrf_token(cookie_value: str, token: str) -> bool:
    if not cookie_value or not token:
        return False
    return hmac.compare_digest(_make_csrf_token(cookie_value), token)


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
