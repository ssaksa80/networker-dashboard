from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_group_ui_markup():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="reportGroupsList"' in html and 'id="reportGroupForm"' in html

def test_group_ui_js():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/report-groups" in js
    assert "renderReportGroups" in js
    assert "reportSectionChecks" in js
    assert "/api/report-jobs" not in js
