from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_config_drawer_markup():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="configDrawer"' in html
    assert 'id="configDrawerClose"' in html
    assert 'id="configDrawerOverlay"' in html

def test_tv_panel_lives_inside_the_drawer():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    # the TV panel must live inside the drawer: configDrawer appears before it
    i = html.find('id="configDrawer"')
    assert i != -1
    assert html.find('id="tvDisplayPanel"') > i, "tvDisplayPanel must be inside the config drawer"


def test_smtp_and_report_panels_no_longer_in_the_drawer():
    # both moved to the /reports page (see tests/test_reports_page.py)
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="smtpSettingsPanel"' not in html
    assert 'id="scheduledReportsPanel"' not in html

def test_js_drawer_open_close():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "openConfigDrawer" in js and "closeConfigDrawer" in js
