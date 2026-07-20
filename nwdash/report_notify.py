"""Turn a rendered dashboard (or a failure) into an email, reusing the proven
send_smtp_email core via a lightweight settings adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .emailer import send_smtp_email
from .reports import dashboard_report_email, render_dashboard_snapshot_png

_STALE_BANNER_HTML = (
    '<div style="background:#b45309;color:#fff;padding:10px 14px;border-radius:6px;'
    'margin-bottom:12px;font-weight:600">STALE DATA — live NetWorker refresh failed; '
    'this report shows the last successful snapshot.</div>'
)
_STALE_BANNER_TEXT = ("STALE DATA - live NetWorker refresh failed; this report shows "
                      "the last successful snapshot.\n\n")


@dataclass
class _SmtpSettings:
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_from: str
    recipients: list[str] = field(default_factory=list)


def _settings(smtp: dict[str, Any], recipients: list[str]) -> _SmtpSettings:
    return _SmtpSettings(
        smtp_host=str(smtp.get("host") or ""),
        smtp_port=int(smtp.get("port") or 25),
        smtp_security=str(smtp.get("security") or "none"),
        smtp_username=str(smtp.get("username") or ""),
        smtp_from=str(smtp.get("from") or ""),
        recipients=list(recipients),
    )


def send_report(job, dashboard: dict[str, Any], smtp: dict[str, Any],
                smtp_password: str, stale: bool = False) -> dict[str, Any]:
    dashboard = dict(dashboard)
    dashboard["theme"] = getattr(job, "theme", "default")
    dashboard["scheduledReport"] = True
    plain, html = dashboard_report_email(dashboard)
    if stale:
        plain = _STALE_BANNER_TEXT + plain
        html = _STALE_BANNER_HTML + html
    attachments: dict[str, tuple[bytes, str, str]] = {}
    png = render_dashboard_snapshot_png(dashboard)
    if png:
        attachments["networker-dashboard.png"] = (png, "image/png", "networker-dashboard.png")
    subject = "NetWorker daily backup status and SLA report"
    if stale:
        subject += " (stale data)"
    return send_smtp_email(_settings(smtp, list(job.recipients)), subject, plain,
                           smtp_password, html, attachments=attachments)


def send_ops_alert(job, error: str, smtp: dict[str, Any], ops_address: str,
                   smtp_password: str) -> dict[str, Any]:
    if not ops_address:
        return {}
    subject = f"NetWorker report FAILED: job {getattr(job, 'id', '?')}"
    body = (f"Scheduled report '{getattr(job, 'id', '?')}' ({getattr(job, 'kind', '?')}) "
            f"failed at fire time.\n\nError: {error}\n")
    return send_smtp_email(_settings(smtp, [ops_address]), subject, body, smtp_password)
