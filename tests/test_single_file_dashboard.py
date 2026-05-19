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


def test_wildcard_bind_detects_localhost_port_already_in_use():
    dashboard = load_single_file_dashboard()

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]

    try:
        assert dashboard.can_exclusively_bind_port("0.0.0.0", busy_port) is False
        server, selected_port, used_random_port = dashboard.bind_dashboard_server("0.0.0.0", busy_port)
        try:
            assert used_random_port is True
            assert selected_port != busy_port
            assert selected_port > 0
        finally:
            server.server_close()
    finally:
        blocker.close()


def test_service_access_urls_include_localhost_and_server_ip(monkeypatch):
    dashboard = load_single_file_dashboard()

    monkeypatch.setattr(dashboard, "local_ipv4_addresses", lambda: ["198.51.100.11"])

    urls = dashboard.service_access_urls("0.0.0.0", 8443)

    assert urls == [
        ("Localhost", "https://localhost:8443/"),
        ("Local server IP", "https://198.51.100.11:8443/"),
    ]


def test_preferred_launch_url_uses_localhost_first():
    dashboard = load_single_file_dashboard()

    url = dashboard.preferred_launch_url(
        [
            ("Local server IP", "https://198.51.100.11:8443/"),
            ("Localhost", "https://localhost:8443/"),
        ]
    )

    assert url == "https://localhost:8443/"


def test_self_test_dashboard_listener_validates_https_health(monkeypatch):
    dashboard = load_single_file_dashboard()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit):
            return json.dumps({"ok": True, "https": True}).encode("utf-8")

    def fake_urlopen(request, timeout=0, context=None):
        assert request.full_url == "https://localhost:9443/api/health"
        assert context is not None
        return FakeResponse()

    monkeypatch.setattr(dashboard, "urlopen", fake_urlopen)

    ok, message = dashboard.self_test_dashboard_listener("https://localhost:9443/", timeout_seconds=0.5)

    assert ok is True
    assert "port 9443" in message


def test_default_bind_serves_all_local_interfaces():
    dashboard = load_single_file_dashboard()

    args = dashboard.parse_args([])

    assert args.bind == "0.0.0.0"
    assert args.no_launch is False


def test_no_launch_argument_disables_auto_browser_launch():
    dashboard = load_single_file_dashboard()

    args = dashboard.parse_args(["--no-launch"])
    opened, message = dashboard.auto_launch_dashboard("https://localhost:8443/", enabled=not args.no_launch)

    assert args.no_launch is True
    assert opened is False
    assert "--no-launch" in message


def test_dashboard_snapshots_save_and_compare_growth(tmp_path, monkeypatch):
    dashboard = load_single_file_dashboard()
    monkeypatch.setattr(dashboard, "DASHBOARD_SNAPSHOT_FILE", tmp_path / "networker_snapshots.json")
    monkeypatch.setattr(dashboard, "DATA_DIR", tmp_path)

    base = {
        "ok": True,
        "generatedAt": "01-05-2026 10:00:00 Arabian Standard Time",
        "target": {"apiMode": "nwui", "backupServer": "198.51.100.11:9090", "reportRange": "24h"},
        "summary": {
            "range": "24h",
            "rangeLabel": "Last 24 Hours",
            "totalJobs": 100,
            "successfulJobs": 98,
            "failedJobs": 2,
            "activeJobs": 0,
            "recoveryJobs": 1,
            "cloneJobs": 5,
            "totalAlerts": 1,
            "totalClients": 25,
            "slaPercent": 98,
            "health": "warning",
        },
        "serverHealth": {"label": "Healthy"},
        "serverProtectionJob": {"label": "Succeeded"},
    }
    newer = json.loads(json.dumps(base))
    newer["summary"]["totalJobs"] = 130
    newer["summary"]["successfulJobs"] = 127
    newer["summary"]["failedJobs"] = 3

    dashboard.save_dashboard_snapshot(base, dashboard.datetime(2026, 5, 1))
    dashboard.save_dashboard_snapshot(newer, dashboard.datetime(2026, 5, 8))

    comparison = dashboard.compare_dashboard_snapshots("7d")
    total_jobs = next(row for row in comparison["metrics"] if row["key"] == "totalJobs")

    assert comparison["ok"] is True
    assert comparison["previousDate"] == "2026-05-01"
    assert comparison["currentDate"] == "2026-05-08"
    assert total_jobs["delta"] == 30
    assert "2026-05-08" in dashboard.snapshot_summary_text()


def test_shared_dashboard_payload_returns_last_server_session(monkeypatch):
    dashboard = load_single_file_dashboard()
    monkeypatch.setattr(dashboard, "snapshot_summary_text", lambda: "snapshot summary")
    dashboard.set_shared_dashboard(
        "session-1",
        {
            "ok": True,
            "generatedAt": "18-05-2026 10:00:00 Arabian Standard Time",
            "summary": {"totalJobs": 10},
            "target": {"apiMode": "nwui"},
        },
    )

    payload = dashboard.shared_dashboard_payload()

    assert payload["ok"] is True
    assert payload["sessionId"] == "session-1"
    assert payload["dashboard"]["sessionId"] == "session-1"
    assert payload["snapshotSummary"] == "snapshot summary"


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
        report_range="custom",
        custom_start_date="10-05-2026",
        custom_end_date="10-05-2026",
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


def test_nwui_login_tries_backup_server_payload_after_initial_401(monkeypatch):
    dashboard = load_single_file_dashboard()
    attempts = []

    def fake_status_request(opener, url, method, headers, timeout, payload):
        attempts.append(payload)
        if payload.get("server"):
            return 200, {"token": "abc123"}, '{"token":"abc123"}'
        return 401, {"errorMessage": "Unauthorized access"}, '{"errorMessage":"Unauthorized access"}'

    monkeypatch.setattr(dashboard, "json_status_request", fake_status_request)

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="networker-password",
        api_mode="nwui",
        api_version="auto",
        report_range="custom",
        custom_start_date="10-05-2026",
        custom_end_date="10-05-2026",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=5,
        verify_tls=False,
        use_authc_header=False,
    )

    auth_headers, info = dashboard.nwui_login(config, opener=None)

    assert auth_headers["Authorization"] == "Bearer abc123"
    assert info["status"] == 200
    assert len(attempts) == 2
    assert attempts[0]["server"] is None
    assert attempts[1]["server"] == "198.51.100.11"


def test_nwui_login_reports_all_401_payload_attempts(monkeypatch):
    dashboard = load_single_file_dashboard()

    def fake_status_request(opener, url, method, headers, timeout, payload):
        return 401, {"errorMessage": "Unauthorized access"}, '{"errorMessage":"Unauthorized access"}'

    monkeypatch.setattr(dashboard, "json_status_request", fake_status_request)

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="wrong-password",
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

    try:
        dashboard.nwui_login(config, opener=None)
        assert False, "Expected RestApiError"
    except dashboard.RestApiError as exc:
        assert exc.status_code == 401
        assert "Tried 3 NWUI login payload variant" in exc.message
        assert "username,pwd,server,port" in exc.message
        assert "wrong-password" not in exc.message


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


def test_nwui_monitoring_retries_smaller_page_after_http_500(monkeypatch):
    dashboard = load_single_file_dashboard()
    attempts = []

    def fake_post(config, opener, auth_headers, endpoint_name, payload):
        attempts.append((payload.get("pageLimit"), "startTime" in payload))
        if payload.get("pageLimit") == 200:
            raise dashboard.RestApiError(500, "HTTP 500 NWUI POST failed")
        return {"actions": [{"startTime": 1778400000000, "status": "completed"}], "totalCount": 1}

    monkeypatch.setattr(dashboard, "nwui_post_json", fake_post)

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

    items = dashboard.nwui_monitoring_all_pages(
        config,
        opener=None,
        auth_headers={},
        endpoint_name="monitoringactions",
        start_ts=1778390000,
        end_ts=1778410000,
    )

    assert items == [{"startTime": 1778400000000, "status": "completed"}]
    assert attempts == [(200, True), (100, True)]


def test_nwui_monitoring_unfiltered_fallback_preserves_report_window(monkeypatch):
    dashboard = load_single_file_dashboard()
    attempts = []

    def fake_post(config, opener, auth_headers, endpoint_name, payload):
        attempts.append((payload.get("pageLimit"), "startTime" in payload))
        if "startTime" in payload:
            raise dashboard.RestApiError(500, "HTTP 500 NWUI POST failed")
        return {
            "actions": [
                {"startTime": 1778400000000, "status": "completed"},
                {"startTime": 1777000000000, "status": "completed"},
            ],
            "totalCount": 2,
        }

    monkeypatch.setattr(dashboard, "nwui_post_json", fake_post)

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
        custom_start_date="10-05-2026",
        custom_end_date="10-05-2026",
        use_wmi_health=False,
        wmi_username="",
        wmi_password="",
        timeout_seconds=5,
        verify_tls=False,
        use_authc_header=False,
    )

    items = dashboard.nwui_monitoring_all_pages(
        config,
        opener=None,
        auth_headers={},
        endpoint_name="monitoringactions",
        start_ts=1778390000,
        end_ts=1778410000,
    )

    assert items == [{"startTime": 1778400000000, "status": "completed"}]
    assert attempts == [(200, True), (100, True), (50, True), (100, False)]


def test_nwui_http_500_actions_use_direct_rest_fallback(monkeypatch):
    dashboard = load_single_file_dashboard()

    def fake_pages(config, opener, auth_headers, endpoint_name, start_ts=None, end_ts=None):
        if endpoint_name in ("monitoringactions", "monitoringpolicies"):
            raise dashboard.RestApiError(500, "HTTP 500 NWUI POST failed")
        return []

    def fake_fetch_json(url, headers, timeout, context, label):
        if "/global/jobs" in url:
            return {
                "jobs": [
                    {
                        "clientHostname": "client-a",
                        "startTime": 1778400000000,
                        "completionStatus": "Succeeded",
                        "name": "Filesystem backup",
                        "policyName": "Daily",
                    },
                    {
                        "clientHostname": "client-b",
                        "startTime": 1778400100000,
                        "completionStatus": "Failed",
                        "name": "Filesystem backup",
                        "policyName": "Daily",
                        "message": "Save failed",
                    },
                ]
            }
        if "/global/protectionpolicies" in url:
            raise dashboard.RestApiError(500, "REST policies failed")
        raise AssertionError(f"Unexpected REST fallback URL: {url}")

    monkeypatch.setattr(dashboard, "nwui_monitoring_all_pages", fake_pages)
    monkeypatch.setattr(dashboard, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(dashboard, "load_server_health_nwui", lambda *args, **kwargs: dashboard.unavailable_server_health())

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="192.0.2.10",
        backup_server_port=9090,
        username="admin",
        password="networker-password",
        api_mode="nwui",
        api_version="auto",
        report_range="custom",
        custom_start_date="10-05-2026",
        custom_end_date="10-05-2026",
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
    assert body["summary"]["totalJobs"] == 2
    assert body["summary"]["successfulJobs"] == 1
    assert body["summary"]["failedJobs"] == 1
    assert body["sources"]["monitoringActions"]["ok"] is True
    assert "/nwrestapi/" in body["sources"]["monitoringActions"]["path"]
    assert "Used direct REST fallback" in body["sources"]["monitoringActions"]["detail"]
    assert body["sources"]["monitoringPolicies"]["ok"] is True
    assert "Optional policy summary unavailable" in body["sources"]["monitoringPolicies"]["detail"]


def test_nwui_direct_rest_fallback_prefers_backup_server_host(monkeypatch):
    dashboard = load_single_file_dashboard()
    requested_urls = []

    def fake_fetch_json(url, headers, timeout, context, label):
        requested_urls.append(url)
        assert "198.51.100.11:9090" in url
        return {
            "jobs": [
                {
                    "clientHostname": "client-a",
                    "startTime": 1778400000000,
                    "completionStatus": "Succeeded",
                    "name": "Filesystem backup",
                    "policyName": "Daily",
                }
            ]
        }

    monkeypatch.setattr(dashboard, "fetch_json", fake_fetch_json)

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="networker-password",
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

    items, fallback_path = dashboard.nwui_rest_fallback_items(
        config,
        "actions",
        dashboard.ssl_context_for_api(False),
    )

    assert requested_urls[0].startswith("https://198.51.100.11:9090/nwrestapi/v3/global/jobs")
    assert "192.0.2.10" not in requested_urls[0]
    assert fallback_path.startswith("https://198.51.100.11:9090/nwrestapi/v3/global/jobs")
    assert items[0]["workflowName"] == "client-a"


def test_nwui_direct_rest_fallback_tries_nwui_host_after_backup_host_404(monkeypatch):
    dashboard = load_single_file_dashboard()
    requested_urls = []

    def fake_fetch_json(url, headers, timeout, context, label):
        requested_urls.append(url)
        if "198.51.100.11:9090" in url:
            raise dashboard.RestApiError(404, "HTTP 404 Not Found")
        if "192.0.2.10:9090" in url:
            return {"jobs": []}
        raise AssertionError(f"Unexpected fallback URL: {url}")

    monkeypatch.setattr(dashboard, "fetch_json", fake_fetch_json)

    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="networker-password",
        api_mode="nwui",
        api_version="v3",
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

    items, fallback_path = dashboard.nwui_rest_fallback_items(
        config,
        "actions",
        dashboard.ssl_context_for_api(False),
    )

    assert len(requested_urls) == 2
    assert requested_urls[0].startswith("https://198.51.100.11:9090/nwrestapi/v3/global/jobs")
    assert requested_urls[1].startswith("https://192.0.2.10:9090/nwrestapi/v3/global/jobs")
    assert fallback_path.startswith("https://192.0.2.10:9090/nwrestapi/v3/global/jobs")
    assert items == []


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
    assert "repeat(auto-fit, minmax(min(100%, 320px), 1fr))" in dashboard.HTML_PAGE
    assert "grid-template-columns: minmax(124px, 0.9fr) minmax(132px, 1fr)" in dashboard.HTML_PAGE
    assert "overflow-wrap: anywhere" in dashboard.HTML_PAGE
    assert "repeat(auto-fit, minmax(170px, 1fr))" in dashboard.HTML_PAGE
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
    assert "Dashboard auto-refresh failed; keeping last successful data" in dashboard.HTML_PAGE


def test_dashboard_html_embeds_networker_logo_data_uri():
    dashboard = load_single_file_dashboard()

    html = dashboard.dashboard_html()

    assert "__NETWORKER_LOGO_SRC__" not in html
    assert "data:image/png;base64," in html
    assert html.count("data:image/png;base64,") >= 4
    assert "/networker-logo.png" not in html
    assert "SHAIKH SHOAIB" in html
    assert "Email Alert Automation" in html
    assert "alertConfigBtn" in html
    assert "showConnectionBtn" in html
    assert "topbar-button" in html
    assert "connection-open" in html
    assert "alertAutomationModal" in html
    assert "openAlertAutomationModal" in html
    assert "/api/alert-automation" in html
    assert "value=\"arctic\"" in html
    assert "value=\"ember\"" in html
    assert "function syncSmtpSecurityFields()" in html
    assert 'smtpPort.value = "25"' in html
    assert "smtpUsername.disabled = isPlainSmtp" in html


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


def test_nwui_backup_summary_uses_save_session_counts_like_dpa(monkeypatch):
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
                    "jobData": {
                        "successfulInputCount": 2843,
                        "failedInputCount": 4,
                        "runningInputCount": 3,
                        "waitingInputCount": 1,
                    },
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
    assert body["summary"]["totalJobs"] == 2847
    assert body["summary"]["completedJobs"] == 2847
    assert body["summary"]["successfulJobs"] == 2843
    assert body["summary"]["failedJobs"] == 4
    assert body["summary"]["activeJobs"] == 4
    assert body["summary"]["slaTotalJobs"] == 2847
    assert body["summary"]["slaMetJobs"] == 2843
    assert body["summary"]["slaMissedJobs"] == 4
    assert body["summary"]["slaPercent"] == 99.86
    assert body["tables"]["jobs"][0]["message"] == "2851 sessions (2843 ok, 4 failed, 3 running, 1 waiting, 0 canceled)"


def test_nwui_verbose_job_output_is_rendered_like_networker_log():
    dashboard = load_single_file_dashboard()
    raw_output = (
        "suppressed 6184 bytes of output. 128137 1779170408 0 0 2 10488 11156 0 "
        "nwsrv01.example.local savegrp NSR info 63 Group %s waiting for %d jobs "
        "(%d awaiting restart) to complete. 3 26 10 NMC server 1 1 1 1 1 0"
    )

    job = dashboard.project_nwui_job(
        {
            "startTime": 1779170408000,
            "duration": 120000,
            "status": "running",
            "workflowName": "NMC server",
            "actionName": "Backup",
            "policyName": "Server Protection",
            "jobOutput": raw_output,
            "jobData": {
                "successfulInputCount": 0,
                "failedInputCount": 0,
                "runningInputCount": 0,
                "waitingInputCount": 2,
                "canceledInputCount": 0,
            },
        }
    )

    assert job["message"] == "Group NMC server waiting for 26 jobs (10 awaiting restart) to complete."
    assert "suppressed" not in job["message"].lower()
    assert "savegrp NSR" not in job["message"]
    assert dashboard.nwui_job_log_row(job) == {
        "priority": "warning",
        "time": job["started"],
        "source": "event",
        "category": "policy",
        "message": "Group NMC server waiting for 26 jobs (10 awaiting restart) to complete.",
    }


def test_nwui_verbose_job_output_without_counts_is_safely_summarized():
    dashboard = load_single_file_dashboard()
    raw_output = (
        "suppressed 6184 bytes of output. 128137 1779170408 0 0 2 10488 11156 0 "
        "nwsrv01.example.local savegrp NSR info 63 Group %s waiting for %d jobs "
        "(%d awaiting restart) to complete. 3 26 10 NMC server 1 1 1"
    )

    message = dashboard.clean_networker_job_message(raw_output)

    assert message == "Group NMC server waiting for 26 jobs (10 awaiting restart) to complete."
    assert "suppressed" not in message.lower()
    assert "128137" not in message


def test_nwui_combined_job_output_uses_final_networker_log_message():
    dashboard = load_single_file_dashboard()
    raw_output = (
        "Group NMC server 1 1 1 1 0 116552 1779170579 2 5 0 10488 11156 0 "
        "nwsrv01.example.local savegrp NSR warning 40 Unable to handle job monitor message: %s "
        "1 49 194 116550 88 Unable to add the job to monitor list: No entry found for save set '%s' "
        "in client '%s'. 2 0 60 D:\\Program Files\\EMC NetWorker\\Management\\GST\\cst "
        "174897 1779170615 1 5 0 10488 11156 0 nwsrv01.example.local savegrp NSR notice 71 "
        "The save job for the save set '%s' on the host '%s' has been completed. 2 20 53 "
        "D:\\Program Files\\EMC NetWorker\\Management\\cstq 12 21 nwsrv01.example.local "
        "90491 1779170618 1 5 0 10488 11156 0 nwsrv01.example.local savegrp NSR notice 9 "
        "%s:%s. 3 12 23 nwsrv01.example.local savegrp waiting for 26 jobs "
        "(10 awaiting restart) to complete."
    )

    job = dashboard.project_nwui_job(
        {
            "startTime": 1779170579000,
            "duration": 222000,
            "status": "completed",
            "workflowName": "NMC server backup",
            "actionName": "NMC server backup",
            "policyName": "Server Protection",
            "jobOutput": raw_output,
            "jobData": {
                "successfulInputCount": 1,
                "failedInputCount": 0,
                "runningInputCount": 0,
                "waitingInputCount": 0,
                "canceledInputCount": 0,
            },
        }
    )

    assert job["message"] == "Group NMC server backup waiting for 26 jobs (10 awaiting restart) to complete."
    assert "Unable to handle job monitor" not in job["message"]
    assert "Program Files" not in job["message"]
    assert "NSR notice" not in job["message"]


def test_rest_job_projection_cleans_combined_networker_output_and_formats_duration():
    dashboard = load_single_file_dashboard()
    raw_output = (
        "Action '' has initialized as '' with job id %u 4 0 6 expire 0 10 Expiration 0 11 utility job "
        "5 8 10934314 88411 1779170620 1 5 0 14540 14372 0 nwsrv01.example.local nsrim "
        "NSR notice 28 Checking for invalid volumes 0 86069 1779170620 1 5 0 14540 14372 0 "
        "nwsrv01.example.local nsrim NSR notice 21 Processing clients 1 1 3 124 86067 "
        "1779170704 1 5 0 14540 14372 0 nwsrv01.example.local nsrim NSR notice 37 "
        "Crosschecking indexes for clients."
    )

    row = dashboard.project_job(
        {
            "clientHostname": "Server backup",
            "name": "Expiration",
            "policyName": "Server Protection",
            "completionStatus": "succeeded",
            "elapsedTime": 331,
            "message": raw_output,
        }
    )

    assert row["duration"] == "5m 31s"
    assert row["message"] == "Crosschecking indexes for clients."
    assert "1779170620" not in row["message"]
    assert "NSR notice" not in row["message"]


def test_long_nwui_job_duration_is_displayed_as_hours_and_minutes():
    dashboard = load_single_file_dashboard()

    job = dashboard.project_nwui_job(
        {
            "duration": 30999000,
            "status": "running",
            "workflowName": "Daily DB Arch Log Backups",
            "actionName": "Cerner_ArchiveLog_Backup",
            "policyName": "Cerner_DB_Backup",
            "jobData": {"runningInputCount": 1},
        }
    )

    assert job["duration"] == "8h 36m"


def test_dashboard_html_includes_networker_style_log_tab():
    dashboard = load_single_file_dashboard()

    assert 'data-table="logs"' in dashboard.HTML_PAGE
    assert 'title: "Log"' in dashboard.HTML_PAGE
    assert '["priority", "Priority"]' in dashboard.HTML_PAGE
    assert '["source", "Source"]' in dashboard.HTML_PAGE
    assert '["category", "Category"]' in dashboard.HTML_PAGE


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


def test_networker_password_is_encrypted_for_session_relogin():
    dashboard = load_single_file_dashboard()
    password = "networker-secret"
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password=password,
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

    session_id = dashboard.create_dashboard_session(config, CookieJar(), {"X-Old": "token"})
    session = dashboard.DASHBOARD_SESSIONS[session_id]

    assert session.config.password == ""
    assert session.encrypted_networker_password
    assert password not in session.encrypted_networker_password
    assert dashboard.decrypt_process_secret(session.encrypted_networker_password) == password


def test_session_refresh_reauthenticates_after_upstream_401(monkeypatch):
    dashboard = load_single_file_dashboard()
    config = dashboard.ApiConfig(
        rest_api_host="192.0.2.10",
        rest_api_port=9090,
        backup_server_host="198.51.100.11",
        backup_server_port=9090,
        username="admin",
        password="networker-secret",
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
    session_id = dashboard.create_dashboard_session(config, CookieJar(), {"X-Old": "token"})
    calls = []

    def fake_build(config, cookie_jar=None, auth_headers=None, create_session=True):
        calls.append((config.password, dict(auth_headers or {})))
        if len(calls) == 1:
            return 502, {
                "ok": False,
                "sources": {
                    "nwuiLogin": {"ok": True},
                    "monitoringActions": {"ok": False, "status": 401, "error": "expired"},
                },
            }
        return 200, {
            "ok": True,
            "generatedAt": "18-05-2026 10:00:00 Arabian Standard Time",
            "summary": {"health": "ok"},
            "sources": {"nwuiLogin": {"ok": True}, "monitoringActions": {"ok": True}},
            "tables": {"jobs": []},
        }

    monkeypatch.setattr(dashboard, "build_dashboard_nwui", fake_build)
    monkeypatch.setattr(dashboard, "nwui_login", lambda config, opener: ({"X-New": "token"}, {"status": 200}))

    status, body = dashboard.build_dashboard_from_session(session_id)

    assert status == 200
    assert body["sessionId"] == session_id
    assert calls[0][0] == "networker-secret"
    assert calls[0][1] == {"X-Old": "token"}
    assert calls[1][0] == "networker-secret"
    assert calls[1][1] == {"X-New": "token"}
    assert dashboard.DASHBOARD_SESSIONS[session_id].config.password == ""


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
    assert "Windows denied the account" in health["detail"]
    assert "root\\cimv2 Remote Enable" in health["detail"]
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


def test_wmi_local_target_omits_explicit_credentials(monkeypatch):
    dashboard = load_single_file_dashboard()
    captured = {}

    def completed_run(*args, **kwargs):
        encoded_index = args[0].index("-EncodedCommand") + 1
        script = dashboard.base64.b64decode(args[0][encoded_index]).decode("utf-16le")
        captured["script"] = script
        captured["payload"] = json.loads(kwargs["input"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "host": "localhost",
                    "cpuUsagePercent": 7,
                    "cpuSampleSeconds": 1,
                    "ramUsagePercent": 25,
                    "totalMemoryMb": 64 * 1024,
                    "freeMemoryMb": 48 * 1024,
                    "osCaption": "Windows Server 2019",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(dashboard.subprocess, "run", completed_run)

    config = dashboard.ApiConfig(
        rest_api_host="localhost",
        rest_api_port=9090,
        backup_server_host="localhost",
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

    assert health["cpuUsagePercent"] == 7
    assert captured["payload"]["isLocal"] is True
    assert captured["payload"]["useCredential"] is False
    assert "if ($payload.isLocal)" in captured["script"]


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


def test_server_protection_refresh_failure_keeps_card_detail_clean(monkeypatch):
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
    assert "refresh failed" not in second["detail"]
    assert second["_lastRefreshError"] == "REST API connection timed out."
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

    def fake_send(settings, subject, body, smtp_password, html_body="", inline_images=None, attachments=None):
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


def test_smtp_sender_reports_exact_login_failure_without_password(monkeypatch):
    dashboard = load_single_file_dashboard()

    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            self.host = host
            self.port = port
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def ehlo(self):
            return 250, b"ok"

        def starttls(self):
            return 220, b"ready"

        def login(self, username, password):
            raise dashboard.smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")

        def send_message(self, message):
            raise AssertionError("send_message should not run after failed login")

    monkeypatch.setattr(dashboard.smtplib, "SMTP", FakeSMTP)
    automation = dashboard.AlertAutomation(
        automation_id="smtp-test",
        session_id="smtp-test",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="svc",
        encrypted_smtp_password="",
        smtp_from="networker@example.com",
        recipients=["ops@example.com"],
        smtp_security="starttls",
        interval_minutes=15,
        trigger="warning",
        schedule_type="alert",
        report_time="08:00",
        created_at=0,
    )

    try:
        dashboard.send_smtp_email(automation, "Subject", "Body", "secret-password")
        assert False, "Expected SmtpDeliveryError"
    except dashboard.SmtpDeliveryError as exc:
        assert exc.stage == "login"
        assert exc.diagnostics["host"] == "smtp.example.com"
        assert exc.diagnostics["port"] == 587
        assert exc.diagnostics["security"] == "starttls"
        assert exc.diagnostics["usernameProvided"] is True
        assert exc.diagnostics["passwordProvided"] is True
        assert "Authentication failed" in exc.detail
        assert "secret-password" not in str(exc)
        assert "secret-password" not in str(exc.diagnostics)


def test_smtp_sender_attaches_dashboard_snapshot_file(monkeypatch):
    dashboard = load_single_file_dashboard()
    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            self.host = host
            self.port = port
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def ehlo(self):
            return 250, b"ok"

        def send_message(self, message):
            sent_messages.append(message)

    monkeypatch.setattr(dashboard.smtplib, "SMTP", FakeSMTP)
    automation = dashboard.AlertAutomation(
        automation_id="smtp-inline-test",
        session_id="smtp-inline-test",
        smtp_host="smtp.example.com",
        smtp_port=25,
        smtp_username="",
        encrypted_smtp_password="",
        smtp_from="networker@example.com",
        recipients=["ops@example.com"],
        smtp_security="none",
        interval_minutes=15,
        trigger="warning",
        schedule_type="daily_report",
        report_time="08:00",
        created_at=0,
    )

    result = dashboard.send_smtp_email(
        automation,
        "Subject",
        "Plain",
        "",
        "<html><body>Dashboard report</body></html>",
        attachments={"dashboard.png": (b"png-bytes", "image/png", "dashboard.png")},
    )

    assert result["stage"] == "sent"
    assert sent_messages
    message_text = sent_messages[0].as_string()
    assert "Content-ID:" not in message_text
    assert "Content-Disposition: attachment" in message_text
    assert "image/png" in message_text
    assert "dashboard.png" in message_text


def test_alert_automation_test_email_returns_smtp_debug_on_failure(monkeypatch):
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

    def fake_send(settings, subject, body, smtp_password, html_body="", inline_images=None, attachments=None):
        diagnostics = dashboard.smtp_debug_snapshot(settings, smtp_password, "login")
        raise dashboard.SmtpDeliveryError("login", "535 Authentication failed", diagnostics)

    monkeypatch.setattr(dashboard, "send_smtp_email", fake_send)

    status, body = dashboard.handle_alert_automation(
        {
            "action": "test",
            "sessionId": session_id,
            "smtpHost": "smtp.example.com",
            "smtpPort": "587",
            "smtpSecurity": "starttls",
            "smtpUsername": "svc",
            "smtpPassword": "special;password!",
            "smtpFrom": "networker@example.com",
            "smtpTo": "ops@example.com",
            "intervalMinutes": "15",
            "trigger": "warning",
            "scheduleType": "alert",
            "reportTime": "08:00",
        }
    )

    assert status == 502
    assert body["ok"] is False
    assert body["smtpDebug"]["stage"] == "login"
    assert body["smtpDebug"]["host"] == "smtp.example.com"
    assert body["smtpDebug"]["port"] == 587
    assert body["smtpDebug"]["passwordProvided"] is True
    assert "535 Authentication failed" in body["error"]
    assert "special;password!" not in json.dumps(body)


def test_daily_report_email_embeds_backup_status_and_sla():
    dashboard = load_single_file_dashboard()
    plain, html = dashboard.dashboard_report_email(
        {
            "generatedAt": "13-05-2026 09:00:00 Arabian Standard Time",
            "target": {"apiMode": "nwui", "backupServer": "198.51.100.11:9090"},
            "summary": {
                "rangeLabel": "Last 24 Hours",
                "totalJobs": 32,
                "successfulJobs": 31,
                "failedJobs": 1,
                "activeJobs": 1,
                "recoveryJobs": 0,
                "recoveryFailed": 0,
                "recoveryRunning": 0,
                "cloneJobs": 7,
                "cloneFailed": 0,
                "cloneRunning": 3,
                "cloneSessionTotal": 3032,
                "totalAlerts": 1,
                "slaPercent": 97,
                "slaMetJobs": 31,
                "slaTotalJobs": 32,
                "slaMissedJobs": 1,
                "health": "critical",
            },
            "serverHealth": {
                "status": "ok",
                "label": "Healthy",
                "detail": "Microsoft Windows Server 2019 Standard via WMI.",
                "cpuUsagePercent": 0,
                "cpuDetail": "Real-time WMI sample from 198.51.100.11 over 1s",
                "ramUsagePercent": 27,
                "ramUsedGb": 17.3,
                "ramFreeGb": 46.7,
                "ramTotalGb": 64,
            },
            "serverProtectionJob": {
                "status": "succeeded",
                "label": "Succeeded",
                "detail": "Server db backup on Server backup at 13-05-2026 10:00:01 Arabian Standard Time",
            },
            "theme": "midnight",
        }
    )

    assert "Total backup jobs: 32" in plain
    assert "Backup SLA: 97% (31 met / 32 total)" in plain
    assert "Memory usage: 17.3 / 64 GB" in plain
    assert "<table" in html
    assert "DELL EMC NetWorker" in html
    assert "Connected - action required" in html
    assert "Activity Mix" in html
    assert "Management Overview" in html
    assert "Recovery Health" in html
    assert "Clone Jobs" in html
    assert "NetWorker Server Health" in html
    assert "17.3 / 64 GB" in html
    assert "Server db backup on Server backup" in html
    assert "Successful Jobs" in html
    assert "background:#101719" in html
    assert "background:#172124" in html
    assert "#6fcf97" in html
    assert html.index("Activity Mix") < html.index("Backup SLA") < html.index("Management Overview")
    assert html.index("Clone Jobs") < html.index("Successful Jobs") < html.index("NetWorker Server Health")


def test_scheduled_snapshot_keeps_selected_theme_not_report_green():
    dashboard = load_single_file_dashboard()
    html = dashboard.dashboard_snapshot_html(
        {
            "generatedAt": "13-05-2026 09:00:00 Arabian Standard Time",
            "target": {"apiMode": "nwui", "backupServer": "198.51.100.11:9090"},
            "summary": {
                "rangeLabel": "Last 24 Hours",
                "totalJobs": 5,
                "successfulJobs": 5,
                "failedJobs": 0,
                "activeJobs": 0,
                "recoveryJobs": 0,
                "cloneJobs": 0,
                "totalAlerts": 0,
                "slaPercent": 100,
                "slaMetJobs": 5,
                "slaTotalJobs": 5,
                "slaMissedJobs": 0,
            },
            "serverHealth": {"status": "ok", "label": "Healthy"},
            "serverProtectionJob": {"status": "succeeded", "label": "Succeeded", "detail": "Server backup completed"},
            "theme": "ruby",
            "scheduledReport": True,
        }
    )

    assert "#9f2d55" in html
    assert "#003b24" not in html


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
    monkeypatch.setattr(dashboard, "render_dashboard_snapshot_png", lambda dashboard_payload: b"png-bytes")

    def fake_send(settings, subject, body, smtp_password, html_body="", inline_images=None, attachments=None):
        sent.append((subject, body, smtp_password, html_body, attachments or {}))

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
            "theme": "forest",
        }
    )
    assert status == 200
    assert "07:30" in body["message"]

    dashboard.run_alert_automation(dashboard.automation_key(session_id, "daily_report"))

    assert sent
    assert sent[0][0] == "NetWorker daily backup status and SLA report"
    assert "Backup SLA: 100% (10 met / 10 total)" in sent[0][1]
    assert sent[0][2] == "daily-secret"
    assert "<table" in sent[0][3]
    assert "Activity Mix" in sent[0][3]
    assert "Backup SLA" in sent[0][3]
    assert "background:#003b24" in sent[0][3]
    assert 'bgcolor="#003b24"' in sent[0][3]
    assert "max-width:60px" in sent[0][3]
    assert "background:#eef5ef" in sent[0][3]
    assert "#1f7a45" in sent[0][3]
    assert "cid:networker-dashboard-snapshot" not in sent[0][3]
    assert sent[0][4]["networker-dashboard.png"][0] == b"png-bytes"


def test_daily_report_skips_empty_email_when_backup_source_failed(monkeypatch):
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
                "ok": True,
                "generatedAt": "19-05-2026 07:30:07 Arabian Standard Time",
                "summary": {
                    "rangeLabel": "Last 24 Hours",
                    "totalJobs": 0,
                    "successfulJobs": 0,
                    "failedJobs": 0,
                    "activeJobs": 0,
                    "recoveryJobs": 0,
                    "cloneJobs": 0,
                    "totalAlerts": 0,
                    "slaPercent": 0,
                    "slaMetJobs": 0,
                    "slaTotalJobs": 0,
                    "slaMissedJobs": 0,
                    "health": "critical",
                },
                "serverHealth": {"status": "ok", "label": "Healthy"},
                "serverProtectionJob": {"status": "unknown", "label": "Not found"},
                "sources": {
                    "monitoringActions": {
                        "ok": False,
                        "path": "/nwui/api/monitoringactions",
                        "status": 500,
                        "error": "HTTP 500 NWUI POST failed; direct REST fallback also failed: HTTP 404",
                    }
                },
            },
        ),
    )
    monkeypatch.setattr(dashboard, "send_smtp_email", lambda *args, **kwargs: sent.append(args))
    monkeypatch.setattr(dashboard, "schedule_alert_automation", lambda automation: None)

    status, _ = dashboard.handle_alert_automation(
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
            "theme": "forest",
        }
    )

    assert status == 200
    dashboard.run_alert_automation(dashboard.automation_key(session_id, "daily_report"))

    assert sent == []
    assert "Daily report skipped because backup job data was unavailable" in dashboard.ALERT_AUTOMATIONS[
        dashboard.automation_key(session_id, "daily_report")
    ].last_result


def test_daily_report_uses_last_good_dashboard_when_scheduled_refresh_loses_job_source(monkeypatch):
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
    good_dashboard = {
        "ok": True,
        "generatedAt": "19-05-2026 07:20:00 Arabian Standard Time",
        "summary": {
            "rangeLabel": "Last 24 Hours",
            "totalJobs": 32,
            "successfulJobs": 31,
            "failedJobs": 1,
            "activeJobs": 0,
            "recoveryJobs": 0,
            "cloneJobs": 0,
            "totalAlerts": 1,
            "slaPercent": 97,
            "slaMetJobs": 31,
            "slaTotalJobs": 32,
            "slaMissedJobs": 1,
        },
        "sources": {"monitoringActions": {"ok": True, "path": "/nwui/api/monitoringactions", "count": 32}},
        "serverHealth": {"label": "Healthy"},
        "serverProtectionJob": {"label": "Succeeded", "detail": "Server backup completed"},
    }
    dashboard.set_shared_dashboard(session_id, good_dashboard)
    sent = []

    monkeypatch.setattr(
        dashboard,
        "build_dashboard_from_session",
        lambda session_id: (
            200,
            {
                "ok": True,
                "generatedAt": "19-05-2026 07:30:07 Arabian Standard Time",
                "summary": {"rangeLabel": "Last 24 Hours", "totalJobs": 0, "slaTotalJobs": 0, "health": "critical"},
                "sources": {
                    "monitoringActions": {
                        "ok": False,
                        "path": "/nwui/api/monitoringactions",
                        "status": 500,
                        "error": "HTTP 500 NWUI POST failed",
                    }
                },
            },
        ),
    )
    monkeypatch.setattr(dashboard, "render_dashboard_snapshot_png", lambda dashboard_payload: None)

    def fake_send(settings, subject, body, smtp_password, html_body="", inline_images=None, attachments=None):
        sent.append((subject, body, html_body))

    monkeypatch.setattr(dashboard, "send_smtp_email", fake_send)
    monkeypatch.setattr(dashboard, "schedule_alert_automation", lambda automation: None)

    status, _ = dashboard.handle_alert_automation(
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
            "theme": "forest",
        }
    )

    assert status == 200
    dashboard.run_alert_automation(dashboard.automation_key(session_id, "daily_report"))

    assert sent
    assert "Total backup jobs: 32" in sent[0][1]
    assert "Report notice:" in sent[0][1]
    assert "last successful dashboard snapshot" in sent[0][2]


def test_alert_and_daily_report_can_be_scheduled_together(monkeypatch):
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
    monkeypatch.setattr(dashboard, "schedule_alert_automation", lambda automation: None)

    common = {
        "sessionId": session_id,
        "smtpHost": "smtp.example.com",
        "smtpPort": "587",
        "smtpSecurity": "starttls",
        "smtpUsername": "svc",
        "smtpFrom": "networker@example.com",
        "smtpTo": "ops@example.com",
        "intervalMinutes": "15",
        "trigger": "warning",
        "reportTime": "07:30",
        "theme": "forest",
    }

    alert_status, alert_body = dashboard.handle_alert_automation(
        {**common, "action": "start", "scheduleType": "alert", "smtpPassword": "shared-secret"}
    )
    report_status, report_body = dashboard.handle_alert_automation(
        {**common, "action": "start", "scheduleType": "daily_report", "smtpPassword": ""}
    )

    alert_key = dashboard.automation_key(session_id, "alert")
    report_key = dashboard.automation_key(session_id, "daily_report")
    assert alert_status == 200
    assert report_status == 200
    assert alert_key in dashboard.ALERT_AUTOMATIONS
    assert report_key in dashboard.ALERT_AUTOMATIONS
    assert "Alerts every 15 minute" in report_body["activeAutomations"]
    assert "Daily dashboard report at 07:30" in report_body["activeAutomations"]
    assert dashboard.decrypt_process_secret(dashboard.ALERT_AUTOMATIONS[report_key].encrypted_smtp_password) == "shared-secret"
    assert "Alert automation scheduled" in alert_body["message"]

    stop_status, stop_body = dashboard.handle_alert_automation(
        {"action": "stop", "sessionId": session_id, "scheduleType": "daily_report"}
    )

    assert stop_status == 200
    assert report_key not in dashboard.ALERT_AUTOMATIONS
    assert alert_key in dashboard.ALERT_AUTOMATIONS
    assert "Alerts every 15 minute" in stop_body["activeAutomations"]


def test_daily_report_test_reuses_scheduled_smtp_password_and_snapshot(monkeypatch):
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

    monkeypatch.setattr(dashboard, "schedule_alert_automation", lambda automation: None)
    monkeypatch.setattr(
        dashboard,
        "build_dashboard_from_session",
        lambda session_id: (_ for _ in ()).throw(AssertionError("test should use supplied dashboard snapshot")),
    )
    monkeypatch.setattr(dashboard, "render_dashboard_snapshot_png", lambda dashboard_payload: b"test-png")

    def fake_send(settings, subject, body, smtp_password, html_body="", inline_images=None, attachments=None):
        sent.append((subject, smtp_password, html_body, attachments or {}))

    monkeypatch.setattr(dashboard, "send_smtp_email", fake_send)

    base_payload = {
        "sessionId": session_id,
        "smtpHost": "smtp.example.com",
        "smtpPort": "587",
        "smtpSecurity": "starttls",
        "smtpUsername": "svc",
        "smtpFrom": "networker@example.com",
        "smtpTo": "ops@example.com",
        "intervalMinutes": "60",
        "trigger": "all",
        "scheduleType": "daily_report",
        "reportTime": "07:30",
        "theme": "ruby",
    }
    status, _ = dashboard.handle_alert_automation({**base_payload, "action": "start", "smtpPassword": "saved-secret"})
    assert status == 200

    status, body = dashboard.handle_alert_automation(
        {
            **base_payload,
            "action": "test",
            "smtpPassword": "",
            "dashboard": {
                "generatedAt": "13-05-2026 09:00:00 Arabian Standard Time",
                "summary": {
                    "rangeLabel": "Last 24 Hours",
                    "totalJobs": 5,
                    "successfulJobs": 5,
                    "failedJobs": 0,
                    "activeJobs": 0,
                    "recoveryJobs": 0,
                    "cloneJobs": 0,
                    "totalAlerts": 0,
                    "slaPercent": 100,
                    "slaMetJobs": 5,
                    "slaTotalJobs": 5,
                    "slaMissedJobs": 0,
                },
                "serverHealth": {"label": "Healthy"},
                "serverProtectionJob": {"label": "Succeeded", "detail": "Server backup completed"},
            },
        }
    )

    assert status == 200
    assert body["message"] == "Test email sent."
    assert sent[-1][0] == "NetWorker daily backup status and SLA report - test"
    assert sent[-1][1] == "saved-secret"
    assert "Activity Mix" in sent[-1][2]
    assert "background:#003b24" in sent[-1][2]
    assert 'bgcolor="#003b24"' in sent[-1][2]
    assert "max-width:60px" in sent[-1][2]
    assert "background:#f8eef1" in sent[-1][2]
    assert "cid:networker-dashboard-snapshot" not in sent[-1][2]
    assert sent[-1][3]["networker-dashboard.png"][0] == b"test-png"


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
