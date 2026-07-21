from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_menu_has_config_buttons():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    for bid in ("emailSettingsBtn", "reportsPanelBtn", "tvDisplayBtn"):
        assert f'id="{bid}"' in html

def test_config_panels_hidden_by_default():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    import re
    m = re.search(r'<section id="tvDisplayPanel"[^>]*class="([^"]*)"', html)
    assert m and "hidden" in m.group(1), "tvDisplayPanel must have hidden class"

def test_appjs_wires_reveal():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "openConfigDrawer" in js

def test_email_and_reports_buttons_launch_the_reports_page():
    # Email (SMTP) and Scheduled Reports now open /reports in a new tab;
    # only TV / Display still reveals a drawer panel.
    js = (A / "app.js").read_text(encoding="utf-8")
    assert 'window.open("/reports", "_blank", "noopener")' in js
    assert "revealTvPanel" in js
