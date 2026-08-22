"""Snapshots, UI theme, auto-snapshot worker, and email configuration.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import email.utils
import json
import re
import threading
from datetime import datetime, timedelta
from typing import Any

from .config import (
    ALERT_AUTOMATION_MAX_INTERVAL_MINUTES,
    ALERT_AUTOMATION_MIN_INTERVAL_MINUTES,
    AUTO_SNAPSHOT_FILE,
    DASHBOARD_SNAPSHOT_FILE,
    DATA_DIR,
    DEFAULT_REPORT_RANGE,
    EMAIL_CONFIG_FILE,
    EMAIL_PROFILES_FILE,
    HOST_PATTERN,
    THEME_PALETTES,
    TIME_HHMM_PATTERN,
    UI_PREFS_FILE,
    _PROFILE_PW_SAVED,
    _PROFILE_PW_SENTINEL,
    debug_log,
)
from .secrets import (
    decrypt_process_secret,
    decrypt_profile_secret,
    encrypt_process_secret,
    encrypt_profile_secret,
)
from .models import BadRequest, SHARED_DASHBOARD_LOCK, SHARED_DASHBOARD_STATE, SHARED_REFRESH_STOP
from .profiles import SNAPSHOTS_LOCK, parse_port

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
    quiet_start = str(payload.get("quietStart") or "").strip()
    quiet_end = str(payload.get("quietEnd") or "").strip()
    for label, value in (("Quiet start", quiet_start), ("Quiet end", quiet_end)):
        if value and not TIME_HHMM_PATTERN.match(value):
            raise BadRequest(f"{label} must be HH:MM.")
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
        "quiet_start": quiet_start,
        "quiet_end": quiet_end,
        "digest": bool(payload.get("digest", False)),
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


# ── Named email notification profiles ───────────────────────────────────────
# data/email_profiles.json: {name: {smtpHost, smtpPort, ..., _enc_smtpPassword}}
# — the same field set the Email Alert Automation modal submits (validated via
# parse_smtp_settings). The SMTP password is stored encrypted with the shared
# profile-secret helpers and NEVER returned by the API: masked responses carry
# the "(saved)" sentinel instead, and submitting that sentinel back means
# "keep the stored password" (same convention as connection profiles).
EMAIL_PROFILES_LOCK = threading.Lock()


def clean_email_profile_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise BadRequest("Email profile name is required.")
    if len(name) > 64:
        raise BadRequest("Email profile name must be 64 characters or fewer.")
    if any(ch in name for ch in "\r\n\t"):
        raise BadRequest("Email profile name contains unsupported characters.")
    return name


def load_email_profiles() -> dict[str, Any]:
    try:
        raw = json.loads(EMAIL_PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_email_profiles(profiles: dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = EMAIL_PROFILES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, separators=(",", ":")), encoding="utf-8")
        tmp.replace(EMAIL_PROFILES_FILE)
    except OSError:
        pass


def _mask_email_profile(profile: dict[str, Any]) -> dict[str, Any]:
    # Underscore-prefixed keys are server-side secrets/snapshots and NEVER
    # leave the API: _enc_smtpPassword (encrypted SMTP password) and
    # _connection (encrypted NetWorker connection snapshot).
    masked = {k: v for k, v in profile.items() if not str(k).startswith("_")}
    masked["smtpPassword"] = _PROFILE_PW_SAVED if profile.get("_enc_smtpPassword") else ""
    return masked


def mask_email_profiles(profiles: dict[str, Any]) -> dict[str, Any]:
    return {
        name: _mask_email_profile(profile)
        for name, profile in sorted(profiles.items())
        if isinstance(profile, dict)
    }


def save_email_profile(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist one named profile; returns ALL profiles masked."""
    settings = parse_smtp_settings(payload)
    with EMAIL_PROFILES_LOCK:
        profiles = load_email_profiles()
        existing = profiles.get(name) if isinstance(profiles.get(name), dict) else {}
        raw_password = settings["smtp_password"]
        if raw_password and raw_password not in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
            encrypted = encrypt_profile_secret(raw_password, name=name, field="smtpPassword")
        else:
            # Blank or sentinel password on save keeps the stored one.
            encrypted = str(existing.get("_enc_smtpPassword") or "")
        profiles[name] = {
            "smtpHost": settings["smtp_host"],
            "smtpPort": settings["smtp_port"],
            "smtpSecurity": settings["smtp_security"],
            "smtpUsername": settings["smtp_username"],
            "smtpFrom": settings["smtp_from"],
            "smtpTo": "; ".join(settings["recipients"]),
            "intervalMinutes": settings["interval_minutes"],
            "trigger": settings["trigger"],
            "scheduleType": settings["schedule_type"],
            "reportTime": settings["report_time"],
            "theme": settings["theme"],
            "quietStart": settings["quiet_start"],
            "quietEnd": settings["quiet_end"],
            "digest": settings["digest"],
            "_enc_smtpPassword": encrypted,
            # Re-saving keeps the previously captured connection snapshot; the
            # caller refreshes it separately when a live session is available
            # (set_email_profile_connection).
            "_connection": existing.get("_connection") if isinstance(existing.get("_connection"), dict) else {},
        }
        write_email_profiles(profiles)
        return mask_email_profiles(profiles)


def delete_email_profile(name: str) -> dict[str, Any]:
    with EMAIL_PROFILES_LOCK:
        profiles = load_email_profiles()
        profiles.pop(name, None)
        write_email_profiles(profiles)
        return mask_email_profiles(profiles)


def get_email_profile_raw(name: str) -> dict[str, Any] | None:
    """Full stored record (INCLUDING _enc_smtpPassword/_connection) for
    server-side use only — arming a schedule from a profile. Never return
    this from the API; use the masked variants for that."""
    with EMAIL_PROFILES_LOCK:
        profile = load_email_profiles().get(name)
    return dict(profile) if isinstance(profile, dict) else None


def set_email_profile_connection(name: str, connection: dict[str, Any]) -> None:
    """Store/refresh the encrypted NetWorker connection snapshot captured for
    a profile so toggling it ON can arm a restart-proof schedule even when no
    live dashboard session exists."""
    if not isinstance(connection, dict) or not connection:
        return
    with EMAIL_PROFILES_LOCK:
        profiles = load_email_profiles()
        profile = profiles.get(name)
        if isinstance(profile, dict) and profile.get("_connection") != connection:
            profile["_connection"] = connection
            write_email_profiles(profiles)


def get_email_profile_masked(name: str) -> dict[str, Any] | None:
    with EMAIL_PROFILES_LOCK:
        profiles = load_email_profiles()
    profile = profiles.get(name)
    return _mask_email_profile(profile) if isinstance(profile, dict) else None


def resolve_email_profile_password(payload: dict[str, Any]) -> dict[str, Any]:
    """When the form submits the "(saved)" sentinel (a loaded email profile),
    swap in the profile's stored SMTP password before parsing. Falls back to
    empty (→ existing automation / saved email-config password) when there is
    no matching stored secret."""
    password = str(payload.get("smtpPassword") or "")
    if password not in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
        return payload
    stored = ""
    name = str(payload.get("emailProfileName") or "").strip()
    if name:
        with EMAIL_PROFILES_LOCK:
            profiles = load_email_profiles()
        profile = profiles.get(name)
        if isinstance(profile, dict):
            stored = decrypt_profile_secret(
                str(profile.get("_enc_smtpPassword") or ""), name=name, field="smtpPassword"
            )
    result = dict(payload)
    result["smtpPassword"] = stored
    return result


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
