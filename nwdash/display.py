"""Persistent display token and display connection for the no-login TV wall.

The display token is a durable capability (unlike session-scoped share tokens):
whoever holds the /tv/<token> URL sees the read-only wall. The display
connection is a NetWorker credential (password sealed with machine-DPAPI via
report_cred) used to keep the shared dashboard live when no one is logged in."""
from __future__ import annotations

import hmac
import json
import threading
import uuid
from typing import Any

from . import config
from .report_cred import encrypt_credential_password

_LOCK = threading.Lock()


def _read(path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path, data: dict[str, Any]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


# display token
def current_token() -> str:
    with _LOCK:
        return str(_read(config.DISPLAY_TOKEN_FILE).get("token") or "")


def get_or_create_token() -> str:
    with _LOCK:
        tok = str(_read(config.DISPLAY_TOKEN_FILE).get("token") or "")
        if not tok:
            tok = uuid.uuid4().hex
            _write(config.DISPLAY_TOKEN_FILE, {"token": tok})
        return tok


def rotate_token() -> str:
    with _LOCK:
        tok = uuid.uuid4().hex
        _write(config.DISPLAY_TOKEN_FILE, {"token": tok})
        return tok


def revoke_token() -> bool:
    with _LOCK:
        if not _read(config.DISPLAY_TOKEN_FILE).get("token"):
            return False
        _write(config.DISPLAY_TOKEN_FILE, {})
        return True


def validate_token(token: str) -> bool:
    if not token or len(token) != 32:
        return False
    return hmac.compare_digest(token, current_token())


# display connection
def save_connection(cred: dict[str, Any]) -> None:
    """Persist a NetWorker credential with the password sealed (no plaintext)."""
    sealed = {k: v for k, v in cred.items() if k != "password"}
    sealed["encrypted_password"] = encrypt_credential_password(str(cred.get("password") or ""))
    with _LOCK:
        _write(config.DISPLAY_CONNECTION_FILE, sealed)


def load_connection() -> dict[str, Any] | None:
    with _LOCK:
        data = _read(config.DISPLAY_CONNECTION_FILE)
        return data or None


def clear_connection() -> bool:
    with _LOCK:
        if not config.DISPLAY_CONNECTION_FILE.exists():
            return False
        try:
            config.DISPLAY_CONNECTION_FILE.unlink()
            return True
        except OSError:
            return False
