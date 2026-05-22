import threading
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
