from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_old_modal_markup_gone():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="alertConfigBtn"' not in html
    assert 'id="alertAutomationModal"' not in html

def test_old_modal_js_gone():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/alert-automation" not in js
    assert "openAlertAutomationModal" not in js
    assert "alertAutomationModal" not in js
