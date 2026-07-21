"""/api/report-groups router. Validates on create/update; enabled requires a
configured reporting connection; on-demand/test send. Never leaks secrets."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Any

from . import report_groups, display, report_render
from .emailer import saved_email_smtp_password
from .config import EMAIL_CONFIG_FILE
from .sessions import snapshot_latest_live_session
from .report_window import compute_window


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


def _validate_stored_connection() -> tuple[int, dict]:
    """Render the daily window through the stored connection and report REAL row
    counts, so a connection that authenticates but returns nothing is caught at
    setup instead of arriving as an empty report."""
    stored = _reporting_connection()
    if not stored:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "message": "No reporting connection is set."}
    window = compute_window("daily", datetime.now().astimezone())
    res = report_render.render_window(stored, window)
    if not res.ok:
        return HTTPStatus.OK, {"ok": False, "message": res.error or "Connection test failed."}
    summary = res.dashboard.get("summary") if isinstance(res.dashboard, dict) else {}
    jobs = int((summary or {}).get("totalJobs") or 0)
    alerts = int((summary or {}).get("totalAlerts") or 0)
    if jobs == 0:
        return HTTPStatus.OK, {"ok": True, "zeroData": True, "jobs": 0, "alerts": alerts,
                               "message": ("Connected, but returned 0 jobs in the last 24h - reports would be "
                                           "empty. Check the account's permissions and the backup server.")}
    return HTTPStatus.OK, {"ok": True, "zeroData": False, "jobs": jobs, "alerts": alerts,
                           "message": f"Validated - {jobs:,} jobs in the last 24h."}


def handle_report_groups(payload: dict) -> tuple[int, dict]:
    action = str(payload.get("action") or "").strip().lower()

    if action == "connection-status":
        stored = _reporting_connection()
        cfg = (stored or {}).get("config") if isinstance(stored, dict) else None
        cfg = cfg if isinstance(cfg, dict) else (stored or {})
        return HTTPStatus.OK, {"ok": True, "hasConnection": bool(stored),
                               "host": str(cfg.get("rest_api_host") or ""),
                               "username": str(cfg.get("username") or ""),
                               "apiMode": str(cfg.get("api_mode") or "")}
    if action == "use-current-connection":
        snap = snapshot_latest_live_session()
        if not snap:
            return HTTPStatus.BAD_REQUEST, {"ok": False,
                "message": "No live dashboard connection. Connect on the dashboard first, then click this."}
        display.save_connection(snap)
        return _validate_stored_connection()
    if action == "validate-connection":
        return _validate_stored_connection()
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
        if existing is not None:
            g.health = existing.health
        warning = ""
        if g.enabled and _reporting_connection() is None:
            g.enabled = False
            warning = "Saved disabled: set the reporting connection first."
        if g.enabled:
            g.health.next_run = report_groups.compute_group_next_run(g)
        report_groups.put_group(g); report_groups.persist_groups()
        return HTTPStatus.OK, {"ok": True, "id": gid, "group": _public(g), "warning": warning}

    return HTTPStatus.BAD_REQUEST, {"ok": False, "message": f"Unknown action {action!r}."}
