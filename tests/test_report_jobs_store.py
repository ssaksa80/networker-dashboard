import importlib
from nwdash import config, report_jobs


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_JOBS_FILE", tmp_path / "report_jobs.json")
    importlib.reload(report_jobs)
    return report_jobs


def test_new_job_defaults(tmp_path, monkeypatch):
    rj = _fresh_store(tmp_path, monkeypatch)
    job = rj.ReportJob(id="j1", kind="digest", recipients=["a@x.com"])
    assert job.enabled is False
    assert job.health.state == "never_run"
    assert job.schedule.report_time == "08:00"


def test_persist_and_restore_roundtrip(tmp_path, monkeypatch):
    rj = _fresh_store(tmp_path, monkeypatch)
    job = rj.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True)
    rj.put_job(job)
    rj.persist_jobs()
    rj.clear_jobs_in_memory()
    assert rj.get_job("j1") is None
    n = rj.restore_jobs_from_disk()
    assert n == 1
    got = rj.get_job("j1")
    assert got is not None and got.enabled is True and got.recipients == ["a@x.com"]


def test_atomic_write_leaves_no_tmp(tmp_path, monkeypatch):
    rj = _fresh_store(tmp_path, monkeypatch)
    rj.put_job(rj.ReportJob(id="j1", kind="digest", recipients=["a@x.com"]))
    rj.persist_jobs()
    assert not (tmp_path / "report_jobs.json.tmp").exists()
    assert (tmp_path / "report_jobs.json").exists()
