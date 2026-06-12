import unittest

import networker_dashboard as nd


class DashboardSignatureTests(unittest.TestCase):
    def _make(self, *, total=10, fail=1, jobs=None, alerts=None, generated="2026-06-08T10:00:00Z"):
        return {
            "generatedAt": generated,
            "summary": {
                "totalJobs": total, "successfulJobs": total - fail, "failedJobs": fail,
                "activeJobs": 0, "recoveryJobs": 0, "cloneJobs": 0,
                "totalAlerts": len(alerts or []), "slaPercent": 95,
                "health": "ok", "range": "24h",
            },
            "tables": {
                "jobs": jobs or [{"name": "j1", "client": "c1", "status": "Succeeded"}],
                "failedJobs": [{"name": "fj", "client": "c1", "status": "Failed"}] if fail else [],
                "alerts": alerts or [],
            },
        }

    def test_same_content_same_signature(self):
        a = self._make()
        b = self._make()
        self.assertEqual(nd._dashboard_content_signature(a), nd._dashboard_content_signature(b))

    def test_timestamp_change_does_not_change_signature(self):
        a = self._make(generated="2026-06-08T10:00:00Z")
        b = self._make(generated="2026-06-08T11:00:00Z")
        self.assertEqual(nd._dashboard_content_signature(a), nd._dashboard_content_signature(b))

    def test_job_order_does_not_change_signature(self):
        a = self._make(jobs=[
            {"name": "j1", "client": "c1", "status": "Succeeded"},
            {"name": "j2", "client": "c2", "status": "Succeeded"},
        ])
        b = self._make(jobs=[
            {"name": "j2", "client": "c2", "status": "Succeeded"},
            {"name": "j1", "client": "c1", "status": "Succeeded"},
        ])
        self.assertEqual(nd._dashboard_content_signature(a), nd._dashboard_content_signature(b))

    def test_new_failed_job_changes_signature(self):
        a = self._make()
        b = self._make(fail=2)
        b["tables"]["failedJobs"] = [
            {"name": "fj", "client": "c1", "status": "Failed"},
            {"name": "fj2", "client": "c2", "status": "Failed"},
        ]
        self.assertNotEqual(nd._dashboard_content_signature(a), nd._dashboard_content_signature(b))


if __name__ == "__main__":
    unittest.main()
