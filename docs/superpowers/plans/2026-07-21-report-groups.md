# Scheduled Report Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the flat report-job model with named, ordered, toggleable **report groups** — each a subset of dashboard sections + recipients + daily/weekly/monthly retrospective cadence — pulling live data through one shared reporting connection, with on-demand and test send.

**Architecture:** Pure cadence math in `report_window.py` (daily/weekly-Sunday/monthly-1st → window + next_run). `ReportGroup` model + ordered store + a single session-free scheduler in `report_groups.py`, rendering fresh via the shared display/reporting connection (`display.py`) and emailing only the group's selected sections (`reports.py` gains a `sections` filter). `/api/report-groups` router. The old `ReportJob`/`/api/report-jobs` is removed.

**Tech Stack:** Python 3.12 stdlib, existing `nwdash` package, pytest, vanilla-JS SPA.

**Reused primitives:**
- `nwdash/restapi.py`: `report_window(config)`, `parse_custom_date_window(start,end)` — custom dates accept `DD-MM-YYYY` or `YYYY-MM-DD`; end date is inclusive (parser adds +1 day). `CUSTOM_REPORT_RANGE = "custom"` (config.py).
- `nwdash/models.py::ApiConfig` (has `report_range`, `custom_start_date`, `custom_end_date`).
- `nwdash/sessions.py::build_dashboard(config)` — fresh connect + build for a window.
- `nwdash/report_cred.py`: `credential_to_apiconfig`, `encrypt/decrypt_credential_password`.
- `nwdash/display.py`: `load_connection()`/`save_connection()` — the shared reporting connection (machine-DPAPI).
- `nwdash/reports.py`: `dashboard_report_email(dashboard, snapshot_cid="")`, `render_dashboard_snapshot_png(dashboard)`.
- `nwdash/report_notify.py`: `send_report`, `send_ops_alert`, `_settings`, STALE banner.
- `nwdash/report_render.py`: `RenderResult`, `render`, `cache_put/get`.
- SMTP config: `report_groups_api` reads it the way `report_api._smtp_config` does (`EMAIL_CONFIG_FILE` + `saved_email_smtp_password`).
- Scheduler tick pattern + `SHARED_REFRESH_STOP` (models.py), `debug_log` (config.py) — mirror `report_jobs.py`.

Repo gotcha (every task adding a `nwdash/*.py`): add it to `deploy/build-bundle.ps1` `$shipFiles` or `tests/test_deploy.py::TestBundleAllowList` fails. Run the FULL suite before each commit.

---

## File Structure
- Create `nwdash/report_window.py` — cadence → (window, next_run) pure functions + section keys.
- Create `nwdash/report_groups.py` — `ReportGroup` model, ordered store, validator, scheduler, fire, on-demand.
- Create `nwdash/report_groups_api.py` — `/api/report-groups` router.
- Modify `nwdash/report_render.py` — `render_window`.
- Modify `nwdash/reports.py` — `dashboard_report_email(dashboard, snapshot_cid="", sections=None)`.
- Modify `nwdash/report_notify.py` — `send_group_report`.
- Modify `nwdash/main.py`, `nwdash/server.py` — boot + routes; remove old job wiring.
- Remove `nwdash/report_jobs.py`, `nwdash/report_api.py` + their tests.
- Modify assets — group manager UI.

---

## Task 1: Section keys + cadence window math

**Files:** Create `nwdash/report_window.py`; Test `tests/test_report_window.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_report_window.py
from datetime import datetime
from nwdash import report_window as rw

def dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi).astimezone()

def test_section_keys_stable():
    assert rw.SECTION_KEYS == ["backup_sla", "management", "recovery", "clone", "alerts", "server_protection", "health"]

def test_daily_window_is_last_24h():
    r, s, e = rw.compute_window("daily", now=dt(2026, 7, 21, 9, 0))
    assert r == "24h" and s == "" and e == ""

def test_weekly_window_prev_sunday_to_saturday():
    # Tue 2026-07-21 -> previous week Sun 2026-07-12 .. Sat 2026-07-18
    r, s, e = rw.compute_window("weekly", now=dt(2026, 7, 21, 9, 0))
    assert r == "custom" and s == "2026-07-12" and e == "2026-07-18"

def test_monthly_window_prev_calendar_month():
    r, s, e = rw.compute_window("monthly", now=dt(2026, 7, 21, 9, 0))
    assert r == "custom" and s == "2026-06-01" and e == "2026-06-30"

def test_monthly_window_january_crosses_year():
    r, s, e = rw.compute_window("monthly", now=dt(2026, 1, 15))
    assert s == "2025-12-01" and e == "2025-12-31"

def test_next_run_daily_today_if_time_ahead():
    nr = rw.next_run("daily", "09:30", now=dt(2026, 7, 21, 9, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 7, 21, 9, 30)

def test_next_run_daily_tomorrow_if_time_passed():
    nr = rw.next_run("daily", "08:00", now=dt(2026, 7, 21, 9, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 7, 22, 8, 0)

def test_next_run_weekly_next_sunday():
    # Tue 2026-07-21 -> next Sunday 2026-07-26 at 07:00
    nr = rw.next_run("weekly", "07:00", now=dt(2026, 7, 21, 9, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 7, 26, 7, 0)

def test_next_run_weekly_today_sunday_time_ahead():
    # Sunday 2026-07-26 08:00, send 09:00 -> same day
    nr = rw.next_run("weekly", "09:00", now=dt(2026, 7, 26, 8, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 7, 26, 9, 0)

def test_next_run_monthly_first_of_next_month():
    nr = rw.next_run("monthly", "06:00", now=dt(2026, 7, 21, 9, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 8, 1, 6, 0)
```

- [ ] **Step 2: Run → FAIL.** `python -m pytest tests/test_report_window.py -v`

- [ ] **Step 3: Implement `nwdash/report_window.py`**
```python
"""Pure cadence math for report groups: which window a report covers and when
it next fires. All functions take an explicit `now` (a tz-aware datetime) so
they are deterministic and unit-testable — no wall-clock reads here."""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta

SECTION_KEYS = ["backup_sla", "management", "recovery", "clone", "alerts", "server_protection", "health"]
CADENCES = ("daily", "weekly", "monthly")


def _prev_sunday(d: datetime) -> datetime:
    # Monday=0..Sunday=6; the Sunday on/before d.
    return (d - timedelta(days=(d.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)


def compute_window(cadence: str, now: datetime) -> tuple[str, str, str]:
    """Return (report_range, custom_start 'YYYY-MM-DD', custom_end 'YYYY-MM-DD').
    Daily -> ('24h','',''). Weekly/monthly -> ('custom', start, end) covering the
    PREVIOUS completed period. custom_end is the inclusive last day."""
    if cadence == "daily":
        return "24h", "", ""
    if cadence == "weekly":
        this_sun = _prev_sunday(now)
        start = this_sun - timedelta(days=7)          # previous Sunday
        end = this_sun - timedelta(days=1)            # previous Saturday
        return "custom", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    if cadence == "monthly":
        first_this = now.replace(day=1)
        last_prev = first_this - timedelta(days=1)    # last day of previous month
        start = last_prev.replace(day=1)
        return "custom", start.strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d")
    raise ValueError(f"unknown cadence {cadence!r}")


def _at_time(d: datetime, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":", 1))
    return d.replace(hour=hh, minute=mm, second=0, microsecond=0)


def next_run(cadence: str, send_time: str, now: datetime) -> float:
    """Epoch seconds of the next fire. Daily: next send_time. Weekly: next Sunday
    at send_time. Monthly: next 1st at send_time."""
    if cadence == "daily":
        target = _at_time(now, send_time)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()
    if cadence == "weekly":
        days_to_sun = (6 - now.weekday()) % 7          # Monday=0 -> 6 days to Sunday; Sunday=6 -> 0
        target = _at_time(now + timedelta(days=days_to_sun), send_time)
        if target <= now:
            target += timedelta(days=7)
        return target.timestamp()
    if cadence == "monthly":
        target = _at_time(now.replace(day=1), send_time)
        if target <= now:
            year = now.year + (1 if now.month == 12 else 0)
            month = 1 if now.month == 12 else now.month + 1
            target = _at_time(now.replace(year=year, month=month, day=1), send_time)
        return target.timestamp()
    raise ValueError(f"unknown cadence {cadence!r}")
```
Note: `calendar` import is unused unless you prefer `calendar.monthrange` for the month end — the `first_this - 1 day` approach above already yields the correct last day; drop the `import calendar` if unused to keep the module clean.

- [ ] **Step 4: Run → PASS (10).** Then FULL suite.

- [ ] **Step 5: Add `'nwdash\report_window.py',` to `deploy/build-bundle.ps1` `$shipFiles`. Commit:**
```bash
git add nwdash/report_window.py tests/test_report_window.py deploy/build-bundle.ps1
git commit -m "feat(groups): report_window — cadence window + next-run math + section keys"
```

---

## Task 2: Section-filtered report email

**Files:** Modify `nwdash/reports.py`; Test `tests/test_report_sections.py`

The current `dashboard_report_email(dashboard, snapshot_cid="")` builds one HTML string of `<section>` cards. Add an optional `sections` parameter that includes only the requested section blocks. `sections=None` keeps ALL (current behavior, so existing callers/tests are unchanged).

- [ ] **Step 1: Read** `dashboard_report_email` fully (`nwdash/reports.py` ~line 630-760). Identify each `<section>`/card block and which SECTION_KEY it maps to: Backup SLA→`backup_sla`, Management Overview→`management`, Recovery Health→`recovery`, Clone Jobs→`clone`, Alerts→`alerts`, Server Protection Job→`server_protection`, Storage/Health→`health`.

- [ ] **Step 2: Write the failing test** `tests/test_report_sections.py`:
```python
from nwdash.reports import dashboard_report_email

def _dash():
    return {"summary": {"totalJobs": 10, "successfulJobs": 9, "failedJobs": 1, "recoveryJobs": 2,
            "cloneJobs": 3, "totalAlerts": 4, "slaPercent": 90, "slaMetJobs": 9, "slaTotalJobs": 10},
            "range": "Last 24 Hours", "tables": {}, "alerts": [], "protection": {"label": "OK", "detail": ""},
            "health": {}}

def test_all_sections_default():
    plain, html = dashboard_report_email(_dash())
    assert "Backup SLA" in html and "Clone Jobs" in html and "Recovery Health" in html

def test_only_selected_sections():
    plain, html = dashboard_report_email(_dash(), sections=["backup_sla", "alerts"])
    assert "Backup SLA" in html
    assert "Clone Jobs" not in html
    assert "Recovery Health" not in html

def test_empty_sections_lists_none_of_the_cards():
    plain, html = dashboard_report_email(_dash(), sections=[])
    assert "Clone Jobs" not in html and "Recovery Health" not in html
```

- [ ] **Step 3: Run → FAIL** (TypeError: unexpected `sections`).

- [ ] **Step 4: Implement.** Change the signature to `def dashboard_report_email(dashboard, snapshot_cid="", sections=None)`. Introduce a helper `_want(key)`:
```python
    want = set(sections) if sections is not None else None
    def _want(key: str) -> bool:
        return want is None or key in want
```
Wrap each card block's HTML so it is only concatenated when `_want("<key>")` is true. Concretely, refactor the big f-string into per-section HTML fragments assembled into a list, each guarded by `_want(...)`, then `"".join(...)`. Keep the brand/header (always shown) and the plain-text body building the same lines but also gate the per-section plain lines by `_want`. Preserve every existing class/markup for shown sections (do not restyle). `sections=None` MUST reproduce the current output for all callers.

- [ ] **Step 5: Run → PASS (3).** Then FULL suite (the existing report/email tests must still pass — they call with no `sections`).

- [ ] **Step 6: Commit:**
```bash
git add nwdash/reports.py tests/test_report_sections.py
git commit -m "feat(groups): dashboard_report_email section filtering (sections=None keeps all)"
```

---

## Task 3: `render_window` (session-free, windowed)

**Files:** Modify `nwdash/report_render.py`; Test `tests/test_render_window.py`

- [ ] **Step 1: Failing test** `tests/test_render_window.py`:
```python
import importlib
from http import HTTPStatus
from nwdash import report_render

def test_render_window_sets_range_and_customs(monkeypatch):
    importlib.reload(report_render)
    captured = {}
    def fake_build(cfg):
        captured["range"] = cfg.report_range
        captured["start"] = cfg.custom_start_date
        captured["end"] = cfg.custom_end_date
        return HTTPStatus.OK, {"summary": {}}
    monkeypatch.setattr(report_render, "build_dashboard", fake_build)
    cred = {"rest_api_host": "h", "username": "u", "encrypted_password": "", "api_mode": "nwui"}
    res = report_render.render_window(cred, ("custom", "2026-06-01", "2026-06-30"))
    assert res.ok is True
    assert captured["range"] == "custom" and captured["start"] == "2026-06-01" and captured["end"] == "2026-06-30"

def test_render_window_daily(monkeypatch):
    importlib.reload(report_render)
    monkeypatch.setattr(report_render, "build_dashboard", lambda cfg: (HTTPStatus.OK, {"summary": {}}))
    res = report_render.render_window({"rest_api_host": "h", "username": "u", "encrypted_password": ""}, ("24h", "", ""))
    assert res.ok is True
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** in `report_render.py` (uses `credential_to_apiconfig` + `dataclasses.replace`):
```python
from dataclasses import replace

def render_window(cred: dict, window: tuple[str, str, str]) -> RenderResult:
    """Render fresh for a cadence window. window = (report_range, custom_start, custom_end)."""
    report_range, start, end = window
    cfg = credential_to_apiconfig(cred)
    cfg = replace(cfg, report_range=report_range, custom_start_date=start, custom_end_date=end)
    status, body = build_dashboard(cfg)
    if status == HTTPStatus.OK:
        return RenderResult(True, body, "")
    err = body.get("error") if isinstance(body, dict) else None
    return RenderResult(False, body if isinstance(body, dict) else {},
                        str(err or f"NetWorker returned HTTP {int(status)}"))
```
(`RenderResult`, `credential_to_apiconfig`, `build_dashboard`, `HTTPStatus` are already imported in this module from 2.9.0.)

- [ ] **Step 4: Run → PASS (2).** FULL suite.

- [ ] **Step 5: Commit:**
```bash
git add nwdash/report_render.py tests/test_render_window.py
git commit -m "feat(groups): render_window — windowed session-free render"
```

---

## Task 4: `ReportGroup` model + ordered store

**Files:** Create `nwdash/report_groups.py`; Test `tests/test_report_groups_store.py`

Add `REPORT_GROUPS_FILE = DATA_DIR / "report_groups.json"` to `nwdash/config.py` first (Step 0).

- [ ] **Step 0:** In `nwdash/config.py`, after `DISPLAY_CONNECTION_FILE = ...`, add:
```python
REPORT_GROUPS_FILE = DATA_DIR / "report_groups.json"
```
Verify: `python -c "from nwdash.config import REPORT_GROUPS_FILE; print(REPORT_GROUPS_FILE.name)"` → `report_groups.json`.

- [ ] **Step 1: Failing test** `tests/test_report_groups_store.py`:
```python
import importlib
from nwdash import config, report_groups

def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_GROUPS_FILE", tmp_path / "report_groups.json")
    importlib.reload(report_groups)
    return report_groups

def test_defaults(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    grp = g.ReportGroup(id="a", name="Ops", sections=["backup_sla"], recipients=["a@x.com"])
    assert grp.enabled is False and grp.cadence == "daily" and grp.send_time == "08:00"
    assert grp.health.state == "never_run" and grp.position == 0

def test_put_assigns_incrementing_positions(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    g.put_group(g.ReportGroup(id="a", name="A", sections=["alerts"], recipients=["a@x.com"]))
    g.put_group(g.ReportGroup(id="b", name="B", sections=["alerts"], recipients=["b@x.com"]))
    ordered = g.groups_ordered()
    assert [x.id for x in ordered] == ["a", "b"] and [x.position for x in ordered] == [0, 1]

def test_persist_restore_roundtrip(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    g.put_group(g.ReportGroup(id="a", name="A", sections=["clone"], recipients=["a@x.com"], enabled=True, cadence="weekly", send_time="07:00"))
    g.persist_groups(); g.clear_groups_in_memory()
    assert g.restore_groups_from_disk() == 1
    got = g.get_group("a")
    assert got.enabled and got.cadence == "weekly" and got.sections == ["clone"]

def test_delete_repacks_positions(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    for i in "abc":
        g.put_group(g.ReportGroup(id=i, name=i, sections=["alerts"], recipients=["x@x.com"]))
    g.delete_group("a")
    assert [x.position for x in g.groups_ordered()] == [0, 1]

def test_reorder(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    for i in "abc":
        g.put_group(g.ReportGroup(id=i, name=i, sections=["alerts"], recipients=["x@x.com"]))
    g.reorder(["c", "a", "b"])
    assert [x.id for x in g.groups_ordered()] == ["c", "a", "b"]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `nwdash/report_groups.py`** (store portion):
```python
"""Report groups: model, ordered persistence, validation, scheduler, fire.
Session-free — renders via the shared display/reporting connection."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config


@dataclass
class GroupHealth:
    last_run: float = 0.0
    last_success: float = 0.0
    next_run: float = 0.0
    last_result: str = ""
    state: str = "never_run"


@dataclass
class ReportGroup:
    id: str
    name: str
    sections: list[str] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    enabled: bool = False
    cadence: str = "daily"
    send_time: str = "08:00"
    position: int = 0
    health: GroupHealth = field(default_factory=GroupHealth)


_GROUPS: dict[str, ReportGroup] = {}
_LOCK = threading.Lock()


def put_group(grp: ReportGroup) -> None:
    with _LOCK:
        if grp.id not in _GROUPS and grp.position == 0:
            grp.position = len(_GROUPS)
        _GROUPS[grp.id] = grp


def get_group(gid: str) -> ReportGroup | None:
    with _LOCK:
        return _GROUPS.get(gid)


def groups_ordered() -> list[ReportGroup]:
    with _LOCK:
        return sorted(_GROUPS.values(), key=lambda g: g.position)


def clear_groups_in_memory() -> None:
    with _LOCK:
        _GROUPS.clear()


def _repack() -> None:
    for i, g in enumerate(sorted(_GROUPS.values(), key=lambda g: g.position)):
        g.position = i


def delete_group(gid: str) -> bool:
    with _LOCK:
        existed = _GROUPS.pop(gid, None) is not None
        if existed:
            _repack()
        return existed


def reorder(order: list[str]) -> None:
    with _LOCK:
        pos = {gid: i for i, gid in enumerate(order)}
        for gid, g in _GROUPS.items():
            if gid in pos:
                g.position = pos[gid]
        _repack()


def _group_from_dict(rec: dict[str, Any]) -> ReportGroup:
    h = rec.get("health") or {}
    return ReportGroup(
        id=str(rec["id"]), name=str(rec.get("name") or ""),
        sections=[str(s) for s in (rec.get("sections") or [])],
        recipients=[str(r) for r in (rec.get("recipients") or [])],
        enabled=bool(rec.get("enabled", False)),
        cadence=str(rec.get("cadence") or "daily"),
        send_time=str(rec.get("send_time") or "08:00"),
        position=int(rec.get("position") or 0),
        health=GroupHealth(**{k: h[k] for k in GroupHealth().__dict__ if k in h}),
    )


def persist_groups() -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = {g.id: asdict(g) for g in groups_ordered()}
        tmp = config.REPORT_GROUPS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
        tmp.replace(config.REPORT_GROUPS_FILE)
    except (OSError, TypeError, ValueError):
        pass


def restore_groups_from_disk() -> int:
    if not config.REPORT_GROUPS_FILE.exists():
        return 0
    try:
        records = json.loads(config.REPORT_GROUPS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(records, dict):
        return 0
    n = 0
    for rec in records.values():
        try:
            put_group(_group_from_dict(rec)); n += 1
        except (KeyError, TypeError, ValueError):
            continue
    return n
```

- [ ] **Step 4: Run → PASS (5).** Add `'nwdash\report_groups.py',` to build-bundle allow-list. FULL suite.

- [ ] **Step 5: Commit:**
```bash
git add nwdash/config.py nwdash/report_groups.py tests/test_report_groups_store.py deploy/build-bundle.ps1
git commit -m "feat(groups): ReportGroup model + ordered store"
```

---

## Task 5: Validator + group email

**Files:** Modify `nwdash/report_groups.py` (append), `nwdash/report_notify.py`; Test `tests/test_group_validate_notify.py`

- [ ] **Step 1: Failing test** `tests/test_group_validate_notify.py`:
```python
import importlib
from nwdash import report_groups, report_notify

def test_validate_ok():
    importlib.reload(report_groups)
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["backup_sla"], recipients=["a@x.com"], cadence="weekly", send_time="07:00")
    r = report_groups.validate_group(g)
    assert r.ok is True and r.errors == {}

def test_validate_collects_errors():
    importlib.reload(report_groups)
    g = report_groups.ReportGroup(id="a", name="", sections=[], recipients=[], cadence="yearly", send_time="99:99")
    r = report_groups.validate_group(g)
    assert r.ok is False
    for f in ("name", "sections", "recipients", "cadence", "send_time"):
        assert f in r.errors

def test_send_group_report_filters_sections_and_test_prefix(monkeypatch):
    importlib.reload(report_notify)
    seen = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        seen["subject"] = subject; seen["to"] = list(settings.recipients); return {}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    monkeypatch.setattr(report_notify, "dashboard_report_email",
                        lambda dash, sections=None: ("plain", "<html>%s</html>" % ",".join(sections or [])))
    monkeypatch.setattr(report_notify, "render_dashboard_snapshot_png", lambda dash: b"")
    class G: recipients=["a@x.com"]; sections=["alerts"]; name="Ops"; theme="default"
    smtp = {"host": "h", "port": 25, "security": "none", "from": "r@x.com"}
    report_notify.send_group_report(G(), {"summary": {}}, smtp, "", test=True)
    assert seen["to"] == ["a@x.com"] and seen["subject"].startswith("[TEST]")
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3a: Append to `nwdash/report_groups.py`:**
```python
from dataclasses import dataclass as _dc
from .config import TIME_HHMM_PATTERN
from .report_window import CADENCES, SECTION_KEYS


@_dc
class GroupValidation:
    ok: bool
    errors: dict


def validate_group(g: "ReportGroup") -> GroupValidation:
    errors = {}
    if not str(g.name).strip():
        errors["name"] = "Name is required."
    if not g.sections or any(s not in SECTION_KEYS for s in g.sections):
        errors["sections"] = "Select at least one valid section."
    if not g.recipients:
        errors["recipients"] = "At least one recipient is required."
    if g.cadence not in CADENCES:
        errors["cadence"] = "Cadence must be daily, weekly, or monthly."
    if not TIME_HHMM_PATTERN.match(str(g.send_time or "")):
        errors["send_time"] = "Send time must be HH:MM (24h)."
    return GroupValidation(not errors, errors)
```

- [ ] **Step 3b: Add to `nwdash/report_notify.py`** `send_group_report` (mirrors `send_report`, adds section filter + `[TEST]`):
```python
def send_group_report(group, dashboard: dict, smtp: dict, smtp_password: str, test: bool = False) -> dict:
    dashboard = dict(dashboard)
    dashboard["theme"] = getattr(group, "theme", "default")
    dashboard["scheduledReport"] = True
    plain, html = dashboard_report_email(dashboard, sections=list(group.sections))
    attachments = {}
    png = render_dashboard_snapshot_png(dashboard)
    if png:
        attachments["networker-dashboard.png"] = (png, "image/png", "networker-dashboard.png")
    subject = f"NetWorker report: {getattr(group, 'name', 'report')}"
    if test:
        subject = "[TEST] " + subject
        plain = "This is a TEST send.\n\n" + plain
    return send_smtp_email(_settings(smtp, list(group.recipients)), subject, plain,
                           smtp_password, html, attachments=attachments)
```
Ensure `dashboard_report_email` in report_notify is imported so the test can monkeypatch `report_notify.dashboard_report_email` (it already imports it from `.reports`).

- [ ] **Step 4: Run → PASS (3).** FULL suite.

- [ ] **Step 5: Commit:**
```bash
git add nwdash/report_groups.py nwdash/report_notify.py tests/test_group_validate_notify.py
git commit -m "feat(groups): group validator + section-filtered/[TEST] group email"
```

---

## Task 6: Scheduler fire + on-demand send

**Files:** Modify `nwdash/report_groups.py` (append); Test `tests/test_group_fire.py`

- [ ] **Step 1: Failing test** `tests/test_group_fire.py`:
```python
import importlib
from datetime import datetime
from nwdash import report_groups, report_render

def _cfg():
    return {"smtp": {"host": "h", "port": 25, "security": "none"}, "smtp_password": "", "ops_address": "ops@x.com",
            "connection": {"rest_api_host": "h", "username": "u", "encrypted_password": ""}}

def test_fire_success_sends_selected_and_healthy(monkeypatch):
    importlib.reload(report_groups)
    sent = {}
    monkeypatch.setattr(report_groups.report_render, "render_window",
                        lambda cred, win: report_render.RenderResult(True, {"summary": {}}, ""))
    monkeypatch.setattr(report_groups.report_render, "cache_put", lambda k, d: None)
    monkeypatch.setattr(report_groups.report_notify, "send_group_report",
                        lambda g, d, smtp, pw, test=False: sent.update(sent=True, test=test) or {})
    monkeypatch.setattr(report_groups.report_notify, "send_ops_alert", lambda *a, **k: sent.update(ops=True))
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["alerts"], recipients=["a@x.com"], enabled=True, cadence="daily")
    report_groups.fire_group(g, _cfg())
    assert sent.get("sent") is True and sent.get("test") is False and "ops" not in sent
    assert g.health.state == "healthy" and g.health.next_run > 0

def test_fire_failure_fallback_and_ops(monkeypatch):
    importlib.reload(report_groups)
    sent = {}
    monkeypatch.setattr(report_groups.report_render, "render_window",
                        lambda cred, win: report_render.RenderResult(False, {}, "down"))
    monkeypatch.setattr(report_groups.report_render, "cache_get", lambda k: {"summary": {}})
    monkeypatch.setattr(report_groups.report_notify, "send_group_report",
                        lambda g, d, smtp, pw, test=False: sent.update(sent=True) or {})
    monkeypatch.setattr(report_groups.report_notify, "send_ops_alert", lambda g, err, smtp, ops, pw: sent.update(ops=True))
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["alerts"], recipients=["a@x.com"], enabled=True)
    report_groups.fire_group(g, _cfg())
    assert sent.get("ops") is True and g.health.state == "unhealthy"

def test_send_on_demand_test(monkeypatch):
    importlib.reload(report_groups)
    sent = {}
    monkeypatch.setattr(report_groups.report_render, "render_window",
                        lambda cred, win: report_render.RenderResult(True, {"summary": {}}, ""))
    monkeypatch.setattr(report_groups.report_notify, "send_group_report",
                        lambda g, d, smtp, pw, test=False: sent.update(test=test) or {})
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["alerts"], recipients=["a@x.com"], cadence="daily")
    ok, msg = report_groups.send_on_demand(g, _cfg(), test=True)
    assert ok is True and sent.get("test") is True
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Append to `nwdash/report_groups.py`:**
```python
import time as _time
from datetime import datetime
from . import report_render, report_notify
from .report_window import compute_window, next_run as _next_run
from .config import SHARED_REFRESH_STOP, debug_log

GROUP_TICK_SECONDS = 30


def _now() -> datetime:
    return datetime.now().astimezone()


def compute_group_next_run(g: "ReportGroup") -> float:
    return _next_run(g.cadence, g.send_time, _now())


def _render_for(g: "ReportGroup", cfg: dict):
    conn = cfg.get("connection") or {}
    window = compute_window(g.cadence, _now())
    return report_render.render_window(conn, window)


def fire_group(g: "ReportGroup", cfg: dict) -> None:
    if not g.enabled:
        return
    smtp = cfg.get("smtp") or {}; pw = str(cfg.get("smtp_password") or ""); ops = str(cfg.get("ops_address") or "")
    g.health.last_run = _time.time()
    try:
        res = _render_for(g, cfg)
        if res.ok:
            try:
                report_notify.send_group_report(g, res.dashboard, smtp, pw, test=False)
                report_render.cache_put("group:" + g.id, res.dashboard)
                g.health.state = "healthy"; g.health.last_success = _time.time()
                g.health.last_result = f"Sent at {_time.strftime('%Y-%m-%d %H:%M:%S')}"
            except Exception as exc:  # noqa: BLE001
                g.health.state = "unhealthy"; g.health.last_result = f"Send failed: {exc}"
                try: report_notify.send_ops_alert(g, str(exc), smtp, ops, pw)
                except Exception: pass  # noqa: BLE001
        else:
            cached = report_render.cache_get("group:" + g.id)
            if cached:
                try: report_notify.send_group_report(g, cached, smtp, pw, test=False)
                except Exception: pass  # noqa: BLE001
            try: report_notify.send_ops_alert(g, res.error, smtp, ops, pw)
            except Exception: pass  # noqa: BLE001
            g.health.state = "unhealthy"; g.health.last_result = f"Failed: {res.error}"
    except Exception as exc:  # noqa: BLE001
        g.health.state = "unhealthy"; g.health.last_result = f"Error: {exc}"
        debug_log(f"fire_group {g.id} crashed: {exc}")
    finally:
        g.health.next_run = compute_group_next_run(g)


def send_on_demand(g: "ReportGroup", cfg: dict, test: bool) -> tuple[bool, str]:
    smtp = cfg.get("smtp") or {}; pw = str(cfg.get("smtp_password") or "")
    res = _render_for(g, cfg)
    if not res.ok:
        return False, res.error or "Render failed."
    try:
        report_notify.send_group_report(g, res.dashboard, smtp, pw, test=test)
        return True, "Test sent." if test else "Sent."
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def group_scheduler_tick(cfg_provider) -> None:
    now = _time.time()
    for g in groups_ordered():
        if not g.enabled:
            continue
        if not g.health.next_run:
            g.health.next_run = compute_group_next_run(g); continue
        if now >= g.health.next_run:
            g.health.next_run = now + 3600  # guard double-fire; fire recomputes real next
            threading.Thread(target=_fire_and_persist, args=(g, cfg_provider),
                             name=f"group-fire-{g.id[:12]}", daemon=True).start()


def _fire_and_persist(g: "ReportGroup", cfg_provider) -> None:
    try:
        fire_group(g, cfg_provider())
    finally:
        persist_groups()


def group_scheduler_loop(cfg_provider) -> None:
    while not SHARED_REFRESH_STOP.wait(GROUP_TICK_SECONDS):
        try:
            group_scheduler_tick(cfg_provider)
        except Exception as exc:  # noqa: BLE001
            debug_log(f"group_scheduler_loop iteration failed: {exc}")
```
Verify `SHARED_REFRESH_STOP` import — it lives in `nwdash/models.py`, NOT config (confirmed in 2.9.0). Use `from .models import SHARED_REFRESH_STOP` and `from .config import debug_log`.

- [ ] **Step 4: Run → PASS (3).** FULL suite. `python -c "import nwdash.report_groups"`.

- [ ] **Step 5: Commit:**
```bash
git add nwdash/report_groups.py tests/test_group_fire.py
git commit -m "feat(groups): scheduler fire (fallback+ops, isolated sends) + on-demand/test send"
```

---

## Task 7: `/api/report-groups` router

**Files:** Create `nwdash/report_groups_api.py`; Test `tests/test_report_groups_api.py`

- [ ] **Step 1: Failing test** `tests/test_report_groups_api.py`:
```python
import importlib
from http import HTTPStatus
from nwdash import report_groups, report_groups_api as api

def _payload(**kw):
    b = {"action": "create", "name": "Ops", "sections": ["backup_sla", "alerts"],
         "recipients": "a@x.com, b@x.com", "cadence": "weekly", "sendTime": "07:00", "enabled": True}
    b.update(kw); return b

def test_create_validates_and_lists_ordered(monkeypatch):
    importlib.reload(report_groups); importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"rest_api_host": "h"})   # connection present
    monkeypatch.setattr(api.report_groups, "persist_groups", lambda: None)
    st, body = api.handle_report_groups(_payload())
    assert st == HTTPStatus.OK and body["ok"] is True
    gid = body["id"]
    st, body = api.handle_report_groups({"action": "list"})
    assert body["groups"][0]["id"] == gid
    assert body["groups"][0]["recipients"] == ["a@x.com", "b@x.com"]
    assert "health" in body["groups"][0]

def test_create_rejects_invalid(monkeypatch):
    importlib.reload(report_groups); importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"rest_api_host": "h"})
    st, body = api.handle_report_groups(_payload(name="", sections=[]))
    assert st == HTTPStatus.BAD_REQUEST and body["ok"] is False
    assert "name" in body["errors"] and "sections" in body["errors"]

def test_enabled_requires_connection(monkeypatch):
    importlib.reload(report_groups); importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: None)   # no connection
    monkeypatch.setattr(api.report_groups, "persist_groups", lambda: None)
    st, body = api.handle_report_groups(_payload())
    assert st == HTTPStatus.OK and body["ok"] is True
    assert report_groups.get_group(body["id"]).enabled is False  # forced off, no connection
    assert body.get("warning")

def test_toggle_delete_reorder_send(monkeypatch):
    importlib.reload(report_groups); importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"rest_api_host": "h"})
    monkeypatch.setattr(api.report_groups, "persist_groups", lambda: None)
    api.report_groups.put_group(report_groups.ReportGroup(id="a", name="A", sections=["alerts"], recipients=["a@x.com"]))
    api.report_groups.put_group(report_groups.ReportGroup(id="b", name="B", sections=["alerts"], recipients=["b@x.com"]))
    assert api.handle_report_groups({"action": "reorder", "order": ["b", "a"]})[0] == HTTPStatus.OK
    assert [g.id for g in report_groups.groups_ordered()] == ["b", "a"]
    assert api.handle_report_groups({"action": "toggle", "id": "a", "enabled": False})[0] == HTTPStatus.OK
    monkeypatch.setattr(api.report_groups, "send_on_demand", lambda g, cfg, test: (True, "Test sent."))
    st, body = api.handle_report_groups({"action": "send", "id": "a", "test": True})
    assert st == HTTPStatus.OK and body["ok"] is True and "Test" in body["message"]
    assert api.handle_report_groups({"action": "delete", "id": "b"})[0] == HTTPStatus.OK
    assert report_groups.get_group("b") is None
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `nwdash/report_groups_api.py`:**
```python
"""/api/report-groups router. Validates on create/update; enabled requires a
configured reporting connection; on-demand/test send. Never leaks secrets."""
from __future__ import annotations

import json
import uuid
from http import HTTPStatus
from typing import Any

from . import report_groups, display
from .emailer import saved_email_smtp_password
from .config import EMAIL_CONFIG_FILE


def _smtp_config() -> tuple[dict, str]:
    try:
        cfg = json.loads(EMAIL_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    smtp = cfg.get("smtp") if isinstance(cfg.get("smtp"), dict) else {}
    return smtp, saved_email_smtp_password()


def _reporting_connection() -> dict | None:
    return display.load_connection()


def _cfg() -> dict:
    smtp, pw = _smtp_config()
    ops = smtp.get("opsAlertAddress", "") if isinstance(smtp, dict) else ""
    return {"smtp": smtp, "smtp_password": pw, "ops_address": ops, "connection": _reporting_connection() or {}}


def _recipients(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else str(raw or "").replace(";", ",").split(",")
    return [r.strip() for r in items if r.strip()]


def _public(g: "report_groups.ReportGroup") -> dict:
    return {"id": g.id, "name": g.name, "sections": g.sections, "recipients": g.recipients,
            "enabled": g.enabled, "cadence": g.cadence, "sendTime": g.send_time, "position": g.position,
            "health": {"state": g.health.state, "lastResult": g.health.last_result,
                       "lastRun": g.health.last_run, "nextRun": g.health.next_run}}


def handle_report_groups(payload: dict) -> tuple[int, dict]:
    action = str(payload.get("action") or "").strip().lower()

    if action == "list":
        return HTTPStatus.OK, {"ok": True, "hasConnection": _reporting_connection() is not None,
                               "groups": [_public(g) for g in report_groups.groups_ordered()]}
    if action == "delete":
        report_groups.delete_group(str(payload.get("id") or "")); report_groups.persist_groups()
        return HTTPStatus.OK, {"ok": True}
    if action == "toggle":
        g = report_groups.get_group(str(payload.get("id") or ""))
        if not g:
            return HTTPStatus.OK, {"ok": True, "message": "No such group."}
        g.enabled = bool(payload.get("enabled")) and _reporting_connection() is not None
        if g.enabled:
            g.health.next_run = report_groups.compute_group_next_run(g)
        report_groups.persist_groups()
        return HTTPStatus.OK, {"ok": True, "enabled": g.enabled}
    if action == "reorder":
        report_groups.reorder([str(x) for x in (payload.get("order") or [])]); report_groups.persist_groups()
        return HTTPStatus.OK, {"ok": True}
    if action == "send":
        g = report_groups.get_group(str(payload.get("id") or ""))
        if not g:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "message": "No such group."}
        ok, msg = report_groups.send_on_demand(g, _cfg(), bool(payload.get("test")))
        return (HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY), {"ok": ok, "message": msg}
    if action in ("create", "update"):
        gid = str(payload.get("id") or "") or uuid.uuid4().hex
        existing = report_groups.get_group(gid)
        g = report_groups.ReportGroup(
            id=gid, name=str(payload.get("name") or ""),
            sections=[str(s) for s in (payload.get("sections") or [])],
            recipients=_recipients(payload.get("recipients")),
            cadence=str(payload.get("cadence") or "daily"),
            send_time=str(payload.get("sendTime") or "08:00"),
            enabled=bool(payload.get("enabled")),
            position=existing.position if existing else len(report_groups.groups_ordered()),
        )
        v = report_groups.validate_group(g)
        if not v.ok:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "errors": v.errors}
        warning = ""
        if g.enabled and _reporting_connection() is None:
            g.enabled = False
            warning = "Saved disabled: set the reporting connection first."
        if g.enabled:
            g.health.next_run = report_groups.compute_group_next_run(g)
        report_groups.put_group(g); report_groups.persist_groups()
        return HTTPStatus.OK, {"ok": True, "id": gid, "group": _public(g), "warning": warning}

    return HTTPStatus.BAD_REQUEST, {"ok": False, "message": f"Unknown action {action!r}."}
```

- [ ] **Step 4:** Add `'nwdash\report_groups_api.py',` to build-bundle allow-list. Run tests (4 passed) + FULL suite. Verify `python -c "from nwdash.emailer import saved_email_smtp_password"` clean.

- [ ] **Step 5: Commit:**
```bash
git add nwdash/report_groups_api.py tests/test_report_groups_api.py deploy/build-bundle.ps1
git commit -m "feat(groups): /api/report-groups router (list/create/update/delete/toggle/reorder/send)"
```

---

## Task 8: Boot wiring + routes + remove old report jobs

**Files:** Modify `nwdash/main.py`, `nwdash/server.py`; Remove `nwdash/report_jobs.py`, `nwdash/report_api.py` + their tests; Test `tests/test_groups_boot.py`

- [ ] **Step 1: Locate** old job wiring: `grep -rn "report_jobs\|report_api\|handle_report_jobs\|/api/report-jobs\|report_scheduler_loop\|report_cfg_provider" nwdash/main.py nwdash/server.py`.

- [ ] **Step 2: Failing test** `tests/test_groups_boot.py`:
```python
import importlib
from nwdash import report_groups, main

def test_group_cfg_provider_shape(monkeypatch):
    importlib.reload(report_groups)
    monkeypatch.setattr(main, "_group_smtp_config", lambda: ({"host": "h"}, ""), raising=False)
    cfg = main.group_cfg_provider()
    assert set(["smtp", "smtp_password", "ops_address", "connection"]) <= set(cfg)
```

- [ ] **Step 3: main.py** — replace the report-jobs boot with groups:
- Remove imports of `report_jobs`/`report_api` job wiring and the `report_scheduler_loop` thread + `restore_jobs_from_disk` call + `report_cfg_provider`.
- Add:
```python
from .report_groups import restore_groups_from_disk, group_scheduler_loop
from .report_groups_api import _cfg as _group_cfg, _smtp_config as _group_smtp_config

def group_cfg_provider() -> dict:
    return _group_cfg()
```
- In boot (where the old restore/scheduler were): `restore_groups_from_disk()` + `threading.Thread(target=group_scheduler_loop, args=(group_cfg_provider,), name="group-scheduler", daemon=True).start()`.

- [ ] **Step 4: server.py** — route `/api/report-groups` (authed, add to `allowed`), and make `/api/report-jobs` a 410 shim:
```python
        if path == "/api/report-groups":
            from .report_groups_api import handle_report_groups
            status, body = handle_report_groups(payload)
            return self._send_json(status, body)
        if path == "/api/report-jobs":
            return self._send_json(HTTPStatus.GONE, {"ok": False,
                "message": "Report jobs were replaced by Report Groups. Reload and use Scheduled Reports."})
```
Remove the old `handle_report_jobs` import/route. Add `"/api/report-groups"` to the POST `allowed` set (keep `/api/report-jobs` there too so the 410 branch is reachable).

- [ ] **Step 5: Remove** `nwdash/report_jobs.py`, `nwdash/report_api.py`, and their test files (`tests/test_report_jobs_*.py`, `tests/test_report_api.py`, `tests/test_report_scheduler.py`, `tests/test_report_validator.py`, `tests/test_report_no_session_regression.py`, `tests/test_report_boot.py`, `tests/test_legacy_migration_notice.py`). Remove their entries from `deploy/build-bundle.ps1` `$shipFiles`. Grep for any remaining references: `grep -rn "report_jobs\|report_api\|handle_report_jobs" nwdash/ tests/` → only the 410 shim message may remain.

- [ ] **Step 6:** `python -c "import nwdash.main, nwdash.server"` clean. Run `tests/test_groups_boot.py` (1 passed). FULL suite — fix any dangling references from the removal. 0 failures.

- [ ] **Step 7: Commit:**
```bash
git add -A
git commit -m "refactor(groups): boot+route report-groups; remove ReportJob engine + /api/report-jobs (410)"
```

---

## Task 9: Group manager UI

**Files:** Modify `nwdash/assets/dashboard.html`, `nwdash/assets/app.js`, `nwdash/assets/app.css`; Test `tests/test_groups_ui.py`

The Scheduled Reports panel (in the config drawer) becomes the group manager. Remove the old report-jobs form markup/JS (from 2.9.0) and add the group list + editor.

- [ ] **Step 1: Failing test** `tests/test_groups_ui.py`:
```python
from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_group_ui_markup():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="reportGroupsList"' in html and 'id="reportGroupForm"' in html

def test_group_ui_js():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/report-groups" in js
    assert "renderReportGroups" in js
    assert "reportSectionChecks" in js  # section checkboxes
    assert "/api/report-jobs" not in js  # old endpoint gone from the SPA
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: dashboard.html** — inside `#scheduledReportsPanel` (in the config drawer), REPLACE the old report-jobs list/form markup with:
```html
  <div id="reportGroupsConn" class="form-error"></div>
  <div id="reportGroupsList" class="report-jobs"></div>
  <button id="reportGroupAddBtn" type="button">New group</button>
  <form id="reportGroupForm" class="report-form" hidden>
    <input type="hidden" name="id">
    <label>Name <input name="name" required></label>
    <fieldset id="reportSectionChecks"><legend>Sections</legend>
      <label><input type="checkbox" value="backup_sla">Backup SLA</label>
      <label><input type="checkbox" value="management">Management Overview</label>
      <label><input type="checkbox" value="recovery">Recovery Health</label>
      <label><input type="checkbox" value="clone">Clone Jobs</label>
      <label><input type="checkbox" value="alerts">Alerts</label>
      <label><input type="checkbox" value="server_protection">Server Protection</label>
      <label><input type="checkbox" value="health">Health</label>
    </fieldset>
    <label>Recipients <input name="recipients" required placeholder="a@x.com, b@x.com"></label>
    <label>Cadence
      <select name="cadence"><option value="daily">Daily</option><option value="weekly">Weekly (Sun)</option><option value="monthly">Monthly (1st)</option></select>
    </label>
    <label>Send time <input name="sendTime" value="08:00" placeholder="HH:MM"></label>
    <label><input type="checkbox" name="enabled" checked> Enabled</label>
    <div id="reportGroupErr" class="form-error" role="alert"></div>
    <button type="submit">Save group</button>
    <button id="reportGroupCancel" type="button">Cancel</button>
  </form>
```

- [ ] **Step 4: app.js** — remove old report-jobs functions (renderReportJobs/submitReportJob and their listeners) and add the group manager. Full code:
```javascript
async function renderReportGroups() {
  var list = document.getElementById("reportGroupsList");
  if (!list) return;
  var r = await fetch("/api/report-groups", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({action:"list"})});
  var d = await r.json();
  document.getElementById("reportGroupsConn").textContent = d.hasConnection ? "" : "Set the reporting connection (TV / Display) before enabling groups.";
  var order = (d.groups||[]).map(function(g){return g.id;});
  list.innerHTML = (d.groups||[]).map(function(g){
    var badge = '<span class="health-badge health-'+({healthy:"ok",unhealthy:"bad",never_run:"idle"}[g.health.state]||"idle")+'">'+g.health.state.replace("_"," ")+'</span>';
    return '<div class="report-job" data-id="'+g.id+'">'
      + '<div class="rj-main"><strong>'+g.name+'</strong> · '+g.cadence+' '+g.sendTime+' · '+g.recipients.length+' recipient(s) '+badge+'</div>'
      + '<div class="rj-sub">'+g.sections.join(", ")+' · last: '+(g.health.lastResult||"—")+'</div>'
      + '<label><input type="checkbox" class="rg-toggle" '+(g.enabled?"checked":"")+'> on</label> '
      + '<label><input type="checkbox" class="rg-test"> test</label> '
      + '<button class="rg-send" type="button">Send now</button> '
      + '<button class="rg-edit" type="button">Edit</button> '
      + '<button class="rg-del" type="button">Delete</button> '
      + '<button class="rg-up" type="button">↑</button><button class="rg-down" type="button">↓</button>'
      + '</div>';
  }).join("") || "<p>No report groups yet.</p>";
  list.querySelectorAll(".report-job").forEach(function(card){
    var id = card.getAttribute("data-id");
    card.querySelector(".rg-toggle").addEventListener("change", function(e){ _groupAction({action:"toggle", id:id, enabled:e.target.checked}); });
    card.querySelector(".rg-del").addEventListener("click", function(){ _groupAction({action:"delete", id:id}); });
    card.querySelector(".rg-send").addEventListener("click", async function(){
      var test = card.querySelector(".rg-test").checked;
      var rr = await fetch("/api/report-groups", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({action:"send", id:id, test:test})});
      var dd = await rr.json(); alert(dd.message || (dd.ok?"Sent":"Failed"));
    });
    card.querySelector(".rg-edit").addEventListener("click", function(){ _editGroup(id, JSON.parse(card.getAttribute("data-json")||"null")); });
    card.setAttribute("data-json", JSON.stringify((d.groups||[]).find(function(g){return g.id===id;})));
    card.querySelector(".rg-up").addEventListener("click", function(){ _move(order, id, -1); });
    card.querySelector(".rg-down").addEventListener("click", function(){ _move(order, id, 1); });
  });
}
async function _groupAction(body){ await fetch("/api/report-groups",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}); renderReportGroups(); }
function _move(order, id, delta){ var i=order.indexOf(id), j=i+delta; if(i<0||j<0||j>=order.length) return; order.splice(j,0,order.splice(i,1)[0]); _groupAction({action:"reorder", order:order}); }
function _editGroup(id, g){
  var f = document.getElementById("reportGroupForm"); f.hidden=false;
  f.id.value = id||""; f.name.value = g?g.name:""; f.recipients.value = g?g.recipients.join(", "):"";
  f.cadence.value = g?g.cadence:"daily"; f.sendTime.value = g?g.sendTime:"08:00"; f.enabled.checked = g?g.enabled:true;
  var sel = g?g.sections:[]; document.querySelectorAll("#reportSectionChecks input").forEach(function(c){ c.checked = sel.indexOf(c.value)>=0; });
  document.getElementById("reportGroupErr").textContent = "";
}
async function submitReportGroup(ev){
  ev.preventDefault(); var f = ev.target; var err = document.getElementById("reportGroupErr");
  var sections = Array.prototype.slice.call(document.querySelectorAll("#reportSectionChecks input:checked")).map(function(c){return c.value;});
  var body = {action: f.id.value?"update":"create", id:f.id.value||undefined, name:f.name.value, sections:sections,
              recipients:f.recipients.value, cadence:f.cadence.value, sendTime:f.sendTime.value, enabled:f.enabled.checked};
  var r = await fetch("/api/report-groups",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  var d = await r.json();
  if(!d.ok){ err.textContent = "Fix: " + Object.values(d.errors||{message:d.message}).join(" "); return; }
  if(d.warning) err.textContent = d.warning;
  f.hidden = true; renderReportGroups();
}
function initReportGroups(){
  var panel = document.getElementById("scheduledReportsPanel"); if(!panel) return;
  var add = document.getElementById("reportGroupAddBtn"); if(add) add.addEventListener("click", function(){ _editGroup("", null); });
  var form = document.getElementById("reportGroupForm"); if(form) form.addEventListener("submit", submitReportGroup);
  var cancel = document.getElementById("reportGroupCancel"); if(cancel) cancel.addEventListener("click", function(){ form.hidden=true; });
  renderReportGroups();
}
```
Replace the old `initScheduledReports()` call in the top-level init with `initReportGroups()` (and delete the old `initScheduledReports`/`renderReportJobs`/`submitReportJob` definitions). `alert()` is acceptable for the send-result toast here (matches existing simple UX).

- [ ] **Step 5: app.css** — append:
```css
#reportSectionChecks { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; border: 1px solid var(--line); border-radius: 6px; padding: 8px; margin: 6px 0; }
#reportSectionChecks legend { padding: 0 6px; }
```

- [ ] **Step 6:** `node -c nwdash/assets/app.js`. Run `tests/test_groups_ui.py` (2 passed) + FULL suite. If the 2.9.0 `tests/test_reports_ui_assets.py` asserts on removed `renderReportJobs`/`/api/report-jobs`, update it to the group markers or delete it (report which).

- [ ] **Step 7: Commit:**
```bash
git add nwdash/assets/dashboard.html nwdash/assets/app.js nwdash/assets/app.css tests/test_groups_ui.py
git commit -m "feat(groups): group manager UI — list, section-checkbox editor, toggle, send/test, reorder"
```

---

## Task 10: Version bump 2.11.0 + build

- [ ] **Step 1:** `APP_VERSION = "2.11.0"` (config.py), `version = "2.11.0"` (pyproject.toml).
- [ ] **Step 2:** README: replace the Scheduled Reports note with the groups model (sections + daily/weekly/monthly + on-demand/test + shared reporting connection).
- [ ] **Step 3:** FULL suite `python -m pytest -q` (0 failures).
- [ ] **Step 4:** `pwsh -ExecutionPolicy Bypass -File deploy\build-bundle.ps1 -SkipRuntimeFetch` → `done -> dist\nwdash-bundle-2.11.0-win-x64.zip`.
- [ ] **Step 5: Commit:**
```bash
git add nwdash/config.py pyproject.toml README.md
git commit -m "chore(groups): bump to 2.11.0 — scheduled report groups"
```

---

## Deployment note
Ships as 2.11.0. `Setup-NWDash.cmd -Upgrade` + Ctrl+F5. Set the reporting connection (Account → TV / Display; it's the shared connection), then Account → Scheduled Reports → create groups (sections + recipients + daily/weekly/monthly). Old report jobs are not migrated — recreate as groups. Use **Send now** (+ **test** checkbox) to verify before relying on the schedule.
