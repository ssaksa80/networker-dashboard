from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_dashboard_has_display_section():
    assert 'id="tvDisplayPanel"' in (A / "dashboard.html").read_text(encoding="utf-8")

def test_appjs_wires_display_config():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/display-config" in js
    assert "renderDisplayConfig" in js

def test_token_ui_kept_but_connection_form_moved_to_reports_page():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    js = (A / "app.js").read_text(encoding="utf-8")
    # token URL + rotate/revoke stay on the dashboard drawer
    for eid in ("tvDisplayUrl", "tvRotateBtn", "tvRevokeBtn"):
        assert f'id="{eid}"' in html and eid in js
    # the shared reporting connection is now set on /reports
    for eid in ("tvConnForm", "tvConnState", "tvConnError"):
        assert eid not in html and eid not in js
    assert 'href="/reports"' in html
