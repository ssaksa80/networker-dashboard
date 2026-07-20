from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_smtp_settings_markup():
    assert 'id="smtpSettingsForm"' in (A / "dashboard.html").read_text(encoding="utf-8")

def test_smtp_settings_js():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "loadSmtpSettings" in js and "/api/email-config" in js
