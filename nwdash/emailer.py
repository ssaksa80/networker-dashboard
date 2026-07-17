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
from .secrets import WMI_CIPHER, decrypt_process_secret, encrypt_process_secret
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
from .sessions import build_dashboard_from_session
from .snapshots import (
    load_ui_theme,
    parse_email_recipients,
    parse_smtp_settings,
    save_email_config_from_payload,
    saved_email_smtp_password,
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


def prune_orphaned_automations() -> int:
    """Drop scheduled automations whose dashboard session no longer exists.

    A new browser connection gets a fresh session id, so a previous session's
    automations become orphans: invisible in the per-session modal list yet still
    fired by the scheduler, which re-emails old reports. Pruning them here (and on
    restore) keeps the live store — and the persisted automations.json — in sync
    with the sessions that actually exist."""
    removed = 0
    for key, automation in _automation_items_snapshot():
        if not _session_exists(automation.session_id):
            if cancel_alert_automation(key):
                removed += 1
                debug_log(
                    f"pruned orphaned automation id={key} "
                    f"type={automation.schedule_type} session={automation.session_id[:8]}… (session gone)"
                )
    if removed:
        persist_automations()
    return removed


def automation_scheduler_tick() -> None:
    # Orphaned automations (session gone) must never fire — they would re-send a
    # stale report. Prune them first, then fire only the survivors that are due.
    prune_orphaned_automations()
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


def restore_automations_from_disk() -> int:
    """Recreate and reschedule automations saved by a previous run. Must be
    called AFTER sessions are restored, since an automation needs its session."""
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
            if not session_id or not _session_exists(session_id):
                # Session gone; cannot refresh its dashboard. Drop the record so
                # the persisted file stops accumulating dead-session schedules.
                dropped += 1
                continue
            automation = AlertAutomation(
                automation_id=str(rec.get("automation_id") or aid),
                session_id=session_id,
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
            )
            _put_automation(automation.automation_id, automation)
            schedule_alert_automation(automation)
            restored += 1
        except (TypeError, ValueError, KeyError) as exc:
            dropped += 1
            debug_log(f"restore_automations: skipped {aid}: {exc}")
    # Rewrite the file from the surviving in-memory store so orphaned/invalid
    # records are pruned from disk, not just ignored.
    if dropped:
        debug_log(f"restore_automations: pruned {dropped} orphaned/invalid record(s) from {AUTOMATIONS_FILE.name}")
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
            subject = alert_email_subject(severity, automation.digest)
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
    if action == "list":
        rows = []
        for _key, automation in _automation_items_snapshot():
            if automation.session_id != session_id:
                continue
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
            })
        return HTTPStatus.OK, {"ok": True, "schedules": rows}
    if action == "set_enabled":
        requested_id = str(payload.get("automationId") or "").strip()
        target = _get_automation(requested_id) if requested_id else None
        if not target or target.session_id != session_id:
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
            if target and target.session_id == session_id:
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
        config = save_email_config_from_payload(payload)
        # Saving an alert/daily-report config with a live session also arms its
        # schedule. Users expect "Save" to enable the notification; the separate
        # "Schedule" button was a hidden second step that left reports inactive.
        scheduled_message = ""
        active_summary = ""
        try:
            raw_schedule_type = str(payload.get("scheduleType") or "").strip().lower()
            schedulable = raw_schedule_type in ("alert", "daily_report")
            if schedulable and session_id and _session_exists(session_id):
                settings = parse_smtp_settings(payload)
                can_schedule = (
                    bool(settings["recipients"])
                    and (
                        settings["schedule_type"] != "daily_report"
                        or bool(settings["report_time"])
                    )
                )
                if can_schedule:
                    requested_id = str(payload.get("automationId") or "").strip()
                    existing_by_id = _get_automation(requested_id) if requested_id else None
                    if existing_by_id and existing_by_id.session_id == session_id:
                        automation_id = existing_by_id.automation_id
                    else:
                        automation_id = f"{session_id}:{uuid.uuid4().hex[:8]}"
                    existing = existing_by_id or existing_smtp_automation(session_id, settings["schedule_type"])
                    smtp_password = settings["smtp_password"] or (
                        decrypt_process_secret(existing.encrypted_smtp_password) if existing else ""
                    ) or saved_email_smtp_password()
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
                        quiet_start=settings["quiet_start"],
                        quiet_end=settings["quiet_end"],
                        digest=settings["digest"],
                    )
                    cancel_alert_automation(automation_id)
                    _put_automation(automation_id, automation)
                    schedule_alert_automation(automation)
                    persist_automations()
                    active_summary = active_automation_summary(session_id)
                    scheduled_message = (
                        f" Daily backup/SLA report scheduled for {automation.report_time}."
                        if automation.schedule_type == "daily_report"
                        else f" Alert automation scheduled every {automation.interval_minutes} minute(s)."
                    )
        except BadRequest:
            # Bad SMTP/schedule fields shouldn't block a plain config save.
            pass
        return HTTPStatus.OK, {
            "ok": True,
            "message": "Email notification configuration saved." + scheduled_message,
            "config": config,
            "activeAutomations": active_summary,
        }
    if action not in ("start", "test"):
        raise BadRequest("Alert automation action must be start, test, save, or stop.")
    if not session_id or not _session_exists(session_id):
        raise BadRequest("A live dashboard session is required before scheduling email alerts.")
    settings = parse_smtp_settings(payload)
    requested_id = str(payload.get("automationId") or "").strip()
    existing_by_id = _get_automation(requested_id) if requested_id else None
    if existing_by_id and existing_by_id.session_id == session_id:
        automation_id = existing_by_id.automation_id
    else:
        automation_id = f"{session_id}:{uuid.uuid4().hex[:8]}"
    existing = existing_by_id or existing_smtp_automation(session_id, settings["schedule_type"])
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
    if active_summary:
        message = f"{message} Active schedules: {active_summary}."
    return HTTPStatus.OK, {
        "ok": True,
        "automationId": automation_id,
        "message": message,
        "activeAutomations": active_summary,
    }
