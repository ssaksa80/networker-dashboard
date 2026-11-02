"""Admin console back end: who may open /admin, the admin window, the audit
log, and the reads and writes behind the console's sections.

Modelled on DEDB's admin console, which gates every /admin route on a signed-in
admin role. This app has one gateway password and no user accounts, so the
gate is: signed in, and the password typed again within ADMIN_UNLOCK_SECONDS
(the "admin window", extended by every admin request). With no password set,
the console opens only to a browser on the server itself.

Request plumbing (cookies, CSRF, JSON bodies) stays in server.py; the functions
here take plain values and return (status, body) like handle_alert_automation.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import config as _cfg
from .auth import (
    LOGIN_SESSIONS,
    _clear_login_failures,
    _login_rate_limited,
    _record_login_failure,
    set_auth_password,
    verify_auth_password,
)
from .certs import is_embedded_dev_certificate, server_tls_context
from .config import (
    ADMIN_UNLOCK_SECONDS,
    APP_VERSION,
    AUDIT_FILE,
    AUDIT_MAX_BYTES,
    AUTH_TTL_SECONDS,
    CONSOLE_ALLOWLIST_FILE,
    DATA_DIR,
    HOST_PATTERN,
    LOG,
    LOG_FILE,
    PROCESS_START_TIME,
    SETUP_CODE_FILE,
)
from .models import (
    SHARE_TOKEN_LOCK,
    SHARE_TOKEN_TTL_SECONDS,
    SHARE_TOKENS,
    SHARED_DASHBOARD_LOCK,
    SHARED_DASHBOARD_STATE,
    SSE_CLIENTS,
    SSE_CLIENTS_LOCK,
    _automation_items_snapshot,
    _get_session,
    _session_ids_snapshot,
)
from .profiles import (
    ALLOWED_HOST_NAMES,
    ALLOWED_NETWORKS,
    PROFILES_LOCK,
    _host_allowed,
    configure_allowed_hosts,
    load_profiles,
)
from .snapshots import load_email_config

PASSWORD_MIN_LENGTH = 12
ALLOWLIST_MAX_ENTRIES = 100
LOG_TAIL_BYTES = 512 * 1024
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_AUDIT_LOCK = threading.Lock()


# ── Who may use the console ─────────────────────────────────────────────────

def is_loopback_client(ip: str) -> bool:
    try:
        return ipaddress.ip_address((ip or "").split("%", 1)[0]).is_loopback
    except ValueError:
        return False


# A reverse proxy on this host connects from loopback, so its socket address
# says nothing about where the browser is. Proxies that forward add one of
# these (IIS ARR adds X-ARR-Log-ID and X-Forwarded-For by default); a request
# carrying any of them is never treated as coming from the server itself.
_PROXY_HEADERS = frozenset({
    "forwarded", "via", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "x-forwarded-server", "x-real-ip", "x-client-ip", "x-original-url", "x-arr-log-id",
})


def is_local_request(client_ip: str, headers: Any = None) -> bool:
    """From a browser on the server itself: a loopback peer, not via a proxy.
    A proxy that adds none of the headers above (nginx's default proxy_pass)
    still passes this, which is why no-password access also needs the setup code."""
    if not is_loopback_client(client_ip):
        return False
    names = {str(name).lower() for name in (headers.keys() if headers is not None else ())}
    return not names & _PROXY_HEADERS


def console_access(client_ip: str, authenticated: bool, headers: Any = None) -> str:
    """'ok' (signed in), 'login', 'open' (no password, on the server itself)
    or 'denied' (no password, from another machine or through a proxy)."""
    if _cfg.AUTH_ENABLED:
        return "ok" if authenticated else "login"
    return "open" if is_local_request(client_ip, headers) else "denied"


def admin_window_open(sid: str, setup_code: str = "") -> bool:
    """Admin reads and changes need the window open. With no password there is
    nothing to type again, so the setup code stands in for it: only someone
    who can read the server's data folder has it."""
    if not _cfg.AUTH_ENABLED:
        return setup_code_ok(setup_code)
    return bool(sid) and LOGIN_SESSIONS.admin_until(sid) > 0


def touch_admin_window(sid: str) -> None:
    """Keep the window open for another full period. Called for admin activity
    only (changes, and reads the person asked for), never for background
    refreshes, so an unattended console still locks."""
    if _cfg.AUTH_ENABLED and sid and LOGIN_SESSIONS.admin_until(sid) > 0:
        LOGIN_SESSIONS.unlock_admin(sid, ADMIN_UNLOCK_SECONDS)


def console_state(sid: str, client_ip: str, authenticated: bool, headers: Any = None, setup_code: str = "") -> dict[str, Any]:
    until = LOGIN_SESSIONS.admin_until(sid) if (_cfg.AUTH_ENABLED and sid) else 0.0
    state = {
        "ok": True,
        "access": console_access(client_ip, authenticated, headers),
        "passwordSet": bool(_cfg.AUTH_ENABLED),
        "unlocked": admin_window_open(sid, setup_code),
        "unlockedUntil": int(until),
        "unlockSeconds": ADMIN_UNLOCK_SECONDS,
        "client": client_ip,
        "version": APP_VERSION,
    }
    if not _cfg.AUTH_ENABLED:
        state["setupCodeFile"] = SETUP_CODE_FILE.name
    return state


# ── Setup code (no password set) ────────────────────────────────────────────
# Loopback alone cannot prove the browser is on the server (see _PROXY_HEADERS),
# so with no password the console also asks for this code. run() makes a new
# one at every start and writes it to SETUP_CODE_FILE; setting the first
# password removes it.

_SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread
_SETUP_LOCK = threading.Lock()
_setup_code = ""


def _normalize_code(code: Any) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def prepare_setup_code() -> tuple[str, bool]:
    """Create this boot's setup code and write it to SETUP_CODE_FILE. Returns
    (code, written); when the file cannot be written the caller prints it."""
    global _setup_code
    raw = "".join(secrets.choice(_SETUP_ALPHABET) for _ in range(12))
    with _SETUP_LOCK:
        _setup_code = raw
    code = "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETUP_CODE_FILE.write_text(code + "\n", encoding="ascii")
    except OSError as exc:
        LOG.warning(f"could not write {SETUP_CODE_FILE.name}: {exc}", extra={"event": "auth"})
        return code, False
    return code, True


def setup_code_ok(code: Any) -> bool:
    with _SETUP_LOCK:
        expected = _setup_code
    given = _normalize_code(code)
    return bool(expected and given) and hmac.compare_digest(given, expected)


def clear_setup_code() -> None:
    global _setup_code
    with _SETUP_LOCK:
        _setup_code = ""
    try:
        SETUP_CODE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        LOG.warning(f"could not remove {SETUP_CODE_FILE.name}: {exc}", extra={"event": "auth"})


def setup_code_problem(code: Any, client_ip: str) -> tuple[int, str]:
    """(0, "") when `code` is this boot's setup code, else the status and error
    to answer with. A wrong code counts toward the sign-in lockout."""
    if _login_rate_limited(client_ip):
        return HTTPStatus.TOO_MANY_REQUESTS, "Too many wrong codes. Wait five minutes and try again."
    if setup_code_ok(code):
        return 0, ""
    if _normalize_code(code):  # only a typed code is a guess
        _record_login_failure(client_ip)
        return HTTPStatus.FORBIDDEN, "That setup code is not right."
    return HTTPStatus.FORBIDDEN, f"Enter the setup code from {SETUP_CODE_FILE.name} in the dashboard's data folder on the server."


def unlock(sid: str, password: str, client_ip: str) -> tuple[int, dict[str, Any]]:
    if not _cfg.AUTH_ENABLED:
        return HTTPStatus.CONFLICT, {"ok": False, "error": "No dashboard password is set, so there is nothing to unlock."}
    if _login_rate_limited(client_ip):
        return HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "Too many wrong passwords. Wait five minutes and try again."}
    if not verify_auth_password(password):
        _record_login_failure(client_ip)
        audit("Admin unlock failed", ip=client_ip, sid=sid, item="admin console")
        return HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Wrong password."}
    _clear_login_failures(client_ip)
    until = LOGIN_SESSIONS.unlock_admin(sid, ADMIN_UNLOCK_SECONDS)
    audit("Admin unlocked", ip=client_ip, sid=sid, item="admin console")
    return HTTPStatus.OK, {"ok": True, "unlockedUntil": int(until), "unlockSeconds": ADMIN_UNLOCK_SECONDS}


def lock(sid: str, client_ip: str) -> dict[str, Any]:
    LOGIN_SESSIONS.lock_admin(sid)
    audit("Admin locked", ip=client_ip, sid=sid, item="admin console")
    return {"ok": True}


# ── Audit log ───────────────────────────────────────────────────────────────

def _session_ref(sid: str) -> str:
    """A short, stable name for a login that does not reveal its id."""
    return hashlib.sha256(f"nwdash-session:{sid}".encode()).hexdigest()[:12] if sid else ""


def _clip(value: Any, limit: int = 300) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def audit(action: str, *, ip: str = "", sid: str = "", item: Any = "", field: Any = "", old: Any = "", new: Any = "") -> None:
    """Append one line to the audit log. Never raises: a full disk must not
    stop the change being audited, so a failed write is logged instead."""
    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ip": _clip(ip, 64),
        "session": _session_ref(sid),
        "action": _clip(action, 80),
        "item": _clip(item, 160),
        "field": _clip(field, 80),
        "old": _clip(old),
        "new": _clip(new),
    }
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    with _AUDIT_LOCK:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size >= AUDIT_MAX_BYTES:
                AUDIT_FILE.replace(AUDIT_FILE.with_name(AUDIT_FILE.name + ".1"))
            with AUDIT_FILE.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            LOG.warning(f"audit write failed ({action}): {exc}", extra={"event": "audit"})


def read_audit(date: str = "", action: str = "", query: str = "", limit: int = 500) -> list[dict[str, Any]]:
    """Newest first. `date` is YYYY-MM-DD in the server's local time."""
    rows: list[dict[str, Any]] = []
    with _AUDIT_LOCK:
        for path in (AUDIT_FILE.with_name(AUDIT_FILE.name + ".1"), AUDIT_FILE):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for raw in text.splitlines():
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    rows.append(entry)
    needle = query.strip().lower()
    picked = []
    for entry in reversed(rows):
        if date and not str(entry.get("ts", "")).startswith(date):
            continue
        if action and entry.get("action") != action:
            continue
        if needle and needle not in " ".join(str(v) for v in entry.values()).lower():
            continue
        picked.append(entry)
        if len(picked) >= limit:
            break
    return picked


def audit_actions() -> list[str]:
    return sorted({str(row.get("action") or "") for row in read_audit(limit=5000)} - {""})


# ── Health ──────────────────────────────────────────────────────────────────

def overview() -> dict[str, Any]:
    with SHARED_DASHBOARD_LOCK:
        updated = float(SHARED_DASHBOARD_STATE.get("updatedAt") or 0)
        last_error = str(SHARED_DASHBOARD_STATE.get("lastError") or "")
    with SSE_CLIENTS_LOCK:
        viewers = len(SSE_CLIENTS)
    automations = [automation for _key, automation in _automation_items_snapshot()]
    try:
        disk = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent)
        free_pct = round(disk.free / disk.total * 100, 1) if disk.total else None
    except OSError:
        free_pct = None
    with PROFILES_LOCK:
        profile_count = len(load_profiles())
    return {
        "ok": True,
        "version": APP_VERSION,
        "python": platform.python_version(),
        "embedded": embedded_runtime(),
        "uptimeSeconds": int(time.time() - PROCESS_START_TIME),
        "startedAt": datetime.fromtimestamp(PROCESS_START_TIME).astimezone().isoformat(timespec="seconds"),
        "threads": threading.active_count(),
        "maxConnections": _cfg.MAX_CONNECTIONS,
        "liveViewers": viewers,
        "connectionSessions": len(_session_ids_snapshot()),
        "schedules": {"total": len(automations), "paused": sum(1 for a in automations if not a.enabled)},
        "profiles": profile_count,
        "lastRefreshAgeSeconds": int(time.time() - updated) if updated else None,
        "lastRefreshError": last_error,
        "passwordSet": bool(_cfg.AUTH_ENABLED),
        "allowlist": {"enabled": bool(_cfg.ALLOWLIST_ENABLED), "entries": len(allowlist_entries()), "source": _cfg.ALLOWLIST_SOURCE},
        "tls": tls_summary(),
        "dataFreePercent": free_pct,
        "auditEntriesToday": len(read_audit(date=datetime.now().strftime("%Y-%m-%d"), limit=10000)),
    }


def embedded_runtime() -> bool:
    """True on python.org's embeddable runtime (what the bundle ships): it is
    the one with a pythonXY._pth file beside python.exe."""
    return any(Path(sys.executable).parent.glob("python*._pth"))


def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{int((time.monotonic() - started) * 1000)} ms"
    except OSError as exc:
        return False, str(exc) or exc.__class__.__name__


def run_self_test(probe_listener: Callable[[], tuple[bool, str]]) -> dict[str, Any]:
    """The checks behind the Health tab's self-test. Each step is ok, warn or
    bad; the listener probe is passed in by server.py (it owns the listener)."""
    steps: list[dict[str, str]] = []

    def step(key: str, label: str, state: str, detail: str) -> None:
        steps.append({"id": key, "label": label, "state": state, "detail": _clip(detail, 240)})

    info = tls_info()
    if not info.get("ok"):
        step("cert", "Certificate", "bad", info.get("error") or "The certificate could not be read.")
    else:
        days = info.get("daysLeft")
        state = "warn" if info.get("selfSigned") or (isinstance(days, int) and days < 30) else "ok"
        step("cert", "Certificate", state, f"{info.get('subject') or 'certificate'}, {days} days left"
             + (", self-signed" if info.get("selfSigned") else ""))

    ok, message = probe_listener()
    step("listener", "HTTPS listener", "ok" if ok else "warn", message)

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / f".write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        disk = shutil.disk_usage(DATA_DIR)
        free = disk.free / disk.total * 100 if disk.total else 100.0
        step("data", "Data folder", "warn" if free < 5 else "ok", f"writable, {free:.0f}% free")
    except OSError as exc:
        step("data", "Data folder", "bad", f"not writable: {exc}")

    with PROFILES_LOCK:
        profiles = load_profiles()
    if not profiles:
        step("networker", "NetWorker servers", "warn", "No saved connection profiles to check.")
    else:
        results = []
        worst = "ok"
        for name, record in list(profiles.items())[:5]:
            host = str(record.get("restApiHost") or "")
            try:
                port = int(record.get("restApiPort") or 9090)
            except (TypeError, ValueError):
                port = 9090
            if not host:
                continue
            if not _host_allowed(host):
                results.append(f"{name}: not on the allow-list")
                worst = "warn" if worst == "ok" else worst
                continue
            reachable, detail = _tcp_reachable(host, port)
            results.append(f"{name}: {'reachable in ' + detail if reachable else 'unreachable (' + detail + ')'}")
            if not reachable:
                worst = "bad"
        step("networker", "NetWorker servers", worst, "; ".join(results) or "No hosts in the saved profiles.")

    smtp = load_email_config().get("smtp")
    if not isinstance(smtp, dict) or not smtp.get("host"):
        step("smtp", "Mail server", "warn", "No SMTP server saved.")
    else:
        try:
            smtp_port = int(smtp.get("port") or 587)
        except (TypeError, ValueError):
            smtp_port = 587
        reachable, detail = _tcp_reachable(str(smtp["host"]), smtp_port)
        step("smtp", "Mail server", "ok" if reachable else "bad",
             f"{smtp['host']}:{smtp_port} " + (f"answers in {detail}" if reachable else f"unreachable ({detail})"))

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        step("log", "Log file", "ok", f"{LOG_FILE.name}, {size // 1024} KB")
    except OSError as exc:
        step("log", "Log file", "warn", str(exc))

    states = [s["state"] for s in steps]
    return {
        "ok": True,
        "passed": "bad" not in states,
        "warnings": states.count("warn"),
        "failures": states.count("bad"),
        "steps": steps,
        "finishedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


# ── Sessions ────────────────────────────────────────────────────────────────

def list_sessions(current_sid: str) -> list[dict[str, Any]]:
    rows = []
    for sid, entry in LOGIN_SESSIONS.entries():
        rows.append({
            "id": _session_ref(sid),
            "current": sid == current_sid,
            "ip": entry.get("ip") or "",
            "agent": entry.get("agent") or "",
            "signedIn": int(entry.get("iat") or 0),
            "expires": int(entry.get("exp") or 0),
            "adminOpen": LOGIN_SESSIONS.admin_until(sid) > 0,
        })
    return rows


def revoke_session(ref: str, current_sid: str, client_ip: str) -> tuple[int, dict[str, Any]]:
    for sid, entry in LOGIN_SESSIONS.entries():
        if _session_ref(sid) != ref:
            continue
        if sid == current_sid:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "That is this browser. Use Sign out to end it."}
        LOGIN_SESSIONS.discard(sid)
        audit("Session revoked", ip=client_ip, sid=current_sid, item=entry.get("ip") or ref, field="session", old="active", new="revoked")
        return HTTPStatus.OK, {"ok": True}
    return HTTPStatus.NOT_FOUND, {"ok": False, "error": "That session has already ended."}


def revoke_other_sessions(current_sid: str, client_ip: str) -> dict[str, Any]:
    ended = LOGIN_SESSIONS.discard_all_except(current_sid)
    audit("Sessions revoked", ip=client_ip, sid=current_sid, item="every other session", field="count", old=ended, new=0)
    return {"ok": True, "ended": ended}


# ── Logs ────────────────────────────────────────────────────────────────────

def read_log(level: str = "INFO", query: str = "", lines: int = 300) -> dict[str, Any]:
    minimum = _LOG_LEVELS.get(level.upper(), 20)
    needle = query.strip().lower()
    lines = max(1, min(int(lines or 300), 2000))
    try:
        with LOG_FILE.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return {"ok": True, "file": LOG_FILE.name, "lines": [], "missing": True}
    raw_lines = chunk.splitlines()
    if size > LOG_TAIL_BYTES and raw_lines:
        raw_lines = raw_lines[1:]  # the first line was cut by the seek
    picked: deque[dict[str, Any]] = deque(maxlen=lines)
    for raw in raw_lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):  # a line that is not one of our JSON records
            entry = {"ts": "", "level": "INFO", "msg": raw}
        name = str(entry.get("level") or "INFO").upper()
        if _LOG_LEVELS.get(name, 20) < minimum:
            continue
        message = str(entry.get("msg") or "")
        if entry.get("exc"):
            message = f"{message}\n{entry['exc']}"
        if needle and needle not in message.lower():
            continue
        picked.append({
            "ts": str(entry.get("ts") or ""),
            "level": name,
            "msg": message[:4000],
            "client": str(entry.get("client") or ""),
            "request": str(entry.get("request_id") or ""),
        })
    return {"ok": True, "file": LOG_FILE.name, "lines": list(picked)}


# ── TLS ─────────────────────────────────────────────────────────────────────

TLS_UPLOAD_MAX_CHARS = 64 * 1024
TLS_CHAIN_MAX = 10
_TLS_LOCK = threading.Lock()
_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL)
_CERT_BEGIN = "-----BEGIN CERTIFICATE-----"
_CERT_END = "-----END CERTIFICATE-----"
_PFX_HINT = ("A .pfx or .p12 file has to be converted to PEM first, for example: "
             "openssl pkcs12 -in site.pfx -nokeys -out site.crt, then "
             "openssl pkcs12 -in site.pfx -nocerts -nodes -out site.key.")


def _name_text(name: Any) -> str:
    try:
        return ", ".join(f"{k}={v}" for rdn in name for k, v in rdn)
    except (TypeError, ValueError):
        return ""


def _tls_files() -> tuple[Path | None, Path | None]:
    """The certificate and key files the listener was started with."""
    return (Path(_cfg.TLS_CERT_FILE) if _cfg.TLS_CERT_FILE else None,
            Path(_cfg.TLS_KEY_FILE) if _cfg.TLS_KEY_FILE else None)


def _previous(path: Path) -> Path:
    """Where the pair an upload replaced is kept (one generation)."""
    return path.with_name(path.name + ".previous")


def _read_leaf(path: Path) -> tuple[bytes, dict[str, Any] | None, str]:
    """The first certificate in a PEM file: its DER bytes, what ssl's decoder
    makes of it (None when that private API is missing or fails) and the
    decoder's error. Raises OSError/ValueError when there is no certificate."""
    pem = path.read_text(encoding="ascii", errors="replace")
    begin = pem.index(_CERT_BEGIN)
    end = pem.index(_CERT_END, begin) + len(_CERT_END)
    der = ssl.PEM_cert_to_DER_cert(pem[begin:end])
    decoder = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decoder is None:
        return der, None, ""
    try:
        return der, decoder(str(path)), ""
    except (OSError, ssl.SSLError, ValueError) as exc:
        return der, None, str(exc)


def _describe_certificate(path: Path, now: float | None = None) -> dict[str, Any]:
    """What a certificate file says, for the TLS screen. Read with the standard
    library: ssl's certificate decoder (private, so guarded) plus the PEM
    bytes for the fingerprint. Private keys are never read here."""
    try:
        der, decoded, detail_error = _read_leaf(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"The certificate could not be read: {exc}"}
    digest = hashlib.sha256(der).hexdigest().upper()
    info: dict[str, Any] = {"ok": True, "sha256": ":".join(digest[i:i + 2] for i in range(0, len(digest), 2)),
                            "builtIn": is_embedded_dev_certificate(der)}
    if decoded is None:
        if detail_error:
            info["detailError"] = detail_error
        return info
    subject = decoded.get("subject") or ()
    issuer = decoded.get("issuer") or ()
    info["subject"] = _name_text(subject)
    info["issuer"] = _name_text(issuer)
    info["selfSigned"] = subject == issuer
    info["altNames"] = [f"{kind} {value}" for kind, value in decoded.get("subjectAltName") or ()]
    for key, out in (("notBefore", "validFrom"), ("notAfter", "validTo")):
        if decoded.get(key):
            try:
                info[out] = datetime.fromtimestamp(ssl.cert_time_to_seconds(decoded[key])).astimezone().isoformat(timespec="seconds")
            except ValueError:
                info[out] = decoded[key]
    if decoded.get("notAfter"):
        try:
            info["daysLeft"] = int((ssl.cert_time_to_seconds(decoded["notAfter"]) - (time.time() if now is None else now)) // 86400)
        except ValueError:
            pass
    return info


def tls_info() -> dict[str, Any]:
    """The serving certificate, and the one an upload replaced if it is kept."""
    cert_file, key_file = _tls_files()
    if not cert_file or not cert_file.exists():
        return {"ok": False, "error": "The certificate file is not available."}
    info = _describe_certificate(cert_file)
    if not info["ok"]:
        return info
    info["auto"] = bool(_cfg.TLS_CERT_AUTO) or info.pop("builtIn")
    info["file"] = cert_file.name
    info["keyFile"] = key_file.name if key_file else ""
    info["canReplace"] = bool(key_file)
    if key_file and _previous(cert_file).exists() and _previous(key_file).exists():
        previous = _describe_certificate(_previous(cert_file))
        if previous["ok"]:
            info["previous"] = {key: previous.get(key) for key in ("subject", "issuer", "selfSigned", "validTo", "daysLeft", "sha256")}
    return info


def tls_summary() -> dict[str, Any]:
    info = tls_info()
    return {key: info.get(key) for key in ("ok", "subject", "selfSigned", "daysLeft", "auto")}


def _pem_blocks(text: str) -> list[tuple[str, str]]:
    """(label, PEM) for each block, in order, re-wrapped cleanly. Text around
    the blocks (openssl's "Bag Attributes", comments) is dropped."""
    blocks = []
    for match in _PEM_BLOCK.finditer(text):
        label = match.group(1)
        body = "\n".join(line.strip() for line in match.group(2).splitlines() if line.strip())
        blocks.append((label, f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"))
    return blocks


def parse_certificate_upload(cert_text: Any, key_text: Any) -> tuple[list[str], str]:
    """The certificate chain (server certificate first, as pasted) and the
    private key, as PEM. The key may also be pasted into the certificate box.
    Raises ValueError with a message for the person uploading."""
    cert_text = cert_text if isinstance(cert_text, str) else ""
    key_text = key_text if isinstance(key_text, str) else ""
    if len(cert_text) > TLS_UPLOAD_MAX_CHARS or len(key_text) > TLS_UPLOAD_MAX_CHARS:
        raise ValueError("That is too large for a certificate and key.")
    cert_blocks = _pem_blocks(cert_text)
    chain = [pem for label, pem in cert_blocks if label == "CERTIFICATE"]
    keys = [pem for label, pem in (_pem_blocks(key_text) or cert_blocks) if label.endswith("PRIVATE KEY")]
    if not chain:
        raise ValueError("No certificate found: paste the PEM text that starts with " + _CERT_BEGIN + ". " + _PFX_HINT)
    if len(chain) > TLS_CHAIN_MAX:
        raise ValueError(f"Paste the server certificate and its intermediates only (at most {TLS_CHAIN_MAX}).")
    if not keys:
        raise ValueError("No private key found: paste the PEM text that starts with -----BEGIN PRIVATE KEY-----. " + _PFX_HINT)
    if len(keys) > 1:
        raise ValueError("Paste one private key.")
    key = keys[0]
    if key.startswith("-----BEGIN ENCRYPTED") or "Proc-Type: 4,ENCRYPTED" in key:
        raise ValueError("The private key is protected by a passphrase. The service reads it unattended, so save "
                         "a copy without one first, for example: openssl pkey -in site.key -out site-open.key.")
    if not all(pem.isascii() for pem in [*chain, key]):
        raise ValueError("The PEM text contains characters that cannot be in a certificate or key.")
    return chain, key


def request_host(header: Any) -> str:
    """The host name of a Host header, without the port."""
    text = str(header or "").strip()
    if text.startswith("["):
        return text[1:text.index("]")] if "]" in text else ""
    return text.rsplit(":", 1)[0] if text.count(":") == 1 else text


def certificate_covers(alt_names: Any, host: str) -> bool:
    """Whether a certificate's subjectAltName entries cover `host`, the way
    browsers match: exact names, a wildcard for one left-most label, and IP
    entries for addresses. The subject's CN is ignored, as browsers do."""
    host = host.strip().rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for kind, value in alt_names or ():
        value = str(value).strip().rstrip(".").lower()
        if address is not None:
            if kind == "IP Address":
                try:
                    if ipaddress.ip_address(value) == address:
                        return True
                except ValueError:
                    continue
        elif kind == "DNS":
            if value == host:
                return True
            if value.startswith("*.") and host.partition(".")[2] == value[2:] and host.partition(".")[0]:
                return True
    return False


def _ssl_problem(exc: ssl.SSLError) -> str:
    reason = str(getattr(exc, "reason", "") or "")
    known = {
        "KEY_VALUES_MISMATCH": "The private key does not belong to this certificate.",
        "EE_KEY_TOO_SMALL": "The certificate's key is too short: use an RSA key of 2048 bits or more, or an EC key.",
        "CA_MD_TOO_WEAK": "The certificate is signed with a weak hash (MD5 or SHA-1): ask your CA for a SHA-256 one.",
    }
    return known.get(reason) or f"The certificate and key could not be loaded together ({reason or exc})."


def _staged(path: Path) -> Path:
    return path.with_name(path.name + ".new")


def _local_minute(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).astimezone().strftime("%Y-%m-%d %H:%M")


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="ascii")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _stage_and_load(cert_file: Path, key_file: Path, chain: list[str], key: str) -> tuple[ssl.SSLContext, dict[Path, Path]]:
    """Write the pair next to the live files as *.new and load it, which is
    also the check that the key belongs to the certificate. A chain pasted
    with the server certificate further down is tried in that order too.
    Returns the context and {staged file: live file}; raises ssl.SSLError."""
    staged_cert, staged_key = _staged(cert_file), _staged(key_file)

    def load(order: list[str]) -> tuple[ssl.SSLContext, dict[Path, Path]]:
        if cert_file == key_file:  # one file holding both, as --cert x.pem --key x.pem
            _write_private(staged_cert, "".join(order) + key)
            return server_tls_context(staged_cert, staged_cert), {staged_cert: cert_file}
        _write_private(staged_cert, "".join(order))
        _write_private(staged_key, key)
        return server_tls_context(staged_cert, staged_key), {staged_cert: cert_file, staged_key: key_file}

    # As pasted first, then each other certificate as the server's own.
    orders = [[chain[i], *chain[:i], *chain[i + 1:]] for i in range(len(chain))]
    for order in orders[:-1]:
        try:
            return load(order)
        except ssl.SSLError as exc:
            if getattr(exc, "reason", "") != "KEY_VALUES_MISMATCH":
                raise
    return load(orders[-1])


def _fingerprint_note(info: dict[str, Any]) -> str:
    if not info.get("ok"):
        return "(unreadable)"
    return f"{info.get('subject') or 'certificate'}, SHA-256 {str(info.get('sha256') or '')[:23]}"


def install_certificate(cert_text: Any, key_text: Any, *, host: str = "", confirm_name: bool = False,
                        sid: str = "", client_ip: str = "", apply: Callable[[ssl.SSLContext], bool],
                        now: float | None = None, action: str = "TLS certificate replaced") -> tuple[int, dict[str, Any]]:
    """Replace the serving certificate and key. Checked before anything is
    replaced: PEM only, no passphrase, the key belongs to the certificate, the
    certificate is valid now, and (unless `confirm_name`) it covers `host`, the
    name this console was opened with. The replaced pair is kept as
    *.previous. `apply` gives the new context to the listener, so new
    connections use it at once; the files make it survive a restart."""
    cert_file, key_file = _tls_files()
    if not cert_file or not key_file:
        return HTTPStatus.CONFLICT, {"ok": False, "error": "This service is not serving a certificate from files."}
    try:
        chain, key = parse_certificate_upload(cert_text, key_text)
    except ValueError as exc:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
    now = time.time() if now is None else now
    with _TLS_LOCK:
        try:
            try:
                context, moves = _stage_and_load(cert_file, key_file, chain, key)
            except ssl.SSLError as exc:
                return HTTPStatus.BAD_REQUEST, {"ok": False, "error": _ssl_problem(exc)}
            staged_cert = _staged(cert_file)
            _der, decoded, _err = _read_leaf(staged_cert)
            if decoded is not None:
                try:
                    not_before = ssl.cert_time_to_seconds(decoded["notBefore"])
                    not_after = ssl.cert_time_to_seconds(decoded["notAfter"])
                except (KeyError, ValueError):
                    not_before, not_after = now, now + 1
                if not_after <= now:
                    return HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"This certificate expired on {_local_minute(not_after)}."}
                if not_before > now:
                    return HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"This certificate is not valid until {_local_minute(not_before)}."}
                alt_names = decoded.get("subjectAltName") or ()
                if host and not confirm_name and not certificate_covers(alt_names, host):
                    names = [str(value) for _kind, value in alt_names]
                    return HTTPStatus.CONFLICT, {
                        "ok": False, "code": "name_not_covered", "host": host, "names": names,
                        "error": (f"This certificate is for {', '.join(names) or 'no host names'}, not {host}, the name "
                                  f"this console was opened with. Browsers that use {host} will refuse it."),
                    }
            new_info = _describe_certificate(staged_cert, now)
            old_info = _describe_certificate(cert_file, now) if cert_file.exists() else {"ok": False}
            for live in dict.fromkeys(moves.values()):
                if live.exists():
                    shutil.copyfile(live, _previous(live))
            for staged, live in moves.items():
                os.replace(staged, live)
        except OSError as exc:
            LOG.warning(f"certificate upload failed: {exc}", extra={"event": "tls"})
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"The certificate files could not be written: {exc}"}
        finally:
            for staged in (_staged(cert_file), _staged(key_file)):
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass
        applied = apply(context)
        _cfg.TLS_CERT_AUTO = False
    audit(action, ip=client_ip, sid=sid, item=cert_file.name, field="certificate",
          old=_fingerprint_note(old_info), new=_fingerprint_note(new_info))
    body: dict[str, Any] = {"ok": True, "applied": applied, "tls": tls_info()}
    days = new_info.get("daysLeft")
    if isinstance(days, int) and days < 30:
        body["warning"] = f"It expires in {days} days."
    return HTTPStatus.OK, body


def restore_previous_certificate(*, sid: str = "", client_ip: str = "", apply: Callable[[ssl.SSLContext], bool],
                                 now: float | None = None) -> tuple[int, dict[str, Any]]:
    """Swap back the pair the last upload replaced (the current one is kept as
    *.previous in turn, so this can be undone the same way)."""
    cert_file, key_file = _tls_files()
    if not cert_file or not key_file or not _previous(cert_file).exists() or not _previous(key_file).exists():
        return HTTPStatus.NOT_FOUND, {"ok": False, "error": "No previous certificate is kept."}
    try:
        cert_text = _previous(cert_file).read_text(encoding="ascii", errors="replace")
        key_text = _previous(key_file).read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"The previous certificate could not be read: {exc}"}
    return install_certificate(cert_text, key_text, confirm_name=True, sid=sid, client_ip=client_ip, apply=apply,
                               now=now, action="TLS certificate put back")


# ── Security: password and allow-list ───────────────────────────────────────

def change_password(sid: str, current: str, new: str, client_ip: str) -> tuple[int, dict[str, Any]]:
    if not _cfg.AUTH_ENABLED:
        return HTTPStatus.CONFLICT, {"ok": False, "error": "No password is set yet. Use Set password instead."}
    if _login_rate_limited(client_ip):
        return HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "Too many wrong passwords. Wait five minutes and try again."}
    if not verify_auth_password(current):
        _record_login_failure(client_ip)
        audit("Password change refused", ip=client_ip, sid=sid, item="gateway password", field="current password", new="wrong")
        return HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "The current password is wrong."}
    problem = password_problem(new)
    if problem:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": problem}
    set_auth_password(new)
    ended = LOGIN_SESSIONS.discard_all_except(sid)
    audit("Password changed", ip=client_ip, sid=sid, item="gateway password", field="password", old="(old)", new="(new)")
    body: dict[str, Any] = {"ok": True, "sessionsEnded": ended}
    if _cfg.AUTH_PASSWORD_FROM_STARTUP:
        body["warning"] = ("The service also sets the password at start-up (DASHBOARD_AUTH_PASSWORD), so the "
                           "next restart puts that one back. Remove it from the service environment to keep this one.")
    return HTTPStatus.OK, body


def password_problem(password: str) -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Use at least {PASSWORD_MIN_LENGTH} characters."
    if password != password.strip():
        return "The password cannot start or end with a space."
    return ""


def first_password(password: str, client_ip: str, headers: Any = None, setup_code: Any = "") -> tuple[int, dict[str, Any]]:
    """Set the first gateway password from the server itself, proved by the
    setup code. Sign-in is on for every viewer from now on; the caller issues
    this browser its cookie."""
    if _cfg.AUTH_ENABLED:
        return HTTPStatus.CONFLICT, {"ok": False, "error": "A password is already set."}
    if not is_local_request(client_ip, headers):
        return HTTPStatus.FORBIDDEN, {"ok": False, "error": "Set the first password from the server itself."}
    status, problem = setup_code_problem(setup_code, client_ip)
    if status:
        return status, {"ok": False, "code": "setup_code_required", "error": problem}
    problem = password_problem(password)
    if problem:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": problem}
    set_auth_password(password)
    _cfg.AUTH_ENABLED = True
    clear_setup_code()
    _clear_login_failures(client_ip)
    audit("Password set", ip=client_ip, item="gateway password", field="sign-in", old="off", new="on")
    LOG.info("gateway password set from the admin console; sign-in is now required", extra={"event": "auth"})
    return HTTPStatus.OK, {"ok": True}


def allowlist_entries() -> list[str]:
    return sorted(ALLOWED_HOST_NAMES) + [str(net) for net in ALLOWED_NETWORKS]


def allowlist_view() -> dict[str, Any]:
    return {
        "ok": True,
        "entries": allowlist_entries(),
        "enabled": bool(_cfg.ALLOWLIST_ENABLED),
        "source": _cfg.ALLOWLIST_SOURCE,
        "editable": _cfg.ALLOWLIST_SOURCE != "startup",
    }


def _clean_allowlist(raw: Any) -> tuple[list[str], str]:
    if not isinstance(raw, list):
        return [], "entries must be a list."
    clean: list[str] = []
    for value in raw:
        entry = str(value or "").strip().lower().strip("[]")
        if not entry:
            continue
        try:
            entry = str(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            if len(entry) > 253 or not HOST_PATTERN.match(entry) or ":" in entry:
                return [], f"'{_clip(value, 60)}' is not a host name, IP address or CIDR range."
        if entry not in clean:
            clean.append(entry)
    if len(clean) > ALLOWLIST_MAX_ENTRIES:
        return [], f"The allow-list holds at most {ALLOWLIST_MAX_ENTRIES} entries."
    return clean, ""


def save_allowlist(raw: Any, sid: str, client_ip: str) -> tuple[int, dict[str, Any]]:
    if _cfg.ALLOWLIST_SOURCE == "startup":
        return HTTPStatus.CONFLICT, {"ok": False, "error": (
            "The allow-list is set when the service starts (--allowed-hosts or DASHBOARD_ALLOWED_HOSTS). "
            "Change it there, for example with Setup-NWDash.cmd -AllowedHosts.")}
    entries, problem = _clean_allowlist(raw)
    if problem:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": problem}
    before = allowlist_entries()
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONSOLE_ALLOWLIST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        tmp.replace(CONSOLE_ALLOWLIST_FILE)
    except OSError as exc:
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"The allow-list could not be saved: {exc}"}
    configure_allowed_hosts(",".join(entries))
    _cfg.ALLOWLIST_SOURCE = "console" if entries else "none"
    added = [e for e in entries if e not in before]
    removed = [e for e in before if e not in entries]
    audit("Allow-list changed", ip=client_ip, sid=sid, item="NetWorker hosts",
          field=("added " + ", ".join(added) if added else "") + ("; " if added and removed else "")
          + ("removed " + ", ".join(removed) if removed else ""),
          old=len(before), new=len(entries))
    return HTTPStatus.OK, allowlist_view()


def load_console_allowlist() -> str:
    """The admin console's saved allow-list as the comma string
    configure_allowed_hosts takes, or "" when there is none."""
    try:
        data = json.loads(CONSOLE_ALLOWLIST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    entries, problem = _clean_allowlist(data.get("entries") if isinstance(data, dict) else None)
    if problem:
        LOG.warning(f"ignoring {CONSOLE_ALLOWLIST_FILE.name}: {problem}", extra={"event": "startup"})
        return ""
    return ",".join(entries)


# ── Share links ─────────────────────────────────────────────────────────────

def _share_ref(token: str) -> str:
    return hashlib.sha256(f"nwdash-share:{token}".encode()).hexdigest()[:16]


def list_share_links() -> list[dict[str, Any]]:
    now = time.time()
    with SHARE_TOKEN_LOCK:
        items = list(SHARE_TOKENS.items())
    rows = []
    for token, entry in items:
        created = float(entry.get("created_at") or 0)
        if now - created > SHARE_TOKEN_TTL_SECONDS:
            continue
        session = _get_session(str(entry.get("session_id") or ""))
        rows.append({
            "id": _share_ref(token),
            "hint": f"{token[:4]}…{token[-4:]}",
            "server": session.config.rest_api_host if session else "",
            "created": int(created),
            "expires": int(created + SHARE_TOKEN_TTL_SECONDS),
        })
    return sorted(rows, key=lambda row: -row["created"])


def revoke_share_link(ref: str, sid: str, client_ip: str) -> tuple[int, dict[str, Any]]:
    with SHARE_TOKEN_LOCK:
        token = next((t for t in SHARE_TOKENS if _share_ref(t) == ref), "")
        if token:
            del SHARE_TOKENS[token]
    if not token:
        return HTTPStatus.NOT_FOUND, {"ok": False, "error": "That link has already ended."}
    audit("Share link revoked", ip=client_ip, sid=sid, item=f"{token[:4]}…{token[-4:]}", old="active", new="revoked")
    return HTTPStatus.OK, {"ok": True}


def revoke_all_share_links(sid: str, client_ip: str) -> dict[str, Any]:
    with SHARE_TOKEN_LOCK:
        count = len(SHARE_TOKENS)
        SHARE_TOKENS.clear()
    audit("Share links revoked", ip=client_ip, sid=sid, item="every share link", field="count", old=count, new=0)
    return {"ok": True, "revoked": count}


def session_lifetime_hours() -> float:
    return AUTH_TTL_SECONDS / 3600
