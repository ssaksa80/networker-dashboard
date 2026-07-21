from nwdash.reports import dashboard_report_email


def _dash():
    return {"summary": {"totalJobs": 10, "successfulJobs": 9, "failedJobs": 1, "recoveryJobs": 2,
            "cloneJobs": 3, "totalAlerts": 4, "slaPercent": 90, "slaMetJobs": 9, "slaTotalJobs": 10},
            "range": "Last 24 Hours", "tables": {}, "alerts": [], "protection": {"label": "OK", "detail": ""},
            "health": {}}


def test_all_sections_default():
    plain, html = dashboard_report_email(_dash())
    assert "Backup SLA" in html and "Clone Jobs" in html and "Recovery Health" in html


def test_only_selected_sections():
    plain, html = dashboard_report_email(_dash(), sections=["backup_sla", "alerts"])
    assert "Backup SLA" in html
    assert "Clone Jobs" not in html
    assert "Recovery Health" not in html


def test_empty_sections_lists_none_of_the_cards():
    plain, html = dashboard_report_email(_dash(), sections=[])
    assert "Clone Jobs" not in html and "Recovery Health" not in html
