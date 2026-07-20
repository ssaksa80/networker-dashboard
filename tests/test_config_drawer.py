from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_config_drawer_markup():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="configDrawer"' in html
    assert 'id="configDrawerClose"' in html
    assert 'id="configDrawerOverlay"' in html

def test_panels_moved_into_drawer_not_bottom_panels():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    # panels must live inside the drawer: configDrawer appears before each panel id
    i = html.find('id="configDrawer"')
    assert i != -1
    for pid in ("smtpSettingsPanel", "scheduledReportsPanel", "tvDisplayPanel"):
        assert html.find(f'id="{pid}"') > i, f"{pid} must be inside the config drawer"

def test_js_drawer_open_close():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "openConfigDrawer" in js and "closeConfigDrawer" in js
