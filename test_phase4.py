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


class MultiScheduleTests(unittest.TestCase):
    def setUp(self):
        import networker_dashboard as _nd
        self.nd = _nd
        self._orig_session_exists = _nd._session_exists
        self._orig_persist = _nd.persist_automations
        self._orig_schedule = _nd.schedule_alert_automation
        _nd._session_exists = lambda sid: sid == "S1"
        _nd.persist_automations = lambda: None
        _nd.schedule_alert_automation = lambda automation: None
        with _nd.REGISTRY_LOCK:
            _nd.ALERT_AUTOMATIONS.clear()

    def tearDown(self):
        self.nd._session_exists = self._orig_session_exists
        self.nd.persist_automations = self._orig_persist
        self.nd.schedule_alert_automation = self._orig_schedule
        with self.nd.REGISTRY_LOCK:
            self.nd.ALERT_AUTOMATIONS.clear()

    def _start(self, recipients, automation_id=""):
        return self.nd.handle_alert_automation({
            "action": "start",
            "sessionId": "S1",
            "automationId": automation_id,
            "smtpHost": "smtp.example.com",
            "smtpPort": 25,
            "smtpSecurity": "none",
            "smtpFrom": "from@example.com",
            "smtpTo": recipients,
            "intervalMinutes": 30,
            "trigger": "all",
            "scheduleType": "daily_report",
            "reportTime": "08:00",
            "theme": "default",
        })

    def test_list_empty(self):
        status, body = self.nd.handle_alert_automation({"action": "list", "sessionId": "S1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["schedules"], [])

    def test_start_twice_makes_two_rows(self):
        s1, b1 = self._start("a@x.com")
        s2, b2 = self._start("b@x.com")
        self.assertEqual(s1, 200); self.assertEqual(s2, 200)
        self.assertNotEqual(b1["automationId"], b2["automationId"])
        _, listed = self.nd.handle_alert_automation({"action": "list", "sessionId": "S1"})
        self.assertEqual(len(listed["schedules"]), 2)

    def test_start_with_existing_id_updates_in_place(self):
        _, b1 = self._start("a@x.com")
        first_id = b1["automationId"]
        _, b2 = self._start("a-updated@x.com", automation_id=first_id)
        self.assertEqual(b2["automationId"], first_id)
        _, listed = self.nd.handle_alert_automation({"action": "list", "sessionId": "S1"})
        self.assertEqual(len(listed["schedules"]), 1)
        self.assertEqual(listed["schedules"][0]["recipients"], "a-updated@x.com")

    def test_stop_by_id_removes_only_that_row(self):
        _, b1 = self._start("a@x.com")
        _, b2 = self._start("b@x.com")
        self.nd.handle_alert_automation({"action": "stop", "sessionId": "S1", "automationId": b1["automationId"]})
        _, listed = self.nd.handle_alert_automation({"action": "list", "sessionId": "S1"})
        ids = [s["automationId"] for s in listed["schedules"]]
        self.assertEqual(ids, [b2["automationId"]])


if __name__ == "__main__":
    unittest.main()
