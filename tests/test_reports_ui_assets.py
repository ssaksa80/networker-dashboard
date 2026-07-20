from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_dashboard_has_reports_panel():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="scheduledReportsPanel"' in html

def test_appjs_wires_report_endpoints():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/report-jobs" in js
    assert "renderReportJobs" in js
    assert "reportHealthBadge" in js
