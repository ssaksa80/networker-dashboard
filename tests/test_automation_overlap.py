"""A schedule's run is never started again while it is still running.

The defect (review C20): the scheduler tick pushed next_run_at one interval
forward and started the run on a thread. A build slower than the interval (a
large jobs database, a NetWorker timeout) was still going when the next tick
found the schedule due again, so a second run of the same schedule started
beside the first: two emails for one alert, and two runs racing on its
signature and next run.
"""
import threading
import time
import unittest
from unittest import mock

from nwdash import emailer, models
from nwdash.models import AlertAutomation


def _automation(automation_id):
    return AlertAutomation(
        automation_id=automation_id, session_id="e" * 32,
        smtp_host="smtp.example.com", smtp_port=25, smtp_username="", encrypted_smtp_password="",
        smtp_from="dashboard@example.com", recipients=[f"{automation_id.split(':')[1]}@example.com"],
        smtp_security="none", interval_minutes=1, trigger="all",
        schedule_type="alert", report_time="08:00", created_at=1.0,
        next_run_at=time.time() - 1,
    )


class TestOneRunPerSchedule(unittest.TestCase):
    def setUp(self):
        self.started = []
        self.started_event = threading.Condition()
        self.finish = threading.Event()
        self.addCleanup(self.finish.set)

        def _slow_run(automation_id):
            with self.started_event:
                self.started.append(automation_id)
                self.started_event.notify_all()
            self.finish.wait(10)

        patchers = [
            mock.patch.dict(models.ALERT_AUTOMATIONS, clear=True),
            mock.patch.object(emailer, "run_alert_automation", _slow_run),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.wait_for_runs_to_end)

    def arm(self, automation_id):
        automation = _automation(automation_id)
        models.ALERT_AUTOMATIONS[automation_id] = automation
        return automation

    def wait_for_starts(self, count, timeout=5.0):
        with self.started_event:
            return self.started_event.wait_for(lambda: len(self.started) >= count, timeout)

    def wait_for_runs_to_end(self):
        self.finish.set()
        deadline = time.time() + 5
        while getattr(emailer, "_RUNS_IN_FLIGHT", None) and time.time() < deadline:
            time.sleep(0.01)

    def test_a_slow_run_is_not_started_again(self):
        automation = self.arm("c20:slow")
        emailer.automation_scheduler_tick()
        self.assertTrue(self.wait_for_starts(1))
        automation.next_run_at = time.time() - 1  # the interval passes while it runs
        emailer.automation_scheduler_tick()
        self.assertFalse(self.wait_for_starts(2, timeout=0.5), self.started)

    def test_once_it_ends_the_next_tick_starts_it(self):
        automation = self.arm("c20:again")
        emailer.automation_scheduler_tick()
        self.assertTrue(self.wait_for_starts(1))
        self.wait_for_runs_to_end()
        self.finish.clear()
        automation.next_run_at = time.time() - 1
        emailer.automation_scheduler_tick()
        self.assertTrue(self.wait_for_starts(2))

    def test_another_schedule_is_not_held_up(self):
        self.arm("c20:slow")
        emailer.automation_scheduler_tick()
        self.assertTrue(self.wait_for_starts(1))
        self.arm("c20:other")
        emailer.automation_scheduler_tick()
        self.assertTrue(self.wait_for_starts(2))
        self.assertEqual(sorted(self.started), ["c20:other", "c20:slow"])

    def test_a_run_that_cannot_start_does_not_block_the_schedule(self):
        automation = self.arm("c20:nothread")

        class _NoThread:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        with mock.patch.object(emailer.threading, "Thread", _NoThread), self.assertRaises(RuntimeError):
            emailer.automation_scheduler_tick()
        automation.next_run_at = time.time() - 1
        emailer.automation_scheduler_tick()
        self.assertTrue(self.wait_for_starts(1))


if __name__ == "__main__":
    unittest.main()
