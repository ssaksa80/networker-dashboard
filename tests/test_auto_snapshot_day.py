"""An automatic snapshot holds one whole calendar day, built fresh, under that
day's date, and never overwrites a snapshot saved meanwhile.

The defect (review C18): the auto-snapshot copied the shared dashboard, which
covers whatever range its viewer picked (a 30-day dashboard one day, 24 hours
the next) and can be hours old when refreshes fail, and dated it by the moment
of the save, so just after midnight yesterday's jobs went in under today. It
checked for an existing snapshot, let go of the lock, then saved, so a
snapshot saved in between was overwritten, annotation and all.
"""
import json
import tempfile
import time
import unittest
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from nwdash import models, snapshots
from nwdash.profiles import SNAPSHOTS_LOCK

SESSION = "a" * 32
YESTERDAY = "2026-11-02"


def _dashboard(range_key, label, **extra):
    return {
        "ok": True,
        "generatedAt": "03-11-2026 00:05:00 UTC",
        "summary": {"range": range_key, "rangeLabel": label, "totalJobs": 12, "failedJobs": 1, "health": "warning"},
        "sources": {"jobs": {"ok": True, "path": "/jobs"}},
        **extra,
    }


class _FrozenClock(datetime):
    """snapshots' datetime, five minutes into 3 November."""

    @classmethod
    def now(cls, tz=None):
        current = datetime(2026, 11, 3, 0, 5)
        return current if tz is None else current.astimezone(tz)


class TestAutoSnapshotDay(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-c18-")
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        self.snapshot_file = data_dir / "networker_snapshots.json"
        auto_file = data_dir / "auto_snapshot_config.json"
        auto_file.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        self.builds = []
        self.build_result = (HTTPStatus.OK, _dashboard("custom", "02-11-2026 to 02-11-2026"))
        self.during_build = None

        def _build(*args, **kwargs):
            self.builds.append((args, kwargs))
            if self.during_build:
                self.during_build()
            return self.build_result

        # The shared view sits on a month-long range, refreshed six hours ago.
        shared = {
            "sessionId": SESSION,
            "dashboard": _dashboard("30d", "Last Month"),
            "updatedAt": time.time() - 6 * 3600,
        }
        patchers = [
            mock.patch.object(snapshots, "DATA_DIR", data_dir),
            mock.patch.object(snapshots, "DASHBOARD_SNAPSHOT_FILE", self.snapshot_file),
            mock.patch.object(snapshots, "AUTO_SNAPSHOT_FILE", auto_file),
            mock.patch.object(snapshots, "datetime", _FrozenClock),
            mock.patch.object(snapshots, "build_dashboard_from_session", _build, create=True),
            mock.patch.dict(models.SHARED_DASHBOARD_STATE, shared),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def stored(self):
        if not self.snapshot_file.exists():
            return {}
        return json.loads(self.snapshot_file.read_text(encoding="utf-8"))

    def test_it_stores_yesterday_midnight_to_midnight_under_its_date(self):
        self.assertEqual(snapshots._auto_snapshot_once(), "saved")
        self.assertEqual(
            self.builds,
            [((SESSION, "custom", "02-11-2026", "02-11-2026"), {"remember_range": False})],
        )
        stored = self.stored()
        self.assertEqual(list(stored), [YESTERDAY])
        self.assertEqual(stored[YESTERDAY]["range"], "custom")
        self.assertEqual(stored[YESTERDAY]["rangeLabel"], "02-11-2026 to 02-11-2026")

    def test_a_failed_build_saves_nothing_rather_than_the_old_shared_view(self):
        self.build_result = (HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "NetWorker did not answer"})
        self.assertEqual(snapshots._auto_snapshot_once(), "no-dashboard")
        self.assertEqual(self.stored(), {})

    def test_a_build_that_raises_saves_nothing(self):
        def _raise():
            raise OSError("connection reset")

        self.during_build = _raise
        self.assertEqual(snapshots._auto_snapshot_once(), "no-dashboard")
        self.assertEqual(self.stored(), {})

    def test_a_stale_fallback_is_not_saved(self):
        self.build_result = (HTTPStatus.OK, _dashboard("custom", "02-11-2026 to 02-11-2026", stale=True))
        self.assertEqual(snapshots._auto_snapshot_once(), "no-dashboard")
        self.assertEqual(self.stored(), {})

    def test_a_build_without_backup_job_data_is_not_saved(self):
        dashboard = _dashboard("custom", "02-11-2026 to 02-11-2026")
        dashboard["sources"] = {"jobs": {"ok": False, "path": "/jobs", "error": "gateway timeout"}}
        self.build_result = (HTTPStatus.OK, dashboard)
        self.assertEqual(snapshots._auto_snapshot_once(), "no-dashboard")
        self.assertEqual(self.stored(), {})

    def test_no_shared_session_saves_nothing(self):
        models.SHARED_DASHBOARD_STATE["sessionId"] = ""
        self.assertEqual(snapshots._auto_snapshot_once(), "no-dashboard")
        self.assertEqual(self.stored(), {})

    def test_a_day_already_stored_is_not_built_again(self):
        snapshots.write_dashboard_snapshots({YESTERDAY: {"date": YESTERDAY, "annotation": "by hand"}})
        self.assertEqual(snapshots._auto_snapshot_once(), "exists")
        self.assertEqual(self.builds, [])

    def test_a_snapshot_saved_during_the_build_is_kept(self):
        def _saved_by_hand():
            with SNAPSHOTS_LOCK:
                snapshots.write_dashboard_snapshots({YESTERDAY: {"date": YESTERDAY, "annotation": "by hand"}})

        self.during_build = _saved_by_hand
        self.assertEqual(snapshots._auto_snapshot_once(), "exists")
        self.assertEqual(self.stored()[YESTERDAY].get("annotation"), "by hand")

    def test_off_means_off(self):
        snapshots.save_auto_snapshot_config(False)
        self.assertEqual(snapshots._auto_snapshot_once(), "disabled")
        self.assertEqual(self.builds, [])


if __name__ == "__main__":
    unittest.main()
