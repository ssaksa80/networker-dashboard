from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"


def test_drawer_panels_removed():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="scheduledReportsPanel"' not in html
    assert 'id="smtpSettingsPanel"' not in html


def test_buttons_open_reports_page():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert '"/reports"' in js or "'/reports'" in js
    assert "renderReportGroups" not in js      # group manager moved to reports.js
