"""automations.json is saved one save at a time, a pause is saved, and a file
that cannot be read says so.

The defect (review C19): persist_automations took no lock. Each save writes the
registry as it stood when that save began, so a slow save could land after a
newer one and bring back a schedule the operator had just stopped. Pausing a
schedule changed it in memory only, so the next restart resumed it. An
unreadable automations.json restored nothing, without a word. (The shared
temporary path and the silent write failures went with review O5's
fileio.atomic_write_text, which this file goes through.)
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from nwdash import emailer, models, snapshots
from nwdash.config import LOG
from nwdash.models import AlertAutomation


def _automation(automation_id):
    return AlertAutomation(
        automation_id=automation_id, session_id="b" * 32,
        smtp_host="smtp.example.com", smtp_port=25, smtp_username="", encrypted_smtp_password="",
        smtp_from="dashboard@example.com", recipients=[f"{automation_id.split(':')[1]}@example.com"],
        smtp_security="none", interval_minutes=60, trigger="critical",
        schedule_type="alert", report_time="08:00", created_at=1.0, profile_name="c19",
    )


class _AutomationsFileCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-c19-")
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        self.automations_file = data_dir / "automations.json"
        patchers = [
            mock.patch.object(emailer, "DATA_DIR", data_dir),
            mock.patch.object(emailer, "AUTOMATIONS_FILE", self.automations_file),
            mock.patch.object(emailer, "WMI_CIPHER", object()),  # persistence is off without a key
            mock.patch.object(snapshots, "EMAIL_CONFIG_FILE", data_dir / "email_config.json"),
            mock.patch.dict(models.ALERT_AUTOMATIONS, clear=True),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def arm(self, automation_id):
        automation = _automation(automation_id)
        models.ALERT_AUTOMATIONS[automation_id] = automation
        return automation

    def saved(self):
        return json.loads(self.automations_file.read_text(encoding="utf-8"))


class TestOneSaveAtATime(_AutomationsFileCase):
    def test_an_older_save_never_lands_after_a_newer_one(self):
        self.arm("c19:kept")
        self.arm("c19:stopped")
        real_write = emailer.atomic_write_text
        first_is_writing = threading.Event()
        let_first_finish = threading.Event()
        self.addCleanup(let_first_finish.set)
        calls = []

        def _slow_first_write(path, text):
            calls.append(text)
            if len(calls) == 1:
                first_is_writing.set()
                let_first_finish.wait(10)
            real_write(path, text)

        with mock.patch.object(emailer, "atomic_write_text", _slow_first_write):
            # A run's save (both schedules) is held up writing...
            first = threading.Thread(target=emailer.persist_automations)
            first.start()
            self.assertTrue(first_is_writing.wait(10))
            # ...while the operator stops one, and that save goes through.
            emailer.cancel_alert_automation("c19:stopped")
            second = threading.Thread(target=emailer.persist_automations)
            second.start()
            second.join(1)
            let_first_finish.set()
            first.join(10)
            second.join(10)
        self.assertEqual(list(self.saved()), ["c19:kept"])


class TestAPauseIsSaved(_AutomationsFileCase):
    def set_enabled(self, automation_id, enabled):
        status, body = emailer.handle_alert_automation(
            {"action": "set_enabled", "automationId": automation_id, "enabled": enabled}
        )
        self.assertEqual(status, 200, body)

    def test_pausing_and_resuming_are_saved(self):
        self.arm("c19:paused")
        emailer.persist_automations()
        self.set_enabled("c19:paused", False)
        self.assertIs(self.saved()["c19:paused"]["enabled"], False)
        self.set_enabled("c19:paused", True)
        self.assertIs(self.saved()["c19:paused"]["enabled"], True)

    def test_a_paused_schedule_is_still_paused_after_a_restart(self):
        self.arm("c19:paused")
        emailer.persist_automations()
        self.set_enabled("c19:paused", False)
        models.ALERT_AUTOMATIONS.clear()
        self.assertEqual(emailer.restore_automations_from_disk(), 1)
        self.assertFalse(models.ALERT_AUTOMATIONS["c19:paused"].enabled)


class TestFailuresAreLogged(_AutomationsFileCase):
    def test_an_unreadable_file_is_logged(self):
        self.automations_file.write_text("{not json", encoding="utf-8")
        with self.assertLogs(LOG, "WARNING") as logs:
            self.assertEqual(emailer.restore_automations_from_disk(), 0)
        self.assertIn("could not read automations.json", "\n".join(logs.output))

    def test_a_save_that_cannot_be_encoded_is_logged(self):
        self.arm("c19:odd").connection = {"opaque": object()}  # json cannot encode it
        with self.assertLogs(LOG, "WARNING") as logs:
            emailer.persist_automations()
        self.assertIn("could not save automations.json", "\n".join(logs.output))
        self.assertFalse(self.automations_file.exists())


if __name__ == "__main__":
    unittest.main()
