from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_dashboard_has_display_section():
    assert 'id="tvDisplayPanel"' in (A / "dashboard.html").read_text(encoding="utf-8")

def test_appjs_wires_display_config():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/display-config" in js
    assert "renderDisplayConfig" in js
