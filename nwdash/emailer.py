"""SMTP delivery and quiet-hours helpers.

Split from networker_dashboard.py (v2.5.0). The legacy AlertAutomation
email-automation engine (scheduling loop, profile toggles, connection
snapshots, and the /api/alert-automation handler) was removed once the
Scheduled Reports subsystem (report_jobs/report_notify/report_api) replaced
it; only the SMTP delivery core and small shared helpers remain here.
"""
from __future__ import annotations

import email.utils
import smtplib
import socket
import ssl
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from .config import debug_log
from .models import AlertAutomation, SmtpDeliveryError, _pop_automation
from .snapshots import saved_email_smtp_password  # re-exported for report_api


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
    """Cancel and drop a scheduled automation from the in-memory registry.
    The legacy engine no longer arms schedules, so this is normally a no-op;
    it is kept for shutdown cleanup of any registry entries."""
    automation = _pop_automation(automation_id)
    if not automation:
        return False
    if automation.timer:
        automation.timer.cancel()
    return True
