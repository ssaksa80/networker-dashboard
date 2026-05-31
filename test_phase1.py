import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import networker_dashboard as nd


class RegistryLockTests(unittest.TestCase):
    def test_concurrent_session_access_no_crash(self):
        # Regression for C1: dict changed size during iteration.
        nd.DASHBOARD_SESSIONS.clear()
        stop = threading.Event()
        errors = []

        def writer(seed):
            i = 0
            while not stop.is_set():
                sid = f"s{seed}-{i % 50}"
                nd._put_session(sid, object())
                nd._pop_session(sid)
                i += 1

        def reader():
            try:
                while not stop.is_set():
                    for _sid, _sess in nd._session_items_snapshot():
                        pass
                    for _key, _auto in nd._automation_items_snapshot():
                        pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(2.0)
        nd.DASHBOARD_SESSIONS.clear()
        self.assertEqual(errors, [])


class LoopGuardTests(unittest.TestCase):
    def test_refresh_loop_survives_iteration_exception(self):
        # Regression for C2: one bad iteration must not kill the loop thread.
        calls = []
        orig_once = nd._shared_dashboard_refresh_once
        orig_interval = nd.SHARED_REFRESH_SECONDS

        def boom():
            calls.append(1)
            raise RuntimeError("boom")

        nd._shared_dashboard_refresh_once = boom
        nd.SHARED_REFRESH_SECONDS = 0.02
        nd.SHARED_REFRESH_STOP.clear()
        thread = threading.Thread(target=nd.shared_dashboard_refresh_loop, daemon=True)
        thread.start()
        try:
            time.sleep(0.3)
            self.assertGreaterEqual(len(calls), 2)  # survived >=2 exceptions
            self.assertTrue(thread.is_alive())
        finally:
            nd.SHARED_REFRESH_STOP.set()
            thread.join(2.0)
            nd._shared_dashboard_refresh_once = orig_once
            nd.SHARED_REFRESH_SECONDS = orig_interval
            nd.SHARED_REFRESH_STOP.clear()


class SseTests(unittest.TestCase):
    def test_sse_register_respects_cap(self):
        nd.SSE_CLIENTS.clear()
        orig_cap = nd.MAX_SSE_CLIENTS
        nd.MAX_SSE_CLIENTS = 2
        try:
            self.assertTrue(nd._sse_register(object()))
            self.assertTrue(nd._sse_register(object()))
            self.assertFalse(nd._sse_register(object()))
            self.assertEqual(len(nd.SSE_CLIENTS), 2)
        finally:
            nd.SSE_CLIENTS.clear()
            nd.MAX_SSE_CLIENTS = orig_cap

    def test_broadcast_prunes_dead_clients(self):
        nd.SSE_CLIENTS.clear()

        class DeadFile:
            def write(self, _data):
                raise OSError("broken pipe")

            def flush(self):
                pass

        class LiveFile:
            def __init__(self):
                self.written = b""

            def write(self, data):
                self.written += data

            def flush(self):
                pass

        live = LiveFile()
        nd.SSE_CLIENTS.extend([DeadFile(), live])
        nd.sse_broadcast("dashboard", "{}")
        remaining_dead = [c for c in nd.SSE_CLIENTS if isinstance(c, DeadFile)]
        self.assertEqual(remaining_dead, [])
        self.assertIn(live, nd.SSE_CLIENTS)
        self.assertTrue(live.written)
        nd.SSE_CLIENTS.clear()


class ConnectionCapTests(unittest.TestCase):
    def test_connection_slot_cap(self):
        srv = nd.ExclusiveThreadingHTTPServer(
            ("127.0.0.1", 0), nd.DashboardHandler, max_connections=2
        )
        try:
            self.assertTrue(srv._acquire_slot())
            self.assertTrue(srv._acquire_slot())
            self.assertFalse(srv._acquire_slot())
            srv._release_slot()
            self.assertTrue(srv._acquire_slot())
        finally:
            srv.server_close()

    def test_config_constants_exist(self):
        self.assertIsInstance(nd.DEFAULT_REQUEST_TIMEOUT_SECONDS, int)
        self.assertIsInstance(nd.DEFAULT_MAX_CONNECTIONS, int)


class SnapshotWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_data_dir = nd.DATA_DIR
        self._orig_snap = nd.DASHBOARD_SNAPSHOT_FILE
        nd.DATA_DIR = Path(self._tmpdir)
        nd.DASHBOARD_SNAPSHOT_FILE = nd.DATA_DIR / "networker_snapshots.json"

    def tearDown(self):
        nd.DATA_DIR = self._orig_data_dir
        nd.DASHBOARD_SNAPSHOT_FILE = self._orig_snap
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_snapshot_write_is_atomic_and_leaves_no_tmp(self):
        data = {"2026-05-22": {"date": "2026-05-22", "metrics": {}}}
        nd.write_dashboard_snapshots(data)
        self.assertTrue(nd.DASHBOARD_SNAPSHOT_FILE.exists())
        tmp = nd.DASHBOARD_SNAPSHOT_FILE.with_suffix(".tmp")
        self.assertFalse(tmp.exists())
        self.assertEqual(nd.load_dashboard_snapshots(), data)


class StaleDashboardNoticeTests(unittest.TestCase):
    def test_stale_dashboard_uses_info_diagnostic_not_source_warning(self):
        cached = {
            "ok": True,
            "summary": {"health": "ok", "totalJobs": 10},
            "sources": {"monitoringActions": {"ok": True, "path": "/nwui/api/monitoringactions"}},
        }
        refresh_body = {
            "ok": True,
            "sources": {
                "monitoringActions": {
                    "ok": False,
                    "path": "/nwui/api/monitoringactions",
                    "status": 500,
                    "error": "temporary upstream failure",
                }
            },
        }
        original = nd.cached_reliable_dashboard_for_session
        nd.cached_reliable_dashboard_for_session = lambda session_id: cached
        try:
            stale = nd.stale_dashboard_from_cache("session-1", refresh_body)
        finally:
            nd.cached_reliable_dashboard_for_session = original

        self.assertIsNotNone(stale)
        self.assertTrue(stale["stale"])
        self.assertIn("last successful dashboard snapshot", stale["reportNotice"])
        live_refresh = stale["sources"]["liveRefresh"]
        self.assertEqual(live_refresh["path"], "live-refresh")
        self.assertEqual(live_refresh["severity"], "info")
        self.assertFalse(live_refresh["displayWarning"])
        self.assertTrue(live_refresh["diagnosticOnly"])
        self.assertNotIn("last-successful-dashboard", str(stale))

    def test_frontend_has_separate_stale_notice_path(self):
        self.assertIn("function sourceNeedsVisibleWarning", nd.HTML_PAGE)
        self.assertIn("data.stale && data.reportNotice", nd.HTML_PAGE)
        self.assertIn('setStatus("Using cached dashboard", "warn")', nd.HTML_PAGE)
        self.assertNotIn("last-successful-dashboard", nd.HTML_PAGE)


class AccountMenuContrastTests(unittest.TestCase):
    def test_account_menu_buttons_use_ink_color_not_white(self):
        """Account dropdown buttons must override the white header-button color
        so they are visible on the white dropdown surface."""
        self.assertIn(".account-menu .topbar-button", nd.HTML_PAGE)
        self.assertIn("color: var(--ink)", nd.HTML_PAGE)
        # The dropdown buttons must NOT keep the header's white-on-dark styling
        self.assertIn(".account-menu .topbar-button.danger", nd.HTML_PAGE)


class ActionHistoryMergeTests(unittest.TestCase):
    def test_dedup_key_normalizes_time_formats(self):
        iso = {"workflowName": "WF", "actionName": "backup", "startTime": "2026-05-21T17:00:02.000Z"}
        epoch_ms = {"workflowName": "WF", "actionName": "backup", "startTime": 1779382802000}
        self.assertEqual(nd.action_dedup_key(iso), nd.action_dedup_key(epoch_ms))

    def test_merge_adds_completed_history(self):
        live = [{"workflowName": "WF1", "actionName": "backup", "startTime": "2026-05-31T04:00:00Z", "status": "Running"}]
        history = [
            {"workflowName": "WF1", "actionName": "backup", "startTime": "2026-05-30T04:00:00Z", "status": "Succeeded"},
            {"workflowName": "WF2", "actionName": "backup", "startTime": "2026-05-30T05:00:00Z", "status": "Failed"},
        ]
        merged = nd.merge_action_history(live, history)
        self.assertEqual(len(merged), 3)
        statuses = sorted(nd.normalize_nwui_status(m["status"]) for m in merged)
        self.assertEqual(statuses, ["failed", "running", "succeeded"])

    def test_merge_prefers_terminal_over_running_on_collision(self):
        key = {"workflowName": "WF", "actionName": "backup", "startTime": "2026-05-31T04:00:00Z"}
        live = [{**key, "status": "Running"}]
        history = [{**key, "status": "Succeeded"}]
        merged = nd.merge_action_history(live, history)
        self.assertEqual(len(merged), 1)
        self.assertEqual(nd.normalize_nwui_status(merged[0]["status"]), "succeeded")

    def test_merge_keeps_unkeyable_extras(self):
        live = [{"status": "Running"}]   # no workflow/action/time -> extra
        merged = nd.merge_action_history(live, [])
        self.assertEqual(len(merged), 1)


class ActiveJobsSlaTests(unittest.TestCase):
    def test_nwui_totalJobs_includes_active_jobs(self):
        """NWUI path: totalJobs must count running jobs, not just completed."""
        activity = nd.nwui_backup_activity_counts([
            {"status": "running"},
            {"status": "running"},
        ])
        self.assertEqual(activity["completed"], 0)
        self.assertEqual(activity["active"], 2)
        # Simulate the summary build
        summary = nd.add_sla_summary({
            "totalJobs": activity["completed"] + activity["active"],
            "completedJobs": activity["completed"],
            "successfulJobs": activity["successful"],
            "failedJobs": activity["failed"],
            "activeJobs": activity["active"],
        })
        self.assertEqual(summary["totalJobs"], 2)
        self.assertEqual(summary["slaTotalJobs"], 0)   # SLA only counts finished
        self.assertEqual(summary["activeJobs"], 2)

    def test_frontend_sla_pie_shows_running_message_not_no_jobs(self):
        """When slaTotalJobs=0 but activeJobs>0 the SLA pie must NOT say 'No backup jobs ran'."""
        self.assertIn("currently running", nd.HTML_PAGE)
        self.assertIn("SLA pending", nd.HTML_PAGE)

    def test_add_sla_summary_with_only_running_jobs(self):
        summary = nd.add_sla_summary({
            "successfulJobs": 0,
            "failedJobs": 0,
            "activeJobs": 2,
        })
        self.assertEqual(summary["slaTotalJobs"], 0)
        self.assertEqual(summary["slaPercent"], 0)

    def test_add_sla_summary_mixed(self):
        summary = nd.add_sla_summary({
            "successfulJobs": 5,
            "failedJobs": 2,
            "activeJobs": 3,
        })
        self.assertEqual(summary["slaTotalJobs"], 7)   # only finished count for SLA
        self.assertAlmostEqual(summary["slaPercent"], 71.43, places=1)


if __name__ == "__main__":
    unittest.main()
