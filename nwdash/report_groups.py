"""Report groups: model, ordered persistence, validation, scheduler, fire.
Session-free — renders via the shared display/reporting connection."""
from __future__ import annotations

import json
import threading
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from . import config
from . import report_notify
from . import report_render
from .config import TIME_HHMM_PATTERN, debug_log
from .models import SHARED_REFRESH_STOP
from .report_window import CADENCES, SECTION_KEYS, compute_window
from .report_window import next_run as _next_run


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


@dataclass
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


GROUP_TICK_SECONDS = 30


def _now() -> datetime:
    return datetime.now().astimezone()


def compute_group_next_run(g: "ReportGroup") -> float:
    return _next_run(g.cadence, g.send_time, _now())


def _render_for(g: "ReportGroup", cfg: dict[str, Any]):
    conn = cfg.get("connection") or {}
    window = compute_window(g.cadence, _now())
    return report_render.render_window(conn, window)


def fire_group(g: "ReportGroup", cfg: dict[str, Any]) -> None:
    """Render and deliver ONE group. Never raises; records health either way.

    Each send_* call (group report, stale fallback, ops alert) is isolated in
    its own try/except so a raise from one can never suppress another send or
    the health update below."""
    if not g.enabled:
        return
    smtp = cfg.get("smtp") or {}
    smtp_password = str(cfg.get("smtp_password") or "")
    ops_address = str(cfg.get("ops_address") or "")
    g.health.last_run = _time.time()
    try:
        try:
            res = _render_for(g, cfg)
        except Exception as exc:  # noqa: BLE001 — render must never kill the loop
            debug_log(f"fire_group {g.id} render crashed: {exc}")
            res = report_render.RenderResult(False, {}, str(exc))

        if res.ok:
            send_error: Exception | None = None
            try:
                report_notify.send_group_report(g, res.dashboard, smtp, smtp_password, test=False)
            except Exception as exc:  # noqa: BLE001 — isolate this send
                send_error = exc
                debug_log(f"fire_group {g.id} send_group_report crashed: {exc}")

            if send_error is None:
                report_render.cache_put("group:" + g.id, res.dashboard)
                g.health.state = "healthy"
                g.health.last_success = _time.time()
                g.health.last_result = f"Sent at {_time.strftime('%Y-%m-%d %H:%M:%S')}"
            else:
                g.health.state = "unhealthy"
                g.health.last_result = f"Send failed: {send_error}"
                try:
                    report_notify.send_ops_alert(g, str(send_error), smtp, ops_address, smtp_password)
                except Exception as exc:  # noqa: BLE001 — isolate ops alert
                    debug_log(f"fire_group {g.id} send_ops_alert crashed: {exc}")
        else:
            cached = report_render.cache_get("group:" + g.id)
            if cached:
                try:
                    report_notify.send_group_report(g, cached, smtp, smtp_password, test=False)
                except Exception as exc:  # noqa: BLE001 — isolate stale-fallback send
                    debug_log(f"fire_group {g.id} stale send_group_report crashed: {exc}")
            try:
                report_notify.send_ops_alert(g, res.error, smtp, ops_address, smtp_password)
            except Exception as exc:  # noqa: BLE001 — isolate ops alert
                debug_log(f"fire_group {g.id} send_ops_alert crashed: {exc}")
            g.health.state = "unhealthy"
            g.health.last_result = f"Failed: {res.error}"
    except Exception as exc:  # noqa: BLE001 — a fire must never kill the loop
        g.health.state = "unhealthy"
        g.health.last_result = f"Error: {exc}"
        debug_log(f"fire_group {g.id} crashed: {exc}")
    finally:
        g.health.next_run = compute_group_next_run(g)


def send_on_demand(g: "ReportGroup", cfg: dict[str, Any], test: bool) -> tuple[bool, str]:
    """Render fresh and send immediately (used by both the manual 'send now'
    and the 'send test' actions). No health tracking, no cache/fallback —
    this is an interactive, on-demand action."""
    smtp = cfg.get("smtp") or {}
    smtp_password = str(cfg.get("smtp_password") or "")
    res = _render_for(g, cfg)
    if not res.ok:
        return False, res.error or "Render failed."
    try:
        report_notify.send_group_report(g, res.dashboard, smtp, smtp_password, test=test)
        return True, "Test sent." if test else "Sent."
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller
        return False, str(exc)


def group_scheduler_tick(cfg_provider) -> None:
    now = _time.time()
    for g in groups_ordered():
        if not g.enabled:
            continue
        nxt = g.health.next_run or 0.0
        if not nxt:
            g.health.next_run = compute_group_next_run(g)
            continue
        if now >= nxt:
            g.health.next_run = now + 3600
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
