"""SMTP delivery, quiet hours, and alert automation scheduling.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import email.utils
import json
import smtplib
import ssl
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from typing import Any

from .config import (
    APP_NAME,
    AUTOMATIONS_FILE,
    DAILY_REPORT_RANGE,
    DATA_DIR,
    DEFAULT_API_PORT,
    LOG,
    REPORT_RANGES,
    debug_log,
    safe_log_text,
)
from .fileio import atomic_write_text
from .models import (
    SHARED_REFRESH_STOP,
    AlertAutomation,
    BadRequest,
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
    dashboard_covers_range,
    existing_smtp_automation,
    generated_at,
    session_automation_keys,
    shared_reliable_dashboard_for_session,
)
from .reports import dashboard_alert_lines, dashboard_report_email, render_dashboard_snapshot_png
from .restapi import report_range_label
from .secrets import WMI_CIPHER, decrypt_process_secret, decrypt_profile_secret, encrypt_process_secret
from .sessions import (
    build_dashboard_from_session,
    connection_snapshot_for_session,
    latest_session_record,
    recreate_session_from_snapshot,
)
from .snapshots import (
    clean_email_profile_name,
    delete_email_profile,
    email_config_public,
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
    saved_email_smtp_secret,
    set_email_profile_connection,
    settings_smtp_target,
    smtp_secret_refused,
    smtp_target,
    smtp_target_changes,
)


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
    except (ValueError, AttributeError):
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
    if isinstance(exc, TimeoutError):
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
            for attachment_bytes, mime_type, filename in attachments.values():
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
    except (smtplib.SMTPException, TimeoutError, OSError, ssl.SSLError) as exc:
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
        "reportRange": profile.get("reportRange"),
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


def _resolve_connection(primary: dict | None, *fallbacks: dict | None) -> dict:
    """First non-empty connection snapshot, else adopt the newest live or
    persisted session record (self-healing: a schedule must never be armed
    or left connectionless while ANY proven session exists)."""
    for candidate in (primary, *fallbacks):
        if isinstance(candidate, dict) and candidate:
            return dict(candidate)
    adopted = latest_session_record()
    if adopted:
        debug_log("self-heal: adopting the newest session record as the schedule connection")
        return dict(adopted)
    return {}


def _stored_smtp_password(settings: dict[str, Any], existing: AlertAutomation | None) -> str:
    """A stored SMTP password saved for exactly the server `settings` names: the
    schedule being replaced, else the shared email settings. A password stored
    for any other server is never substituted (see snapshots.smtp_target). When
    one exists and the request logs in, it is refused so the operator types the
    password; with no username nothing is sent, so "" is safe."""
    requested = settings_smtp_target(settings)
    candidates: list[tuple[str, tuple[str, int, str, str], str]] = []
    if existing and existing.encrypted_smtp_password:
        candidates.append((
            "existing schedule",
            smtp_target(existing.smtp_host, existing.smtp_port, existing.smtp_username, existing.smtp_security),
            decrypt_process_secret(existing.encrypted_smtp_password),
        ))
    candidates.append(("email settings", *saved_email_smtp_secret()))
    refused: tuple[str, tuple[str, int, str, str]] | None = None
    for source, saved, password in candidates:
        if not password:
            continue
        if not smtp_target_changes(saved, requested):
            return password
        refused = refused or (source, saved)
    if refused and settings["smtp_username"]:
        raise smtp_secret_refused(refused[0], refused[1], requested)
    return ""


def _arm_profile_automation(name: str, profile: dict[str, Any], session_id: str) -> AlertAutomation:
    """Arm (or replace) the schedule a stored profile describes, stamped with
    the profile's name. Identity-replace semantics: every same-identity or
    same-profile schedule is cancelled first, so toggling/re-saving never
    accumulates duplicates."""
    settings = parse_smtp_settings(_profile_form_payload(profile))
    matches = _profile_automations(name, profile)
    existing = matches[0] if matches else None
    # The profile's own password is bound to its server by save_email_profile.
    smtp_password = (
        decrypt_profile_secret(str(profile.get("_enc_smtpPassword") or ""), name=name, field="smtpPassword")
        or _stored_smtp_password(settings, existing)
    )
    resolved_session = session_id or (existing.session_id if existing else "") or uuid.uuid4().hex
    stored_connection = profile.get("_connection") if isinstance(profile.get("_connection"), dict) else {}
    connection = _resolve_connection(
        connection_snapshot_for_session(resolved_session),
        stored_connection,
        dict(existing.connection) if existing and existing.connection else None,
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
        report_range=settings["report_range"],
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


def _daily_report_time_today(report_time: str, now: datetime) -> datetime:
    hour, minute = (int(part) for part in report_time.split(":", 1))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def seconds_until_daily_report(report_time: str, now: datetime | None = None) -> float:
    now = now or datetime.now().astimezone()
    target = _daily_report_time_today(report_time, now)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


# A daily report whose run fails is tried again after these waits, the last one
# repeating, until it is sent or its day is over (review C6). It used to wait
# for the next day's send time.
DAILY_REPORT_RETRY_SECONDS = (300, 900, 1800, 3600)


def daily_report_owed(automation: AlertAutomation, now: datetime | None = None) -> bool:
    """True from today's send time until midnight while today's report has not
    gone out: after a failed run, or a restart over the send time. A schedule
    armed after today's send time owes nothing before tomorrow's."""
    now = now or datetime.now().astimezone()
    send_time = _daily_report_time_today(automation.report_time, now)
    if now < send_time or send_time.timestamp() < automation.created_at:
        return False
    return not _daily_report_sent_on(automation, now)


def _daily_report_delay(automation: AlertAutomation, failed: bool) -> float:
    now = datetime.now().astimezone()
    regular = seconds_until_daily_report(automation.report_time, now)
    if not failed or not daily_report_owed(automation, now):
        return regular
    wait = DAILY_REPORT_RETRY_SECONDS[min(max(automation.failures, 1), len(DAILY_REPORT_RETRY_SECONDS)) - 1]
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # A retry after midnight would send the new day's report early, under its date.
    return wait if wait < (midnight - now).total_seconds() else regular


def schedule_alert_automation(automation: AlertAutomation, failed: bool = False) -> None:
    """Compute the automation's next fire time. A single background scheduler
    loop (automation_scheduler_loop) drives the actual firing — far more robust
    than a per-automation long-lived threading.Timer, which could be lost on
    restart, drift, or fail silently. failed: the run that just ended did not
    get through, so a daily report still owed today is retried soon."""
    if _get_automation(automation.automation_id) is None:
        return
    delay = (
        _daily_report_delay(automation, failed)
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


# Schedules with a run in progress, by automation id (review C20). Pushing
# next_run_at forward holds the next tick off for one interval only, so a build
# slower than that (a large jobs database, a NetWorker timeout) used to start a
# second run of the same schedule beside the first: two emails for one alert,
# and two runs racing on its signature and next run.
_RUNS_IN_FLIGHT: set[str] = set()
_RUNS_IN_FLIGHT_LOCK = threading.Lock()


def _claim_run(automation_id: str) -> bool:
    """Mark the schedule's run as started; False when one still is."""
    with _RUNS_IN_FLIGHT_LOCK:
        if automation_id in _RUNS_IN_FLIGHT:
            return False
        _RUNS_IN_FLIGHT.add(automation_id)
        return True


def _release_run(automation_id: str) -> None:
    with _RUNS_IN_FLIGHT_LOCK:
        _RUNS_IN_FLIGHT.discard(automation_id)


def _fire_automation_async(automation_id: str) -> None:
    """Run a schedule claimed with _claim_run on its own thread, which releases
    the claim when the run ends."""
    def _run() -> None:
        try:
            run_alert_automation(automation_id)
        finally:
            _release_run(automation_id)

    try:
        threading.Thread(target=_run, name=f"automation-fire-{automation_id[:12]}", daemon=True).start()
    except RuntimeError:
        _release_run(automation_id)
        raise


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
            if not _claim_run(key):
                debug_log(f"automation id={key} is still running; not started again")
                continue
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
        "report_range": automation.report_range,
        "created_at": automation.created_at,
        "theme": automation.theme,
        "last_signature": automation.last_signature,
        "enabled": automation.enabled,
        "quiet_start": automation.quiet_start,
        "quiet_end": automation.quiet_end,
        "digest": automation.digest,
        "connection": automation.connection if isinstance(automation.connection, dict) else {},
        "profile_name": automation.profile_name,
        # Run state, so a restart neither loses a due run nor resets the clock (review C6).
        "next_run_at": automation.next_run_at,
        "last_run": automation.last_run,
        "last_result": automation.last_result,
    }


# One save of automations.json at a time (review C19). A save writes the
# registry as it stood when that save began, so two at once could land out of
# order and the older one would bring back a schedule just stopped or changed.
_AUTOMATIONS_FILE_LOCK = threading.Lock()


def persist_automations() -> None:
    """Write scheduled automations to disk so they survive a restart. Without a
    stable encryption key the SMTP password cannot be persisted safely, so we
    skip persistence entirely (matches the session-persistence policy). A
    failure is logged once, and the schedules stay armed until a restart."""
    if not WMI_CIPHER:
        return
    with _AUTOMATIONS_FILE_LOCK:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            records = {key: _automation_to_dict(a) for key, a in _automation_items_snapshot()}
            text = json.dumps(records, separators=(",", ":"))
        except (OSError, TypeError, ValueError) as exc:
            LOG.warning(f"could not save {AUTOMATIONS_FILE.name}: {exc}", extra={"event": "file_write_failed"})
            return
        try:
            atomic_write_text(AUTOMATIONS_FILE, text)
        except OSError:
            pass  # logged by atomic_write_text


# A saved config re-aims a live schedule (_sync_automation_from_email_config)
# while a send may be in flight on a scheduler thread. A send works from its own
# copy, taken under this lock, so the password it logs in with always belongs to
# the server it connects to (see snapshots.smtp_target).
_AUTOMATION_SMTP_LOCK = threading.Lock()


def _smtp_send_view(automation: AlertAutomation) -> tuple[AlertAutomation, str]:
    """A private copy of the schedule to send with, and its SMTP password."""
    with _AUTOMATION_SMTP_LOCK:
        return replace(automation), decrypt_process_secret(automation.encrypted_smtp_password)


def _sync_automation_from_email_config(automation: AlertAutomation, cfg: dict[str, Any]) -> bool:
    """Overwrite a CONFIG-DRIVEN schedule's settings from email_config.json —
    the single source of truth for schedules not armed from a named profile.
    Fields the config file does not store for that type (quiet hours, digest,
    the other type's cadence fields) keep the automation's current values.
    Identity fields (automation_id, session_id, connection, last_signature,
    enabled) are never touched. Returns True when anything changed. No config
    entry for the type, or an invalid one, keeps the schedule as-is — config
    deletion is not schedule deletion (the Stop button is). Runs on a live
    schedule after every save, not only at boot, so it never moves the next run
    unless the cadence moved."""
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
        "reportRange": _entry("report_range", automation.report_range),
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
    old_target = smtp_target(
        automation.smtp_host, automation.smtp_port, automation.smtp_username, automation.smtp_security
    )
    new_target = settings_smtp_target(settings)
    saved_target, saved_password = saved_email_smtp_secret()
    if smtp_target_changes(saved_target, new_target):
        saved_password = ""  # read after `cfg`, and a save has re-aimed it since
    old_cadence = (automation.interval_minutes, automation.report_time)
    changed = False
    with _AUTOMATION_SMTP_LOCK:
        for attr in (
            "smtp_host", "smtp_port", "smtp_security", "smtp_username", "smtp_from",
            "recipients", "interval_minutes", "trigger", "report_time", "report_range", "theme",
            "quiet_start", "quiet_end", "digest",
        ):
            if getattr(automation, attr) != settings[attr]:
                setattr(automation, attr, settings[attr])
                changed = True
        if smtp_target_changes(old_target, new_target):
            # Re-aimed at the config's server: the schedule's own password was saved
            # for its old server and must not follow it (snapshots.smtp_target).
            automation.encrypted_smtp_password = ""
        # The config's password is bound to this server, and it wins over the
        # schedule's own: that is how a rotated password reaches the schedule.
        if saved_password and saved_password != decrypt_process_secret(automation.encrypted_smtp_password):
            automation.encrypted_smtp_password = encrypt_process_secret(saved_password)
            changed = True
    if changed:
        debug_log(
            f"reconcile: synced {automation.schedule_type} schedule "
            f"{automation.automation_id} from email_config.json"
        )
    if (automation.interval_minutes, automation.report_time) != old_cadence:
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
    profiles may be ON concurrently by design. Runs at boot and whenever the
    config is saved. Never arms a schedule. Returns (dropped, synced)."""
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


def resume_alert_automation(automation: AlertAutomation, saved_next_run_at: float) -> None:
    """Pick a restored schedule up where the last process left it (review C6).
    A daily report still owed today runs at the next tick; one that is not
    waits for its next send time. An alert check keeps its saved time, and runs
    at once when that passed while the service was down; a restart used to
    push it a whole interval away."""
    if automation.schedule_type == "daily_report":
        if daily_report_owed(automation):
            automation.next_run_at = time.time()
            LOG.info(f"Email automation {automation.automation_id}: today's daily report was missed; sending it now.")
            return
    elif saved_next_run_at > 0:
        automation.next_run_at = min(saved_next_run_at, time.time() + automation.interval_minutes * 60)
        return
    schedule_alert_automation(automation)


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
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning(f"could not read {AUTOMATIONS_FILE.name}; no schedule restored: {exc}")
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
            report_range = str(rec.get("report_range") or "")
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
                report_range=report_range if report_range in REPORT_RANGES else DAILY_REPORT_RANGE,
                created_at=float(rec.get("created_at") or time.time()),
                theme=str(rec.get("theme") or "default"),
                last_signature=str(rec.get("last_signature") or ""),
                enabled=bool(rec.get("enabled", True)),
                quiet_start=str(rec.get("quiet_start") or ""),
                quiet_end=str(rec.get("quiet_end") or ""),
                digest=bool(rec.get("digest", True)),
                profile_name=str(rec.get("profile_name") or ""),
                last_run=float(rec.get("last_run") or 0.0),
                last_result=str(rec.get("last_result") or "Scheduled"),
            )
            _put_automation(automation.automation_id, automation)
            resume_alert_automation(automation, float(rec.get("next_run_at") or 0.0))
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


def _daily_report_signature(dashboard: dict[str, Any]) -> str:
    """Dedup key of a scheduled daily report: the local date it is sent on plus
    the report window it covers. Content is deliberately left out — an
    unchanged, all-green estate still gets its email every day. Alerts keep
    their content signature (the same alert must not repeat)."""
    summary = dashboard.get("summary") if isinstance(dashboard.get("summary"), dict) else {}
    window = f"{summary.get('range') or ''}|{summary.get('rangeLabel') or ''}"
    return f"daily:{datetime.now().astimezone().date().isoformat()}:{window}"


def _daily_report_sent_on(automation: AlertAutomation, day: datetime) -> bool:
    """Whether the schedule's last daily report went out on day's local date."""
    return automation.last_signature.startswith(f"daily:{day.date().isoformat()}:")


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
    usable = bool(snapshot) and bool(
        decrypt_process_secret(str(snapshot.get("encrypted_networker_password") or ""))
    )
    if not usable:
        # Self-heal: the automation's own snapshot is missing or unreadable
        # (legacy record, or the DPAPI key was regenerated). Adopt the newest
        # live/persisted session whose credentials DO decrypt, store it on the
        # automation, and proceed. Only when nothing is adoptable do we fall
        # back to waiting (still scheduled — a schedule is never cancelled).
        adopted = latest_session_record()
        if adopted and decrypt_process_secret(str(adopted.get("encrypted_networker_password") or "")):
            automation.connection = dict(adopted)
            snapshot = automation.connection
            persist_automations()
            LOG.info(
                f"Email automation {automation.automation_id}: adopted the newest session "
                "connection (self-heal) after its own was missing or unreadable."
            )
        elif not snapshot:
            # No snapshot of its own and nothing anywhere to adopt: wait (still
            # scheduled) until any dashboard session appears — it heals itself.
            automation.last_result = (
                f"Waiting for a dashboard session at {generated_at()} — no saved connection is "
                "available yet; the schedule will heal itself as soon as anyone connects."
            )
            debug_log(f"automation {automation.automation_id}: no snapshot and nothing to adopt; run skipped")
            return False
        else:
            if automation.automation_id not in _SNAPSHOT_DECRYPT_WARNED:
                _SNAPSHOT_DECRYPT_WARNED.add(automation.automation_id)
                LOG.warning(
                    f"Email automation {automation.automation_id} ({automation.schedule_type}) cannot decrypt its "
                    "saved NetWorker credentials and no other session is available to adopt. It remains scheduled "
                    "and will heal itself as soon as anyone connects."
                )
            automation.last_result = (
                f"Saved connection credentials are unreadable ({generated_at()}); will adopt the next live connection."
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
    failed = False
    try:
        if not automation.enabled:
            automation.last_result = "Paused"
            return
        if not _ensure_automation_session(automation):
            failed = True
            return  # run skipped; finally-block reschedules the next attempt
        # A daily report always covers its own range, never whatever range the
        # dashboard was last viewed with, and building it leaves that view alone.
        report_range = automation.report_range if automation.schedule_type == "daily_report" else None
        status, dashboard = build_dashboard_from_session(
            automation.session_id, report_range, remember_range=False
        )
        debug_log(f"run_alert_automation build status={status} id={automation_id}")
        if status != HTTPStatus.OK:
            raise RuntimeError(dashboard.get("error") or f"Dashboard session refresh failed (HTTP {status}).")
        if automation.schedule_type == "daily_report":
            if not dashboard_backup_source_available(dashboard):
                source_error = dashboard_backup_source_error(dashboard)
                last_good_dashboard = shared_reliable_dashboard_for_session(
                    automation.session_id, automation.report_range
                )
                if not last_good_dashboard:
                    raise RuntimeError(
                        "Daily report skipped because backup job data was unavailable and no saved "
                        f"dashboard covers {report_range_label(automation.report_range)}: {source_error}"
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
            new_signature = _daily_report_signature(dashboard)
            if new_signature == automation.last_signature:
                automation.last_result = (
                    f"Skipped at {generated_at()}: today's report for this window was already sent"
                )
                automation.last_run = time.time()
                return
            duplicate_sent_at = _duplicate_send_suppressed(automation, new_signature)
            if duplicate_sent_at:
                automation.last_result = (
                    f"Duplicate suppressed (same alert already sent at {duplicate_sent_at})"
                )
                automation.last_signature = new_signature  # today's went out, from its twin
                automation.last_run = time.time()
                return
            plain, html_body, attachments = scheduled_dashboard_email_payload(dashboard)
            smtp_view, report_password = _smtp_send_view(automation)
            smtp_debug = send_smtp_email(
                smtp_view,
                "NetWorker daily backup status and SLA report",
                plain,
                report_password,
                html_body,
                attachments=attachments,
            ) or smtp_debug_snapshot(smtp_view, report_password, "sent")
            _record_identity_send(automation, new_signature)
            automation.last_signature = new_signature
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
            smtp_view, alert_password = _smtp_send_view(automation)
            smtp_debug = send_smtp_email(
                smtp_view,
                subject,
                "\n".join(lines),
                alert_password,
            ) or smtp_debug_snapshot(smtp_view, alert_password, "sent")
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
        failed = True
        automation.last_result = f"SMTP failed at {exc.stage}: {exc.detail}"
        LOG.warning(f"Scheduled email automation {automation_id} SMTP failure: {exc.stage}: {exc.detail}")
    except Exception as exc:  # noqa: BLE001
        failed = True
        automation.last_result = f"Alert automation failed: {exc}"
        LOG.warning(f"Scheduled email automation {automation_id} failed: {safe_log_text(exc, 300)}")
    finally:
        debug_log(f"run_alert_automation done id={automation_id} result={safe_log_text(automation.last_result, 200)}")
        automation.failures = automation.failures + 1 if failed else 0
        # Recompute the next fire time (the scheduler loop drives the actual run).
        schedule_alert_automation(automation, failed)
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
                "reportRange": automation.report_range,
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
        persist_automations()  # a pause used to end at the next restart (review C19)
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
        # Save persists the email configuration and syncs the config-driven
        # schedules already armed from it, as the next restart would. Every
        # type's, not just the saved one: the SMTP server is shared. It must
        # never start sending, so it never arms a schedule. (It used to also arm
        # one, which combined with the explicit Schedule button created
        # duplicate automations and sent alerts the user never scheduled.) Only
        # action=start creates schedules. Paused ones stay paused, and one armed
        # from a profile follows its profile.
        config = save_email_config_from_payload(payload)
        dropped, synced = _reconcile_config_driven_automations()
        if dropped or synced:
            persist_automations()
        following = [automation for _key, automation in _automation_items_snapshot() if not automation.profile_name]
        saved_type = str(payload.get("scheduleType") or "alert").strip().lower()
        message = "Email notification configuration saved"
        message += f" and {synced} schedule(s) updated." if synced else "."
        if not any(automation.schedule_type == saved_type for automation in following):
            message += (
                " No schedule of this type follows it yet — "
                "use the Schedule selected report button to start sending."
            )
        warning = ""
        if any(a.smtp_username and not decrypt_process_secret(a.encrypted_smtp_password) for a in following):
            warning = (
                "No SMTP password is saved for this server, so the schedules that follow it "
                "cannot log in. Type the password and save again."
            )
            message += f" {warning}"
        return HTTPStatus.OK, {
            "ok": True,
            "message": message,
            "warning": warning,
            "schedulesUpdated": synced,
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
    smtp_password = settings["smtp_password"] or _stored_smtp_password(settings, existing)
    # Scheduling a notification also persists its configuration so it survives a
    # restart and pre-fills the form next time.
    if action == "start":
        try:
            save_email_config_from_payload(payload)
        except BadRequest:
            pass
    connection = _resolve_connection(
        connection_snapshot_for_session(session_id),
        dict(existing.connection) if existing and existing.connection else None,
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
        report_range=settings["report_range"],
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
            # The page sends the dashboard on its screen, which may be another range.
            if dashboard and dashboard_covers_range(dashboard, automation.report_range):
                dashboard["theme"] = automation.theme
                dashboard["scheduledReport"] = True
                plain_body, html_body, attachments = scheduled_dashboard_email_payload(dashboard)
                subject = "NetWorker daily backup status and SLA report - test"
            else:
                status, dashboard = build_dashboard_from_session(
                    session_id, automation.report_range, remember_range=False
                )
                if status == HTTPStatus.OK:
                    if not dashboard_backup_source_available(dashboard):
                        dashboard["reportNotice"] = (
                            "Backup job data was unavailable and no saved dashboard covers "
                            f"{report_range_label(automation.report_range)}, so the job figures below are "
                            f"not reliable. Refresh error: {dashboard_backup_source_error(dashboard)}"
                        )
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
    # Scheduling saved the email configuration too, and its SMTP server is
    # shared: the other type's config-driven schedule follows it like a save.
    _reconcile_config_driven_automations()
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
        "config": email_config_public(),
        "activeAutomations": active_summary,
    }
