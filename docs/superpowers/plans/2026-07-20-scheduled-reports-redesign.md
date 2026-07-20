# Scheduled Reports Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the browser-session-coupled email automation with self-contained, credentialed, validated-on-save, observable scheduled report jobs that never fail silently.

**Architecture:** Each `ReportJob` owns its NetWorker credential (machine-DPAPI encrypted at rest) and every setting it needs. A session-free scheduler renders each job by connecting fresh via `build_dashboard(ApiConfig)`. A hard save-time gate (connect + render + SMTP) blocks broken jobs from going Active. Fire-time failures send a fallback report (stale banner) plus an ops alert. New `/api/report-jobs` endpoints and a new UI panel; the old `AlertAutomation` path is removed after parity, keeping only the SMTP core.

**Tech Stack:** Python 3.12 (stdlib + `cryptography` fallback), existing `nwdash` package, pytest, vanilla-JS SPA assets.

**Reused primitives (already in the codebase):**
- `nwdash/sessions.py::build_dashboard(config: ApiConfig) -> (int, dict)` — fresh connect + build, no session.
- `nwdash/models.py::ApiConfig` (frozen dataclass; fields: rest_api_host, rest_api_port, backup_server_host, backup_server_port, username, password, api_mode, api_version, report_range, custom_start_date, custom_end_date, use_wmi_health, wmi_username, wmi_password, timeout_seconds, verify_tls, use_authc_header).
- `nwdash/secrets.py::_dpapi_available()`, `_dpapi_protect(bytes)->bytes`, `_dpapi_unprotect(bytes)->bytes` (already `CRYPTPROTECT_LOCAL_MACHINE`), `WMI_CIPHER` (Fernet fallback).
- `nwdash/emailer.py::send_smtp_email(settings, subject, body, smtp_password, html_body, inline_images, attachments)` — `settings` is duck-typed: needs `.smtp_from`, `.recipients`, `.smtp_host`, `.smtp_port`, `.smtp_security`, `.smtp_username`.
- `nwdash/reports.py::dashboard_report_email(dashboard)->(plain, html)`, `render_dashboard_snapshot_png(dashboard)->bytes|None`.
- `nwdash/config.py::DATA_DIR`.

---

## File Structure

- Create `nwdash/report_cred.py` — CredentialStore: encrypt/decrypt the NetWorker password; (de)serialize credential ⇄ `ApiConfig`.
- Create `nwdash/report_jobs.py` — `ReportJob` dataclass, `JobStore` (atomic persist/restore), `JobValidator`, `Scheduler`.
- Create `nwdash/report_render.py` — `ReportRenderer` (credential → dashboard → email payload) + `LastGoodCache`.
- Create `nwdash/report_notify.py` — `Notifier` (`send_report`, `send_ops_alert`) + `_SmtpSettings` adapter.
- Create `nwdash/report_api.py` — `handle_report_jobs(payload)` request router for `/api/report-jobs`.
- Modify `nwdash/main.py` — boot: restore jobs, start scheduler, legacy-migration notice; route `/api/report-jobs`; deprecate `/api/alert-automation`.
- Modify assets (`nwdash/assets/dashboard.html`, `app.js`, `app.css`) — Scheduled Reports panel.
- Create tests under `tests/` per task.

Constants added to `nwdash/config.py`: `REPORT_JOBS_FILE = DATA_DIR / "report_jobs.json"`, `REPORT_CACHE_DIR = DATA_DIR / "report_cache"`.

---

## Task 1: Config constants

**Files:**
- Modify: `nwdash/config.py` (after the existing `AUTOMATIONS_FILE` line ~108)

- [ ] **Step 1: Add constants**

In `nwdash/config.py`, after `EMAIL_CONFIG_FILE = DATA_DIR / "email_config.json"`:

```python
REPORT_JOBS_FILE = DATA_DIR / "report_jobs.json"
REPORT_CACHE_DIR = DATA_DIR / "report_cache"
```

- [ ] **Step 2: Verify import**

Run: `python -c "from nwdash.config import REPORT_JOBS_FILE, REPORT_CACHE_DIR; print(REPORT_JOBS_FILE.name, REPORT_CACHE_DIR.name)"`
Expected: `report_jobs.json report_cache`

- [ ] **Step 3: Commit**

```bash
git add nwdash/config.py
git commit -m "feat(reports): add report jobs + cache path constants"
```

---

## Task 2: CredentialStore

Encrypts the NetWorker password with machine-scoped DPAPI (Fernet fallback), and converts a stored credential dict into an `ApiConfig` for rendering.

**Files:**
- Create: `nwdash/report_cred.py`
- Test: `tests/test_report_cred.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_cred.py
from nwdash.report_cred import encrypt_credential_password, decrypt_credential_password, credential_to_apiconfig

def test_password_roundtrip():
    token = encrypt_credential_password("s3cret")
    assert token and token != "s3cret"
    assert decrypt_credential_password(token) == "s3cret"

def test_empty_password_roundtrips_to_empty():
    assert decrypt_credential_password(encrypt_credential_password("")) == ""

def test_bad_token_returns_empty():
    assert decrypt_credential_password("not-a-real-token") == ""

def test_credential_to_apiconfig_maps_fields_and_injects_password():
    cred = {
        "rest_api_host": "10.0.0.9", "rest_api_port": 9090,
        "backup_server_host": "10.0.0.9", "backup_server_port": 9090,
        "username": "administrator", "encrypted_password": encrypt_credential_password("pw"),
        "api_mode": "nwui", "api_version": "auto", "verify_tls": False, "report_range": "7d",
    }
    cfg = credential_to_apiconfig(cred)
    assert cfg.rest_api_host == "10.0.0.9"
    assert cfg.username == "administrator"
    assert cfg.password == "pw"          # decrypted for use, never stored on the job
    assert cfg.api_mode == "nwui"
    assert cfg.report_range == "7d"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report_cred.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nwdash.report_cred'`

- [ ] **Step 3: Write minimal implementation**

```python
# nwdash/report_cred.py
"""Encryption at rest for a report job's NetWorker credential.

The password is sealed with machine-scoped DPAPI (already LOCAL_MACHINE in
secrets.py), so a service-account change on the same host does not invalidate
it. On non-Windows/dev hosts it falls back to the app's Fernet key. Tokens are
prefixed so the reader knows which scheme sealed them."""
from __future__ import annotations

import base64
from dataclasses import replace
from typing import Any

from .models import ApiConfig
from .secrets import _dpapi_available, _dpapi_protect, _dpapi_unprotect, WMI_CIPHER

_DPAPI_PREFIX = "dpapi:"
_FERNET_PREFIX = "fernet:"


def encrypt_credential_password(password: str) -> str:
    if not password:
        return ""
    raw = password.encode("utf-8")
    if _dpapi_available():
        return _DPAPI_PREFIX + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
    if WMI_CIPHER:
        return _FERNET_PREFIX + WMI_CIPHER.encrypt(raw).decode("ascii")
    return ""  # no cipher available: never store plaintext


def decrypt_credential_password(token: str) -> str:
    if not token:
        return ""
    try:
        if token.startswith(_DPAPI_PREFIX):
            blob = base64.b64decode(token[len(_DPAPI_PREFIX):].encode("ascii"))
            return _dpapi_unprotect(blob).decode("utf-8")
        if token.startswith(_FERNET_PREFIX) and WMI_CIPHER:
            return WMI_CIPHER.decrypt(token[len(_FERNET_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""
    return ""


def credential_to_apiconfig(cred: dict[str, Any]) -> ApiConfig:
    """Build a render-ready ApiConfig from a stored credential dict. The
    password is decrypted here and lives only on the transient ApiConfig."""
    return ApiConfig(
        rest_api_host=str(cred.get("rest_api_host") or ""),
        rest_api_port=int(cred.get("rest_api_port") or 0),
        backup_server_host=str(cred.get("backup_server_host") or cred.get("rest_api_host") or ""),
        backup_server_port=int(cred.get("backup_server_port") or cred.get("rest_api_port") or 0),
        username=str(cred.get("username") or ""),
        password=decrypt_credential_password(str(cred.get("encrypted_password") or "")),
        api_mode=str(cred.get("api_mode") or "nwui"),
        api_version=str(cred.get("api_version") or "auto"),
        report_range=str(cred.get("report_range") or "7d"),
        custom_start_date=str(cred.get("custom_start_date") or ""),
        custom_end_date=str(cred.get("custom_end_date") or ""),
        use_wmi_health=bool(cred.get("use_wmi_health") or False),
        wmi_username=str(cred.get("wmi_username") or ""),
        wmi_password="",
        timeout_seconds=int(cred.get("timeout_seconds") or 30),
        verify_tls=bool(cred.get("verify_tls") or False),
        use_authc_header=bool(cred.get("use_authc_header") or False),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report_cred.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add nwdash/report_cred.py tests/test_report_cred.py
git commit -m "feat(reports): CredentialStore — machine-DPAPI password sealing + ApiConfig mapping"
```

---

## Task 3: ReportJob model + JobStore

**Files:**
- Create: `nwdash/report_jobs.py`
- Test: `tests/test_report_jobs_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_jobs_store.py
import importlib
from nwdash import config, report_jobs

def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_JOBS_FILE", tmp_path / "report_jobs.json")
    importlib.reload(report_jobs)  # rebind module-level file reference
    return report_jobs

def test_new_job_defaults(tmp_path, monkeypatch):
    rj = _fresh_store(tmp_path, monkeypatch)
    job = rj.ReportJob(id="j1", kind="digest", recipients=["a@x.com"])
    assert job.enabled is False
    assert job.health.state == "never_run"
    assert job.schedule.report_time == "08:00"

def test_persist_and_restore_roundtrip(tmp_path, monkeypatch):
    rj = _fresh_store(tmp_path, monkeypatch)
    job = rj.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True)
    rj.put_job(job)
    rj.persist_jobs()
    rj.clear_jobs_in_memory()
    assert rj.get_job("j1") is None
    n = rj.restore_jobs_from_disk()
    assert n == 1
    got = rj.get_job("j1")
    assert got is not None and got.enabled is True and got.recipients == ["a@x.com"]

def test_atomic_write_leaves_no_tmp(tmp_path, monkeypatch):
    rj = _fresh_store(tmp_path, monkeypatch)
    rj.put_job(rj.ReportJob(id="j1", kind="digest", recipients=["a@x.com"]))
    rj.persist_jobs()
    assert not (tmp_path / "report_jobs.json.tmp").exists()
    assert (tmp_path / "report_jobs.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report_jobs_store.py -v`
Expected: FAIL — `AttributeError: module 'nwdash.report_jobs' has no attribute 'ReportJob'`

- [ ] **Step 3: Write minimal implementation**

```python
# nwdash/report_jobs.py
"""Report job model, persistence, validation, and the scheduler loop.

A ReportJob is self-contained: it owns its NetWorker credential and every
setting needed to render and send. Nothing here reads the interactive session
store."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import REPORT_JOBS_FILE, DATA_DIR


@dataclass
class JobSchedule:
    report_time: str = "08:00"      # digest fire time HH:MM (local)
    cadence: str = "daily"          # digest cadence
    interval_minutes: int = 1440    # alert cadence
    trigger: str = "critical"       # alert severity threshold


@dataclass
class JobHealth:
    last_run: float = 0.0
    last_result: str = ""
    last_success: float = 0.0
    next_run: float = 0.0
    consecutive_failures: int = 0
    state: str = "never_run"        # never_run | healthy | unhealthy


@dataclass
class ReportJob:
    id: str
    kind: str                       # "digest" | "alert"
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
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = {j.id: asdict(j) for j in jobs_snapshot()}
        tmp = REPORT_JOBS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
        tmp.replace(REPORT_JOBS_FILE)
    except (OSError, TypeError, ValueError):
        pass


def restore_jobs_from_disk() -> int:
    if not REPORT_JOBS_FILE.exists():
        return 0
    try:
        records = json.loads(REPORT_JOBS_FILE.read_text(encoding="utf-8"))
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report_jobs_store.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add nwdash/report_jobs.py tests/test_report_jobs_store.py
git commit -m "feat(reports): ReportJob model + atomic JobStore persistence"
```

---

## Task 4: ReportRenderer + LastGoodCache

**Files:**
- Create: `nwdash/report_render.py`
- Test: `tests/test_report_render.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_render.py
import importlib
from http import HTTPStatus
from nwdash import config, report_render

def test_render_ok(monkeypatch):
    importlib.reload(report_render)
    monkeypatch.setattr(report_render, "build_dashboard",
                        lambda cfg: (HTTPStatus.OK, {"summary": {"totalJobs": 5}}))
    cred = {"rest_api_host": "h", "rest_api_port": 1, "username": "u",
            "encrypted_password": "", "api_mode": "nwui", "report_range": "7d"}
    res = report_render.render(cred)
    assert res.ok is True
    assert res.dashboard["summary"]["totalJobs"] == 5
    assert res.error == ""

def test_render_failure_surfaces_error(monkeypatch):
    importlib.reload(report_render)
    monkeypatch.setattr(report_render, "build_dashboard",
                        lambda cfg: (HTTPStatus.UNAUTHORIZED, {"error": "login rejected"}))
    res = report_render.render({"rest_api_host": "h", "username": "u", "encrypted_password": ""})
    assert res.ok is False
    assert "login rejected" in res.error

def test_last_good_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_CACHE_DIR", tmp_path / "cache")
    importlib.reload(report_render)
    assert report_render.cache_get("j1") is None
    report_render.cache_put("j1", {"summary": {"totalJobs": 3}})
    assert report_render.cache_get("j1")["summary"]["totalJobs"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nwdash.report_render'`

- [ ] **Step 3: Write minimal implementation**

```python
# nwdash/report_render.py
"""Render a report job by connecting FRESH to NetWorker from the job's own
credential — no session store. Also a small per-job disk cache of the last
successful dashboard, used for fallback emails."""
from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from .config import REPORT_CACHE_DIR
from .report_cred import credential_to_apiconfig
from .sessions import build_dashboard


@dataclass
class RenderResult:
    ok: bool
    dashboard: dict[str, Any]
    error: str = ""


def render(cred: dict[str, Any]) -> RenderResult:
    cfg = credential_to_apiconfig(cred)
    status, body = build_dashboard(cfg)
    if status == HTTPStatus.OK:
        return RenderResult(True, body, "")
    err = body.get("error") if isinstance(body, dict) else None
    return RenderResult(False, body if isinstance(body, dict) else {},
                        str(err or f"NetWorker returned HTTP {int(status)}"))


def _cache_path(job_id: str):
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_.")
    return REPORT_CACHE_DIR / f"{safe}.json"


def cache_put(job_id: str, dashboard: dict[str, Any]) -> None:
    try:
        REPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(job_id).write_text(json.dumps(dashboard), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def cache_get(job_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_cache_path(job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report_render.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add nwdash/report_render.py tests/test_report_render.py
git commit -m "feat(reports): ReportRenderer (session-free) + LastGoodCache"
```

---

## Task 5: Notifier

Wraps the existing `send_smtp_email` behind a small settings adapter, and adds a STALE banner + ops-alert message.

**Files:**
- Create: `nwdash/report_notify.py`
- Test: `tests/test_report_notify.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_notify.py
import importlib
from nwdash import report_notify

class _Job:
    id = "j1"; recipients = ["a@x.com"]; theme = "default"; kind = "digest"

def test_send_report_calls_smtp_with_recipients(monkeypatch):
    importlib.reload(report_notify)
    calls = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        calls["to"] = list(settings.recipients); calls["subject"] = subject
        calls["stale"] = "STALE" in (html_body + body)
        return {"host": settings.smtp_host}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    smtp = {"host": "203.0.113.7", "port": 25, "security": "none", "from": "r@x.com", "username": ""}
    report_notify.send_report(_Job(), {"summary": {}}, smtp, "", stale=False)
    assert calls["to"] == ["a@x.com"]
    assert calls["stale"] is False

def test_send_report_stale_banner(monkeypatch):
    importlib.reload(report_notify)
    seen = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        seen["stale"] = ("STALE" in html_body) or ("stale" in body.lower()); return {}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    smtp = {"host": "h", "port": 25, "security": "none", "from": "r@x.com", "username": ""}
    report_notify.send_report(_Job(), {"summary": {}}, smtp, "", stale=True)
    assert seen["stale"] is True

def test_send_ops_alert_uses_ops_address(monkeypatch):
    importlib.reload(report_notify)
    seen = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        seen["to"] = list(settings.recipients); seen["subject"] = subject; return {}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    smtp = {"host": "h", "port": 25, "security": "none", "from": "r@x.com", "username": ""}
    report_notify.send_ops_alert(_Job(), "connect failed", smtp, "ops@x.com", "")
    assert seen["to"] == ["ops@x.com"]
    assert "FAILED" in seen["subject"].upper()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report_notify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nwdash.report_notify'`

- [ ] **Step 3: Write minimal implementation**

```python
# nwdash/report_notify.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report_notify.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add nwdash/report_notify.py tests/test_report_notify.py
git commit -m "feat(reports): Notifier — report + stale-banner + ops-alert emails"
```

---

## Task 6: JobValidator (the hard save gate)

**Files:**
- Modify: `nwdash/report_jobs.py` (append)
- Test: `tests/test_report_validator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_validator.py
import importlib
from nwdash import report_jobs

def _job():
    return report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"],
                                 credential={"rest_api_host": "h", "username": "u",
                                             "encrypted_password": ""})

def test_validate_all_pass(monkeypatch):
    importlib.reload(report_jobs)
    from nwdash import report_render
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(True, {"summary": {}}, ""))
    monkeypatch.setattr(report_jobs, "_smtp_probe", lambda smtp, pw: (True, ""))
    res = report_jobs.validate(_job(), smtp={"host": "h", "port": 25, "security": "none"}, smtp_password="")
    assert res.ok is True
    assert res.checks["credential"] and res.checks["render"] and res.checks["smtp"]

def test_validate_render_fails(monkeypatch):
    importlib.reload(report_jobs)
    from nwdash import report_render
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(False, {}, "login rejected"))
    monkeypatch.setattr(report_jobs, "_smtp_probe", lambda smtp, pw: (True, ""))
    res = report_jobs.validate(_job(), smtp={"host": "h", "port": 25, "security": "none"}, smtp_password="")
    assert res.ok is False
    assert res.checks["render"] is False
    assert "login rejected" in res.detail

def test_validate_smtp_fails(monkeypatch):
    importlib.reload(report_jobs)
    from nwdash import report_render
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(True, {"summary": {}}, ""))
    monkeypatch.setattr(report_jobs, "_smtp_probe", lambda smtp, pw: (False, "connection refused"))
    res = report_jobs.validate(_job(), smtp={"host": "h", "port": 25, "security": "none"}, smtp_password="")
    assert res.ok is False
    assert res.checks["smtp"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report_validator.py -v`
Expected: FAIL — `AttributeError: module 'nwdash.report_jobs' has no attribute 'validate'`

- [ ] **Step 3: Write minimal implementation**

Append to `nwdash/report_jobs.py`:

```python
import smtplib
from dataclasses import dataclass as _dataclass
from . import report_render


@_dataclass
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report_validator.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add nwdash/report_jobs.py tests/test_report_validator.py
git commit -m "feat(reports): JobValidator — connect + render + SMTP save gate"
```

---

## Task 7: Scheduler (fire logic + health)

**Files:**
- Modify: `nwdash/report_jobs.py` (append)
- Test: `tests/test_report_scheduler.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_scheduler.py
import importlib, time
from nwdash import report_jobs, report_render

def _cfg():
    return {"smtp": {"host": "h", "port": 25, "security": "none", "from": "r@x.com"},
            "smtp_password": "", "ops_address": "ops@x.com"}

def test_fire_success_sends_and_caches(monkeypatch):
    importlib.reload(report_jobs)
    sent = {}
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(True, {"summary": {"totalJobs": 4}}, ""))
    monkeypatch.setattr(report_jobs.report_render, "cache_put", lambda jid, dash: sent.setdefault("cached", dash))
    monkeypatch.setattr(report_jobs.report_notify, "send_report",
                        lambda job, dash, smtp, pw, stale=False: sent.update(report=True, stale=stale) or {})
    monkeypatch.setattr(report_jobs.report_notify, "send_ops_alert", lambda *a, **k: sent.update(ops=True))
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True,
                                credential={"rest_api_host": "h", "username": "u", "encrypted_password": ""})
    report_jobs.fire_job(job, _cfg())
    assert sent.get("report") is True and sent.get("stale") is False
    assert "cached" in sent and "ops" not in sent
    assert job.health.state == "healthy" and job.health.consecutive_failures == 0

def test_fire_failure_sends_fallback_and_ops_alert(monkeypatch):
    importlib.reload(report_jobs)
    sent = {}
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(False, {}, "netWorker down"))
    monkeypatch.setattr(report_jobs.report_render, "cache_get", lambda jid: {"summary": {"totalJobs": 9}})
    monkeypatch.setattr(report_jobs.report_notify, "send_report",
                        lambda job, dash, smtp, pw, stale=False: sent.update(report=True, stale=stale) or {})
    monkeypatch.setattr(report_jobs.report_notify, "send_ops_alert",
                        lambda job, err, smtp, ops, pw: sent.update(ops=True, err=err))
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True,
                                credential={"rest_api_host": "h", "username": "u", "encrypted_password": ""})
    report_jobs.fire_job(job, _cfg())
    assert sent.get("report") is True and sent.get("stale") is True   # fallback with stale banner
    assert sent.get("ops") is True and "netWorker down" in sent["err"]
    assert job.health.state == "unhealthy" and job.health.consecutive_failures == 1

def test_fire_failure_no_cache_still_alerts_ops(monkeypatch):
    importlib.reload(report_jobs)
    sent = {}
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(False, {}, "down"))
    monkeypatch.setattr(report_jobs.report_render, "cache_get", lambda jid: None)
    monkeypatch.setattr(report_jobs.report_notify, "send_report", lambda *a, **k: sent.update(report=True))
    monkeypatch.setattr(report_jobs.report_notify, "send_ops_alert", lambda *a, **k: sent.update(ops=True))
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True, credential={})
    report_jobs.fire_job(job, _cfg())
    assert "report" not in sent and sent.get("ops") is True

def test_disabled_job_does_not_fire(monkeypatch):
    importlib.reload(report_jobs)
    sent = {}
    monkeypatch.setattr(report_jobs.report_render, "render", lambda cred: sent.update(rendered=True))
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=False, credential={})
    report_jobs.fire_job(job, _cfg())
    assert "rendered" not in sent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report_scheduler.py -v`
Expected: FAIL — `AttributeError: module 'nwdash.report_jobs' has no attribute 'fire_job'`

- [ ] **Step 3: Write minimal implementation**

Append to `nwdash/report_jobs.py` (add `from . import report_notify` and `from .config import ... SHARED_REFRESH_STOP` imports at top; `SHARED_REFRESH_STOP` is exported from `nwdash.config` — verify and import):

```python
from datetime import datetime, timedelta
from . import report_notify
from .config import SHARED_REFRESH_STOP, debug_log

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
            job.health.next_run = now + max(60, job.schedule.interval_minutes * 60)  # guard double-fire
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report_scheduler.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Verify `SHARED_REFRESH_STOP` import path**

Run: `python -c "from nwdash.config import SHARED_REFRESH_STOP; print(type(SHARED_REFRESH_STOP).__name__)"`
Expected: `Event`
(If it lives in another module, adjust the import in `report_jobs.py` accordingly — grep: `grep -rn "SHARED_REFRESH_STOP =" nwdash/`.)

- [ ] **Step 6: Commit**

```bash
git add nwdash/report_jobs.py tests/test_report_scheduler.py
git commit -m "feat(reports): scheduler — fire logic, fallback, ops-alert, health, next-run"
```

---

## Task 8: Regression test — the original bug

Proves a job fires after a restart with ZERO sessions, because it owns its credential.

**Files:**
- Test: `tests/test_report_no_session_regression.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_no_session_regression.py
"""Regression: the exact production failure. A restart wipes all in-memory
sessions; an enabled report job must STILL render and send because it carries
its own credential — nothing here touches the session store."""
import importlib
from http import HTTPStatus
from nwdash import report_jobs, report_render, sessions

def test_enabled_job_fires_with_no_sessions(monkeypatch):
    importlib.reload(report_jobs)
    # Simulate a fresh process: no sessions at all.
    monkeypatch.setattr(sessions, "_SESSIONS", {}, raising=False)
    # NetWorker answers because the job's OWN credential is used (build_dashboard).
    monkeypatch.setattr(report_render, "build_dashboard",
                        lambda cfg: (HTTPStatus.OK, {"summary": {"totalJobs": 7}}))
    sent = {}
    monkeypatch.setattr(report_jobs.report_notify, "send_report",
                        lambda job, dash, smtp, pw, stale=False: sent.update(ok=True, n=dash["summary"]["totalJobs"]))
    monkeypatch.setattr(report_jobs.report_render, "cache_put", lambda *a, **k: None)
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True,
                                credential={"rest_api_host": "h", "username": "administrator",
                                            "encrypted_password": "", "api_mode": "nwui"})
    report_jobs.fire_job(job, {"smtp": {"host": "h", "port": 25, "security": "none"}, "smtp_password": ""})
    assert sent.get("ok") is True and sent.get("n") == 7
    assert job.health.state == "healthy"
```

- [ ] **Step 2: Run test to verify it fails then passes**

Run: `python -m pytest tests/test_report_no_session_regression.py -v`
Expected: PASS (the machinery from Tasks 4/7 already supports it). If it fails, the fire path is wrongly consulting the session store — fix `report_render.render` to use only `build_dashboard(cfg)`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_report_no_session_regression.py
git commit -m "test(reports): regression — job fires with zero sessions after restart"
```

---

## Task 9: API router

**Files:**
- Create: `nwdash/report_api.py`
- Test: `tests/test_report_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_api.py
import importlib
from http import HTTPStatus
from nwdash import report_api, report_jobs

def _payload(**kw):
    base = {"action": "create", "kind": "digest", "recipients": "a@x.com, b@x.com",
            "reportTime": "07:30",
            "credential": {"rest_api_host": "h", "rest_api_port": 9090, "username": "u",
                           "password": "pw", "api_mode": "nwui"}}
    base.update(kw); return base

def test_create_rejected_when_validation_fails(monkeypatch):
    importlib.reload(report_jobs); importlib.reload(report_api)
    monkeypatch.setattr(report_api.report_jobs, "validate",
                        lambda job, smtp, smtp_password: report_jobs.ValidationResult(
                            False, {"credential": True, "render": False, "smtp": True}, "login rejected"))
    monkeypatch.setattr(report_api, "_smtp_config", lambda: ({"host": "h"}, ""))
    status, body = report_api.handle_report_jobs(_payload())
    assert status == HTTPStatus.BAD_REQUEST
    assert body["ok"] is False
    assert body["checks"]["render"] is False
    assert report_jobs.get_job(body.get("id", "")) is None  # not stored

def test_create_stores_enabled_job_when_valid(monkeypatch):
    importlib.reload(report_jobs); importlib.reload(report_api)
    monkeypatch.setattr(report_api.report_jobs, "validate",
                        lambda job, smtp, smtp_password: report_jobs.ValidationResult(
                            True, {"credential": True, "render": True, "smtp": True}, ""))
    monkeypatch.setattr(report_api, "_smtp_config", lambda: ({"host": "h"}, ""))
    monkeypatch.setattr(report_api.report_jobs, "persist_jobs", lambda: None)
    status, body = report_api.handle_report_jobs(_payload())
    assert status == HTTPStatus.OK and body["ok"] is True
    job = report_jobs.get_job(body["id"])
    assert job is not None and job.enabled is True
    assert job.recipients == ["a@x.com", "b@x.com"]
    assert job.credential["encrypted_password"]        # sealed, not plaintext
    assert "password" not in job.credential

def test_list_returns_health(monkeypatch):
    importlib.reload(report_jobs); importlib.reload(report_api)
    report_jobs.put_job(report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True))
    status, body = report_api.handle_report_jobs({"action": "list"})
    assert status == HTTPStatus.OK
    assert body["jobs"][0]["id"] == "j1"
    assert "health" in body["jobs"][0]

def test_delete_removes_job(monkeypatch):
    importlib.reload(report_jobs); importlib.reload(report_api)
    monkeypatch.setattr(report_api.report_jobs, "persist_jobs", lambda: None)
    report_jobs.put_job(report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"]))
    status, body = report_api.handle_report_jobs({"action": "delete", "id": "j1"})
    assert status == HTTPStatus.OK and report_jobs.get_job("j1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nwdash.report_api'`

- [ ] **Step 3: Write minimal implementation**

```python
# nwdash/report_api.py
"""HTTP action router for /api/report-jobs. Enforces the hard save gate:
create/update run JobValidator and refuse to enable a job unless all checks
pass."""
from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

from . import report_jobs
from .report_cred import encrypt_credential_password
from .emailer import saved_email_smtp_password
from .config import EMAIL_CONFIG_FILE
import json


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
    """Replace plaintext password with an encrypted token; never keep plaintext."""
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
        # On update with no new password, keep the sealed one.
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report_api.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Verify `saved_email_smtp_password` is importable from emailer**

Run: `python -c "from nwdash.emailer import saved_email_smtp_password; print('ok')"`
Expected: `ok` (it is imported in emailer per earlier grep; if it lives in `secrets`, adjust the import).

- [ ] **Step 6: Commit**

```bash
git add nwdash/report_api.py tests/test_report_api.py
git commit -m "feat(reports): /api/report-jobs router with hard save gate"
```

---

## Task 10: Boot wiring + route + deprecation shim

**Files:**
- Modify: `nwdash/main.py` (imports; the boot block near the existing `restore_automations_from_disk()` at ~227 and scheduler thread at ~243)
- Modify: request dispatch (find where `/api/alert-automation` is routed — grep `alert-automation` in `nwdash/main.py`/`server.py`/`restapi.py`)
- Test: `tests/test_report_boot.py`

- [ ] **Step 1: Locate the alert-automation route**

Run: `grep -rn "alert-automation\|handle_alert_automation\|restore_automations_from_disk\|automation_scheduler_loop" nwdash/main.py nwdash/server.py nwdash/restapi.py`
Note the file + line where `/api/alert-automation` dispatches to `handle_alert_automation`, and where the boot restore/scheduler start live.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_report_boot.py
import importlib
from nwdash import report_jobs, main

def test_report_cfg_provider_shape(monkeypatch):
    importlib.reload(report_jobs)
    monkeypatch.setattr(main, "_report_smtp_config", lambda: ({"host": "h", "port": 25}, ""))
    cfg = main.report_cfg_provider()
    assert "smtp" in cfg and "smtp_password" in cfg and "ops_address" in cfg
```

- [ ] **Step 3: Add boot wiring to `nwdash/main.py`**

Add imports near the other `from .emailer import ...`:

```python
from .report_jobs import restore_jobs_from_disk, scheduler_loop as report_scheduler_loop, persist_jobs as persist_report_jobs
from .report_api import handle_report_jobs, _smtp_config as _report_smtp_config
```

Add a config provider (module-level function) and ops-address source. Place near the other boot helpers:

```python
def report_cfg_provider() -> dict:
    """Fresh SMTP + ops-address config each fire (so admin edits take effect)."""
    smtp, smtp_password = _report_smtp_config()
    return {"smtp": smtp, "smtp_password": smtp_password,
            "ops_address": smtp.get("opsAlertAddress", "") if isinstance(smtp, dict) else ""}
```

In the boot block, right after `automations = restore_automations_from_disk()` (~line 227) add:

```python
    report_count = restore_jobs_from_disk()
    LOG.info(f"restored {report_count} scheduled report job(s)", extra={"event": "startup"})
```

Right after the existing automation scheduler thread start (~line 243) add:

```python
    threading.Thread(target=report_scheduler_loop, args=(report_cfg_provider,),
                     name="report-scheduler", daemon=True).start()
```

- [ ] **Step 4: Route `/api/report-jobs`**

In the request dispatcher (same file/pattern where `/api/alert-automation` is handled), add a branch that reads the JSON body into `payload` exactly as the alert-automation branch does, then:

```python
        if path == "/api/report-jobs":
            status, body = handle_report_jobs(payload)
            return self._send_json(status, body)   # match the existing helper name used nearby
```

- [ ] **Step 5: Deprecation shim for `/api/alert-automation`**

Replace the body of the `/api/alert-automation` branch so it no longer arms anything, returning a clear message (keep it reachable so old cached SPAs get a signal):

```python
        if path == "/api/alert-automation":
            return self._send_json(HTTPStatus.GONE, {
                "ok": False,
                "message": ("Email automation moved to Scheduled Reports. Reload the "
                            "dashboard and re-create your schedules under Scheduled Reports."),
            })
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_report_boot.py -v`
Expected: PASS (1 passed)
Run: `python -c "import nwdash.main"` — Expected: no ImportError.

- [ ] **Step 7: Commit**

```bash
git add nwdash/main.py tests/test_report_boot.py
git commit -m "feat(reports): boot restore + scheduler start, route /api/report-jobs, deprecate alert-automation"
```

---

## Task 11: UI — Scheduled Reports panel

**Files:**
- Modify: `nwdash/assets/dashboard.html` (add the panel markup + a nav entry)
- Modify: `nwdash/assets/app.js` (fetch/render jobs, create/edit/delete, test button)
- Modify: `nwdash/assets/app.css` (health badges)
- Test: `tests/test_reports_ui_assets.py`

- [ ] **Step 1: Write the failing test (asset guard)**

```python
# tests/test_reports_ui_assets.py
from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_dashboard_has_reports_panel():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="scheduledReportsPanel"' in html

def test_appjs_wires_report_endpoints():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/report-jobs" in js
    assert "renderReportJobs" in js       # list renderer
    assert "reportHealthBadge" in js      # health badge helper
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reports_ui_assets.py -v`
Expected: FAIL (markers absent).

- [ ] **Step 3: Add the panel markup**

In `nwdash/assets/dashboard.html`, add (near the existing Email Alert Automation modal, or as a new panel):

```html
<section id="scheduledReportsPanel" class="panel" hidden>
  <header class="panel-head">
    <h2>Scheduled Reports</h2>
    <button id="reportAddBtn" type="button">New report</button>
  </header>
  <div id="reportJobsList" class="report-jobs"></div>
  <form id="reportJobForm" class="report-form" hidden>
    <label>NetWorker host <input name="rest_api_host" required></label>
    <label>Port <input name="rest_api_port" type="number" value="9090"></label>
    <label>Username <input name="username" required></label>
    <label>Password <input name="password" type="password" required></label>
    <label>Recipients <input name="recipients" required placeholder="a@x.com, b@x.com"></label>
    <label>Report time <input name="reportTime" value="07:30" placeholder="HH:MM"></label>
    <div id="reportFormError" class="form-error" role="alert"></div>
    <button id="reportTestSaveBtn" type="submit">Validate &amp; save</button>
  </form>
</section>
```

- [ ] **Step 4: Add the JS (list, create with gate feedback, delete)**

In `nwdash/assets/app.js`, add:

```javascript
function reportHealthBadge(state) {
  const map = {healthy: "ok", unhealthy: "bad", never_run: "idle"};
  const cls = map[state] || "idle";
  return `<span class="health-badge health-${cls}">${state.replace("_", " ")}</span>`;
}

async function renderReportJobs() {
  const list = document.getElementById("reportJobsList");
  const r = await fetch("/api/report-jobs", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "list"}),
  });
  const data = await r.json();
  list.innerHTML = (data.jobs || []).map(j => `
    <div class="report-job">
      <div class="rj-main">
        <strong>${j.kind}</strong> → ${j.recipients.join(", ")}
        · ${j.kind === "digest" ? "at " + j.reportTime : "every " + j.intervalMinutes + " min"}
        ${reportHealthBadge(j.health.state)}
      </div>
      <div class="rj-sub">last: ${j.health.lastResult || "—"} · next: ${
        j.health.nextRun ? new Date(j.health.nextRun * 1000).toLocaleString() : "—"}</div>
      <button data-del="${j.id}" type="button">Delete</button>
    </div>`).join("") || "<p>No scheduled reports yet.</p>";
  list.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", async () => {
    await fetch("/api/report-jobs", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "delete", id: b.getAttribute("data-del")})});
    renderReportJobs();
  }));
}

async function submitReportJob(ev) {
  ev.preventDefault();
  const f = ev.target;
  const err = document.getElementById("reportFormError");
  err.textContent = "Validating (connect + render + SMTP)…";
  const payload = {
    action: "create", kind: "digest",
    recipients: f.recipients.value, reportTime: f.reportTime.value,
    credential: {
      rest_api_host: f.rest_api_host.value, rest_api_port: Number(f.rest_api_port.value),
      backup_server_host: f.rest_api_host.value, backup_server_port: Number(f.rest_api_port.value),
      username: f.username.value, password: f.password.value, api_mode: "nwui",
    },
  };
  const r = await fetch("/api/report-jobs", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)});
  const data = await r.json();
  if (!data.ok) {
    const failed = Object.entries(data.checks || {}).filter(([, v]) => !v).map(([k]) => k).join(", ");
    err.textContent = `Not saved — failed: ${failed || "validation"}. ${data.message || ""}`;
    return;
  }
  err.textContent = "";
  f.reset(); f.hidden = true;
  renderReportJobs();
}

function initScheduledReports() {
  const panel = document.getElementById("scheduledReportsPanel");
  if (!panel) return;
  document.getElementById("reportAddBtn").addEventListener("click",
    () => { document.getElementById("reportJobForm").hidden = false; });
  document.getElementById("reportJobForm").addEventListener("submit", submitReportJob);
  renderReportJobs();
}
```

Call `initScheduledReports();` from the existing DOM-ready/init path (where other panels initialise).

- [ ] **Step 5: Add health-badge CSS**

In `nwdash/assets/app.css`:

```css
.health-badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.health-ok   { background: #16794322; color: #16a34a; }
.health-bad  { background: #b4530922; color: #dc2626; }
.health-idle { background: #64748b22; color: #64748b; }
.report-job  { padding: 10px; border: 1px solid var(--line); border-radius: 8px; margin-bottom: 8px; }
.form-error  { color: #dc2626; min-height: 1.2em; }
```

- [ ] **Step 6: Run the asset test**

Run: `python -m pytest tests/test_reports_ui_assets.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Add the panel + assets to the bundle allow-list**

The assets already ship (`app.js`/`app.css`/`dashboard.html` are allow-listed in `deploy/build-bundle.ps1`). No new asset files, so no allow-list change. Confirm:

Run: `grep -c "assets\\\\app.js" deploy/build-bundle.ps1`
Expected: `1`

- [ ] **Step 8: Commit**

```bash
git add nwdash/assets/dashboard.html nwdash/assets/app.js nwdash/assets/app.css tests/test_reports_ui_assets.py
git commit -m "feat(reports): Scheduled Reports UI panel with per-job health + validate-and-save"
```

---

## Task 12: Remove the legacy automation path + migration notice

**Files:**
- Modify: `nwdash/emailer.py` (remove `AlertAutomation` scheduler/arm/session-snapshot code; keep `send_smtp_email` + SMTP helpers + `saved_email_smtp_password`)
- Modify: `nwdash/main.py` (drop the old `restore_automations_from_disk()` + `automation_scheduler_loop` thread; add a legacy-present flag)
- Modify: `nwdash/report_api.py` (add `legacyMigrationNeeded` to `list`)
- Modify: `nwdash/assets/app.js` (show a one-time notice when `legacyMigrationNeeded`)
- Test: `tests/test_legacy_migration_notice.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_legacy_migration_notice.py
import importlib
from http import HTTPStatus
from nwdash import config, report_api, report_jobs

def test_list_flags_legacy_when_automations_file_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOMATIONS_FILE", tmp_path / "automations.json")
    (tmp_path / "automations.json").write_text('{"x": {"schedule_type": "daily_report"}}', encoding="utf-8")
    importlib.reload(report_jobs); importlib.reload(report_api)
    status, body = report_api.handle_report_jobs({"action": "list"})
    assert status == HTTPStatus.OK
    assert body["legacyMigrationNeeded"] is True

def test_list_no_legacy_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOMATIONS_FILE", tmp_path / "nope.json")
    importlib.reload(report_jobs); importlib.reload(report_api)
    status, body = report_api.handle_report_jobs({"action": "list"})
    assert body["legacyMigrationNeeded"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_legacy_migration_notice.py -v`
Expected: FAIL — `KeyError: 'legacyMigrationNeeded'`

- [ ] **Step 3: Add the legacy flag to `list`**

In `nwdash/report_api.py`, add import `from .config import AUTOMATIONS_FILE` and extend the `list` branch:

```python
    if action == "list":
        return HTTPStatus.OK, {
            "ok": True,
            "jobs": [_job_public(j) for j in report_jobs.jobs_snapshot()],
            "legacyMigrationNeeded": AUTOMATIONS_FILE.exists(),
        }
```

- [ ] **Step 4: Run the migration-notice test**

Run: `python -m pytest tests/test_legacy_migration_notice.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Remove the legacy automation engine**

Delete from `nwdash/emailer.py`: `AlertAutomation` dataclass usage for scheduling, `schedule_alert_automation`, `automation_scheduler_tick/loop`, `run_alert_automation`, `_ensure_automation_session`, `restore_automations_from_disk`, `persist_automations`, `handle_alert_automation`, profile-arm helpers (`_arm_profile_automation`, toggle/save-profile handlers) and the connection-snapshot calls. KEEP: `send_smtp_email`, `smtp_*` helpers, `within_quiet_hours`, `saved_email_smtp_password`, `parse_smtp_settings` if used elsewhere.

Guard the removal by grep first:

Run: `grep -rn "handle_alert_automation\|restore_automations_from_disk\|automation_scheduler_loop\|run_alert_automation\|connection_snapshot_for_session\|recreate_session_from_snapshot" nwdash/ tests/`
Remove/adjust every caller. `connection_snapshot_for_session`/`recreate_session_from_snapshot` in `sessions.py` become dead — delete them and their tests.

- [ ] **Step 6: Remove old boot lines from `main.py`**

Delete the `restore_automations_from_disk()` call and the `automation_scheduler_loop` thread start (added report equivalents already replace them in Task 10).

- [ ] **Step 7: Add the one-time UI notice**

In `nwdash/assets/app.js` `renderReportJobs`, after fetching:

```javascript
  const notice = document.getElementById("reportLegacyNotice");
  if (notice) notice.hidden = !data.legacyMigrationNeeded;
```

And in `dashboard.html` inside the panel, above the list:

```html
  <div id="reportLegacyNotice" class="legacy-notice" hidden>
    Legacy email schedules were found. They cannot run under the new engine —
    re-create them here (enter the NetWorker credential once).
  </div>
```

- [ ] **Step 8: Run the FULL suite**

Run: `python -m pytest -q`
Expected: all pass except the pre-existing flaky `test_profile_toggle` E2E (if `test_profile_toggle.py` targeted the removed engine, delete it — it tested `AlertAutomation`). Fix any import breakage surfaced by the removal.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(reports): remove legacy AlertAutomation engine + session-snapshot path; add migration notice"
```

---

## Task 13: Version bump + docs

**Files:**
- Modify: `nwdash/config.py` (`APP_VERSION`), `pyproject.toml` (`version`)
- Modify: `README.md` / SOP docs as applicable
- Test: full suite + offline bundle build

- [ ] **Step 1: Bump version**

Set `APP_VERSION = "2.9.0"` in `nwdash/config.py:18` and `version = "2.9.0"` in `pyproject.toml:3` (minor: new subsystem + removed endpoint).

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest -q`
Expected: all green (minus known flaky E2E).

- [ ] **Step 3: Build the offline bundle (proves it packs + runtime imports)**

Run: `pwsh -ExecutionPolicy Bypass -File deploy\build-bundle.ps1 -SkipRuntimeFetch`
Expected: `done -> dist\nwdash-bundle-2.9.0-win-x64.zip`

- [ ] **Step 4: Commit**

```bash
git add nwdash/config.py pyproject.toml README.md
git commit -m "chore(reports): bump to 2.9.0 for Scheduled Reports subsystem"
```

---

## Deployment note (not a code task)

Ships as 2.9.0 with a service restart (`Setup-NWDash.cmd -Upgrade`). Post-deploy, the operator opens **Scheduled Reports**, sees the legacy notice, and re-creates the two production jobs once (daily digest 07:30 → 5 recipients; alert 1440 min → 2 recipients) by entering the NetWorker `administrator` credential. The save gate validates connect + render + SMTP before either goes Active; from then on the jobs are restart- and session-independent.
