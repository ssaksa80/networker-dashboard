"""SMTP delivery, quiet hours, and alert automation scheduling.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import email.utils
import json
import hashlib
import smtplib
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from typing import Any

from .config import APP_NAME, AUTOMATIONS_FILE, DATA_DIR, DEFAULT_API_PORT, LOG, debug_log, safe_log_text
from .secrets import WMI_CIPHER, decrypt_process_secret, decrypt_profile_secret, encrypt_process_secret
from .models import (
    AlertAutomation,
    BadRequest,
    SHARED_REFRESH_STOP,
    SmtpDeliveryError,
    _automation_items_snapshot,
    _get_automation,
    _pop_automation,
    _put_automation,
    _session_exists,
    active_automation_summary,
    automation_key,
    dashboard_backup_source_available,
    dashboard_backup_source_error,
    existing_smtp_automation,
    generated_at,
    session_automation_keys,
    shared_reliable_dashboard_for_session,
)
from .sessions import (
    build_dashboard_from_session,
    connection_snapshot_for_session,
    recreate_session_from_snapshot,
)
from .snapshots import (
    clean_email_profile_name,
    delete_email_profile,
    get_email_profile_masked,
    get_email_profile_raw,
    load_email_config,
    load_email_profiles,
    load_ui_theme,
    mask_email_profiles,
    parse_email_recipients,
    parse_smtp_settings,
    resolve_email_profile_password,
    save_email_config_from_payload,
    save_email_profile,
    saved_email_smtp_password,
    set_email_profile_connection,
)
from .reports import dashboard_alert_lines, dashboard_report_email, render_dashboard_snapshot_png

def within_quiet_hours(start: str, end: str, hhmm: str | None = None) -> bool:
    """True when the current time (HH:MM) falls inside [start, end).
    Empty start/end disables quiet hours. Windows may wrap past midnight
    (start > end), e.g. 22:00->06:00."""
    start = (start or "").strip()
    end = (end or "").strip()
    if not start or not end:
        return False
    try:
        if hhmm is None:
            hhmm = datetime.now().astimezone().strftime("%H:%M")
        def _mins(v: str) -> int:
            h, m = (int(p) for p in v.split(":", 1))
            return h * 60 + m
        now_m, s_m, e_m = _mins(hhmm), _mins(start), _mins(end)
    except Exception:
        return False
    if s_m == e_m:
        return False
    if s_m < e_m:
        return s_m <= now_m < e_m
    return now_m >= s_m or now_m < e_m


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


def automation_identity(
    schedule_type: str,
    recipients: list[str] | tuple[str, ...],
    smtp_host: str,
    smtp_from: str,
) -> tuple[str, frozenset[str], str, str]:
    """Stable identity of a notification schedule: WHAT it sends and TO WHOM.
    Session ids and automation ids deliberately do not participate — the same
    report type to the same recipients through the same SMTP server/sender is
    the SAME schedule no matter which browser session created it. Recipient
    order and letter case never change the identity."""
    return (
        str(schedule_type or "").strip().lower(),
        frozenset(str(r).strip().lower() for r in (recipients or []) if str(r).strip()),
        str(smtp_host or "").strip().lower(),
        str(smtp_from or "").strip().lower(),
    )


def automation_identity_of(automation: AlertAutomation) -> tuple[str, frozenset[str], str, str]:
    return automation_identity(
        automation.schedule_type,
        automation.recipients,
        automation.smtp_host,
        automation.smtp_from,
    )


def find_automations_by_identity(
    identity: tuple[str, frozenset[str], str, str],
) -> list[AlertAutomation]:
    """All registered automations whose identity matches — across ALL sessions,
    live or dead. Used to replace (never add to) same-identity schedules."""
    return [
        automation
        for _key, automation in _automation_items_snapshot()
        if automation_identity_of(automation) == identity
    ]


# ── Profile-driven schedules (ON/OFF toggle per saved email profile) ─────────
# A profile's enabled state is DERIVED, never stored: it is "on" iff a live
# automation exists whose profile_name matches OR whose identity equals the
# profile's identity. Toggling ON arms a schedule from the STORED profile via
# the same identity-replace path the Schedule button uses; toggling OFF cancels
# every matching schedule.


def _profile_identity(profile: dict[str, Any]) -> tuple[str, frozenset[str], str, str] | None:
    """Identity of the schedule a stored profile would arm, or None when the
    stored record cannot yield a valid recipient list (corrupt/legacy)."""
    try:
        recipients = parse_email_recipients(profile.get("smtpTo"))
    except BadRequest:
        return None
    return automation_identity(
        str(profile.get("scheduleType") or "alert"),
        recipients,
        str(profile.get("smtpHost") or ""),
        str(profile.get("smtpFrom") or ""),
    )


def _profile_automations(name: str, profile: dict[str, Any] | None) -> list[AlertAutomation]:
    """Every live automation that belongs to this profile: stamped with its
    name, or matching its identity (covers schedules armed from the form with
    the same type/recipients/server/sender)."""
    identity = _profile_identity(profile) if isinstance(profile, dict) else None
    return [
        automation
        for _key, automation in _automation_items_snapshot()
        if automation.profile_name == name
        or (identity is not None and automation_identity_of(automation) == identity)
    ]


def _profile_form_payload(profile: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the SMTP form payload from a stored profile record so arming a
    profile flows through parse_smtp_settings — the exact validation pathway
    the modal submit uses (no duplicated parsing)."""
    return {
        "smtpHost": profile.get("smtpHost"),
        "smtpPort": profile.get("smtpPort"),
        "smtpSecurity": profile.get("smtpSecurity"),
        "smtpUsername": profile.get("smtpUsername"),
        "smtpPassword": "",  # decrypted separately from _enc_smtpPassword
        "smtpFrom": profile.get("smtpFrom"),
        "smtpTo": profile.get("smtpTo"),
        "intervalMinutes": profile.get("intervalMinutes"),
        "trigger": profile.get("trigger"),
        "scheduleType": profile.get("scheduleType"),
        "reportTime": profile.get("reportTime"),
        "theme": profile.get("theme"),
        "quietStart": profile.get("quietStart"),
        "quietEnd": profile.get("quietEnd"),
        "digest": bool(profile.get("digest", False)),
    }


def _email_profile_state() -> dict[str, Any]:
    """Masked profiles plus the DERIVED per-profile schedule state the cards
    render: enabled (a matching automation is armed) and its automationId."""
    profiles = load_email_profiles()
    masked = mask_email_profiles(profiles)
    for name, masked_profile in masked.items():
        matches = _profile_automations(name, profiles.get(name))
        masked_profile["enabled"] = bool(matches)
        masked_profile["automationId"] = matches[0].automation_id if matches else ""
    return masked


def _arm_profile_automation(name: str, profile: dict[str, Any], session_id: str) -> AlertAutomation:
    """Arm (or replace) the schedule a stored profile describes, stamped with
    the profile's name. Identity-replace semantics: every same-identity or
    same-profile schedule is cancelled first, so toggling/re-saving never
    accumulates duplicates."""
    settings = parse_smtp_settings(_profile_form_payload(profile))
    matches = _profile_automations(name, profile)
    existing = matches[0] if matches else None
    smtp_password = (
        decrypt_profile_secret(str(profile.get("_enc_smtpPassword") or ""))
        or (decrypt_process_secret(existing.encrypted_smtp_password) if existing else "")
        or saved_email_smtp_password()
    )
    resolved_session = session_id or (existing.session_id if existing else "") or uuid.uuid4().hex
    stored_connection = profile.get("_connection") if isinstance(profile.get("_connection"), dict) else {}
    connection = (
        connection_snapshot_for_session(resolved_session)
        or dict(stored_connection)
        or (dict(existing.connection) if existing and existing.connection else {})
    )
    automation = AlertAutomation(
        automation_id=f"{resolved_session}:{uuid.uuid4().hex[:8]}",
        session_id=resolved_session,
        connection=connection,
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
        quiet_start=settings["quiet_start"],
        quiet_end=settings["quiet_end"],
        digest=settings["digest"],
        profile_name=name,
    )
    for stale in matches + find_automations_by_identity(automation_identity_of(automation)):
        if stale.automation_id != automation.automation_id:
            debug_log(
                f"toggle-profile: replacing schedule {stale.automation_id} "
                f"with {automation.automation_id} (profile {name!r})"
            )
        cancel_alert_automation(stale.automation_id)
    _put_automation(automation.automation_id, automation)
    schedule_alert_automation(automation)
    persist_automations()
    return automation


# ── Cross-automation duplicate-send guard (belt & braces) ────────────────────
# Two schedules with the same identity must never both deliver the same
# content, even if a duplicate automation somehow exists in the registry. Keyed
# by identity (not automation id) so EVERY sender shares one dedup slot.
# In-memory only (per process): restore-time dedup already heals persisted
# duplicates, this guard covers the running process.
_SEND_GUARD_LOCK = threading.Lock()
_LAST_SENT_BY_IDENTITY: dict[tuple[str, frozenset[str], str, str], tuple[str, float]] = {}


def _duplicate_send_suppressed(automation: AlertAutomation, signature: str) -> str | None:
    """Return the formatted local time of a recent identical send by ANY
    automation with this identity (caller must suppress), else None."""
    if not signature:
        return None
    window_seconds = max(automation.interval_minutes * 60, 600)
    with _SEND_GUARD_LOCK:
        previous = _LAST_SENT_BY_IDENTITY.get(automation_identity_of(automation))
    if previous and previous[0] == signature and (time.time() - previous[1]) < window_seconds:
        return datetime.fromtimestamp(previous[1]).astimezone().strftime("%d-%m-%Y %H:%M:%S %Z")
    return None


def _record_identity_send(automation: AlertAutomation, signature: str) -> None:
    with _SEND_GUARD_LOCK:
        _LAST_SENT_BY_IDENTITY[automation_identity_of(automation)] = (str(signature or ""), time.time())


def seconds_until_daily_report(report_time: str, now: datetime | None = None) -> float:
    now = now or datetime.now().astimezone()
    hour, minute = (int(part) for part in report_time.split(":", 1))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def schedule_alert_automation(automation: AlertAutomation) -> None:
    """Compute the automation's next fire time. A single background scheduler
    loop (automation_scheduler_loop) drives the actual firing — far more robust
    than a per-automation long-lived threading.Timer, which could be lost on
    restart, drift, or fail silently."""
    if _get_automation(automation.automation_id) is None:
        return
    delay = (
        seconds_until_daily_report(automation.report_time)
        if automation.schedule_type == "daily_report"
        else automation.interval_minutes * 60
    )
    automation.next_run_at = time.time() + delay
    target = datetime.now().astimezone() + timedelta(seconds=delay)
    debug_log(
        f"automation scheduled id={automation.automation_id} "
        f"type={automation.schedule_type} delaySeconds={int(delay)} "
        f"nextRun={target.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )


AUTOMATION_TICK_SECONDS = 30


def _fire_automation_async(automation_id: str) -> None:
    threading.Thread(
        target=run_alert_automation, args=(automation_id,),
        name=f"automation-fire-{automation_id[:12]}", daemon=True,
    ).start()


def automation_scheduler_tick() -> None:
    # Automations deliberately OUTLIVE their dashboard session: each carries a
    # connection snapshot and recreates a session at fire time (see
    # run_alert_automation). A missing session must never cancel a schedule —
    # that was the "saved schedules vanish after restart" bug. The modal lists
    # ALL schedules (not per-session), so nothing here is invisible.
    now = time.time()
    for key, automation in _automation_items_snapshot():
        nxt = float(getattr(automation, "next_run_at", 0) or 0)
        if nxt and now >= nxt:
            # Push next_run_at forward to avoid a double-fire while this run is in
            # flight; run_alert_automation reschedules the real next time when done.
            automation.next_run_at = now + max(60, automation.interval_minutes * 60)
            debug_log(f"automation firing id={key} type={automation.schedule_type}")
            _fire_automation_async(key)


def automation_scheduler_loop() -> None:
    while not SHARED_REFRESH_STOP.wait(AUTOMATION_TICK_SECONDS):
        try:
            automation_scheduler_tick()
        except Exception as exc:  # noqa: BLE001 — loop must never die.
            debug_log(f"automation_scheduler_loop iteration failed: {exc}")


def _automation_to_dict(automation: AlertAutomation) -> dict[str, Any]:
    return {
        "automation_id": automation.automation_id,
        "session_id": automation.session_id,
        "smtp_host": automation.smtp_host,
        "smtp_port": automation.smtp_port,
        "smtp_username": automation.smtp_username,
        "encrypted_smtp_password": automation.encrypted_smtp_password,
        "smtp_from": automation.smtp_from,
        "recipients": list(automation.recipients),
        "smtp_security": automation.smtp_security,
        "interval_minutes": automation.interval_minutes,
        "trigger": automation.trigger,
        "schedule_type": automation.schedule_type,
        "report_time": automation.report_time,
        "created_at": automation.created_at,
        "theme": automation.theme,
        "last_signature": automation.last_signature,
        "enabled": automation.enabled,
        "quiet_start": automation.quiet_start,
        "quiet_end": automation.quiet_end,
        "digest": automation.digest,
        "connection": automation.connection if isinstance(automation.connection, dict) else {},
        "profile_name": automation.profile_name,
    }


def persist_automations() -> None:
    """Write scheduled automations to disk so they survive a restart. Without a
    stable encryption key the SMTP password cannot be persisted safely, so we
    skip persistence entirely (matches the session-persistence policy)."""
    if not WMI_CIPHER:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = {key: _automation_to_dict(a) for key, a in _automation_items_snapshot()}
        tmp = AUTOMATIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
        tmp.replace(AUTOMATIONS_FILE)
    except (OSError, TypeError, ValueError):
        pass


def _sync_automation_from_email_config(automation: AlertAutomation, cfg: dict[str, Any]) -> bool:
    """Overwrite a CONFIG-DRIVEN schedule's settings from email_config.json —
    the single source of truth for schedules not armed from a named profile.
    Fields the config file does not store for that type (quiet hours, digest,
    the other type's cadence fields) keep the automation's current values.
    Identity fields (automation_id, session_id, connection, last_signature,
    enabled) are never touched. Returns True when anything changed. No config
    entry for the type, or an invalid one, keeps the schedule as-is — config
    deletion is not schedule deletion (the Stop button is)."""
    types = cfg.get("types") if isinstance(cfg.get("types"), dict) else {}
    entry = types.get(automation.schedule_type)
    if not isinstance(entry, dict):
        debug_log(
            f"reconcile: no saved email config for type {automation.schedule_type!r}; "
            f"schedule {automation.automation_id} kept as-is"
        )
        return False
    smtp = cfg.get("smtp") if isinstance(cfg.get("smtp"), dict) else {}

    def _entry(key: str, current: Any) -> Any:
        value = entry.get(key)
        return current if value is None else value

    payload = {
        "smtpHost": str(smtp.get("host") or ""),
        "smtpPort": smtp.get("port"),
        "smtpSecurity": str(smtp.get("security") or "starttls"),
        "smtpUsername": str(smtp.get("username") or ""),
        "smtpPassword": "",
        "smtpFrom": str(smtp.get("from") or ""),
        "smtpTo": ", ".join(str(r) for r in (entry.get("recipients") or [])),
        "intervalMinutes": _entry("interval_minutes", automation.interval_minutes),
        "trigger": _entry("trigger", automation.trigger),
        "scheduleType": automation.schedule_type,
        "reportTime": _entry("report_time", automation.report_time),
        "theme": _entry("theme", automation.theme),
        "quietStart": _entry("quiet_start", automation.quiet_start),
        "quietEnd": _entry("quiet_end", automation.quiet_end),
        "digest": bool(_entry("digest", automation.digest)),
    }
    try:
        settings = parse_smtp_settings(payload)
    except BadRequest as exc:
        debug_log(
            f"reconcile: could not sync schedule {automation.automation_id} "
            f"from email config: {exc}"
        )
        return False
    changed = False
    for attr in (
        "smtp_host", "smtp_port", "smtp_security", "smtp_username", "smtp_from",
        "recipients", "interval_minutes", "trigger", "report_time", "theme",
        "quiet_start", "quiet_end", "digest",
    ):
        if getattr(automation, attr) != settings[attr]:
            setattr(automation, attr, settings[attr])
            changed = True
    if not decrypt_process_secret(automation.encrypted_smtp_password):
        saved_password = saved_email_smtp_password()
        if saved_password:
            automation.encrypted_smtp_password = encrypt_process_secret(saved_password)
            changed = True
    if changed:
        debug_log(
            f"reconcile: synced {automation.schedule_type} schedule "
            f"{automation.automation_id} from email_config.json"
        )
        # The cadence may have moved (report_time/interval) — recompute.
        schedule_alert_automation(automation)
    return changed


def _reconcile_config_driven_automations() -> tuple[int, int]:
    """Enforce the email-config contract on CONFIG-DRIVEN schedules (empty
    profile_name — includes every legacy record): email_config.json holds
    exactly ONE configuration per schedule type, so at most ONE such schedule
    may exist per type, and its settings must match the saved config.
    Recipients drift over time, so the identity dedup (which keys on
    recipients) cannot catch these — a stale schedule with yesterday's
    recipient list has a different identity and would keep firing. Keeps the
    NEWEST schedule per type, drops the rest, then syncs each survivor from
    the config file. Profile-driven schedules are untouched: multiple named
    profiles may be ON concurrently by design. Returns (dropped, synced)."""
    grouped: dict[str, list[AlertAutomation]] = {}
    for _key, automation in _automation_items_snapshot():
        if not automation.profile_name:
            grouped.setdefault(
                str(automation.schedule_type or "").strip().lower(), []
            ).append(automation)
    dropped = 0
    synced = 0
    cfg = load_email_config()
    for schedule_type, group in grouped.items():
        group.sort(key=lambda a: a.created_at)
        keeper = group[-1]
        for stale in group[:-1]:
            if cancel_alert_automation(stale.automation_id):
                dropped += 1
                LOG.warning(
                    f"reconcile: dropped stale {schedule_type} schedule "
                    f"{stale.automation_id} superseded by {keeper.automation_id}"
                )
        if _sync_automation_from_email_config(keeper, cfg):
            synced += 1
    return dropped, synced


def restore_automations_from_disk() -> int:
    """Recreate and reschedule ALL automations saved by a previous run —
    regardless of whether their dashboard session still exists. Sessions are
    recreated lazily at fire time from each automation's connection snapshot
    (run_alert_automation), so restore no longer depends on session restore.
    Legacy records without a snapshot are kept too: they stay scheduled and
    wait until a matching session appears again."""
    if not WMI_CIPHER or not AUTOMATIONS_FILE.exists():
        return 0
    try:
        records = json.loads(AUTOMATIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(records, dict):
        return 0
    restored = 0
    dropped = 0
    for aid, rec in records.items():
        try:
            session_id = str(rec.get("session_id") or "")
            if not session_id:
                dropped += 1
                continue
            connection = rec.get("connection")
            automation = AlertAutomation(
                automation_id=str(rec.get("automation_id") or aid),
                session_id=session_id,
                connection=connection if isinstance(connection, dict) else {},
                smtp_host=str(rec.get("smtp_host") or ""),
                smtp_port=int(rec.get("smtp_port") or DEFAULT_API_PORT),
                smtp_username=str(rec.get("smtp_username") or ""),
                encrypted_smtp_password=str(rec.get("encrypted_smtp_password") or ""),
                smtp_from=str(rec.get("smtp_from") or ""),
                recipients=[str(r) for r in (rec.get("recipients") or [])],
                smtp_security=str(rec.get("smtp_security") or "starttls"),
                interval_minutes=int(rec.get("interval_minutes") or 60),
                trigger=str(rec.get("trigger") or "critical"),
                schedule_type=str(rec.get("schedule_type") or "alert"),
                report_time=str(rec.get("report_time") or "08:00"),
                created_at=float(rec.get("created_at") or time.time()),
                theme=str(rec.get("theme") or "default"),
                last_signature=str(rec.get("last_signature") or ""),
                enabled=bool(rec.get("enabled", True)),
                quiet_start=str(rec.get("quiet_start") or ""),
                quiet_end=str(rec.get("quiet_end") or ""),
                digest=bool(rec.get("digest", True)),
                profile_name=str(rec.get("profile_name") or ""),
            )
            _put_automation(automation.automation_id, automation)
            schedule_alert_automation(automation)
            restored += 1
        except (TypeError, ValueError, KeyError) as exc:
            dropped += 1
            debug_log(f"restore_automations: skipped {aid}: {exc}")
    # Migration/healing: earlier versions could accumulate several schedules
    # with the same identity (same type + recipients + SMTP server/sender),
    # each of which fired — the "duplicate notification" bug. Keep only the
    # newest per identity and drop the rest, from memory AND from disk.
    duplicates_dropped = 0
    grouped: dict[tuple[str, frozenset[str], str, str], list[AlertAutomation]] = {}
    for _key, automation in _automation_items_snapshot():
        grouped.setdefault(automation_identity_of(automation), []).append(automation)
    for group in grouped.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda a: a.created_at)
        keeper = group[-1]
        for stale in group[:-1]:
            if cancel_alert_automation(stale.automation_id):
                duplicates_dropped += 1
                restored = max(0, restored - 1)
                LOG.warning(
                    f"restore_automations: dropped duplicate schedule {stale.automation_id} "
                    f"superseded by {keeper.automation_id}"
                )
    # Second healing pass: config-driven schedules (no profile_name) follow
    # email_config.json — one schedule per type, settings synced from the
    # config file. Catches stale duplicates whose RECIPIENTS drifted, which
    # the identity dedup above deliberately treats as distinct schedules.
    reconcile_dropped, reconcile_synced = _reconcile_config_driven_automations()
    restored = max(0, restored - reconcile_dropped)
    # Rewrite the file from the surviving in-memory store so invalid/corrupt/
    # duplicate/stale records are pruned from disk, not just ignored — one
    # write for the combined result of both healing passes.
    if dropped or duplicates_dropped or reconcile_dropped or reconcile_synced:
        if dropped:
            debug_log(f"restore_automations: pruned {dropped} invalid record(s) from {AUTOMATIONS_FILE.name}")
        persist_automations()
    return restored


def scheduled_dashboard_email_payload(dashboard: dict[str, Any]) -> tuple[str, str, dict[str, tuple[bytes, str, str]]]:
    snapshot_png = render_dashboard_snapshot_png(dashboard)
    attachments: dict[str, tuple[bytes, str, str]] = {}
    plain, html_body = dashboard_report_email(dashboard)
    if snapshot_png:
        attachments["networker-dashboard.png"] = (snapshot_png, "image/png", "networker-dashboard.png")
    return plain, html_body, attachments


def _dashboard_content_signature(dashboard: dict[str, Any]) -> str:
    """Stable hash of meaningful dashboard fields (no timestamps), used for
    daily-report dedup so an unchanged dashboard is not re-emailed."""
    try:
        summary = dashboard.get("summary") if isinstance(dashboard.get("summary"), dict) else {}
        tables = dashboard.get("tables") if isinstance(dashboard.get("tables"), dict) else {}
        keys = ("totalJobs", "successfulJobs", "failedJobs", "activeJobs",
                "recoveryJobs", "cloneJobs", "totalAlerts", "slaPercent", "health", "range")
        payload: dict[str, Any] = {"summary": {k: summary.get(k) for k in keys}}

        def _job_key(j: Any) -> tuple[str, str, str]:
            if not isinstance(j, dict):
                return ("", "", "")
            return (str(j.get("name") or ""), str(j.get("client") or ""), str(j.get("status") or ""))

        def _alert_key(a: Any) -> tuple[str, str]:
            if not isinstance(a, dict):
                return ("", "")
            return (str(a.get("message") or ""), str(a.get("severity") or ""))

        payload["jobs"] = sorted(_job_key(j) for j in (tables.get("jobs") or []))
        payload["failedJobs"] = sorted(_job_key(j) for j in (tables.get("failedJobs") or []))
        payload["alerts"] = sorted(_alert_key(a) for a in (tables.get("alerts") or []))
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha1(blob).hexdigest()
    except Exception:
        return ""


def alert_email_subject(severity: str, digest: bool = True) -> str:
    label = (severity or "alert").title()
    if digest:
        return f"NetWorker dashboard alert: {label}"
    return f"NetWorker dashboard alert (single): {label}"


# Automations whose connection snapshot can no longer be decrypted (DPAPI key
# regenerated for a different Windows account) warn ONCE per process, then stay
# scheduled-but-inert until re-saved. Fail soft, visible.
_SNAPSHOT_DECRYPT_WARNED: set[str] = set()


def _ensure_automation_session(automation: AlertAutomation) -> bool:
    """Make sure the automation's dashboard session exists before a run,
    recreating it (under the original session id) from the automation's stored
    connection snapshot when needed. NEVER cancels the automation — on any
    failure the run is skipped and the schedule stays armed for the next tick."""
    if _session_exists(automation.session_id):
        return True
    snapshot = automation.connection if isinstance(automation.connection, dict) else {}
    if not snapshot:
        # Legacy automations.json record from before connection snapshots
        # existed: it cannot reconnect on its own, so it waits (still scheduled)
        # until a matching session appears again.
        automation.last_result = (
            f"Waiting for a dashboard session at {generated_at()} — this schedule was saved by an "
            "older version; reconnect and re-save it to make it restart-proof."
        )
        debug_log(f"automation {automation.automation_id}: session gone and no connection snapshot; run skipped")
        return False
    if not decrypt_process_secret(str(snapshot.get("encrypted_networker_password") or "")):
        if automation.automation_id not in _SNAPSHOT_DECRYPT_WARNED:
            _SNAPSHOT_DECRYPT_WARNED.add(automation.automation_id)
            LOG.warning(
                f"Email automation {automation.automation_id} ({automation.schedule_type}) cannot decrypt its "
                "saved NetWorker credentials (encryption key regenerated?). It remains scheduled but inert "
                "until it is re-saved from the Email Alert Automation dialog."
            )
        automation.last_result = (
            f"Saved connection credentials are unreadable ({generated_at()}); re-save this schedule."
        )
        return False
    if recreate_session_from_snapshot(automation.session_id, snapshot):
        debug_log(f"automation {automation.automation_id}: recreated dashboard session from stored connection")
        return True
    automation.last_result = (
        f"Skipped at {generated_at()}: could not reconnect to NetWorker with the saved connection; will retry."
    )
    LOG.warning(
        f"Email automation {automation.automation_id} could not recreate its NetWorker session; "
        "run skipped, schedule kept."
    )
    return False


def run_alert_automation(automation_id: str) -> None:
    automation = _get_automation(automation_id)
    if not automation:
        debug_log(f"run_alert_automation: {automation_id} not found (cancelled?)")
        return
    debug_log(
        f"run_alert_automation start id={automation_id} type={automation.schedule_type} "
        f"session={automation.session_id[:8]}… recipients={len(automation.recipients)}"
    )
    try:
        if not automation.enabled:
            automation.last_result = "Paused"
            return
        if not _ensure_automation_session(automation):
            return  # run skipped; finally-block reschedules the next attempt
        status, dashboard = build_dashboard_from_session(automation.session_id)
        debug_log(f"run_alert_automation build status={status} id={automation_id}")
        if status != HTTPStatus.OK:
            raise RuntimeError(dashboard.get("error") or f"Dashboard session refresh failed (HTTP {status}).")
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
            new_signature = _dashboard_content_signature(dashboard)
            if new_signature and new_signature == automation.last_signature:
                automation.last_result = (
                    f"Skipped at {generated_at()}: no change since last successful report"
                )
                automation.last_run = time.time()
                return
            duplicate_sent_at = _duplicate_send_suppressed(automation, new_signature)
            if duplicate_sent_at:
                automation.last_result = (
                    f"Duplicate suppressed (same alert already sent at {duplicate_sent_at})"
                )
                automation.last_run = time.time()
                return
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
            _record_identity_send(automation, new_signature)
            automation.last_signature = new_signature or (dashboard.get("generatedAt") or generated_at())
            automation.last_result = (
                f"Sent daily backup/SLA report at {generated_at()} "
                f"via {smtp_debug.get('host')}:{smtp_debug.get('port')}"
            )
            automation.last_run = time.time()
            return
        if within_quiet_hours(automation.quiet_start, automation.quiet_end):
            automation.last_result = f"Quiet hours: suppressed at {generated_at()}"
            automation.last_run = time.time()
            return
        severity, lines = dashboard_alert_lines(dashboard)
        signature = "|".join(lines)
        cooldown_ok = (time.time() - automation.last_run) >= (automation.interval_minutes * 60) if automation.last_run else True
        if should_send_alert(automation.trigger, severity) and signature != automation.last_signature and cooldown_ok:
            duplicate_sent_at = _duplicate_send_suppressed(automation, signature)
            if duplicate_sent_at:
                automation.last_result = (
                    f"Duplicate suppressed (same alert already sent at {duplicate_sent_at})"
                )
                automation.last_run = time.time()
                return
            subject = alert_email_subject(severity, automation.digest)
            alert_password = decrypt_process_secret(automation.encrypted_smtp_password)
            smtp_debug = send_smtp_email(
                automation,
                subject,
                "\n".join(lines),
                alert_password,
            ) or smtp_debug_snapshot(automation, alert_password, "sent")
            _record_identity_send(automation, signature)
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
        LOG.warning(f"Scheduled email automation {automation_id} SMTP failure: {exc.stage}: {exc.detail}")
    except Exception as exc:  # noqa: BLE001
        automation.last_result = f"Alert automation failed: {exc}"
        LOG.warning(f"Scheduled email automation {automation_id} failed: {safe_log_text(exc, 300)}")
    finally:
        debug_log(f"run_alert_automation done id={automation_id} result={safe_log_text(automation.last_result, 200)}")
        # Recompute the next fire time (the scheduler loop drives the actual run).
        schedule_alert_automation(automation)
        persist_automations()


def handle_alert_automation(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    action = str(payload.get("action") or "").strip().lower()
    session_id = str(payload.get("sessionId") or "").strip()
    _smtp_to_raw = payload.get("smtpTo")
    try:
        _recip_count = len(parse_email_recipients(_smtp_to_raw))
    except BadRequest:
        _recip_count = 0
    debug_log(
        f"alert-automation request action={action!r} "
        f"scheduleType={str(payload.get('scheduleType') or '')!r} "
        f"session={session_id[:8]}… recipients={_recip_count}"
    )
    # ── Named email notification profiles (form presets, stored on disk) ────
    if action == "save-profile":
        name = clean_email_profile_name(payload.get("profileName"))
        was_on = bool(_profile_automations(name, get_email_profile_raw(name)))
        save_email_profile(name, payload)
        # Capture a connection snapshot for the profile: live session first,
        # else inherit one from an automation that belongs to this profile.
        connection = connection_snapshot_for_session(session_id) if session_id else {}
        profile = get_email_profile_raw(name) or {}
        if not connection:
            for automation in _profile_automations(name, profile):
                if automation.connection:
                    connection = dict(automation.connection)
                    break
        if connection:
            set_email_profile_connection(name, connection)
            profile["_connection"] = connection
        message = f"Email profile '{name}' saved."
        if was_on:
            # Re-saving a profile that is ON keeps it on: re-arm the schedule
            # from the NEW settings (identity-replace swaps the old one out).
            _arm_profile_automation(name, profile, session_id)
            message = f"Email profile '{name}' saved and its schedule updated."
        return HTTPStatus.OK, {
            "ok": True,
            "message": message,
            "profiles": _email_profile_state(),
        }
    if action == "list-profiles":
        return HTTPStatus.OK, {"ok": True, "profiles": _email_profile_state()}
    if action == "toggle-profile":
        name = clean_email_profile_name(payload.get("profileName"))
        profile = get_email_profile_raw(name)
        if profile is None:
            raise BadRequest(f"No saved email profile named '{name}'.")
        if not bool(payload.get("enabled")):
            stopped = 0
            for automation in _profile_automations(name, profile):
                if cancel_alert_automation(automation.automation_id):
                    stopped += 1
            if stopped:
                persist_automations()
            return HTTPStatus.OK, {
                "ok": True,
                "enabled": False,
                "profileName": name,
                "automationId": "",
                "message": (
                    f"Profile '{name}' switched off — stopped {stopped} schedule(s)."
                    if stopped else f"Profile '{name}' is off (nothing was scheduled)."
                ),
                "profiles": _email_profile_state(),
            }
        automation = _arm_profile_automation(name, profile, session_id)
        # Keep the freshest connection snapshot on the profile so future
        # toggles (e.g. after a restart, with no live session) still arm a
        # reconnectable schedule.
        set_email_profile_connection(name, automation.connection)
        cadence = (
            f"daily at {automation.report_time}"
            if automation.schedule_type == "daily_report"
            else f"every {automation.interval_minutes} minute(s)"
        )
        return HTTPStatus.OK, {
            "ok": True,
            "enabled": True,
            "profileName": name,
            "automationId": automation.automation_id,
            "message": f"Profile '{name}' switched on — {cadence} to {len(automation.recipients)} recipient(s).",
            "schedule": {
                "automationId": automation.automation_id,
                "scheduleType": automation.schedule_type,
                "recipients": ", ".join(automation.recipients or []),
                "intervalMinutes": automation.interval_minutes,
                "reportTime": automation.report_time,
                "profileName": automation.profile_name,
                "reconnectable": bool(automation.connection),
            },
            "profiles": _email_profile_state(),
        }
    if action == "get-profile":
        name = clean_email_profile_name(payload.get("profileName"))
        profile = get_email_profile_masked(name)
        if profile is None:
            raise BadRequest(f"No saved email profile named '{name}'.")
        return HTTPStatus.OK, {"ok": True, "name": name, "profile": profile}
    if action == "delete-profile":
        name = clean_email_profile_name(payload.get("profileName"))
        # Deliberately does NOT stop schedules: a running automation stays
        # visible/manageable in the Saved schedules list (same behavior as
        # before profile toggles existed).
        delete_email_profile(name)
        return HTTPStatus.OK, {
            "ok": True,
            "message": f"Email profile '{name}' deleted.",
            "profiles": _email_profile_state(),
        }
    # A form loaded from a profile submits the "(saved)" password sentinel;
    # swap in the stored profile password before any SMTP parsing below.
    payload = resolve_email_profile_password(payload)
    if action == "list":
        # Deliberately lists ALL schedules, not just the requesting session's:
        # automations outlive sessions now, and a schedule that is running in
        # the background must never be invisible/unmanageable in the UI.
        rows = []
        for _key, automation in _automation_items_snapshot():
            rows.append({
                "automationId": automation.automation_id,
                "scheduleType": automation.schedule_type,
                "recipients": ", ".join(automation.recipients or []),
                "intervalMinutes": automation.interval_minutes,
                "reportTime": automation.report_time,
                "trigger": automation.trigger,
                "theme": automation.theme,
                "smtpHost": automation.smtp_host,
                "smtpPort": automation.smtp_port,
                "smtpSecurity": automation.smtp_security,
                "smtpUsername": automation.smtp_username,
                "smtpFrom": automation.smtp_from,
                "lastResult": automation.last_result,
                "lastRun": automation.last_run,
                "enabled": automation.enabled,
                "nextRunAt": getattr(automation, "next_run_at", 0.0),
                "quietStart": automation.quiet_start,
                "quietEnd": automation.quiet_end,
                "digest": automation.digest,
                "sessionId": automation.session_id,
                "sessionLive": _session_exists(automation.session_id),
                "reconnectable": bool(automation.connection),
                "profileName": automation.profile_name,
            })
        return HTTPStatus.OK, {"ok": True, "schedules": rows}
    if action == "set_enabled":
        requested_id = str(payload.get("automationId") or "").strip()
        target = _get_automation(requested_id) if requested_id else None
        if not target:
            return HTTPStatus.OK, {"ok": True, "message": "No such schedule."}
        target.enabled = bool(payload.get("enabled"))
        return HTTPStatus.OK, {
            "ok": True,
            "automationId": requested_id,
            "enabled": target.enabled,
            "message": "Schedule resumed." if target.enabled else "Schedule paused.",
        }
    if action == "stop":
        requested_id = str(payload.get("automationId") or "").strip()
        if requested_id:
            target = _get_automation(requested_id)
            if target:
                cancel_alert_automation(requested_id)
                persist_automations()
                return HTTPStatus.OK, {
                    "ok": True,
                    "message": "Schedule stopped.",
                    "activeAutomations": active_automation_summary(session_id),
                }
            return HTTPStatus.OK, {
                "ok": True,
                "message": "No such schedule.",
                "activeAutomations": active_automation_summary(session_id),
            }
        raw_schedule_type = str(payload.get("scheduleType") or "").strip().lower()
        if raw_schedule_type in ("alert", "daily_report"):
            schedule_type = raw_schedule_type
            stopped = cancel_alert_automation(automation_key(session_id, schedule_type))
            if stopped:
                persist_automations()
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
        if stopped_count:
            persist_automations()
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
        # Save ONLY persists the email configuration — it never arms a
        # schedule. (It used to also arm one, which combined with the explicit
        # Schedule button created duplicate automations and sent alerts the
        # user never scheduled.) Only action=start creates/updates schedules.
        config = save_email_config_from_payload(payload)
        return HTTPStatus.OK, {
            "ok": True,
            "message": (
                "Email notification configuration saved. Nothing is scheduled yet — "
                "use the Schedule selected report button to start sending."
            ),
            "config": config,
            "activeAutomations": active_automation_summary(session_id) if session_id else "",
        }
    if action not in ("start", "test"):
        raise BadRequest(
            "Alert automation action must be start, test, save, stop, "
            "save-profile, list-profiles, get-profile, delete-profile, "
            "or toggle-profile."
        )
    if not session_id:
        raise BadRequest("A dashboard session id is required before scheduling email alerts.")
    # NOTE: the session does not have to be LIVE any more. When it is, its
    # connection snapshot is captured onto the automation so the schedule can
    # recreate a session after restarts; when it is not, the schedule still
    # arms (it inherits the snapshot of the automation it updates, or waits
    # for a session).
    settings = parse_smtp_settings(payload)
    identity = automation_identity(
        settings["schedule_type"], settings["recipients"],
        settings["smtp_host"], settings["smtp_from"],
    )
    same_identity = find_automations_by_identity(identity)
    requested_id = str(payload.get("automationId") or "").strip()
    existing_by_id = _get_automation(requested_id) if requested_id else None
    if existing_by_id:
        # Editing keeps the schedule's identity even when it was created by a
        # previous (e.g. pre-restart) session; the current session adopts it —
        # otherwise the edit would DUPLICATE the schedule.
        automation_id = existing_by_id.automation_id
    else:
        automation_id = f"{session_id}:{uuid.uuid4().hex[:8]}"
    # Inherit password/connection from the schedule being replaced even when
    # the UI did not pass its automationId (e.g. re-scheduling from a new
    # browser session after a restart).
    existing = (
        existing_by_id
        or (same_identity[0] if same_identity else None)
        or existing_smtp_automation(session_id, settings["schedule_type"])
    )
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
    connection = connection_snapshot_for_session(session_id) or (
        dict(existing.connection) if existing and existing.connection else {}
    )
    automation = AlertAutomation(
        automation_id=automation_id,
        session_id=session_id,
        connection=connection,
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
        quiet_start=settings["quiet_start"],
        quiet_end=settings["quiet_end"],
        digest=settings["digest"],
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

    # Replace, never add: cancel EVERY schedule with the same identity —
    # regardless of which (possibly dead) session created it. This is what
    # keeps duplicate schedules from accumulating across restarts.
    for duplicate in same_identity:
        if duplicate.automation_id != automation_id:
            debug_log(
                f"start: replacing same-identity schedule {duplicate.automation_id} "
                f"with {automation_id}"
            )
        cancel_alert_automation(duplicate.automation_id)
    # Config-driven singleton: this schedule is armed from the form (no
    # profile), so it follows email_config.json — which holds exactly ONE
    # configuration per schedule type. Cancel every other config-driven
    # schedule of the same type, whatever its recipients: recipients drift
    # over time, so identity alone (which includes them) cannot catch stale
    # config-driven duplicates. Named profiles keep their own schedules —
    # multiple profiles may be ON concurrently by design.
    for _key, other in _automation_items_snapshot():
        if (
            not other.profile_name
            and str(other.schedule_type or "").strip().lower() == settings["schedule_type"]
            and other.automation_id != automation_id
        ):
            debug_log(
                f"start: replacing config-driven {settings['schedule_type']} schedule "
                f"{other.automation_id} with {automation_id}"
            )
            cancel_alert_automation(other.automation_id)
    cancel_alert_automation(automation_id)
    _put_automation(automation_id, automation)
    schedule_alert_automation(automation)
    persist_automations()
    active_summary = active_automation_summary(session_id)
    message = (
        f"Daily backup/SLA report scheduled for {automation.report_time}."
        if automation.schedule_type == "daily_report"
        else f"Alert automation scheduled every {automation.interval_minutes} minute(s)."
    )
    if not connection and not _session_exists(session_id):
        message += (
            " Note: no live NetWorker connection was available to snapshot;"
            " the schedule is saved and will run once a dashboard session exists."
        )
    if active_summary:
        message = f"{message} Active schedules: {active_summary}."
    return HTTPStatus.OK, {
        "ok": True,
        "automationId": automation_id,
        "message": message,
        "activeAutomations": active_summary,
    }
