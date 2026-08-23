"""Report/snapshot HTML + PNG rendering, email body, and Excel export.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import html as html_lib
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from .config import (
    APP_NAME,
    BRAND_CARD_GRADIENT,
    BRAND_CARD_INK,
    BRAND_CARD_SOLID,
    THEME_PALETTES,
    debug_log,
    safe_log_text,
)
from .ui import networker_logo_src
from .models import generated_at
from .snapshots import report_theme_palette

def dashboard_alert_lines(dashboard: dict[str, Any]) -> tuple[str, list[str]]:
    summary = dashboard.get("summary") or {}
    protection = dashboard.get("serverProtectionJob") or {}
    lines: list[str] = []
    failed = int(summary.get("failedJobs") or 0)
    critical = int(summary.get("criticalAlerts") or 0)
    warnings = int(summary.get("warningAlerts") or 0)
    active = int(summary.get("activeJobs") or 0)
    protection_status = str(protection.get("status") or "unknown").lower()
    if failed:
        lines.append(f"Failed backup jobs: {failed}")
    if critical:
        lines.append(f"Critical alerts: {critical}")
    if warnings:
        lines.append(f"Warning alerts: {warnings}")
    if protection_status and protection_status not in ("succeeded", "success", "completed", "ok"):
        lines.append(f"Server Protection Job: {protection.get('label') or protection_status} - {protection.get('detail') or ''}")
    lines.append(f"Active jobs: {active}")
    lines.append(f"SLA: {summary.get('slaPercent', 0)}% ({summary.get('slaMetJobs', 0)} met / {summary.get('slaTotalJobs', 0)} total)")
    lines.append(f"Generated: {dashboard.get('generatedAt') or generated_at()}")
    severity = "critical" if failed or critical or protection_status in ("failed", "critical") else ("warning" if warnings or protection_status in ("running", "queued", "warning", "unknown") else "ok")
    return severity, lines




def dashboard_report_rows(dashboard: dict[str, Any]) -> list[tuple[str, str]]:
    summary = dashboard.get("summary") or {}
    protection = dashboard.get("serverProtectionJob") or {}
    health = dashboard.get("serverHealth") or {}
    rows = [
        ("Report range", str(summary.get("rangeLabel") or summary.get("range") or "--")),
        ("Total backup jobs", str(summary.get("totalJobs", 0))),
        ("Successful jobs", str(summary.get("successfulJobs", 0))),
        ("Failed jobs", str(summary.get("failedJobs", 0))),
        ("Running/queued jobs", str(summary.get("activeJobs", 0))),
        ("Recovery jobs", str(summary.get("recoveryJobs", 0))),
        ("Clone jobs", str(summary.get("cloneJobs", 0))),
        ("Alerts", str(summary.get("totalAlerts", 0))),
        ("Backup SLA", f"{summary.get('slaPercent', 0)}% ({summary.get('slaMetJobs', 0)} met / {summary.get('slaTotalJobs', 0)} total)"),
        ("SLA not met", str(summary.get("slaMissedJobs", 0))),
        ("Server status", str(health.get("label") or "--")),
        ("CPU usage", "--" if health.get("cpuUsagePercent") is None else f"{health.get('cpuUsagePercent')}%"),
        ("Memory usage", report_memory_value(health)),
        ("Server Protection Job", f"{protection.get('label') or 'Not found'} - {protection.get('detail') or ''}".strip()),
        ("Generated", str(dashboard.get("generatedAt") or generated_at())),
    ]
    notice = str(dashboard.get("reportNotice") or "").strip()
    if notice:
        rows.insert(1, ("Report notice", safe_log_text(notice, 420)))
    return rows


def report_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def report_percent(part: int, total: int) -> int:
    return round((part / total) * 100) if total else 0


def report_bar(label: str, value: int, max_value: int, color: str, palette: dict[str, str] | None = None) -> str:
    palette = palette or THEME_PALETTES["default"]
    width = max(2, min(100, round((value / max(1, max_value)) * 100)))
    return (
        '<tr>'
        f'<td style="padding:7px 10px 7px 0;width:92px;font-size:12px;color:{palette["ink"]};">{html_lib.escape(label)}</td>'
        '<td style="padding:7px 8px;width:150px;">'
        f'<div style="height:9px;background:{palette["surface2"]};border:1px solid {palette["line"]};border-radius:6px;overflow:hidden;">'
        f'<div style="height:9px;width:{width}%;background:{color};border-radius:6px;"></div>'
        '</div>'
        '</td>'
        f'<td style="padding:7px 0 7px 6px;width:42px;text-align:right;font-size:12px;font-weight:700;color:{palette["ink"]};">{value:,}</td>'
        '</tr>'
    )


def report_metric_card(label: str, value: int, color: str, palette: dict[str, str] | None = None) -> str:
    palette = palette or THEME_PALETTES["default"]
    return (
        '<td style="padding:0 8px 8px 0;width:16.66%;">'
        f'<div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px 14px 12px;">'
        f'<div style="font-size:12px;color:{palette["ink"]};font-weight:700;margin-bottom:20px;">{html_lib.escape(label)}</div>'
        f'<div style="font-size:28px;line-height:1;font-weight:800;color:{color};">{value:,}</div>'
        '</div>'
        '</td>'
    )


def report_donut_card(
    title: str,
    center_value: str,
    center_label: str,
    legend: list[tuple[str, int, str]],
    meta: str,
    width: str = "16.66%",
    min_height: str = "252px",
    donut_size: int = 138,
    inner_size: int = 82,
    palette: dict[str, str] | None = None,
) -> str:
    palette = palette or THEME_PALETTES["default"]
    total = sum(max(0, value) for _, value, _ in legend)
    cursor = 0.0
    segments = []
    for _, value, color in legend:
        if not value or total <= 0:
            continue
        end = cursor + ((value / total) * 360)
        segments.append(f"{color} {cursor:.2f}deg {end:.2f}deg")
        cursor = end
    gradient = ", ".join(segments) if segments else f"{palette['line']} 0deg 360deg"
    legend_rows = "".join(
        '<tr>'
        f'<td style="padding:4px 6px 4px 0;"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};"></span></td>'
        f'<td style="padding:4px 8px 4px 0;font-size:12px;font-weight:700;color:{palette["ink"]};">{html_lib.escape(label)}</td>'
        f'<td style="padding:4px 0;text-align:right;font-size:12px;font-weight:800;color:{palette["ink"]};">{value:,} ({report_percent(value, total)}%)</td>'
        '</tr>'
        for label, value, color in legend
    )
    return f"""
      <td style="padding:0 8px 12px 0;width:{width};min-width:280px;vertical-align:top;">
        <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:{min_height};">
          <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:10px;">
            <tr>
              <td style="font-size:14px;font-weight:800;color:{palette["ink"]};">{html_lib.escape(title)}</td>
              <td style="font-size:12px;color:{palette["muted"]};text-align:right;">{html_lib.escape(meta)}</td>
            </tr>
          </table>
          <table role="presentation" style="width:100%;border-collapse:collapse;">
            <tr>
              <td style="width:{donut_size}px;vertical-align:middle;">
                <div style="width:{donut_size}px;height:{donut_size}px;border-radius:50%;background:conic-gradient({gradient});display:table;text-align:center;">
                  <div style="display:table-cell;vertical-align:middle;">
                    <div style="width:{inner_size}px;height:{inner_size}px;margin:0 auto;border-radius:50%;background:{palette["surface"]};border:1px solid {palette["line"]};display:table;">
                      <div style="display:table-cell;vertical-align:middle;text-align:center;">
                        <div style="font-size:26px;font-weight:850;color:{palette["ink"]};line-height:1;">{html_lib.escape(center_value)}</div>
                        <div style="font-size:10px;text-transform:uppercase;color:{palette["muted"]};font-weight:800;margin-top:5px;">{html_lib.escape(center_label)}</div>
                      </div>
                    </div>
                  </div>
                </div>
              </td>
              <td style="vertical-align:middle;padding-left:14px;">
                <table role="presentation" style="width:100%;border-collapse:collapse;">{legend_rows}</table>
              </td>
            </tr>
          </table>
        </div>
      </td>
    """


def report_decimal(value: Any, digits: int = 1) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "--"
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.{digits}f}"


def report_connection_label(summary: dict[str, Any]) -> tuple[str, str]:
    health = str(summary.get("health") or "").lower()
    if health == "critical":
        return "Connected - action required", "#f36c7d"
    if health == "warning":
        return "Connected with warnings", "#e0a11b"
    return "Connection established", "#4fd17b"


def report_memory_value(health: dict[str, Any]) -> str:
    total = health.get("ramTotalGb")
    used = health.get("ramUsedGb")
    free = health.get("ramFreeGb")
    try:
        total_float = float(total)
    except (TypeError, ValueError):
        total_float = 0.0
    if total_float > 0:
        try:
            used_float = float(used)
        except (TypeError, ValueError):
            try:
                used_float = max(0.0, total_float - float(free))
            except (TypeError, ValueError):
                used_float = 0.0
        return f"{report_decimal(used_float)} / {report_decimal(total_float)} GB"
    ram = health.get("ramUsagePercent")
    return "--" if ram is None else f"{report_int(ram)}%"


def report_memory_detail(health: dict[str, Any]) -> str:
    free = health.get("ramFreeGb")
    percent = health.get("ramUsagePercent")
    try:
        free_float = float(free)
    except (TypeError, ValueError):
        free_float = -1.0
    if free_float >= 0 and percent is not None:
        return f"{report_decimal(free_float)} GB free - {report_int(percent)}% used"
    return str(health.get("ramDetail") or health.get("source") or "No memory metric returned.")


def report_health_card(
    label: str,
    value: str,
    detail: str,
    color: str,
    meter_percent: Any = None,
    palette: dict[str, str] | None = None,
) -> str:
    palette = palette or THEME_PALETTES["default"]
    meter = ""
    if meter_percent is not None:
        width = max(0, min(100, report_int(meter_percent)))
        meter = (
            f'<div style="height:7px;background:{palette["surface2"]};border:1px solid {palette["line"]};'
            'border-radius:6px;overflow:hidden;margin:8px 0 6px;">'
            f'<div style="height:7px;width:{width}%;background:{color};border-radius:6px;"></div>'
            '</div>'
        )
    return (
        '<td style="padding:0 10px 10px 0;width:25%;min-width:260px;vertical-align:top;">'
        f'<div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:92px;">'
        f'<div style="font-size:12px;color:{palette["ink"]};font-weight:700;margin-bottom:12px;">{html_lib.escape(label)}</div>'
        f'<div style="font-size:18px;line-height:1.1;font-weight:850;color:{color};">{html_lib.escape(value)}</div>'
        f'{meter}'
        f'<div style="font-size:11px;line-height:1.35;color:{palette["muted"]};margin-top:10px;">{html_lib.escape(detail)}</div>'
        '</div>'
        '</td>'
    )


def report_color_for_server_status(status: Any, palette: dict[str, str]) -> str:
    return palette["green"] if str(status or "").lower() == "ok" else palette["red"]


def report_color_for_protection_status(status: Any, palette: dict[str, str]) -> str:
    normalized = str(status or "").lower()
    if normalized == "failed":
        return palette["red"]
    if normalized in ("running", "queued", "warning"):
        return palette["amber"]
    return palette["green"]


def report_status_model(dashboard: dict[str, Any]) -> dict[str, Any]:
    summary = dashboard.get("summary") or {}
    target = dashboard.get("target") or {}
    health = dashboard.get("serverHealth") or {}
    protection = dashboard.get("serverProtectionJob") or dashboard.get("maintenanceBackup") or {}
    palette = report_theme_palette(dashboard.get("theme") or target.get("theme"))
    successful = report_int(summary.get("successfulJobs"))
    failed = report_int(summary.get("failedJobs"))
    active = report_int(summary.get("activeJobs"))
    recovery = report_int(summary.get("recoveryJobs"))
    clones = report_int(summary.get("cloneJobs"))
    alerts = report_int(summary.get("totalAlerts"))
    clients = report_int(summary.get("totalClients"))
    sla_total = report_int(summary.get("slaTotalJobs", summary.get("totalJobs")))
    sla_met = report_int(summary.get("slaMetJobs"))
    sla_missed = report_int(summary.get("slaMissedJobs"))
    range_label = str(summary.get("rangeLabel") or summary.get("range") or "Selected range")
    generated = str(dashboard.get("generatedAt") or generated_at())
    connection_label, connection_color = report_connection_label(summary)
    return {
        "summary": summary,
        "target": target,
        "health": health,
        "protection": protection,
        "palette": palette,
        "brand_background": palette["brand"],
        "brand_ink": palette["brandInk"],
        "successful": successful,
        "failed": failed,
        "active": active,
        "recovery": recovery,
        "clones": clones,
        "alerts": alerts,
        "clients": clients,
        "sla_total": sla_total,
        "sla_met": sla_met,
        "sla_missed": sla_missed,
        "sla_percent": report_decimal(summary.get("slaPercent"), 2),
        "range_label": range_label,
        "generated": generated,
        "backup_server": str(target.get("backupServer") or "--"),
        "api_mode": str(target.get("apiMode") or "--").upper(),
        "connection_label": connection_label,
        "connection_color": connection_color,
        "server_status": str(health.get("label") or "Unavailable"),
        "server_status_color": report_color_for_server_status(health.get("status"), palette),
        "cpu_value": "--" if health.get("cpuUsagePercent") is None else f"{report_int(health.get('cpuUsagePercent'))}%",
        "cpu_detail": str(health.get("cpuDetail") or health.get("source") or "No CPU metric returned."),
        "ram_value": report_memory_value(health),
        "ram_detail": report_memory_detail(health),
        "ram_percent": health.get("ramUsagePercent"),
        "protection_color": report_color_for_protection_status(protection.get("status"), palette),
        "protection_label": str(protection.get("label") or "Not found"),
        "protection_detail": str(protection.get("detail") or "No Server Protection job found in this range."),
    }


def snapshot_donut(title: str, value: str, label: str, rows: list[tuple[str, int, str]], meta: str) -> str:
    total = sum(max(0, row_value) for _, row_value, _ in rows)
    cursor = 0.0
    segments = []
    for _, row_value, color in rows:
        if not row_value or total <= 0:
            continue
        end = cursor + ((row_value / total) * 360)
        segments.append(f"{color} {cursor:.2f}deg {end:.2f}deg")
        cursor = end
    gradient = ", ".join(segments) if segments else "#d7e1e7 0deg 360deg"
    legend = "".join(
        f'<div class="legend-item"><i style="background:{color}"></i><span>{html_lib.escape(name)}</span><b>{row_value:,} ({report_percent(row_value, total)}%)</b></div>'
        for name, row_value, color in rows
    )
    return (
        '<article class="card">'
        f'<header><h2>{html_lib.escape(title)}</h2><span>{html_lib.escape(meta)}</span></header>'
        '<div class="donut-layout">'
        f'<div class="donut" style="background:conic-gradient({gradient});"><div class="donut-hole"><strong>{html_lib.escape(value)}</strong><small>{html_lib.escape(label)}</small></div></div>'
        f'<div class="legend-list">{legend}</div>'
        '</div>'
        '</article>'
    )


def snapshot_bar(label: str, value: int, max_value: int, color: str) -> str:
    width = max(2, min(100, round((value / max(1, max_value)) * 100)))
    return (
        '<div class="bar-row">'
        f'<span>{html_lib.escape(label)}</span>'
        f'<div class="bar-track"><i style="width:{width}%;background:{color};"></i></div>'
        f'<strong>{value:,}</strong>'
        '</div>'
    )


def snapshot_metric(label: str, value: int, color: str) -> str:
    return (
        '<article class="metric">'
        f'<span>{html_lib.escape(label)}</span>'
        f'<strong style="color:{color};">{value:,}</strong>'
        '</article>'
    )


def snapshot_health_card(label: str, value: str, detail: str, color: str, percent: Any = None) -> str:
    meter = ""
    if percent is not None:
        width = max(0, min(100, report_int(percent)))
        meter = f'<div class="health-meter"><i style="width:{width}%;background:{color};"></i></div>'
    return (
        '<article class="health-card">'
        f'<span>{html_lib.escape(label)}</span>'
        f'<strong style="color:{color};">{html_lib.escape(value)}</strong>'
        f'{meter}'
        f'<small>{html_lib.escape(detail)}</small>'
        '</article>'
    )


def dashboard_snapshot_html(dashboard: dict[str, Any]) -> str:
    model = report_status_model(dashboard)
    palette = model["palette"]
    # Match the live dashboard brand card exactly (fixed navy->teal gradient).
    # Chrome renders this PNG, so the CSS gradient works here.
    model["brand_background"] = BRAND_CARD_GRADIENT
    model["brand_ink"] = BRAND_CARD_INK
    successful = model["successful"]
    failed = model["failed"]
    active = model["active"]
    recovery = model["recovery"]
    clones = model["clones"]
    alerts = model["alerts"]
    clients = model["clients"]
    summary = model["summary"]
    top_activity = successful + failed + active + recovery
    overview = [
        ("Clients", clients, palette["blue"]),
        ("Successful", successful, palette["green"]),
        ("Failed", failed, palette["red"]),
        ("Running", active, palette["blue"]),
        ("Restores", recovery, palette["amber"]),
        ("Clones", clones, palette["brand"]),
        ("Alerts", alerts, palette["muted"]),
    ]
    max_overview = max([value for _, value, _ in overview] + [1])
    overview_rows = "".join(snapshot_bar(label, value, max_overview, color) for label, value, color in overview)
    activity = snapshot_donut(
        "Activity Mix",
        f"{top_activity:,}",
        "Activity",
        [
            ("Successful", successful, palette["green"]),
            ("Failed", failed, palette["red"]),
            ("Running", active, palette["blue"]),
            ("Restores", recovery, palette["amber"]),
        ],
        model["range_label"],
    )
    sla = snapshot_donut(
        "Backup SLA",
        f'{model["sla_percent"]}%',
        "SLA",
        [
            ("SLA met", model["sla_met"], palette["green"]),
            ("Not met", model["sla_missed"], palette["red"]),
        ],
        f'{model["sla_total"]:,} jobs',
    )
    metrics = "".join(
        [
            snapshot_metric("Clients", clients, palette["blue"]),
            snapshot_metric("Successful Jobs", successful, palette["green"]),
            snapshot_metric("Failed Jobs", failed, palette["red"]),
            snapshot_metric("Active Jobs", active, palette["blue"]),
            snapshot_metric("Recovery Jobs", recovery, palette["amber"]),
            snapshot_metric("Alerts", alerts, palette["amber"]),
        ]
    )
    return f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    :root {{
      --bg:{palette["bg"]}; --surface:{palette["surface"]}; --surface2:{palette["surface2"]};
      --ink:{palette["ink"]}; --muted:{palette["muted"]}; --line:{palette["line"]};
      --green:{palette["green"]}; --red:{palette["red"]}; --amber:{palette["amber"]}; --blue:{palette["blue"]};
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font-family:Aptos,"Aptos Display",Arial,"Segoe UI",sans-serif; }}
    .snapshot {{ width:1880px; padding:6px; }}
    .top-grid {{ display:grid; grid-template-columns:repeat(6, 1fr); gap:12px; }}
    .card, .metric, .health-section {{ background:var(--surface); border:1px solid var(--line); border-radius:8px; box-shadow:0 1px 2px rgba(15,23,42,.08); }}
    .card {{ min-height:338px; padding:16px; display:flex; flex-direction:column; gap:14px; }}
    .brand-card {{ background:{model["brand_background"]}; background-color:{model["brand_background"]}; color:{model["brand_ink"]}; border-color:rgba(255,255,255,.18); }}
    .brand-top {{ display:flex; align-items:center; gap:14px; }}
    .logo {{ width:68px; height:68px; border-radius:7px; background:#f3fbff; padding:4px; object-fit:contain; }}
    h2 {{ margin:0; font-size:14px; line-height:1.25; font-weight:800; }}
    header {{ display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }}
    header span {{ color:var(--muted); font-size:12px; font-weight:700; white-space:nowrap; }}
    .brand-title strong {{ display:block; font-size:18px; line-height:1.15; font-weight:850; }}
    .brand-title span {{ display:block; margin-top:4px; font-size:12px; font-weight:720; color:rgba(255,255,255,.82); }}
    .connection-line {{ min-height:38px; display:flex; align-items:center; gap:8px; padding:9px 11px; margin-top:12px; border-radius:8px; background:rgba(255,255,255,.14); font-size:13px; font-weight:800; }}
    .dot {{ width:12px; height:12px; border-radius:50%; background:{model["connection_color"]}; box-shadow:0 0 0 4px rgba(255,255,255,.14); }}
    .brand-details {{ display:grid; gap:9px; margin-top:auto; }}
    .brand-detail {{ display:flex; justify-content:space-between; gap:12px; font-size:12px; min-height:26px; }}
    .brand-detail span {{ color:rgba(255,255,255,.7); font-weight:700; }}
    .brand-detail strong {{ font-weight:820; text-align:right; }}
    .signature {{ border-top:1px solid rgba(255,255,255,.2); padding-top:11px; display:grid; gap:2px; font-size:11px; line-height:1.25; color:rgba(255,255,255,.85); font-weight:650; }}
    .signature strong {{ color:#fff; font-size:12px; font-weight:850; }}
    .donut-layout {{ display:grid; grid-template-columns:172px minmax(0,1fr); align-items:center; gap:14px; margin-top:auto; }}
    .donut {{ width:172px; height:172px; border-radius:50%; display:grid; place-items:center; box-shadow:inset 0 0 0 1px var(--line); }}
    .donut-hole {{ width:112px; height:112px; border-radius:50%; background:var(--surface); border:1px solid var(--line); display:grid; place-items:center; align-content:center; text-align:center; }}
    .donut-hole strong {{ display:block; font-size:28px; line-height:1; font-weight:850; }}
    .donut-hole small {{ display:block; margin-top:5px; color:var(--muted); font-size:11px; font-weight:800; text-transform:uppercase; }}
    .legend-list {{ display:grid; gap:9px; min-width:0; }}
    .legend-item {{ display:grid; grid-template-columns:12px minmax(0,1fr) auto; align-items:center; gap:8px; font-size:12px; font-weight:730; }}
    .legend-item i {{ width:10px; height:10px; border-radius:50%; }}
    .legend-item span {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
    .legend-item b {{ font-size:12px; font-weight:850; }}
    .bar-chart {{ display:grid; gap:12px; margin-top:auto; }}
    .bar-row {{ display:grid; grid-template-columns:92px minmax(120px,1fr) 54px; gap:10px; align-items:center; min-height:26px; font-size:12px; font-weight:720; }}
    .bar-row > span {{ color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
    .bar-track {{ height:12px; border-radius:999px; background:var(--surface-2); border:1px solid var(--line); overflow:hidden; }}
    .bar-track i {{ display:block; height:100%; border-radius:inherit; }}
    .bar-row strong {{ text-align:right; font-weight:850; }}
    .summary-band {{ padding:12px; border:1px solid var(--line); border-radius:8px; background:var(--surface-2); margin-top:auto; }}
    .summary-band strong {{ display:block; font-size:24px; line-height:1; font-weight:850; }}
    .summary-band span {{ display:block; margin-top:8px; color:var(--muted); font-size:12px; font-weight:720; }}
    .summary-row {{ display:flex; justify-content:space-between; gap:12px; padding:7px 0; font-size:12px; font-weight:730; }}
    .summary-row span {{ color:var(--muted); }}
    .metric-grid {{ display:grid; grid-template-columns:repeat(6, 1fr); gap:12px; margin-top:16px; }}
    .metric {{ min-height:92px; padding:14px; display:grid; align-content:space-between; gap:10px; }}
    .metric span {{ color:var(--muted); font-size:12px; font-weight:720; }}
    .metric strong {{ font-size:30px; line-height:1; font-weight:820; }}
    .health-section {{ margin-top:16px; overflow:hidden; }}
    .health-head {{ display:flex; justify-content:space-between; gap:12px; align-items:center; min-height:52px; padding:14px 16px; background:var(--surface-2); border-bottom:1px solid var(--line); }}
    .health-head strong {{ font-size:14px; font-weight:850; }}
    .health-head span {{ color:var(--muted); font-size:12px; }}
    .health-grid {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; padding:14px 16px 16px; }}
    .health-card {{ min-height:104px; padding:12px; border:1px solid var(--line); border-radius:8px; background:var(--surface); display:grid; gap:8px; align-content:space-between; }}
    .health-card span {{ color:var(--muted); font-size:12px; font-weight:760; }}
    .health-card strong {{ font-size:20px; line-height:1.1; font-weight:850; }}
    .health-card small {{ color:var(--muted); font-size:11px; line-height:1.35; overflow-wrap:anywhere; }}
    .health-meter {{ height:9px; border-radius:999px; background:var(--surface-2); border:1px solid var(--line); overflow:hidden; }}
    .health-meter i {{ display:block; height:100%; border-radius:inherit; }}
  </style>
</head>
<body>
  <main class="snapshot">
    <section class="top-grid">
      <article class="card brand-card">
        <div class="brand-top">
          <img class="logo" alt="NetWorker" width="68" height="68" src="{networker_logo_src()}">
          <div class="brand-title"><strong>DELL EMC NetWorker</strong><span>Backup &amp; Recovery Status</span></div>
        </div>
        <div class="connection-line"><i class="dot"></i>{html_lib.escape(model["connection_label"])}</div>
        <div class="brand-details">
          <div class="brand-detail"><span>API source</span><strong>{html_lib.escape(model["api_mode"])}</strong></div>
          <div class="brand-detail"><span>Backup server</span><strong>{html_lib.escape(model["backup_server"])}</strong></div>
          <div class="brand-detail"><span>Updated</span><strong>{html_lib.escape(model["generated"])}</strong></div>
        </div>
        <div class="signature"><span>Maintained &amp; developed by</span><strong>SHAIKH SHOAIB</strong><span>Sr. Advisor Delivery Specialist</span><span>DELL Technologies</span></div>
      </article>
      {activity}
      {sla}
      <article class="card">
        <header><h2>Management Overview</h2><span>Live API</span></header>
        <div class="bar-chart">{overview_rows}</div>
      </article>
      <article class="card">
        <header><h2>Recovery Health</h2><span>Restores</span></header>
        <div class="summary-band"><strong>{recovery:,}</strong><span>Restore jobs in {html_lib.escape(model["range_label"])}</span></div>
        <div class="summary-row"><span>Failed restores</span><strong>{report_int(summary.get("recoveryFailed")):,}</strong></div>
        <div class="summary-row"><span>Running restores</span><strong>{report_int(summary.get("recoveryRunning")):,}</strong></div>
        <div class="summary-row"><span>Clone jobs excluded</span><strong>{clones:,}</strong></div>
      </article>
      <article class="card">
        <header><h2>Clone Jobs</h2><span>Actions</span></header>
        <div class="summary-band"><strong>{clones:,}</strong><span>Clone jobs in {html_lib.escape(model["range_label"])}</span></div>
        <div class="summary-row"><span>Failed clone jobs</span><strong>{report_int(summary.get("cloneFailed")):,}</strong></div>
        <div class="summary-row"><span>Running clone jobs</span><strong>{report_int(summary.get("cloneRunning")):,}</strong></div>
        <div class="summary-row"><span>Clone sessions</span><strong>{report_int(summary.get("cloneSessionTotal")):,}</strong></div>
      </article>
    </section>
    <section class="metric-grid">{metrics}</section>
    <section class="health-section">
      <div class="health-head"><strong>NetWorker Server Health</strong><span>Updated {html_lib.escape(model["generated"])} - {html_lib.escape(model["range_label"])}</span></div>
      <div class="health-grid">
        {snapshot_health_card("Server status", model["server_status"], str(model["health"].get("detail") or "CPU/RAM endpoint did not return data."), model["server_status_color"])}
        {snapshot_health_card("CPU usage", model["cpu_value"], model["cpu_detail"], palette["blue"], model["health"].get("cpuUsagePercent"))}
        {snapshot_health_card("Memory usage", model["ram_value"], model["ram_detail"], palette["amber"], model["ram_percent"])}
        {snapshot_health_card("Server Protection Job", model["protection_label"], model["protection_detail"], model["protection_color"])}
      </div>
    </section>
  </main>
</body>
</html>
"""


def headless_browser_path() -> str:
    candidates = [
        shutil.which("msedge"),
        shutil.which("microsoft-edge"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return ""


def render_dashboard_snapshot_png(dashboard: dict[str, Any]) -> bytes | None:
    browser = headless_browser_path()
    if not browser:
        debug_log("Dashboard email snapshot skipped: no Edge/Chrome browser found.")
        return None
    with tempfile.TemporaryDirectory(prefix="networker-dashboard-report-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        html_path = tmp_path / "dashboard-report.html"
        png_path = tmp_path / "dashboard-report.png"
        html_path.write_text(dashboard_snapshot_html(dashboard), encoding="utf-8")
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--disable-dev-shm-usage",
            "--window-size=1880,760",
            f"--screenshot={png_path}",
            html_path.as_uri(),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not png_path.exists():
            debug_log(f"Dashboard email snapshot failed: {result.stderr or result.stdout or result.returncode}")
            return None
        try:
            return png_path.read_bytes()
        except OSError as exc:
            debug_log(f"Dashboard email snapshot read failed: {exc}")
            return None


def dashboard_report_email(dashboard: dict[str, Any], snapshot_cid: str = "") -> tuple[str, str]:
    summary = dashboard.get("summary") or {}
    target = dashboard.get("target") or {}
    health = dashboard.get("serverHealth") or {}
    protection = dashboard.get("serverProtectionJob") or dashboard.get("maintenanceBackup") or {}
    palette = report_theme_palette(dashboard.get("theme") or target.get("theme"))
    # Match the live dashboard brand card. It uses a fixed navy->teal gradient,
    # not the theme brand color. Email clients (Outlook) ignore CSS gradients, so
    # every bgcolor uses the solid dark-teal fallback; the outer brand container
    # additionally carries the gradient via background-image for modern clients.
    brand_background = BRAND_CARD_SOLID
    brand_ink = BRAND_CARD_INK
    rows = dashboard_report_rows(dashboard)
    plain = "\n".join(f"{label}: {value}" for label, value in rows)
    total_jobs = report_int(summary.get("slaTotalJobs", summary.get("totalJobs")))
    successful = report_int(summary.get("successfulJobs"))
    failed = report_int(summary.get("failedJobs"))
    active = report_int(summary.get("activeJobs"))
    recovery = report_int(summary.get("recoveryJobs"))
    clones = report_int(summary.get("cloneJobs"))
    alerts = report_int(summary.get("totalAlerts"))
    clients = report_int(summary.get("totalClients"))
    sla_percent = report_decimal(summary.get("slaPercent"), 2)
    sla_met = report_int(summary.get("slaMetJobs"))
    sla_missed = report_int(summary.get("slaMissedJobs"))
    range_label = str(summary.get("rangeLabel") or summary.get("range") or "Selected range")
    generated = str(dashboard.get("generatedAt") or generated_at())
    backup_server = str((target.get("backupServer") or "--"))
    api_mode = str((target.get("apiMode") or "--")).upper()
    connection_label, connection_color = report_connection_label(summary)
    server_status = str(health.get("label") or "Unavailable")
    server_status_color = palette["green"] if str(health.get("status") or "").lower() == "ok" else palette["red"]
    cpu_value = "--" if health.get("cpuUsagePercent") is None else f"{report_int(health.get('cpuUsagePercent'))}%"
    cpu_detail = str(health.get("cpuDetail") or health.get("source") or "No CPU metric returned.")
    ram_value = report_memory_value(health)
    ram_detail = report_memory_detail(health)
    ram_percent = health.get("ramUsagePercent")
    protection_status = str(protection.get("status") or "").lower()
    protection_color = (
        palette["red"]
        if protection_status == "failed"
        else (palette["amber"] if protection_status in ("running", "queued", "warning") else palette["green"])
    )
    protection_label = str(protection.get("label") or "Not found")
    protection_detail = str(protection.get("detail") or "No Server Protection job found in this range.")
    overview = [
        ("Clients", clients, palette["blue"]),
        ("Successful", successful, palette["green"]),
        ("Failed", failed, palette["red"]),
        ("Running", active, palette["blue"]),
        ("Restores", recovery, palette["amber"]),
        ("Clones", clones, palette["brand"]),
        ("Alerts", alerts, palette["muted"]),
    ]
    max_overview = max([value for _, value, _ in overview] + [1])
    top_activity = successful + failed + active + recovery
    table_rows = "\n".join(
        "<tr>"
        f"<td style=\"padding:8px 10px;border:1px solid {palette['line']};font-weight:700;color:{palette['ink']};\">{html_lib.escape(label)}</td>"
        f"<td style=\"padding:8px 10px;border:1px solid {palette['line']};color:{palette['ink']};\">{html_lib.escape(value)}</td>"
        "</tr>"
        for label, value in rows
    )
    metric_rows = (
        "<tr>"
        + report_metric_card("Clients", clients, palette["blue"], palette)
        + report_metric_card("Successful Jobs", successful, palette["green"], palette)
        + report_metric_card("Failed Jobs", failed, palette["red"], palette)
        + report_metric_card("Active Jobs", active, palette["blue"], palette)
        + report_metric_card("Recovery Jobs", recovery, palette["amber"], palette)
        + report_metric_card("Alerts", alerts, palette["amber"], palette)
        + "</tr>"
    )
    overview_rows = "".join(report_bar(label, value, max_overview, color, palette) for label, value, color in overview)
    recovery_rows = (
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Failed restores <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("recoveryFailed")):,}</strong></div>'
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Running restores <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("recoveryRunning")):,}</strong></div>'
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Clone jobs excluded <strong style="float:right;color:{palette["ink"]};">{clones:,}</strong></div>'
    )
    clone_rows = (
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Failed clone jobs <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("cloneFailed")):,}</strong></div>'
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Running clone jobs <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("cloneRunning")):,}</strong></div>'
        f'<div style="font-size:12px;padding:6px 0;color:{palette["ink"]};">Clone sessions <strong style="float:right;color:{palette["ink"]};">{report_int(summary.get("cloneSessionTotal")):,}</strong></div>'
    )
    snapshot_block = ""
    if snapshot_cid:
        escaped_cid = html_lib.escape(snapshot_cid, quote=True)
        snapshot_block = (
            f'<div style="margin:0 0 14px;background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:10px;">'
            f'<img alt="NetWorker dashboard snapshot" src="cid:{escaped_cid}" '
            'style="display:block;width:100%;max-width:1880px;height:auto;border:0;border-radius:6px;">'
            '</div>'
        )
    html_body = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:{palette["bg"]};font-family:Aptos,Arial,Segoe UI,sans-serif;color:{palette["ink"]};">
    <div style="padding:18px;background:{palette["bg"]};">
      {snapshot_block}
      <table role="presentation" style="width:100%;min-width:1680px;border-collapse:collapse;">
        <tr>
          <td bgcolor="{brand_background}" style="padding:0 8px 12px 0;width:16.66%;min-width:280px;vertical-align:top;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
            <div style="background:{brand_background};background-color:{brand_background};background-image:{BRAND_CARD_GRADIENT};border-radius:8px;padding:16px;color:{brand_ink};min-height:252px;">
              <table role="presentation" bgcolor="{brand_background}" style="width:100%;border-collapse:collapse;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                <tr>
                  <td bgcolor="{brand_background}" style="width:68px;vertical-align:top;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                    <img alt="NetWorker" width="60" height="60" src="{networker_logo_src()}" style="display:block;width:60px;height:60px;max-width:60px;max-height:60px;object-fit:contain;background:{palette["surface"]};border-radius:6px;padding:4px;">
                  </td>
                  <td bgcolor="{brand_background}" style="vertical-align:top;padding-left:10px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                    <div style="font-size:16px;font-weight:850;color:{brand_ink};">DELL EMC NetWorker</div>
                    <div style="font-size:12px;font-weight:700;margin-top:3px;color:{brand_ink};">Backup &amp; Recovery Status</div>
                  </td>
                </tr>
              </table>
              <div style="margin-top:14px;background:{brand_background};background-color:{brand_background};border-radius:7px;padding:10px;font-size:13px;font-weight:800;color:{brand_ink};"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:{connection_color};vertical-align:-1px;margin-right:6px;"></span>{html_lib.escape(connection_label)}</div>
              <table role="presentation" bgcolor="{brand_background}" style="width:100%;border-collapse:collapse;margin-top:18px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                <tr><td bgcolor="{brand_background}" style="padding:6px 0;font-size:12px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">API source</td><td bgcolor="{brand_background}" style="padding:6px 0;text-align:right;font-size:12px;font-weight:800;background:{brand_background};background-color:{brand_background};color:{brand_ink};">{html_lib.escape(api_mode)}</td></tr>
                <tr><td bgcolor="{brand_background}" style="padding:6px 0;font-size:12px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">Backup server</td><td bgcolor="{brand_background}" style="padding:6px 0;text-align:right;font-size:12px;font-weight:800;background:{brand_background};background-color:{brand_background};color:{brand_ink};">{html_lib.escape(backup_server)}</td></tr>
                <tr><td bgcolor="{brand_background}" style="padding:6px 0;font-size:12px;background:{brand_background};background-color:{brand_background};color:{brand_ink};">Updated</td><td bgcolor="{brand_background}" style="padding:6px 0;text-align:right;font-size:12px;font-weight:800;background:{brand_background};background-color:{brand_background};color:{brand_ink};">{html_lib.escape(generated)}</td></tr>
              </table>
              <div style="margin-top:12px;border-top:1px solid rgba(255,255,255,0.22);padding-top:10px;font-size:11px;line-height:1.35;background:{brand_background};background-color:{brand_background};color:{brand_ink};">
                <div>Maintained &amp; developed by</div>
                <div style="font-weight:850;">SHAIKH SHOAIB</div>
                <div>Sr. Advisor Delivery Specialist</div>
                <div>DELL Technologies</div>
              </div>
            </div>
          </td>
          {report_donut_card(
              "Activity Mix",
              f"{top_activity:,}",
              "Activity",
              [
                  ("Successful", successful, palette["green"]),
                  ("Failed", failed, palette["red"]),
                  ("Running", active, palette["blue"]),
                  ("Restores", recovery, palette["amber"]),
              ],
              range_label,
              palette=palette,
          )}
          {report_donut_card(
              "Backup SLA",
              f"{sla_percent}%",
              "SLA",
              [
                  ("SLA met", sla_met, palette["green"]),
                  ("Not met", sla_missed, palette["red"]),
              ],
              f"{total_jobs:,} jobs",
              palette=palette,
          )}
          <td style="padding:0 8px 12px 0;width:16.66%;min-width:280px;vertical-align:top;">
            <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:252px;">
              <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:12px;">
                <tr><td style="font-size:14px;font-weight:800;color:{palette["ink"]};">Management Overview</td><td style="font-size:12px;text-align:right;color:{palette["muted"]};">Live API</td></tr>
              </table>
              <table role="presentation" style="width:100%;border-collapse:collapse;">{overview_rows}</table>
            </div>
          </td>
          <td style="padding:0 8px 12px 0;width:16.66%;min-width:280px;vertical-align:top;">
            <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:252px;">
              <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:18px;">
                <tr><td style="font-size:14px;font-weight:800;color:{palette["ink"]};">Recovery Health</td><td style="font-size:12px;text-align:right;color:{palette["muted"]};">Restores</td></tr>
              </table>
              <div style="background:{palette["surface2"]};border:1px solid {palette["line"]};border-radius:7px;padding:12px;margin-bottom:13px;">
                <div style="font-size:24px;font-weight:850;color:{palette["ink"]};">{recovery:,}</div>
                <div style="font-size:12px;font-weight:700;color:{palette["muted"]};">Restore jobs in {html_lib.escape(range_label)}</div>
              </div>
              {recovery_rows}
            </div>
          </td>
          <td style="padding:0 8px 12px 0;width:16.66%;min-width:280px;vertical-align:top;">
            <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;min-height:252px;">
              <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:18px;">
                <tr><td style="font-size:14px;font-weight:800;color:{palette["ink"]};">Clone Jobs</td><td style="font-size:12px;text-align:right;color:{palette["muted"]};">Actions</td></tr>
              </table>
              <div style="background:{palette["surface2"]};border:1px solid {palette["line"]};border-radius:7px;padding:12px;margin-bottom:13px;">
                <div style="font-size:24px;font-weight:850;color:{palette["ink"]};">{clones:,}</div>
                <div style="font-size:12px;font-weight:700;color:{palette["muted"]};">Clone jobs in {html_lib.escape(range_label)}</div>
              </div>
              {clone_rows}
            </div>
          </td>
        </tr>
      </table>

      <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:12px;">{metric_rows}</table>

      <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;margin-bottom:12px;">
        <table role="presentation" style="width:100%;border-collapse:collapse;border-bottom:1px solid {palette["line"]};">
          <tr>
            <td style="padding:14px 16px;font-size:14px;font-weight:850;color:{palette["ink"]};">NetWorker Server Health</td>
            <td style="padding:14px 16px;font-size:12px;text-align:right;color:{palette["muted"]};">Updated {html_lib.escape(generated)} - {html_lib.escape(range_label)}</td>
          </tr>
        </table>
        <table role="presentation" style="width:100%;border-collapse:collapse;padding:12px;">
          <tr>
            {report_health_card("Server status", server_status, str(health.get("detail") or "CPU/RAM endpoint did not return data."), server_status_color, palette=palette)}
            {report_health_card("CPU usage", cpu_value, cpu_detail, palette["blue"], health.get("cpuUsagePercent"), palette)}
            {report_health_card("Memory usage", ram_value, ram_detail, palette["amber"], ram_percent, palette)}
            {report_health_card("Server Protection Job", protection_label, protection_detail, protection_color, palette=palette)}
          </tr>
        </table>
      </div>

      <div style="background:{palette["surface"]};border:1px solid {palette["line"]};border-radius:8px;padding:14px;">
        <h3 style="margin:0 0 12px;font-size:14px;color:{palette["ink"]};">Report Details</h3>
        <table style="border-collapse:collapse;border:1px solid {palette["line"]};min-width:520px;width:100%;">
          {table_rows}
        </table>
      </div>
    </div>
  </body>
</html>
"""
    return plain, html_body




def excel_col(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def excel_ref(row: int, col: int) -> str:
    return f"{excel_col(col)}{row}"


def xml_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def xlsx_cell(row: int, col: int, value: Any, style: int = 0) -> str:
    ref = excel_ref(row, col)
    style_attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{style_attr} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{xml_escape(value)}</t></is></c>'


def worksheet_xml(
    rows: list[list[Any]],
    sheet_name: str,
    drawing_id: str | None = None,
    column_widths: list[int] | None = None,
) -> str:
    max_cols = max((len(row) for row in rows), default=1)
    widths = column_widths or [18] * max_cols
    cols_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(widths[:max_cols], start=1)
    )
    row_xml = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx in range(1, max_cols + 1):
            value = row[c_idx - 1] if c_idx <= len(row) else ""
            style = 1 if r_idx == 1 else 0
            if sheet_name == "Dashboard" and r_idx in (1, 3, 11):
                style = 2 if r_idx == 1 else 1
            cells.append(xlsx_cell(r_idx, c_idx, value, style))
        row_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    drawing_xml = f'<drawing r:id="{drawing_id}"/>' if drawing_id else ""
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <cols>{cols_xml}</cols>
 <sheetData>{"".join(row_xml)}</sheetData>
 {drawing_xml}
</worksheet>'''


def workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, name in enumerate(sheet_names, start=1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets>{sheets}</sheets>
</workbook>'''


def workbook_rels_xml(sheet_count: int) -> str:
    rels = []
    for idx in range(1, sheet_count + 1):
        rels.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )
    rels.append(
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 {"".join(rels)}
</Relationships>'''


def styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <fonts count="3">
  <font><sz val="11"/><color rgb="FF172026"/><name val="Aptos"/></font>
  <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font>
  <font><b/><sz val="18"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font>
 </fonts>
 <fills count="4">
  <fill><patternFill patternType="none"/></fill>
  <fill><patternFill patternType="gray125"/></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FF126E82"/><bgColor indexed="64"/></patternFill></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FF102832"/><bgColor indexed="64"/></patternFill></fill>
 </fills>
 <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
 <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
 <cellXfs count="3">
  <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
  <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/>
  <xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyFill="1" applyFont="1"/>
 </cellXfs>
</styleSheet>'''


def content_types_xml(sheet_count: int) -> str:
    overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
        '<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>',
        '<Override PartName="/xl/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>',
    ]
    for idx in range(1, sheet_count + 1):
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 {"".join(overrides)}
</Types>'''


def package_rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''


def drawing_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <xdr:twoCellAnchor>
  <xdr:from><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
  <xdr:to><xdr:col>9</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>17</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>
  <xdr:graphicFrame macro="">
   <xdr:nvGraphicFramePr><xdr:cNvPr id="2" name="Backup Status Pie Chart"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr>
   <xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>
   <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
    <c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="rId1"/>
   </a:graphicData></a:graphic>
  </xdr:graphicFrame>
  <xdr:clientData/>
 </xdr:twoCellAnchor>
</xdr:wsDr>'''


def chart_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <c:chart>
  <c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Backup Status Distribution</a:t></a:r></a:p></c:rich></c:tx></c:title>
  <c:plotArea>
   <c:layout/>
   <c:pieChart>
    <c:varyColors val="1"/>
    <c:ser>
     <c:idx val="0"/><c:order val="0"/>
     <c:cat><c:strRef><c:f>Dashboard!$A$12:$A$16</c:f></c:strRef></c:cat>
     <c:val><c:numRef><c:f>Dashboard!$B$12:$B$16</c:f></c:numRef></c:val>
    </c:ser>
    <c:firstSliceAng val="270"/>
   </c:pieChart>
  </c:plotArea>
  <c:legend><c:legendPos val="r"/><c:layout/></c:legend>
  <c:plotVisOnly val="1"/>
 </c:chart>
 <c:printSettings><c:headerFooter/><c:pageMargins b="0.75" l="0.7" r="0.7" t="0.75" header="0.3" footer="0.3"/><c:pageSetup/></c:printSettings>
</c:chartSpace>'''


def simple_rels_xml(target: str, rel_type: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="{rel_type}" Target="{target}"/>
</Relationships>'''


def core_props_xml() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <dc:title>NetWorker Backup and Restore Dashboard Report</dc:title>
 <dc:creator>{APP_NAME}</dc:creator>
 <cp:lastModifiedBy>{APP_NAME}</cp:lastModifiedBy>
 <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
 <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>'''


def app_props_xml(sheet_names: list[str]) -> str:
    titles = "".join(f'<vt:lpstr>{xml_escape(name)}</vt:lpstr>' for name in sheet_names)
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
 <Application>NetWorker Dashboard</Application>
 <TitlesOfParts><vt:vector size="{len(sheet_names)}" baseType="lpstr">{titles}</vt:vector></TitlesOfParts>
</Properties>'''


def rows_from_table(items: list[dict[str, Any]], columns: list[tuple[str, str]]) -> list[list[Any]]:
    rows = [[label for _, label in columns]]
    for item in items:
        rows.append([item.get(key, "") for key, _ in columns])
    return rows


def build_excel_report(dashboard: dict[str, Any]) -> bytes:
    summary = dashboard.get("summary", {}) if isinstance(dashboard.get("summary"), dict) else {}
    tables = dashboard.get("tables", {}) if isinstance(dashboard.get("tables"), dict) else {}
    target = dashboard.get("target", {}) if isinstance(dashboard.get("target"), dict) else {}
    status_rows = [
        ["Succeeded", int(summary.get("successfulJobs") or 0)],
        ["Failed", int(summary.get("failedJobs") or 0)],
        ["Running / Queued", int(summary.get("activeJobs") or 0)],
        ["Restores", int(summary.get("recoveryJobs") or 0)],
        ["Clones", int(summary.get("cloneJobs") or 0)],
        ["Alerts", int(summary.get("totalAlerts") or 0)],
    ]
    dashboard_rows = [
        ["NetWorker Backup and Restore Dashboard Report", ""],
        ["Generated", dashboard.get("generatedAt", "")],
        ["Range", summary.get("rangeLabel", "")],
        ["API Source", target.get("apiMode", "")],
        ["API Base", target.get("restApiBase", "")],
        ["Total Jobs", int(summary.get("totalJobs") or 0)],
        ["Successful Jobs", int(summary.get("successfulJobs") or 0)],
        ["Failed Jobs", int(summary.get("failedJobs") or 0)],
        ["Active Jobs", int(summary.get("activeJobs") or 0)],
        ["Restore Jobs", int(summary.get("recoveryJobs") or 0)],
        ["Clone Jobs", int(summary.get("cloneJobs") or 0)],
        ["Status", "Count"],
        *status_rows,
    ]
    jobs_cols = [
        ("client", "Client"),
        ("name", "Job"),
        ("policy", "Policy"),
        ("status", "Status"),
        ("started", "Started"),
        ("duration", "Duration"),
        ("message", "Message"),
    ]
    failed_cols = [
        ("client", "Client"),
        ("name", "Job"),
        ("policy", "Policy"),
        ("started", "Started"),
        ("message", "Message"),
    ]
    log_cols = [
        ("priority", "Priority"),
        ("time", "Time"),
        ("source", "Source"),
        ("category", "Category"),
        ("message", "Message"),
    ]
    alert_cols = [("severity", "Severity"), ("time", "Time"), ("message", "Message"), ("resource", "Resource")]
    client_cols = [
        ("hostname", "Hostname"),
        ("enabled", "Enabled"),
        ("backupType", "Backup Type"),
        ("saveSets", "Save Sets"),
        ("protectionGroups", "Protection Groups"),
    ]
    sheets = [
        ("Dashboard", dashboard_rows, [28, 18, 18, 18, 18, 18, 18, 18, 18]),
        ("Backup Jobs", rows_from_table(tables.get("jobs", []), jobs_cols), [22, 24, 24, 16, 24, 14, 48]),
        ("Failed Jobs", rows_from_table(tables.get("failedJobs", []), failed_cols), [22, 24, 24, 24, 58]),
        ("Restores", rows_from_table(tables.get("recovery", []), jobs_cols), [22, 24, 24, 16, 24, 14, 48]),
        ("Clone Jobs", rows_from_table(tables.get("cloneJobs", []), jobs_cols), [22, 24, 24, 16, 24, 14, 48]),
        ("Logs", rows_from_table(tables.get("logs", []), log_cols), [14, 24, 16, 18, 80]),
        ("Alerts", rows_from_table(tables.get("alerts", []), alert_cols), [16, 24, 64, 28]),
        ("Clients", rows_from_table(tables.get("clients", []), client_cols), [28, 16, 20, 24, 40]),
    ]
    sheet_names = [name for name, _, _ in sheets]
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml(len(sheets)))
        zf.writestr("_rels/.rels", package_rels_xml())
        zf.writestr("docProps/core.xml", core_props_xml())
        zf.writestr("docProps/app.xml", app_props_xml(sheet_names))
        zf.writestr("xl/workbook.xml", workbook_xml(sheet_names))
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml(len(sheets)))
        zf.writestr("xl/styles.xml", styles_xml())
        for idx, (name, rows, widths) in enumerate(sheets, start=1):
            drawing_id = "rId1" if idx == 1 else None
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", worksheet_xml(rows, name, drawing_id, widths))
        zf.writestr(
            "xl/worksheets/_rels/sheet1.xml.rels",
            simple_rels_xml("../drawings/drawing1.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"),
        )
        zf.writestr("xl/drawings/drawing1.xml", drawing_xml())
        zf.writestr(
            "xl/drawings/_rels/drawing1.xml.rels",
            simple_rels_xml("../charts/chart1.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"),
        )
        zf.writestr("xl/charts/chart1.xml", chart_xml())
    return output.getvalue()
