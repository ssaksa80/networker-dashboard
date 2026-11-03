"""A daily report keeps its local send time on the days the clocks change.

The defect (review C23, the next-run half): the next send time was found on
datetime.now().astimezone(), which carries the UTC offset of the moment it is
read, fixed. The next morning's send time, past a DST change, was placed in
that same offset, so on the nights the clocks change the report went out an
hour early (autumn) or an hour late (spring).

CI runs in UTC, and zoneinfo has no tz database on Windows, so the test brings
its own zone: Central European time with the EU's 2026 change days. The
scheduler reads the local clock through emailer's `datetime`, which the test
replaces with one that keeps local time in that zone, as a server there would.
"""
import time
import unittest
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from unittest import mock

from nwdash import emailer, models
from nwdash.models import AlertAutomation

HOUR = 3600


class _CentralEurope(tzinfo):
    """UTC+2 from 29 March 01:00 UTC to 25 October 01:00 UTC 2026, else UTC+1."""

    SUMMER_UTC = (datetime(2026, 3, 29, 1, 0), datetime(2026, 10, 25, 1, 0))
    # The same moments on the wall clock: 02:00 jumps to 03:00; 03:00 falls back to 02:00.
    SUMMER_WALL = (datetime(2026, 3, 29, 2, 0), datetime(2026, 10, 25, 3, 0))

    def _summer(self, dt):
        wall = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        start, end = self.SUMMER_WALL
        if dt.fold and end - timedelta(hours=1) <= wall < end:
            return False  # the second pass through the repeated hour
        return start <= wall < end

    def utcoffset(self, dt):
        return timedelta(hours=2 if self._summer(dt) else 1)

    def dst(self, dt):
        return timedelta(hours=1 if self._summer(dt) else 0)

    def tzname(self, dt):
        return "CEST" if self._summer(dt) else "CET"

    def fromutc(self, dt):
        utc = dt.replace(tzinfo=None)
        start, end = self.SUMMER_UTC
        wall = utc + timedelta(hours=2 if start <= utc < end else 1)
        return wall.replace(tzinfo=self, fold=1 if end <= utc < end + timedelta(hours=1) else 0)


EUROPE = _CentralEurope()


class _HostClock(datetime):
    """emailer's datetime on a host whose local zone is EUROPE: now() is the
    naive local wall clock, and .timestamp() and .astimezone() read a naive
    value in that zone, as the system's own local time does."""

    utc_now = datetime(2026, 10, 20, 6, 0)

    @classmethod
    def now(cls, tz=None):
        local = cls.utc_now.replace(tzinfo=UTC).astimezone(EUROPE)
        if tz is not None:
            return local.astimezone(tz)
        return cls(local.year, local.month, local.day, local.hour, local.minute, local.second, fold=local.fold)

    def _in_zone(self):
        return datetime(
            self.year, self.month, self.day, self.hour, self.minute, self.second, self.microsecond,
            tzinfo=EUROPE, fold=self.fold,
        )

    def timestamp(self):
        return self._in_zone().timestamp() if self.tzinfo is None else super().timestamp()

    def astimezone(self, tz=None):
        if self.tzinfo is not None:
            return super().astimezone(tz)
        local = self._in_zone()
        if tz is not None:
            return local.astimezone(tz)
        return local.astimezone(timezone(local.utcoffset(), local.tzname()))  # this moment's offset, fixed


class TestTheSendTimeIsLocalOnChangeDays(unittest.TestCase):
    def setUp(self):
        self.automation = AlertAutomation(
            automation_id="c23-dst:daily", session_id="d" * 32,
            smtp_host="smtp.example.com", smtp_port=25, smtp_username="", encrypted_smtp_password="",
            smtp_from="dashboard@example.com", recipients=["dst@example.com"],
            smtp_security="none", interval_minutes=60, trigger="critical",
            schedule_type="daily_report", report_time="08:00", created_at=1.0,
        )
        patchers = [
            mock.patch.object(emailer, "datetime", _HostClock),
            mock.patch.dict(models.ALERT_AUTOMATIONS, {self.automation.automation_id: self.automation}, clear=True),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def next_run_from(self, utc_now):
        """Schedule at utc_now; return the hours until the next run."""
        _HostClock.utc_now = utc_now
        emailer.schedule_alert_automation(self.automation)
        return (self.automation.next_run_at - time.time()) / HOUR

    def test_on_an_ordinary_day_it_is_24_hours_away(self):
        self.assertAlmostEqual(self.next_run_from(datetime(2026, 10, 20, 6, 0)), 24, delta=0.01)  # 08:00 CEST

    def test_the_night_the_clocks_go_back_it_is_25_hours_away(self):
        # 08:00 CEST on 24 October; the next 08:00 is CET.
        self.assertAlmostEqual(self.next_run_from(datetime(2026, 10, 24, 6, 0)), 25, delta=0.01)

    def test_the_night_the_clocks_go_forward_it_is_23_hours_away(self):
        # 08:00 CET on 28 March; the next 08:00 is CEST.
        self.assertAlmostEqual(self.next_run_from(datetime(2026, 3, 28, 7, 0)), 23, delta=0.01)

    def test_a_change_before_todays_send_time_is_counted(self):
        # 02:00 CEST on 25 October, an hour before the clocks go back: 08:00 CET is seven hours away.
        self.assertAlmostEqual(self.next_run_from(datetime(2026, 10, 25, 0, 0)), 7, delta=0.01)

    def test_seconds_until_daily_report_agrees(self):
        _HostClock.utc_now = datetime(2026, 10, 24, 6, 0)
        self.assertAlmostEqual(emailer.seconds_until_daily_report("08:00"), 25 * HOUR, delta=1)


if __name__ == "__main__":
    unittest.main()
