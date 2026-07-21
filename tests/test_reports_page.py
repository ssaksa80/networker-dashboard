# tests/test_reports_page.py
from pathlib import Path
from nwdash import ui
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_reports_assets_exist():
    assert (A / "reports.html").is_file() and (A / "reports.js").is_file()

def test_reports_page_html_renders_sections():
    html = ui.reports_page_html()
    assert 'id="connPanel"' in html and 'id="smtpPanel"' in html and 'id="groupsPanel"' in html
    assert "__REPORTS_JS__" not in html   # placeholder substituted

def test_reports_js_wires_endpoints():
    js = (A / "reports.js").read_text(encoding="utf-8")
    assert "use-current-connection" in js and "validate-connection" in js
    assert "/api/report-groups" in js and "/api/email-config" in js


def test_no_legacy_report_jobs_endpoint():
    # guard carried over from the removed tests/test_groups_ui.py: the legacy
    # /api/report-jobs endpoint is 410 Gone; no front-end may still call it.
    for name in ("reports.js", "app.js"):
        assert "/api/report-jobs" not in (A / name).read_text(encoding="utf-8")


def test_group_manager_lives_on_reports_page_only():
    assert "renderGroups" in (A / "reports.js").read_text(encoding="utf-8")
    assert "reportSectionChecks" in (A / "reports.html").read_text(encoding="utf-8")
    # ...and nowhere on the dashboard
    assert "reportSectionChecks" not in (A / "app.js").read_text(encoding="utf-8")
