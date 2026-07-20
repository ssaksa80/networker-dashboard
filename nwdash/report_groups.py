"""Report groups: model, ordered persistence, validation, scheduler, fire.
Session-free — renders via the shared display/reporting connection."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config
from .config import TIME_HHMM_PATTERN
from .report_window import CADENCES, SECTION_KEYS


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
