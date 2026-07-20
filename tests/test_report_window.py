from datetime import datetime
from nwdash import report_window as rw

def dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi).astimezone()

def test_section_keys_stable():
    assert rw.SECTION_KEYS == ["backup_sla", "management", "recovery", "clone", "alerts", "server_protection", "health"]

def test_daily_window_is_last_24h():
    r, s, e = rw.compute_window("daily", now=dt(2026, 7, 21, 9, 0))
    assert r == "24h" and s == "" and e == ""

def test_weekly_window_prev_sunday_to_saturday():
    r, s, e = rw.compute_window("weekly", now=dt(2026, 7, 21, 9, 0))
    assert r == "custom" and s == "2026-07-12" and e == "2026-07-18"

def test_monthly_window_prev_calendar_month():
    r, s, e = rw.compute_window("monthly", now=dt(2026, 7, 21, 9, 0))
    assert r == "custom" and s == "2026-06-01" and e == "2026-06-30"

def test_monthly_window_january_crosses_year():
    r, s, e = rw.compute_window("monthly", now=dt(2026, 1, 15))
    assert s == "2025-12-01" and e == "2025-12-31"

def test_next_run_daily_today_if_time_ahead():
    nr = rw.next_run("daily", "09:30", now=dt(2026, 7, 21, 9, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 7, 21, 9, 30)

def test_next_run_daily_tomorrow_if_time_passed():
    nr = rw.next_run("daily", "08:00", now=dt(2026, 7, 21, 9, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 7, 22, 8, 0)

def test_next_run_weekly_next_sunday():
    nr = rw.next_run("weekly", "07:00", now=dt(2026, 7, 21, 9, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 7, 26, 7, 0)

def test_next_run_weekly_today_sunday_time_ahead():
    nr = rw.next_run("weekly", "09:00", now=dt(2026, 7, 26, 8, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 7, 26, 9, 0)

def test_next_run_monthly_first_of_next_month():
    nr = rw.next_run("monthly", "06:00", now=dt(2026, 7, 21, 9, 0))
    assert datetime.fromtimestamp(nr).astimezone() == dt(2026, 8, 1, 6, 0)
