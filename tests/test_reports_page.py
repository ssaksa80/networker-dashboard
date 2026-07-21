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
