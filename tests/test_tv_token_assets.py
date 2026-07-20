from pathlib import Path
TV = Path(__file__).resolve().parents[1] / "nwdash" / "assets" / "tv.js"

def test_tv_js_is_token_aware():
    js = TV.read_text(encoding="utf-8")
    assert "/api/display/" in js
    assert "DISPLAY_TOKEN" in js
