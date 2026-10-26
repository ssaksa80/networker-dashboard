"""Password hashing, auth cookies, CSRF tokens, and login rate limiting.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any

from .config import (
    AUTH_CONFIG_FILE,
    AUTH_SESSIONS_FILE,
    AUTH_TTL_SECONDS,
    DATA_DIR,
    LOG,
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


class _LoginSessions:
    """Session ids of the logins that are still valid, with their expiry.

    The signed cookie alone stayed valid for its whole TTL after logout: the
    server only told the browser to drop it, so a copied cookie kept working.
    Each login now puts a random id in the signed payload, a cookie is accepted
    only while its id is listed here, and logout removes the id. The table is
    persisted in data/ so a restart does not sign everyone out, and entries are
    pruned once they expire.

    Each entry also keeps when and from where the login happened, for the admin
    console's Sessions list; files written before that hold a bare expiry. The
    admin window (the password typed again for admin changes) is kept in
    memory only, so a restart closes it.
    """

    MAX_ENTRIES = 1000  # oldest logins are dropped beyond this

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._table: dict[str, dict[str, Any]] | None = None  # loaded on first use
        self._admin_until: dict[str, float] = {}

    def _live(self) -> dict[str, dict[str, Any]]:
        """The pruned table, loading it on first use. Caller holds the lock."""
        if self._table is None:
            self._table = {}
            try:
                raw = json.loads(AUTH_SESSIONS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                for key, value in raw.items():
                    entry = _session_entry(value)
                    if entry:
                        self._table[str(key)] = entry
        now = int(time.time())
        for sid in [sid for sid, entry in self._table.items() if entry["exp"] <= now]:
            del self._table[sid]
            self._admin_until.pop(sid, None)
        return self._table

    def _save(self, table: dict[str, dict[str, Any]]) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = AUTH_SESSIONS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(table), encoding="utf-8")
            tmp.replace(AUTH_SESSIONS_FILE)
        except OSError as exc:
            # The in-memory table still holds until restart; after one, a logout
            # that was not saved would be undone.
            LOG.warning(f"could not write {AUTH_SESSIONS_FILE.name}: {exc}", extra={"event": "auth"})

    def add(self, sid: str, exp: int, ip: str = "", agent: str = "") -> None:
        with self._lock:
            table = self._live()
            table[sid] = {"exp": int(exp), "iat": int(time.time()), "ip": ip[:64], "agent": agent[:160]}
            while len(table) > self.MAX_ENTRIES:
                del table[min(table, key=lambda key: table[key]["exp"])]
            self._save(table)

    def __contains__(self, sid: str) -> bool:
        with self._lock:
            return bool(sid) and sid in self._live()

    def discard(self, sid: str) -> bool:
        with self._lock:
            table = self._live()
            self._admin_until.pop(sid, None)
            if table.pop(sid, None) is None:
                return False
            self._save(table)
            return True

    def discard_all_except(self, keep_sid: str) -> int:
        """End every login but `keep_sid`; returns how many were ended."""
        with self._lock:
            table = self._live()
            ended = [sid for sid in table if sid != keep_sid]
            for sid in ended:
                del table[sid]
                self._admin_until.pop(sid, None)
            if ended:
                self._save(table)
            return len(ended)

    def entries(self) -> list[tuple[str, dict[str, Any]]]:
        """(session id, entry) for every live login, newest first."""
        with self._lock:
            table = self._live()
            return sorted(((sid, dict(entry)) for sid, entry in table.items()), key=lambda item: -item[1]["iat"])

    def unlock_admin(self, sid: str, seconds: int) -> float:
        """Open (or extend) this login's admin window; returns when it closes."""
        with self._lock:
            if sid not in self._live():
                return 0.0
            until = time.time() + seconds
            self._admin_until[sid] = until
            return until

    def admin_until(self, sid: str) -> float:
        """When this login's admin window closes, or 0 when it is closed."""
        with self._lock:
            until = self._admin_until.get(sid, 0.0)
            if until <= time.time() or sid not in self._live():
                self._admin_until.pop(sid, None)
                return 0.0
            return until

    def lock_admin(self, sid: str) -> None:
        with self._lock:
            self._admin_until.pop(sid, None)


def _session_entry(value: Any) -> dict[str, Any] | None:
    """A stored login as a dict; older files hold the bare expiry number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return {"exp": int(value), "iat": 0, "ip": "", "agent": ""}
    if isinstance(value, dict) and isinstance(value.get("exp"), (int, float)):
        return {
            "exp": int(value["exp"]),
            "iat": int(value.get("iat") or 0),
            "ip": str(value.get("ip") or "")[:64],
            "agent": str(value.get("agent") or "")[:160],
        }
    return None


LOGIN_SESSIONS = _LoginSessions()


def _sign(payload: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(AUTH_SECRET_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def _make_auth_cookie(ip: str = "", agent: str = "") -> str:
    now = int(time.time())
    sid = os.urandom(16).hex()
    exp = now + AUTH_TTL_SECONDS
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": now, "exp": exp, "sid": sid}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    LOGIN_SESSIONS.add(sid, exp, ip, agent)
    return f"{payload}.{_sign(payload)}"


def _signed_cookie_payload(value: str) -> dict[str, Any] | None:
    """The payload of a cookie this server signed, or None."""
    if not value or "." not in value:
        return None
    payload, _, sig = value.rpartition(".")
    if not payload or not sig:
        return None
    try:
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError):  # bad base64/JSON/UTF-8, or a non-ASCII cookie
        return None
    return data if isinstance(data, dict) else None


def _verify_auth_cookie(value: str) -> bool:
    data = _signed_cookie_payload(value)
    if data is None:
        return False
    try:
        if int(data.get("exp", 0)) <= int(time.time()):
            return False
    except (TypeError, ValueError):
        return False
    # Cookies issued before session ids existed carry none: a one-time re-login.
    return str(data.get("sid") or "") in LOGIN_SESSIONS


def session_id_of_cookie(value: str) -> str:
    """The session id of a live, correctly signed cookie, or ""."""
    if not _verify_auth_cookie(value):
        return ""
    data = _signed_cookie_payload(value) or {}
    return str(data.get("sid") or "")


def revoke_auth_cookie(value: str) -> bool:
    """Logout: forget this cookie's session id, so every copy of the cookie
    stops working, not only the one the browser deletes. True when a live
    session was ended."""
    data = _signed_cookie_payload(value)
    if data is None:
        return False
    return LOGIN_SESSIONS.discard(str(data.get("sid") or ""))


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
