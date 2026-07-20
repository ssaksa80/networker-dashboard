import importlib
from nwdash import config, report_groups

def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_GROUPS_FILE", tmp_path / "report_groups.json")
    importlib.reload(report_groups)
    return report_groups

def test_defaults(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    grp = g.ReportGroup(id="a", name="Ops", sections=["backup_sla"], recipients=["a@x.com"])
    assert grp.enabled is False and grp.cadence == "daily" and grp.send_time == "08:00"
    assert grp.health.state == "never_run" and grp.position == 0

def test_put_assigns_incrementing_positions(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    g.put_group(g.ReportGroup(id="a", name="A", sections=["alerts"], recipients=["a@x.com"]))
    g.put_group(g.ReportGroup(id="b", name="B", sections=["alerts"], recipients=["b@x.com"]))
    ordered = g.groups_ordered()
    assert [x.id for x in ordered] == ["a", "b"] and [x.position for x in ordered] == [0, 1]

def test_persist_restore_roundtrip(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    g.put_group(g.ReportGroup(id="a", name="A", sections=["clone"], recipients=["a@x.com"], enabled=True, cadence="weekly", send_time="07:00"))
    g.persist_groups(); g.clear_groups_in_memory()
    assert g.restore_groups_from_disk() == 1
    got = g.get_group("a")
    assert got.enabled and got.cadence == "weekly" and got.sections == ["clone"]

def test_delete_repacks_positions(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    for i in "abc":
        g.put_group(g.ReportGroup(id=i, name=i, sections=["alerts"], recipients=["x@x.com"]))
    g.delete_group("a")
    assert [x.position for x in g.groups_ordered()] == [0, 1]

def test_reorder(tmp_path, monkeypatch):
    g = _fresh(tmp_path, monkeypatch)
    for i in "abc":
        g.put_group(g.ReportGroup(id=i, name=i, sections=["alerts"], recipients=["x@x.com"]))
    g.reorder(["c", "a", "b"])
    assert [x.id for x in g.groups_ordered()] == ["c", "a", "b"]
