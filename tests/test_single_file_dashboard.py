import importlib.util
import json
import socket
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace


def load_single_file_dashboard():
    path = Path(__file__).resolve().parents[1] / "networker_dashboard.py"
    spec = importlib.util.spec_from_file_location("single_file_dashboard", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_busy_https_port_falls_back_to_random_available_port():
    dashboard = load_single_file_dashboard()

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]

    try:
        server, selected_port, used_random_port = dashboard.bind_dashboard_server("127.0.0.1", busy_port)
        try:
            assert used_random_port is True
            assert selected_port != busy_port
            assert selected_port > 0
        finally:
            server.server_close()
    finally:
        blocker.close()


def test_reused_cookie_session_does_not_login_with_blank_password(monkeypatch):
    dashboard = load_single_file_dashboard()

    def fail_login(*args, **kwargs):
        raise AssertionError("reused cookie session should not call nwui_login")

    monkeypatch.setattr(dashboard, "nwui_login", fail_login)
    monkeypatch.setattr(dashboard, "nwui_monitoring_all_pages", lambda *args, **kwargs: [])
    monkeypatch.setattr(dashboard, "load_server_health_nwui", lambda *args, **kwargs: dashboard.unavailable_server_health())

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="192.0.2.10",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=True,
        wmi_username=r"DOMAIN\svc_networker_health",
        wmi_password="wmi-password",
        timeout_seconds=5,
        verify_tls=False,
        use_authc_header=False,
    )

    status, body = dashboard.build_dashboard_nwui(
        config,
        cookie_jar=CookieJar(),
        auth_headers={},
        create_session=False,
    )

    assert status == 200
    assert body["sources"]["nwuiLogin"]["ok"] is True
    assert body["sources"]["nwuiLogin"]["path"] == "volatile-session"


def test_optional_nwui_endpoint_failure_keeps_backup_rows(monkeypatch):
    dashboard = load_single_file_dashboard()

    def fake_pages(config, opener, auth_headers, endpoint_name, start_ts=None, end_ts=None):
        if endpoint_name == "monitoringactions":
            return [
                {
                    "startTime": 1778400000000,
                    "duration": 120000,
                    "status": "completed",
                    "workflowName": "client-a",
                    "actionName": "Filesystem backup",
                    "policyName": "Daily",
                    "jobData": {"successfulInputCount": 1},
                }
            ]
        if endpoint_name == "monitoringrecoveries":
            raise dashboard.RestApiError(404, "Recoveries endpoint is not available")
        return []

    monkeypatch.setattr(dashboard, "nwui_monitoring_all_pages", fake_pages)
    monkeypatch.setattr(
        dashboard,
        "load_server_health_nwui",
        lambda *args, **kwargs: {
            "status": "ok",
            "label": "Healthy",
            "detail": "test",
            "source": "/test",
            "cpuUsagePercent": 33,
            "ramUsagePercent": 44,
            "cpuDetail": "CPU utilization",
            "ramDetail": "Memory utilization",
        },
    )

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="192.0.2.10",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="30d",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=5,
        verify_tls=False,
        use_authc_header=False,
    )

    status, body = dashboard.build_dashboard_nwui(
        config,
        cookie_jar=CookieJar(),
        auth_headers={},
        create_session=False,
    )

    assert status == 200
    assert body["tables"]["jobs"][0]["client"] == "client-a"
    assert body["sources"]["monitoringRecoveries"]["ok"] is False
    assert body["summary"]["health"] == "warning"
    assert body["serverHealth"]["cpuUsagePercent"] == 33


def test_custom_report_range_builds_expected_nwui_payload():
    dashboard = load_single_file_dashboard()

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="192.0.2.10",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="custom",
        custom_start_date="01-05-2026",
        custom_end_date="10-05-2026",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=5,
        verify_tls=False,
        use_authc_header=False,
    )

    start_ts, end_ts, label = dashboard.report_window(config)
    payload = dashboard.monitoring_payload(1, 200, start_ts, end_ts)

    assert label == "01-05-2026 to 10-05-2026"
    assert payload["endTime"] > payload["startTime"]
    assert payload["pageNumber"] == 1


def test_dashboard_dates_and_sla_summary_are_formatted():
    dashboard = load_single_file_dashboard()

    summary = dashboard.add_sla_summary({"totalJobs": 10, "successfulJobs": 8, "failedJobs": 2})

    assert dashboard.display_datetime(1778400000000).startswith("10-05-2026")
    assert summary["slaTotalJobs"] == 10
    assert summary["slaMetJobs"] == 8
    assert summary["slaMissedJobs"] == 2
    assert summary["slaPercent"] == 80


def test_sla_excludes_running_jobs_until_they_finish():
    dashboard = load_single_file_dashboard()

    summary = dashboard.add_sla_summary(
        {"totalJobs": 36, "successfulJobs": 35, "failedJobs": 0, "activeJobs": 1}
    )

    assert summary["slaTotalJobs"] == 35
    assert summary["slaMetJobs"] == 35
    assert summary["slaMissedJobs"] == 0
    assert summary["slaPercent"] == 100


def test_management_bar_fill_is_block_level():
    dashboard = load_single_file_dashboard()

    assert ".bar-fill" in dashboard.HTML_PAGE
    assert "display: block;" in dashboard.HTML_PAGE
    assert "Backup SLA" in dashboard.HTML_PAGE
    assert "DD-MM-YYYY" in dashboard.HTML_PAGE
    assert "NetWorker Server Health" in dashboard.HTML_PAGE
    assert "Server Protection Job" in dashboard.HTML_PAGE
    assert "Clone Jobs" in dashboard.HTML_PAGE
    assert "Memory usage" in dashboard.HTML_PAGE
    assert "__NETWORKER_LOGO_SRC__" in dashboard.HTML_PAGE
    assert "topbar-logo" in dashboard.HTML_PAGE
    assert "Maintained &amp; developed by" in dashboard.HTML_PAGE
    assert "SHAIKH SHOAIB" in dashboard.HTML_PAGE
    assert "/api/server-health" in dashboard.HTML_PAGE
    assert "SERVER_HEALTH_REFRESH_MS = 60000" in dashboard.HTML_PAGE


def test_dashboard_html_embeds_networker_logo_data_uri():
    dashboard = load_single_file_dashboard()

    html = dashboard.dashboard_html()

    assert "__NETWORKER_LOGO_SRC__" not in html
    assert "data:image/png;base64," in html
    assert html.count("data:image/png;base64,") >= 4
    assert "/networker-logo.png" not in html
    assert "SHAIKH SHOAIB" in html
    assert "Email Alert Automation" in html
    assert "/api/alert-automation" in html
    assert "value=\"arctic\"" in html
    assert "value=\"ember\"" in html


def test_server_health_payload_and_maintenance_status():
    dashboard = load_single_file_dashboard()

    health = dashboard.server_health_from_payload(
        {"system": {"cpuUsagePercent": "81%", "memoryUsagePercent": 0.66}},
        "/health",
    )
    maintenance = dashboard.maintenance_backup_status(
        [
            {
                "client": "networker-server",
                "name": "Bootstrap maintenance backup",
                "policy": "Server Protection",
                "status": "Completed",
                "started": "11-05-2026 09:00:00 Arabian Standard Time",
                "message": "",
            }
        ]
    )

    assert health["status"] == "warning"
    assert health["cpuUsagePercent"] == 81
    assert health["ramUsagePercent"] == 66
    assert maintenance["status"] == "succeeded"
    assert maintenance["count"] == 1


def test_expiration_job_does_not_count_as_server_protection_job():
    dashboard = load_single_file_dashboard()

    maintenance = dashboard.maintenance_backup_status(
        [
            {
                "client": "Server backup",
                "name": "Expiration",
                "policy": "",
                "status": "Completed",
                "started": "11-05-2026 10:03:24 Arabian Standard Time",
                "message": "",
            }
        ]
    )

    assert maintenance["status"] == "unknown"
    assert maintenance["label"] == "Not found"
    assert maintenance["count"] == 0


def test_server_backup_job_counts_as_server_protection_job():
    dashboard = load_single_file_dashboard()

    maintenance = dashboard.maintenance_backup_status(
        [
            {
                "client": "Server backup",
                "name": "Backup",
                "policy": "",
                "status": "Completed",
                "started": "11-05-2026 15:30:00 Arabian Standard Time",
                "message": "Server db backup completed successfully",
            }
        ]
    )

    assert maintenance["status"] == "succeeded"
    assert maintenance["label"] == "Succeeded"
    assert maintenance["count"] == 1
    assert "Backup on Server backup" in maintenance["detail"]


def test_sql_server_db_backup_does_not_count_as_server_protection_job():
    dashboard = load_single_file_dashboard()

    maintenance = dashboard.maintenance_backup_status(
        [
            {
                "client": "Copy2 of SQL Server DB Backup",
                "name": "backup",
                "policy": "SQL Server DB Backup",
                "status": "running",
                "started": "11-05-2026 16:30:00 Arabian Standard Time",
                "message": "SQL server database backup is running",
                "_workflow": "Copy2 of SQL Server DB Backup",
                "_group": "SQL Server DB Backup",
            }
        ]
    )

    assert maintenance["status"] == "unknown"
    assert maintenance["label"] == "Not found"
    assert maintenance["count"] == 0


def test_nwui_clone_jobs_are_separate_from_recovery_health(monkeypatch):
    dashboard = load_single_file_dashboard()

    def fake_pages(config, opener, auth_headers, endpoint_name, start_ts=None, end_ts=None):
        if endpoint_name == "monitoringactions":
            return [
                {
                    "startTime": 1778400000000,
                    "duration": 120000,
                    "status": "completed",
                    "workflowName": "Filesystem",
                    "actionName": "Backup",
                    "policyName": "Daily",
                    "jobData": {"successfulInputCount": 1},
                },
                {
                    "startTime": 1778400100000,
                    "duration": 90000,
                    "status": "completed",
                    "workflowName": "Clone Workflow",
                    "actionName": "Clone",
                    "policyName": "Clone Policy",
                    "jobData": {"successfulInputCount": 5},
                },
            ]
        if endpoint_name == "monitoringrecoveries":
            return [
                {
                    "clientName": "client-a",
                    "recoverType": "File Restore",
                    "status": "completed",
                    "startTime": 1778400200000,
                    "saveSet": "/data",
                }
            ]
        return []

    monkeypatch.setattr(dashboard, "nwui_monitoring_all_pages", fake_pages)
    monkeypatch.setattr(dashboard, "load_server_health_nwui", lambda *args, **kwargs: dashboard.unavailable_server_health())

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="192.0.2.10",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=5,
        verify_tls=False,
        use_authc_header=False,
    )

    status, body = dashboard.build_dashboard_nwui(
        config,
        cookie_jar=CookieJar(),
        auth_headers={},
        create_session=False,
    )

    assert status == 200
    assert body["summary"]["totalJobs"] == 1
    assert body["summary"]["recoveryJobs"] == 1
    assert body["summary"]["cloneJobs"] == 1
    assert body["summary"]["cloneSessionTotal"] == 5
    assert "sessionTotal" not in body["summary"]
    assert body["tables"]["jobs"][0]["name"] == "Backup"
    assert body["tables"]["recovery"][0]["name"] == "File Restore"
    assert body["tables"]["cloneJobs"][0]["name"] == "Clone"


def test_wmi_credentials_accept_special_characters_and_encrypt_in_session():
    dashboard = load_single_file_dashboard()
    password = r"P@ss;word!`\"'&<>|{}[]$"

    config = dashboard.validate_payload(
        {
            "restApiHost": "192.0.2.10",
            "restApiPort": "9090",
            "backupServerHost": "198.51.100.11",
            "backupServerPort": "9090",
            "username": "admin",
            "password": "networker-password",
            "apiMode": "nwui",
            "apiVersion": "auto",
            "reportRange": "24h",
            "timeoutSeconds": "10",
            "useWmiHealth": True,
            "wmiUsername": r"DOMAIN\svc_networker_health",
            "wmiPassword": password,
        }
    )
    session_id = dashboard.create_dashboard_session(config, CookieJar(), {})
    session = dashboard.DASHBOARD_SESSIONS[session_id]

    assert config.wmi_password == password
    assert session.config.wmi_password == ""
    assert session.encrypted_wmi_password
    assert password not in session.encrypted_wmi_password
    assert dashboard.decrypt_wmi_password(session.encrypted_wmi_password) == password


def test_wmi_timeout_error_hides_encoded_command(monkeypatch):
    dashboard = load_single_file_dashboard()

    def timeout_run(*args, **kwargs):
        raise dashboard.subprocess.TimeoutExpired(
            cmd=["powershell.exe", "-EncodedCommand", "secret-script"],
            timeout=10,
        )

    monkeypatch.setattr(dashboard.subprocess, "run", timeout_run)

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=True,
        wmi_username=r"DOMAIN\svc_networker_health",
        wmi_password="wmi-password",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )

    health = dashboard.load_server_health_wmi(config)

    assert health["status"] == "unknown"
    assert "WMI query timed out after 10s" in health["detail"]
    assert "WMI/DCOM access to 198.51.100.11" in health["detail"]
    assert "EncodedCommand" not in health["detail"]
    assert "secret-script" not in health["detail"]


def test_wmi_clixml_progress_is_hidden_from_error(monkeypatch):
    dashboard = load_single_file_dashboard()
    clixml = """#< CLIXML
<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">
  <Obj S="progress" RefId="0"><MS><PR N="Record"><AV>Preparing modules for first use.</AV></PR></MS></Obj>
  <S S="Error">Access is denied.</S>
</Objs>"""

    def failed_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=clixml)

    monkeypatch.setattr(dashboard.subprocess, "run", failed_run)

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=True,
        wmi_username=r"DOMAIN\svc_networker_health",
        wmi_password="wmi-password",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )

    health = dashboard.load_server_health_wmi(config)

    assert "Access is denied." in health["detail"]
    assert "CLIXML" not in health["detail"]
    assert "Preparing modules" not in health["detail"]


def test_wmi_health_reports_memory_in_gb(monkeypatch):
    dashboard = load_single_file_dashboard()

    def completed_run(*args, **kwargs):
        encoded_index = args[0].index("-EncodedCommand") + 1
        script = dashboard.base64.b64decode(args[0][encoded_index]).decode("utf-16le")
        assert "Win32_PerfRawData_PerfOS_Processor" in script
        assert "Win32_PerfFormattedData_PerfOS_Processor" not in script
        assert "Start-Sleep -Seconds $cpuSampleSeconds" in script
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "host": "198.51.100.11",
                    "cpuUsagePercent": 21,
                    "cpuSampleSeconds": 1,
                    "ramUsagePercent": 50,
                    "totalMemoryMb": 64 * 1024,
                    "freeMemoryMb": 32 * 1024,
                    "uptimeSeconds": 123,
                    "osCaption": "Windows Server 2019",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(dashboard.subprocess, "run", completed_run)

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=True,
        wmi_username=r"DOMAIN\svc_networker_health",
        wmi_password="wmi-password",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )

    health = dashboard.load_server_health_wmi(config)

    assert health["cpuUsagePercent"] == 21
    assert health["ramUsagePercent"] == 50
    assert health["ramUsedGb"] == 32
    assert health["ramFreeGb"] == 32
    assert health["ramTotalGb"] == 64
    assert "GB used" in health["ramDetail"]


def test_server_health_session_refresh_reuses_session(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )
    session_id = dashboard.create_dashboard_session(config, CookieJar(), {})

    monkeypatch.setattr(
        dashboard,
        "load_server_health_nwui",
        lambda *args, **kwargs: {
            "status": "ok",
            "label": "Healthy",
            "detail": "refreshed",
            "source": "test",
            "cpuUsagePercent": 12,
            "ramUsagePercent": 25,
            "ramUsedGb": 16,
            "ramFreeGb": 48,
            "ramTotalGb": 64,
            "cpuDetail": "CPU utilization",
            "ramDetail": "16 GB used of 64 GB",
        },
    )

    status, body = dashboard.build_server_health_from_session(session_id)

    assert status == 200
    assert body["ok"] is True
    assert body["serverHealth"]["cpuUsagePercent"] == 12
    assert body["serverHealth"]["ramUsedGb"] == 16
    assert body["serverProtectionJob"]["label"] == "Not found"


def test_server_health_session_refresh_returns_live_server_protection(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )
    session_id = dashboard.create_dashboard_session(config, CookieJar(), {"X-Test": "token"})

    monkeypatch.setattr(
        dashboard,
        "load_server_health_nwui",
        lambda *args, **kwargs: dashboard.unavailable_server_health("health skipped"),
    )
    monkeypatch.setattr(
        dashboard,
        "nwui_monitoring_all_pages",
        lambda *args, **kwargs: [
            {
                "startTime": 1778400000000,
                "duration": 120000,
                "status": "completed",
                "workflowName": "Server Protection",
                "actionName": "Server backup",
                "policyName": "Server Protection",
                "jobData": {"successfulInputCount": 1},
            }
        ],
    )

    status, body = dashboard.build_server_health_from_session(session_id)

    assert status == 200
    assert body["serverProtectionJob"]["status"] == "succeeded"
    assert body["serverProtectionJob"]["count"] == 1


def test_server_protection_refresh_failure_does_not_repeat_last_known_text(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )
    previous = {
        "status": "succeeded",
        "label": "Succeeded",
        "detail": "Server db backup on Server backup at 13-05-2026 10:00:01 Arabian Standard Time",
        "count": 1,
    }
    monkeypatch.setattr(
        dashboard,
        "nwui_monitoring_all_pages",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            dashboard.RestApiError(502, "REST API connection timed out.")
        ),
    )

    first = dashboard.refresh_server_protection_job_nwui(config, CookieJar(), {"X-Test": "token"}, previous)
    second = dashboard.refresh_server_protection_job_nwui(config, CookieJar(), {"X-Test": "token"}, first)

    assert second["detail"].count("last known") == 1
    assert second["detail"].count("refresh failed") == 1
    assert second["detail"].startswith("Server db backup on Server backup")


def test_alert_automation_test_email_uses_smtp_settings(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )
    session_id = dashboard.create_dashboard_session(config, CookieJar(), {"X-Test": "token"})
    sent = []

    def fake_send(settings, subject, body, smtp_password, html_body=""):
        sent.append((settings.smtp_host, settings.smtp_port, settings.recipients, subject, smtp_password, html_body))

    monkeypatch.setattr(dashboard, "send_smtp_email", fake_send)

    status, body = dashboard.handle_alert_automation(
        {
            "action": "test",
            "sessionId": session_id,
            "smtpHost": "smtp.example.com",
            "smtpPort": "587",
            "smtpSecurity": "starttls",
            "smtpUsername": "svc",
            "smtpPassword": "p@ss;word",
            "smtpFrom": "networker@example.com",
            "smtpTo": "ops@example.com;backup@example.com",
            "intervalMinutes": "15",
            "trigger": "warning",
            "scheduleType": "alert",
            "reportTime": "08:00",
        }
    )

    assert status == 200
    assert body["message"] == "Test email sent."
    assert sent == [
        (
            "smtp.example.com",
            587,
            ["ops@example.com", "backup@example.com"],
            "NetWorker dashboard test email",
            "p@ss;word",
            "",
        )
    ]


def test_daily_report_email_embeds_backup_status_and_sla():
    dashboard = load_single_file_dashboard()
    plain, html = dashboard.dashboard_report_email(
        {
            "generatedAt": "13-05-2026 09:00:00 Arabian Standard Time",
            "summary": {
                "rangeLabel": "Last 24 Hours",
                "totalJobs": 36,
                "successfulJobs": 35,
                "failedJobs": 1,
                "activeJobs": 0,
                "recoveryJobs": 2,
                "cloneJobs": 3,
                "totalAlerts": 1,
                "slaPercent": 97,
                "slaMetJobs": 35,
                "slaTotalJobs": 36,
                "slaMissedJobs": 1,
            },
            "serverHealth": {"label": "Healthy", "cpuUsagePercent": 12, "ramUsagePercent": 44},
            "serverProtectionJob": {"label": "Succeeded", "detail": "Server backup completed"},
        }
    )

    assert "Total backup jobs: 36" in plain
    assert "Backup SLA: 97% (35 met / 36 total)" in plain
    assert "<table" in html
    assert "Server backup completed" in html


def test_daily_report_automation_sends_embedded_report(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="nwui",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )
    session_id = dashboard.create_dashboard_session(config, CookieJar(), {"X-Test": "token"})
    sent = []

    monkeypatch.setattr(
        dashboard,
        "build_dashboard_from_session",
        lambda session_id: (
            200,
            {
                "generatedAt": "13-05-2026 09:00:00 Arabian Standard Time",
                "summary": {
                    "rangeLabel": "Last 24 Hours",
                    "totalJobs": 10,
                    "successfulJobs": 10,
                    "failedJobs": 0,
                    "activeJobs": 0,
                    "recoveryJobs": 0,
                    "cloneJobs": 0,
                    "totalAlerts": 0,
                    "slaPercent": 100,
                    "slaMetJobs": 10,
                    "slaTotalJobs": 10,
                    "slaMissedJobs": 0,
                },
                "serverHealth": {"label": "Healthy"},
                "serverProtectionJob": {"label": "Succeeded", "detail": "Server backup completed"},
            },
        ),
    )

    def fake_send(settings, subject, body, smtp_password, html_body=""):
        sent.append((subject, body, smtp_password, html_body))

    monkeypatch.setattr(dashboard, "send_smtp_email", fake_send)
    monkeypatch.setattr(dashboard, "schedule_alert_automation", lambda automation: None)

    status, body = dashboard.handle_alert_automation(
        {
            "action": "start",
            "sessionId": session_id,
            "smtpHost": "smtp.example.com",
            "smtpPort": "587",
            "smtpSecurity": "starttls",
            "smtpUsername": "svc",
            "smtpPassword": "daily-secret",
            "smtpFrom": "networker@example.com",
            "smtpTo": "ops@example.com",
            "intervalMinutes": "60",
            "trigger": "all",
            "scheduleType": "daily_report",
            "reportTime": "07:30",
        }
    )
    assert status == 200
    assert "07:30" in body["message"]

    dashboard.run_alert_automation(session_id)

    assert sent
    assert sent[0][0] == "NetWorker daily backup status and SLA report"
    assert "Backup SLA: 100% (10 met / 10 total)" in sent[0][1]
    assert sent[0][2] == "daily-secret"
    assert "<table" in sent[0][3]


def test_server_health_session_refresh_uses_wmi_without_nwui_fallback(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="auto",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=True,
        wmi_username=r"DOMAIN\svc_networker_health",
        wmi_password="wmi-password",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )
    session_id = dashboard.create_dashboard_session(config, CookieJar(), {"X-Test": "token"})

    monkeypatch.setattr(
        dashboard,
        "refresh_server_protection_job_nwui",
        lambda *args, **kwargs: dashboard.maintenance_backup_status([]),
    )
    monkeypatch.setattr(
        dashboard,
        "load_server_health_wmi",
        lambda config: {
            "status": "ok",
            "label": "Healthy",
            "detail": "Windows Server 2019 via WMI.",
            "source": "WMI 198.51.100.11",
            "cpuUsagePercent": 17,
            "ramUsagePercent": 41,
            "ramUsedGb": 26,
            "ramFreeGb": 38,
            "ramTotalGb": 64,
            "cpuDetail": "Real-time WMI sample",
            "ramDetail": "26 GB used of 64 GB",
        },
    )
    monkeypatch.setattr(
        dashboard,
        "load_server_health_nwui",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("NWUI health fallback should not run")),
    )

    status, body = dashboard.build_server_health_from_session(session_id)

    assert status == 200
    assert body["serverHealth"]["cpuUsagePercent"] == 17
    assert body["serverHealth"]["ramUsedGb"] == 26


def test_auto_mode_session_refresh_uses_nwui_health(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="",
        api_mode="auto",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )
    session_id = dashboard.create_dashboard_session(config, CookieJar(), {"X-Test": "token"})
    calls = []
    monkeypatch.setattr(
        dashboard,
        "refresh_server_protection_job_nwui",
        lambda *args, **kwargs: dashboard.maintenance_backup_status([]),
    )

    def health(config, cookie_jar, auth_headers):
        calls.append((config.api_mode, auth_headers))
        return {
            "status": "ok",
            "label": "Healthy",
            "detail": "refreshed",
            "source": "test",
            "cpuUsagePercent": 11,
            "ramUsagePercent": 22,
            "ramUsedGb": 10,
            "ramFreeGb": 54,
            "ramTotalGb": 64,
            "cpuDetail": "CPU",
            "ramDetail": "Memory",
        }

    monkeypatch.setattr(dashboard, "load_server_health_nwui", health)

    status, body = dashboard.build_server_health_from_session(session_id)

    assert status == 200
    assert calls == [("nwui", {"X-Test": "token"})]
    assert body["serverHealth"]["cpuUsagePercent"] == 11
    assert "available after an NWUI session login" not in body["serverHealth"]["detail"]


def test_auto_mode_keeps_nwui_result_after_login_success(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="bad-host-name",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="password",
        api_mode="auto",
        api_version="auto",
        report_range="24h",
        custom_start_date="",
        custom_end_date="",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=10,
        verify_tls=False,
        use_authc_header=False,
    )

    monkeypatch.setattr(
        dashboard,
        "build_dashboard_nwui",
        lambda config: (
            502,
            {
                "ok": False,
                "sources": {
                    "nwuiLogin": {"ok": True},
                    "monitoringActions": {"ok": False, "error": "monitoring endpoint failed"},
                },
                "target": {"apiMode": "nwui"},
                "error": "NWUI login worked, but no monitoring endpoints returned data.",
            },
        ),
    )
    monkeypatch.setattr(
        dashboard,
        "build_dashboard_rest_auto",
        lambda config: (_ for _ in ()).throw(AssertionError("REST fallback should not run after NWUI login succeeds")),
    )

    status, body = dashboard.build_dashboard(config)

    assert status == 502
    assert body["target"]["apiMode"] == "nwui"
    assert "monitoring endpoint failed" in body["sources"]["monitoringActions"]["error"]
