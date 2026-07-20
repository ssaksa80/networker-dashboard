"""Report job model, persistence, validation, and the scheduler loop.

A ReportJob is self-contained: it owns its NetWorker credential and every
setting needed to render and send. Nothing here reads the interactive session
store."""
from __future__ import annotations

import json
import smtplib
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import config
from . import report_notify
from . import report_render
from .config import debug_log
from .models import SHARED_REFRESH_STOP


@dataclass
class JobSchedule:
    report_time: str = "08:00"
    cadence: str = "daily"
    interval_minutes: int = 1440
    trigger: str = "critical"


@dataclass
class JobHealth:
    last_run: float = 0.0
    last_result: str = ""
    last_success: float = 0.0
    next_run: float = 0.0
    consecutive_failures: int = 0
    state: str = "never_run"


@dataclass
class ReportJob:
    id: str
    kind: str
    recipients: list[str] = field(default_factory=list)
    enabled: bool = False
    credential: dict[str, Any] = field(default_factory=dict)
    schedule: JobSchedule = field(default_factory=JobSchedule)
    quiet_start: str = ""
    quiet_end: str = ""
    digest: bool = True
    theme: str = "default"
    health: JobHealth = field(default_factory=JobHealth)


_JOBS: dict[str, ReportJob] = {}
_JOBS_LOCK = threading.Lock()


def put_job(job: ReportJob) -> None:
    with _JOBS_LOCK:
        _JOBS[job.id] = job


def get_job(job_id: str) -> ReportJob | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def delete_job(job_id: str) -> bool:
    with _JOBS_LOCK:
        return _JOBS.pop(job_id, None) is not None


def jobs_snapshot() -> list[ReportJob]:
    with _JOBS_LOCK:
        return list(_JOBS.values())


def clear_jobs_in_memory() -> None:
    with _JOBS_LOCK:
        _JOBS.clear()


def _job_from_dict(rec: dict[str, Any]) -> ReportJob:
    sched = rec.get("schedule") or {}
    health = rec.get("health") or {}
    return ReportJob(
        id=str(rec["id"]),
        kind=str(rec.get("kind") or "digest"),
        recipients=[str(r) for r in (rec.get("recipients") or [])],
        enabled=bool(rec.get("enabled", False)),
        credential=rec.get("credential") if isinstance(rec.get("credential"), dict) else {},
        schedule=JobSchedule(**{k: sched[k] for k in JobSchedule().__dict__ if k in sched}),
        quiet_start=str(rec.get("quiet_start") or ""),
        quiet_end=str(rec.get("quiet_end") or ""),
        digest=bool(rec.get("digest", True)),
        theme=str(rec.get("theme") or "default"),
        health=JobHealth(**{k: health[k] for k in JobHealth().__dict__ if k in health}),
    )


def persist_jobs() -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = {j.id: asdict(j) for j in jobs_snapshot()}
        tmp = config.REPORT_JOBS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
        tmp.replace(config.REPORT_JOBS_FILE)
    except (OSError, TypeError, ValueError):
        pass


def restore_jobs_from_disk() -> int:
    if not config.REPORT_JOBS_FILE.exists():
        return 0
    try:
        records = json.loads(config.REPORT_JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(records, dict):
        return 0
    restored = 0
    for rec in records.values():
        try:
            put_job(_job_from_dict(rec))
            restored += 1
        except (KeyError, TypeError, ValueError):
            continue
    return restored


@dataclass
class ValidationResult:
    ok: bool
    checks: dict[str, bool]
    detail: str = ""


def _smtp_probe(smtp: dict[str, Any], smtp_password: str) -> tuple[bool, str]:
    """Open a real SMTP connection (and STARTTLS/login when configured) without
    sending, to prove delivery works before a job goes Active."""
    host = str(smtp.get("host") or "")
    port = int(smtp.get("port") or 25)
    security = str(smtp.get("security") or "none").lower()
    username = str(smtp.get("username") or "")
    try:
        if security == "ssl":
            conn = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            conn = smtplib.SMTP(host, port, timeout=20)
        with conn as s:
            s.ehlo()
            if security == "starttls":
                s.starttls(); s.ehlo()
            if username:
                s.login(username, smtp_password)
        return True, ""
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator
        return False, str(exc)


def validate(job: "ReportJob", smtp: dict[str, Any], smtp_password: str) -> ValidationResult:
    checks = {"credential": False, "render": False, "smtp": False}
    detail = ""
    render_res = report_render.render(job.credential)
    checks["credential"] = bool(job.credential.get("rest_api_host") and job.credential.get("username"))
    checks["render"] = render_res.ok
    if not render_res.ok:
        detail = render_res.error
    smtp_ok, smtp_err = _smtp_probe(smtp, smtp_password)
    checks["smtp"] = smtp_ok
    if not smtp_ok and not detail:
        detail = smtp_err
    ok = all(checks.values())
    return ValidationResult(ok, checks, detail)


REPORT_TICK_SECONDS = 30


def _seconds_until_report_time(report_time: str, now: datetime | None = None) -> float:
    now = now or datetime.now().astimezone()
    hour, minute = (int(p) for p in report_time.split(":", 1))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def compute_next_run(job: "ReportJob") -> float:
    if job.kind == "digest":
        return time.time() + _seconds_until_report_time(job.schedule.report_time)
    return time.time() + max(60, job.schedule.interval_minutes * 60)


def fire_job(job: "ReportJob", cfg: dict[str, Any]) -> None:
    """Render and deliver ONE job. Never raises; records health either way."""
    if not job.enabled:
        return
    smtp = cfg.get("smtp") or {}
    smtp_password = str(cfg.get("smtp_password") or "")
    ops_address = str(cfg.get("ops_address") or "")
    job.health.last_run = time.time()
    try:
        res = report_render.render(job.credential)
        if res.ok:
            report_notify.send_report(job, res.dashboard, smtp, smtp_password, stale=False)
            report_render.cache_put(job.id, res.dashboard)
            job.health.state = "healthy"
            job.health.last_success = time.time()
            job.health.consecutive_failures = 0
            job.health.last_result = f"Sent at {time.strftime('%Y-%m-%d %H:%M:%S')}"
        else:
            cached = report_render.cache_get(job.id)
            if cached:
                report_notify.send_report(job, cached, smtp, smtp_password, stale=True)
            report_notify.send_ops_alert(job, res.error, smtp, ops_address, smtp_password)
            job.health.state = "unhealthy"
            job.health.consecutive_failures += 1
            job.health.last_result = f"Failed: {res.error}"
    except Exception as exc:  # noqa: BLE001 — a fire must never kill the loop
        job.health.state = "unhealthy"
        job.health.consecutive_failures += 1
        job.health.last_result = f"Error: {exc}"
        debug_log(f"fire_job {job.id} crashed: {exc}")
    finally:
        job.health.next_run = compute_next_run(job)


def scheduler_tick(cfg_provider) -> None:
    now = time.time()
    for job in jobs_snapshot():
        if not job.enabled:
            continue
        nxt = job.health.next_run or 0.0
        if not nxt:
            job.health.next_run = compute_next_run(job)
            continue
        if now >= nxt:
            job.health.next_run = now + max(60, job.schedule.interval_minutes * 60)
            threading.Thread(target=_fire_and_persist, args=(job, cfg_provider),
                             name=f"report-fire-{job.id[:12]}", daemon=True).start()


def _fire_and_persist(job: "ReportJob", cfg_provider) -> None:
    try:
        fire_job(job, cfg_provider())
    finally:
        persist_jobs()


def scheduler_loop(cfg_provider) -> None:
    while not SHARED_REFRESH_STOP.wait(REPORT_TICK_SECONDS):
        try:
            scheduler_tick(cfg_provider)
        except Exception as exc:  # noqa: BLE001
            debug_log(f"report scheduler_loop iteration failed: {exc}")
