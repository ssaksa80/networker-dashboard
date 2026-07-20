"""HTTP action router for /api/report-jobs. Enforces the hard save gate:
create/update run JobValidator and refuse to enable a job unless all checks
pass."""
from __future__ import annotations

import json
import uuid
from http import HTTPStatus
from typing import Any

from . import report_jobs
from .report_cred import encrypt_credential_password
from .emailer import saved_email_smtp_password
from .config import EMAIL_CONFIG_FILE


def _smtp_config() -> tuple[dict[str, Any], str]:
    """Return (smtp_dict, smtp_password) from the app-level email config."""
    try:
        cfg = json.loads(EMAIL_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    smtp = cfg.get("smtp") if isinstance(cfg.get("smtp"), dict) else {}
    return smtp, saved_email_smtp_password()


def _parse_recipients(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").replace(";", ",").split(",")
    return [r.strip() for r in items if r.strip()]


def _seal_credential(cred: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in cred.items() if k != "password"}
    out["encrypted_password"] = encrypt_credential_password(str(cred.get("password") or ""))
    return out


def _job_public(job: "report_jobs.ReportJob") -> dict[str, Any]:
    return {
        "id": job.id, "kind": job.kind, "enabled": job.enabled,
        "recipients": job.recipients, "reportTime": job.schedule.report_time,
        "intervalMinutes": job.schedule.interval_minutes, "trigger": job.schedule.trigger,
        "credentialHost": job.credential.get("rest_api_host", ""),
        "credentialUser": job.credential.get("username", ""),
        "health": {
            "state": job.health.state, "lastResult": job.health.last_result,
            "lastRun": job.health.last_run, "lastSuccess": job.health.last_success,
            "nextRun": job.health.next_run, "consecutiveFailures": job.health.consecutive_failures,
        },
    }


def handle_report_jobs(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    action = str(payload.get("action") or "").strip().lower()

    if action == "list":
        return HTTPStatus.OK, {"ok": True, "jobs": [_job_public(j) for j in report_jobs.jobs_snapshot()]}

    if action == "delete":
        report_jobs.delete_job(str(payload.get("id") or ""))
        report_jobs.persist_jobs()
        return HTTPStatus.OK, {"ok": True}

    if action in ("create", "update"):
        job_id = str(payload.get("id") or "") or uuid.uuid4().hex
        cred_in = payload.get("credential") if isinstance(payload.get("credential"), dict) else {}
        existing = report_jobs.get_job(job_id)
        if existing and not cred_in.get("password"):
            sealed = dict(existing.credential)
            sealed.update({k: v for k, v in cred_in.items() if k != "password"})
        else:
            sealed = _seal_credential(cred_in)
        job = report_jobs.ReportJob(
            id=job_id, kind=str(payload.get("kind") or "digest"),
            recipients=_parse_recipients(payload.get("recipients")),
            credential=sealed, enabled=False,
            schedule=report_jobs.JobSchedule(
                report_time=str(payload.get("reportTime") or "08:00"),
                interval_minutes=int(payload.get("intervalMinutes") or 1440),
                trigger=str(payload.get("trigger") or "critical")),
            quiet_start=str(payload.get("quietStart") or ""),
            quiet_end=str(payload.get("quietEnd") or ""),
            theme=str(payload.get("theme") or "default"),
        )
        smtp, smtp_password = _smtp_config()
        result = report_jobs.validate(job, smtp, smtp_password)
        if not result.ok:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "id": job_id,
                                            "checks": result.checks, "message": result.detail}
        job.enabled = True
        job.health.next_run = report_jobs.compute_next_run(job)
        report_jobs.put_job(job)
        report_jobs.persist_jobs()
        return HTTPStatus.OK, {"ok": True, "id": job_id, "job": _job_public(job)}

    return HTTPStatus.BAD_REQUEST, {"ok": False, "message": f"Unknown action {action!r}."}
