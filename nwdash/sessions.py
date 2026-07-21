"""Dashboard session lifecycle: create, persist, restore, build.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import concurrent.futures
import json
import time
import uuid
from http.cookiejar import CookieJar
from dataclasses import replace
from http import HTTPStatus
from typing import Any

from . import config as _cfg
from .config import (
    API_VERSION_CANDIDATES,
    CUSTOM_REPORT_RANGE,
    DATA_DIR,
    DEFAULT_API_PORT,
    DEFAULT_REPORT_RANGE,
    DEFAULT_TIMEOUT_SECONDS,
    REPORT_RANGES,
    SESSION_PERSISTENCE_FILE,
    SESSION_TTL_SECONDS,
    debug_log,
)
from .secrets import (
    WMI_CIPHER,
    decrypt_process_secret,
    decrypt_wmi_password,
    encrypt_process_secret,
    encrypt_wmi_password,
)
from .models import (
    ApiConfig,
    DashboardSession,
    RestApiError,
    _get_session,
    _pop_session,
    _put_session,
    _session_items_snapshot,
    dashboard_backup_source_available,
    generated_at,
    stale_dashboard_from_cache,
)
from .profiles import _host_allowed
from .wmi_health import load_server_health_wmi, unavailable_server_health
from .restapi import (
    build_dashboard_rest,
    build_headers,
    load_server_health_nwui,
    maintenance_backup_status,
    parse_custom_date_window,
    ssl_context_for_api,
)
from .nwui import build_dashboard_nwui, nwui_login, refresh_server_protection_job_nwui

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


def _restore_session_record(session_id: str, rec: dict[str, Any], now: float) -> bool:
    """Re-establish ONE NetWorker session (under its original id) from a
    persisted record — the JSON shape written by _session_to_dict. Shared by
    restore_sessions_from_disk (records from sessions.json) and by
    recreate_session_from_snapshot (an automation's connection snapshot).
    Raises on login/network failure; returns False for unusable records.
    """
    import urllib.request as _urllib_request

    enc_pw = str(rec.get("encrypted_networker_password") or "")
    password = decrypt_process_secret(enc_pw)
    if not password:
        return False  # can't reauth without password

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
        verify_tls=bool(cfg_raw.get("verify_tls", True)),
        use_authc_header=bool(cfg_raw.get("use_authc_header", True)),
    )
    if not config.rest_api_host or not config.username:
        return False
    if _cfg.ALLOWLIST_ENABLED and not (
        _host_allowed(config.rest_api_host)
        and _host_allowed(config.backup_server_host or config.rest_api_host)
    ):
        return False

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
    return True


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
    for session_id, rec in records.items():
        try:
            if now - float(rec.get("last_used", 0)) > SESSION_TTL_SECONDS:
                continue
            if _restore_session_record(session_id, rec, now):
                restored += 1
        except Exception:
            continue

    return restored


def connection_snapshot_for_session(session_id: str) -> dict[str, Any]:
    """JSON-safe connection snapshot of a live session (sanitized config +
    encrypted credentials — the same shape sessions.json stores). Captured onto
    email automations at schedule time so they can outlive the session. Returns
    {} when the session does not exist."""
    session = _get_session(session_id)
    if not session:
        return {}
    return _session_to_dict(session_id, session)


def recreate_session_from_snapshot(session_id: str, snapshot: dict[str, Any]) -> bool:
    """Recreate a dashboard session under its ORIGINAL id from the connection
    snapshot an automation captured at schedule time. Used at fire time when
    the session no longer exists (restart, TTL expiry). Returns True when the
    NetWorker login succeeded and the session is registered again."""
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    try:
        if _restore_session_record(session_id, snapshot, time.time()):
            persist_sessions()
            return True
    except Exception as exc:  # login/network failure — caller keeps the schedule armed
        debug_log(f"recreate_session_from_snapshot: {session_id[:8]}… failed: {exc}")
    return False


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
    # NOTE: expiring a session deliberately does NOT cancel its email
    # automations any more — automations carry their own connection snapshot
    # and recreate a session at fire time (see emailer.run_alert_automation).
    now = time.time()
    stale = [
        session_id
        for session_id, session in _session_items_snapshot()
        if now - session.last_used > SESSION_TTL_SECONDS
    ]
    for session_id in stale:
        _pop_session(session_id)
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


def latest_session_record() -> dict[str, Any] | None:
    """Newest usable connection record for self-healing schedules: prefer the
    most-recently-used LIVE session; else the newest record persisted in
    sessions.json; else None. Both sources share the _session_to_dict shape,
    which is exactly what automation connection snapshots store."""
    from . import config as _config
    items = _session_items_snapshot()
    if items:
        session_id, session = max(items, key=lambda kv: float(getattr(kv[1], "last_used", 0) or 0))
        return _session_to_dict(session_id, session)
    try:
        records = json.loads(_config.SESSION_PERSISTENCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(records, dict) or not records:
        return None
    best = max(records.values(), key=lambda r: float((r or {}).get("last_used") or 0) if isinstance(r, dict) else 0)
    return best if isinstance(best, dict) and best.get("config") else None


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
