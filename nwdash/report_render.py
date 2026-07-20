"""Render a report job by connecting FRESH to NetWorker from the job's own
credential — no session store. Also a small per-job disk cache of the last
successful dashboard, used for fallback emails."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import Any

from . import config
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


def _cache_path(job_id: str):
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_.")
    return config.REPORT_CACHE_DIR / f"{safe}.json"


def cache_put(job_id: str, dashboard: dict[str, Any]) -> None:
    try:
        config.REPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(job_id).write_text(json.dumps(dashboard), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def cache_get(job_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_cache_path(job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
