from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_checkbox_exempt_from_global_input_sizing():
    css = (A / "app.css").read_text(encoding="utf-8")
    assert 'input[type="checkbox"]' in css
    i = css.find('input[type="checkbox"], input[type="radio"]')
    assert i != -1
    # the exemption must come AFTER the global `input, select` sizing rule
    assert i > css.find("input, select {")

def test_group_checkbox_labels_are_check_rows():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'class="check-row"' in html
    # the report-group checkbox rows moved to the /reports page
    assert 'class="check-row"' in (A / "reports.html").read_text(encoding="utf-8")
    assert "check-row" in (A / "reports.js").read_text(encoding="utf-8")
