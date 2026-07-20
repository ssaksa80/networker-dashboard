from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_menu_has_config_buttons():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    for bid in ("emailSettingsBtn", "reportsPanelBtn", "tvDisplayBtn"):
        assert f'id="{bid}"' in html

def test_config_panels_hidden_by_default():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    import re
    for pid in ("smtpSettingsPanel", "scheduledReportsPanel", "tvDisplayPanel"):
        m = re.search(r'<section id="%s"[^>]*class="([^"]*)"' % pid, html)
        assert m and "hidden" in m.group(1), f"{pid} must have hidden class"

def test_appjs_wires_reveal():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "revealConfigPanel" in js
