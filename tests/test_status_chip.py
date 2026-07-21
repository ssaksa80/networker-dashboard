# tests/test_status_chip.py
from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_schedule_rows_render_status_chip():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "em-status" in js
    for status in ("live", "reconnectable", "waiting"):
        assert status in js

def test_status_chip_css():
    css = (A / "app.css").read_text(encoding="utf-8")
    assert ".em-status" in css
