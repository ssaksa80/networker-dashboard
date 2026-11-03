"""One job status classifier for the REST and NWUI collectors (review C5).

The two collectors used to read NetWorker's status text with their own word
lists. A cancelled or abandoned job was a failure in NWUI mode and nothing at
all in REST mode, "never started" counted as running in REST mode, and REST
mode never asked for a job's `state`, so a running job whose completion
status still read "Unknown" was not counted as active. The SLA figures of the
two modes could not be compared.

Every job now gets one of OUTCOMES:

- succeeded, failed: finished, and counted by the SLA. A cancelled or
  abandoned job did not protect its data, so it is a failure.
- warning: finished with something to look at (missed, skipped, interrupted).
- running, queued: not finished.
- unknown: nothing to go on.
"""
from __future__ import annotations

from typing import Any

SUCCEEDED = "succeeded"
FAILED = "failed"
WARNING = "warning"
RUNNING = "running"
QUEUED = "queued"
UNKNOWN = "unknown"

OUTCOMES = (SUCCEEDED, FAILED, WARNING, RUNNING, QUEUED, UNKNOWN)
FINISHED = (SUCCEEDED, FAILED, WARNING)
ACTIVE = (RUNNING, QUEUED)

_EXACT = {
    "succeeded": SUCCEEDED,
    "completed": SUCCEEDED,
    "complete": SUCCEEDED,
    "success": SUCCEEDED,
    "successful": SUCCEEDED,
    "ok": SUCCEEDED,
    "done": SUCCEEDED,
    "finished": SUCCEEDED,
    "failed": FAILED,
    "failure": FAILED,
    "error": FAILED,
    "critical": FAILED,
    "aborted": FAILED,
    "abandoned": FAILED,
    "cancelled": FAILED,
    "canceled": FAILED,
    "terminated": FAILED,
    "warning": WARNING,
    "warnings": WARNING,
    "incomplete": WARNING,
    "interrupted": WARNING,
    "missed": WARNING,
    "missedtheschedule": WARNING,
    "missed_the_schedule": WARNING,
    "skipped": WARNING,
    "never_started": WARNING,
    "notstarted": WARNING,
    "not_started": WARNING,
    "running": RUNNING,
    "in_progress": RUNNING,
    "active": RUNNING,
    "sessionactive": RUNNING,
    "session_active": RUNNING,
    "started": RUNNING,
    "queued": QUEUED,
    "waiting": QUEUED,
    "pending": QUEUED,
    "scheduled": QUEUED,
    "unknown": UNKNOWN,
}

# Checked in order when the text is not an exact match: "succeeded with
# errors" is a success, and "never started" a warning, not a running job.
_CONTAINS = (
    (SUCCEEDED, ("succ", "complet")),
    (FAILED, ("fail", "error", "abort", "abandon", "cancel", "critical")),
    (WARNING, ("warn", "miss", "skip", "interrupt", "never", "not_start")),
    (RUNNING, ("run", "progress", "active", "start")),
    (QUEUED, ("wait", "pend", "queue")),
)


def classify_status(value: Any) -> str:
    """The outcome one status word stands for."""
    raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not raw:
        return UNKNOWN
    if raw in _EXACT:
        return _EXACT[raw]
    for outcome, fragments in _CONTAINS:
        if any(fragment in raw for fragment in fragments):
            return outcome
    return UNKNOWN


def job_outcome(completion: Any, state: Any = "") -> str:
    """The outcome of a job from its completion status and, when that says
    nothing, its state: a NetWorker job that is still running has state
    "Active" or "Queued" and no completion status yet. A finished state
    ("Completed") says nothing about how the job went, so it stays unknown."""
    outcome = classify_status(completion)
    if outcome != UNKNOWN:
        return outcome
    from_state = classify_status(state)
    return from_state if from_state in ACTIVE else UNKNOWN
