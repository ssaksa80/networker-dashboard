"""Dataclasses, session/automation/SSE registries, shared dashboard state.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.cookiejar import CookieJar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import config as _cfg
from .config import DATA_DIR, LAST_GOOD_DASHBOARD_FILE, SHARED_REFRESH_SECONDS, debug_log, safe_log_text

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
    enabled: bool = True
    quiet_start: str = ""
    quiet_end: str = ""
    digest: bool = True
    last_run: float = 0.0
    last_result: str = "Scheduled"
    last_signature: str = ""
    timer: threading.Timer | None = None
    next_run_at: float = 0.0  # epoch seconds; driven by the scheduler loop
    # Name of the saved email profile this schedule was armed FROM (empty for
    # schedules armed directly from the form). Persisted and restored so a
    # profile's ON/OFF state — which is DERIVED from whether a matching
    # automation exists — survives restarts. Additive/backward compatible:
    # legacy automations.json records simply restore with "".
    profile_name: str = ""
    # Connection snapshot captured at schedule time (same JSON shape that
    # sessions.py persists to sessions.json: sanitized config + encrypted
    # credentials). Lets the automation recreate its dashboard session at fire
    # time so schedules survive restarts, session TTL expiry, and new browser
    # sessions. Empty dict for legacy records saved before this field existed —
    # those wait until a matching session appears again.
    connection: dict[str, Any] = field(default_factory=dict)


ALERT_AUTOMATIONS: dict[str, AlertAutomation] = {}

# One reentrant lock guards both global registries. Reentrant so nested calls
# (stop-all -> cancel_session_automations -> cancel_alert_automation) cannot
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


def _sse_register(wfile: Any) -> bool:
    """Register an SSE client if under cap. Returns False when full."""
    with SSE_CLIENTS_LOCK:
        if len(SSE_CLIENTS) >= _cfg.MAX_SSE_CLIENTS:
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
        except Exception as exc:
            debug_log(f"SSE broadcast of stale dashboard failed: {exc}")
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
    except Exception as exc:
        debug_log(f"SSE broadcast of shared dashboard failed: {exc}")


def shared_dashboard_payload() -> dict[str, Any]:
    from .snapshots import snapshot_summary_text  # late import: avoids circular module import
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
    from .sessions import build_dashboard_from_session, reauthenticate_dashboard_session, session_config_with_secrets  # late import: avoids circular module import
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




def generated_at() -> str:
    return datetime.now().astimezone().strftime("%d-%m-%Y %H:%M:%S %Z")




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
