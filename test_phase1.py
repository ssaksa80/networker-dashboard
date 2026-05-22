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


if __name__ == "__main__":
    unittest.main()
