"""NWUI API client, fallbacks, and the NWUI dashboard builder.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request

from . import config as _cfg
from . import job_status
from .config import (
    API_VERSION_CANDIDATES,
    APP_VERSION,
    MAX_JOBS_RESPONSE_BYTES,
    MAX_RESPONSE_BYTES,
    TABLE_LIMIT,
    debug_log,
    safe_log_text,
)
from .models import JOB_HISTORY_UNAVAILABLE, ApiConfig, RestApiError, generated_at
from .restapi import (
    add_sla_summary,
    api_base_url_for_version,
    api_opener,
    authc_header_value,
    build_headers,
    clean_networker_job_message,
    collection_from,
    compact_path_for_log,
    dashboard_endpoints,
    describe_http_error,
    describe_url_error,
    display_datetime,
    fetch_json,
    first_value,
    format_duration_seconds,
    invalid_rest_query_field,
    is_clone_job,
    is_failed_job,
    is_queued_job,
    is_recovery_job,
    is_running_job,
    is_success_job,
    job_history_endpoint,
    job_in_report_window,
    job_report_time,
    load_server_health_nwui,
    maintenance_backup_status,
    networker_log_category,
    networker_log_row,
    nwui_api_base_url,
    oldest_job_start,
    origin_url,
    project_client,
    read_limited,
    remove_rest_field_from_path,
    report_coverage,
    report_window,
    sort_jobs,
    ssl_context_for_api,
    status_text,
    stringify,
    strip_query_param,
    timestamp,
    with_failure_messages,
)
from .wmi_health import unavailable_server_health


def json_status_request(
    opener: Any,
    url: str,
    method: str,
    headers: dict[str, str],
    timeout: int,
    payload: Any | None = None,
) -> tuple[int, Any, str]:
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = read_limited(response, MAX_RESPONSE_BYTES)
            text = raw.decode(response.headers.get_content_charset() or "utf-8", "replace")
            if not text:
                return response.status, None, ""
            try:
                return response.status, json.loads(text), text
            except json.JSONDecodeError:
                return response.status, text, text
    except HTTPError as exc:
        raw = exc.read(8192)
        text = raw.decode("utf-8", "replace")
        try:
            data_obj: Any = json.loads(text)
        except json.JSONDecodeError:
            data_obj = text
        return exc.code, data_obj, text
    except (URLError, TimeoutError, ssl.SSLError) as exc:
        raise RestApiError(502, describe_url_error(exc)) from exc


def nwui_headers(config: ApiConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    origin = origin_url(config)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": f"{origin}/nwui/",
        "User-Agent": f"networker-dashboard/{APP_VERSION}",
    }
    if extra:
        headers.update(extra)
    return headers


def extract_nwui_list(data: Any, *keys: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    nested = data.get("data")
    if isinstance(nested, dict):
        return extract_nwui_list(nested, *keys)
    if isinstance(nested, list):
        return nested
    for value in data.values():
        if isinstance(value, list):
            return value
    return []


def unwrap_nwui_data(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data and len(data) <= 3:
        return data["data"]
    return data


def nwui_login(config: ApiConfig, opener: Any) -> tuple[dict[str, str], dict[str, Any]]:
    origin = origin_url(config)
    login_url = f"{origin}/nwui/api/login"
    headers = nwui_headers(config, {"Content-Type": "application/json"})
    auth_headers: dict[str, str] = {}
    payloads = [
        {"username": config.username, "pwd": config.password, "server": None, "port": None},
        {
            "username": config.username,
            "pwd": config.password,
            "server": config.backup_server_host,
            "port": config.backup_server_port,
        },
        {"username": config.username, "pwd": config.password},
    ]
    last_error = "NWUI login did not return a successful response."
    last_auth_status = 0
    auth_failures: list[str] = []
    for payload in payloads:
        keys = ",".join(payload.keys())
        debug_log(f"NWUI login try url={login_url} payloadKeys={keys}")
        status, data, body = json_status_request(
            opener,
            login_url,
            "POST",
            headers,
            config.timeout_seconds,
            payload,
        )
        debug_log(f"NWUI login result status={status} payloadKeys={keys}")
        if status in (200, 201):
            data_obj = data if isinstance(data, dict) else {}
            token = ""
            for key in ("token", "access_token", "Token", "accessToken", "auth_token", "authToken"):
                if data_obj.get(key):
                    token = str(data_obj[key])
                    break
            if token:
                auth_headers["Authorization"] = f"Bearer {token}"
            if not data_obj.get("errorCode") and not data_obj.get("errorMessage"):
                return auth_headers, {"status": status, "hasToken": bool(token)}
            last_error = stringify(data_obj.get("errorMessage") or data_obj.get("errorCode") or body, 260)
        elif status in (401, 403):
            detail = data.get("errorMessage") if isinstance(data, dict) else ""
            last_auth_status = status
            auth_failures.append(
                f"{keys}: {stringify(detail or 'NWUI login rejected this payload shape.', 180)}"
            )
            last_error = detail or "NWUI login failed. Check username/password and account access."
            continue
        elif status == 404:
            last_error = (
                "NWUI login endpoint /nwui/api/login was not found. Check that REST API server IP/port "
                "points to the NWUI host, or use NetWorker REST API mode."
            )
        else:
            last_error = describe_http_error(status, "NWUI login failed", body, login_url)
    if last_auth_status:
        detail = (
            f"{last_error} Tried {len(auth_failures)} NWUI login payload variant(s): "
            + " | ".join(auth_failures)
        )
        raise RestApiError(last_auth_status, safe_log_text(detail, 700))
    raise RestApiError(502, last_error)


def nwui_get_json(config: ApiConfig, opener: Any, auth_headers: dict[str, str], path: str) -> Any:
    url = f"{nwui_api_base_url(config)}/{path.lstrip('/')}"
    headers = nwui_headers(config, auth_headers)
    debug_log(f"NWUI GET path=/{path.lstrip('/')}")
    status, data, body = json_status_request(opener, url, "GET", headers, config.timeout_seconds)
    if status not in (200, 201):
        raise RestApiError(status, describe_http_error(status, "NWUI GET failed", body, url))
    return unwrap_nwui_data(data)


def nwui_post_json(
    config: ApiConfig,
    opener: Any,
    auth_headers: dict[str, str],
    path: str,
    payload: dict[str, Any],
) -> Any:
    url = f"{nwui_api_base_url(config)}/{path.lstrip('/')}"
    headers = nwui_headers(config, {**auth_headers, "Content-Type": "application/json"})
    debug_log(
        f"NWUI POST path=/{path.lstrip('/')} page={payload.get('pageNumber', '')} "
        f"limit={payload.get('pageLimit', '')}"
    )
    status, data, body = json_status_request(opener, url, "POST", headers, config.timeout_seconds, payload)
    if status not in (200, 201):
        raise RestApiError(status, describe_http_error(status, "NWUI POST failed", body, url))
    return unwrap_nwui_data(data)


def monitoring_payload(
    page: int,
    page_limit: int = 200,
    start_ts: float | None = None,
    end_ts: float | None = None,
    include_window: bool = True,
) -> dict[str, Any]:
    now = datetime.now().timestamp()
    start = start_ts if start_ts is not None else now - (30 * 24 * 60 * 60)
    end = end_ts if end_ts is not None else now
    payload = {
        "lastRun": False,
        "noRun": False,
        "pageNumber": page,
        "pageLimit": page_limit,
    }
    if include_window:
        payload["startTime"] = int(start * 1000)
        payload["endTime"] = int(end * 1000)
    return payload


def item_in_report_window(item: Any, start_ts: float | None, end_ts: float | None) -> bool:
    """Whether an NWUI item falls in [start_ts, end_ts): a finished job by
    when it ended (review C7), anything else by its own time."""
    if start_ts is None and end_ts is None:
        return True
    if not isinstance(item, dict):
        return True
    item_ts = job_report_time(item) or timestamp(
        first_value(item, "timestamp", "time", "createdTime", "lastRunTime")
    )
    if not item_ts:
        return True
    if start_ts is not None and item_ts < start_ts:
        return False
    return not (end_ts is not None and item_ts >= end_ts)


def filter_items_to_report_window(items: list[Any], start_ts: float | None, end_ts: float | None) -> list[Any]:
    return [item for item in items if item_in_report_window(item, start_ts, end_ts)]


# A safety stop for one NWUI list: a list that would need more pages than this
# is returned as far as it got, flagged as truncated.
NWUI_MAX_PAGES = 50
# HTTP errors that another page size or window cannot fix.
NWUI_FINAL_STATUSES = (401, 403, 404)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def nwui_monitoring_pages_with_strategy(
    config: ApiConfig,
    opener: Any,
    auth_headers: dict[str, str],
    endpoint_name: str,
    start_ts: float | None = None,
    end_ts: float | None = None,
    page_limit: int = 200,
    include_window: bool = True,
    paging: dict[str, Any] | None = None,
) -> list[Any]:
    """Every page of one NWUI monitoring list (review C8). When an answer
    gives its `totalCount`, pages are asked for until that many items are in;
    without one, a short page is the last. A first page shorter than asked,
    with more to come, is the server's own page size, and paging carries on
    at that size so the next page starts where this one ended.

    `paging`, when given, receives "totalCount" (0 when NWUI gave none) and
    "truncated": whether the list stopped before its end (an empty page
    before totalCount, or the NWUI_MAX_PAGES stop)."""
    all_items: list[Any] = []
    total = 0
    stopped_at_cap = False
    page = 1
    while True:
        if page > NWUI_MAX_PAGES:
            stopped_at_cap = True
            break
        data = nwui_post_json(
            config,
            opener,
            auth_headers,
            endpoint_name,
            monitoring_payload(page, page_limit, start_ts, end_ts, include_window=include_window),
        )
        items = extract_nwui_list(
            data,
            "policies",
            "workflows",
            "actions",
            "sessions",
            "alerts",
            "recoveries",
            "items",
            "data",
            "results",
            "rows",
            "content",
        )
        if isinstance(data, dict):
            total = _count(data.get("totalCount") or data.get("total") or data.get("totalItems")) or total
        if not items:
            break
        all_items.extend(items)
        if total:
            if len(all_items) >= total:
                break
            if page == 1 and len(items) < page_limit:
                page_limit = len(items)
        elif len(items) < page_limit:
            break
        page += 1
    truncated = len(all_items) < total if total else stopped_at_cap
    if truncated:
        debug_log(
            f"NWUI monitoring list truncated endpoint={endpoint_name} "
            f"items={len(all_items)} totalCount={total or 'unknown'} pages={page - 1}"
        )
    if paging is not None:
        paging["totalCount"] = total
        paging["truncated"] = truncated
    return all_items


def nwui_monitoring_all_pages(
    config: ApiConfig,
    opener: Any,
    auth_headers: dict[str, str],
    endpoint_name: str,
    start_ts: float | None = None,
    end_ts: float | None = None,
    paging: dict[str, Any] | None = None,
) -> list[Any]:
    """One NWUI monitoring list, trying smaller pages and then no window
    when NWUI refuses a request. Only an error no other request can fix
    (NWUI_FINAL_STATUSES) stops the retries (review C8). `paging` is as for
    nwui_monitoring_pages_with_strategy."""
    attempts: list[str] = []
    strategies = [
        (200, True),
        (100, True),
        (50, True),
        (100, False),
        (50, False),
    ]
    last_error: RestApiError | None = None
    for page_limit, include_window in strategies:
        strategy_name = f"pageLimit={page_limit},window={'on' if include_window else 'off'}"
        try:
            items = nwui_monitoring_pages_with_strategy(
                config,
                opener,
                auth_headers,
                endpoint_name,
                start_ts=start_ts,
                end_ts=end_ts,
                page_limit=page_limit,
                include_window=include_window,
                paging=paging,
            )
            if not include_window:
                items = filter_items_to_report_window(items, start_ts, end_ts)
                debug_log(
                    f"NWUI monitoring fallback succeeded endpoint={endpoint_name} "
                    f"strategy={strategy_name} filteredCount={len(items)}"
                )
            elif attempts:
                debug_log(f"NWUI monitoring retry succeeded endpoint={endpoint_name} strategy={strategy_name}")
            return items
        except RestApiError as exc:
            last_error = exc
            attempts.append(f"{strategy_name}: {exc.message}")
            if exc.status_code in NWUI_FINAL_STATUSES:
                break
            debug_log(f"NWUI monitoring retry endpoint={endpoint_name} strategy={strategy_name} error={exc.message}")
    if last_error:
        detail = " | ".join(attempts)
        raise RestApiError(last_error.status_code, f"{last_error.message} (retry attempts: {detail})") from last_error
    return []


# Short-TTL cache for the completed-job history pulled from the NetWorker jobs
# database. The /global/jobs response is large (NetWorker has no server-side
# time filter, so the whole retained set is returned — ~11 MB / thousands of
# jobs on a busy server) and barely changes between rapid refreshes. Without
# caching, every dashboard build — for every restored session and the shared
# refresh loop — re-downloads and re-parses it, starving the request workers
# and causing unrelated endpoints to time out. Cache keyed by server+range.
_JOBS_HISTORY_CACHE: dict[tuple[Any, ...], tuple[float, list[Any], str, float]] = {}
_JOBS_HISTORY_LOCK = threading.Lock()
JOBS_HISTORY_TTL_SECONDS = 180
JOBS_HISTORY_CACHE_MAX = 16


def cached_nwui_job_history(
    config: ApiConfig, context: ssl.SSLContext
) -> tuple[list[Any], str, bool, float]:
    """Return (items, path, from_cache, oldest_start) for the NetWorker
    completed-job history, served from a process-wide short-TTL cache shared
    across sessions and the shared refresh loop. oldest_start is the oldest job
    the jobs database still holds (0 when unknown), for report_coverage."""
    key = (
        str(config.backup_server_host or "").lower(),
        int(config.backup_server_port or 0),
        str(config.rest_api_host or "").lower(),
        int(config.rest_api_port or 0),
        str(config.username or ""),
        str(config.report_range or ""),
        str(config.custom_start_date or ""),
        str(config.custom_end_date or ""),
    )
    now = time.time()
    with _JOBS_HISTORY_LOCK:
        entry = _JOBS_HISTORY_CACHE.get(key)
        if entry and now - entry[0] < JOBS_HISTORY_TTL_SECONDS:
            return entry[1], entry[2], True, entry[3]
    retained: dict[str, float] = {}
    items, path = nwui_rest_fallback_items(config, "actions", context, retained=retained)
    oldest_start = retained.get("oldestStart", 0.0)
    with _JOBS_HISTORY_LOCK:
        _JOBS_HISTORY_CACHE[key] = (now, items, path, oldest_start)
        if len(_JOBS_HISTORY_CACHE) > JOBS_HISTORY_CACHE_MAX:
            for old_key in sorted(
                _JOBS_HISTORY_CACHE, key=lambda k: _JOBS_HISTORY_CACHE[k][0]
            )[:-JOBS_HISTORY_CACHE_MAX]:
                _JOBS_HISTORY_CACHE.pop(old_key, None)
    return items, path, False, oldest_start


# The REST API's client list, for NWUI mode's Clients figure (review C21). It
# does not depend on the report range, so it is cached per server.
_CLIENTS_CACHE: dict[tuple[Any, ...], tuple[float, list[Any], str]] = {}


def cached_nwui_clients(config: ApiConfig, context: ssl.SSLContext) -> tuple[list[Any], str, bool]:
    """Return (clients, path, from_cache): /global/clients from the REST API
    behind NWUI, cached like the job history."""
    key = (
        str(config.backup_server_host or "").lower(),
        int(config.backup_server_port or 0),
        str(config.rest_api_host or "").lower(),
        int(config.rest_api_port or 0),
        str(config.username or ""),
    )
    now = time.time()
    with _JOBS_HISTORY_LOCK:
        entry = _CLIENTS_CACHE.get(key)
        if entry and now - entry[0] < JOBS_HISTORY_TTL_SECONDS:
            return entry[1], entry[2], True
    items, path = nwui_rest_fallback_items(config, "clients", context)
    with _JOBS_HISTORY_LOCK:
        _CLIENTS_CACHE[key] = (now, items, path)
        if len(_CLIENTS_CACHE) > JOBS_HISTORY_CACHE_MAX:
            for old_key in sorted(_CLIENTS_CACHE, key=lambda k: _CLIENTS_CACHE[k][0])[:-JOBS_HISTORY_CACHE_MAX]:
                _CLIENTS_CACHE.pop(old_key, None)
    return items, path, False


def cmd_flag_value(command: str, flag: str) -> str:
    if not command:
        return ""
    match = re.search(r"\s-" + re.escape(flag) + r'\s+("[^"]+"|\S+)', " " + command)
    if not match:
        return ""
    return match.group(1).strip('"')


def parse_nwui_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return display_datetime(value)
    text = str(value)
    if text.isdigit() and len(text) >= 10:
        return display_datetime(text)
    return display_datetime(text).replace(".000", "")


def normalize_nwui_status(value: Any, failed_sessions: int = 0) -> str:
    """The job_status outcome of an NWUI status, the classifier REST mode uses
    too (review C5). A success with failed sessions is a warning."""
    status = job_status.classify_status(value)
    if status == job_status.SUCCEEDED and failed_sessions > 0:
        return job_status.WARNING
    return status


def project_nwui_job(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    job_data = item.get("jobData") if isinstance(item.get("jobData"), dict) else {}
    success = int(job_data.get("successfulInputCount") or 0)
    failed = int(job_data.get("failedInputCount") or 0)
    waiting = int(job_data.get("waitingInputCount") or 0)
    running = int(job_data.get("runningInputCount") or 0)
    canceled = int(job_data.get("canceledInputCount") or 0)
    total_sessions = success + failed + waiting + running + canceled
    command = str(item.get("jobCmd") or "")
    server_from_cmd = cmd_flag_value(command, "s")
    pool_from_cmd = cmd_flag_value(command, "b")
    group_from_cmd = cmd_flag_value(command, "g")
    start = parse_nwui_time(item.get("startTime"))
    try:
        duration_ms = int(float(item.get("duration") or 0))
    except (TypeError, ValueError):
        duration_ms = 0
    duration_seconds = int(duration_ms / 1000) if duration_ms else 0
    status = normalize_nwui_status(item.get("status"), failed)
    workflow = str(item.get("workflowName") or item.get("groupName") or server_from_cmd or "")
    action = str(item.get("actionName") or item.get("jobType") or item.get("name") or "")
    session_summary = (
        f"{total_sessions} sessions ({success} ok, {failed} failed, {running} running, {waiting} waiting, {canceled} canceled)"
        if total_sessions
        else ""
    )
    return {
        "client": workflow,
        "name": action,
        "policy": str(item.get("policyName") or ""),
        "status": status,
        "started": start,
        # `started` is the dd-mm-YYYY display string; order jobs by this.
        "_started_ts": timestamp(item.get("startTime")),
        "duration": format_duration_seconds(duration_seconds),
        "message": clean_networker_job_message(
            first_value(item, "jobOutput", "message", "statusMessage", "errorMessage"),
            session_summary,
            workflow,
        ),
        "_workflow": workflow,
        "_group": str(item.get("groupName") or group_from_cmd or ""),
        "_pool": pool_from_cmd,
        "_server": server_from_cmd,
        # The client host of a jobs-database record; NWUI's own actions are
        # per workflow and name none.
        "_client_host": str(item.get("clientHostname") or ""),
        "_sessions": {
            "success": success,
            "failed": failed,
            "waiting": waiting,
            "running": running,
            "canceled": canceled,
            "total": total_sessions,
        },
        "_save_set": session_summary,
        "_action_type": str(item.get("policyActionName") or item.get("jobType") or action or ""),
    }


def nwui_job_table_row(job: dict[str, Any]) -> dict[str, str]:
    return {
        "client": str(job.get("client") or ""),
        "name": str(job.get("name") or ""),
        "policy": str(job.get("policy") or ""),
        "status": str(job.get("status") or ""),
        "started": str(job.get("started") or ""),
        "duration": str(job.get("duration") or ""),
        "message": str(job.get("message") or job.get("_save_set") or ""),
    }


def nwui_job_log_row(job: dict[str, Any]) -> dict[str, str]:
    message = str(job.get("message") or job.get("_save_set") or job.get("name") or "")
    return networker_log_row(
        message,
        job.get("started"),
        job.get("status"),
        networker_log_category(message, "policy"),
        "event",
    )


def project_nwui_recovery(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        return {
            "client": "",
            "name": "Restore",
            "policy": "",
            "status": "unknown",
            "started": "",
            "duration": "",
            "message": "",
        }
    return {
        "client": str(item.get("clientName") or item.get("client") or item.get("hostname") or ""),
        "name": str(item.get("recoverType") or item.get("type") or "Restore"),
        "policy": str(item.get("policyName") or item.get("policy") or ""),
        "status": normalize_nwui_status(item.get("status") or item.get("state")),
        "started": parse_nwui_time(item.get("startTime") or item.get("start")),
        "duration": str(item.get("durationSeconds") or item.get("duration") or ""),
        "message": str(item.get("saveSet") or item.get("path") or item.get("message") or ""),
    }


def rest_job_as_nwui_action(job: Any) -> dict[str, Any]:
    status = status_text(job)
    success = 1 if is_success_job(job) else 0
    failed = 1 if is_failed_job(job) else 0
    running = 1 if is_running_job(job) else 0
    waiting = 1 if is_queued_job(job) else 0
    return {
        # The NetWorker job id, under a name the NWUI feed never uses: the
        # history merge keys on it (review C1).
        "restJobId": first_value(job, "id"),
        "clientHostname": first_value(job, "clientHostname", "client", "hostname"),
        "startTime": first_value(job, "startTime", "started", "start"),
        "duration": first_value(job, "elapsedTime", "duration", "elapsed"),
        "status": status,
        "workflowName": first_value(job, "clientHostname", "client", "hostname", "workflowName"),
        # policyActionName is the NetWorker action TYPE (backup/clone/...), the
        # same thing the live monitoringactions feed exposes as actionName. It
        # must take priority over the job `name` (often a save-set string) so
        # clone/recovery jobs are classified correctly after projection.
        "actionName": first_value(job, "policyActionName", "actionName", "name"),
        "policyActionName": first_value(job, "policyActionName"),
        "policyName": first_value(job, "policyName", "policy", "workflowName", "protectionPolicyName"),
        "message": first_value(job, "message", "messages", "statusMessage", "errorMessage"),
        "jobData": {
            "successfulInputCount": success,
            "failedInputCount": failed,
            "runningInputCount": running,
            "waitingInputCount": waiting,
            "canceledInputCount": 0,
        },
    }


def action_dedup_key(item: Any) -> tuple[str, str, int] | None:
    """Stable identity for a workflow-action run, used to merge the live
    monitoringactions feed with completed jobs from the NetWorker jobs DB.
    Normalizes startTime to epoch seconds so ISO/epoch format differences
    between the two sources collapse to the same key."""
    if not isinstance(item, dict):
        return None
    workflow = str(item.get("workflowName") or item.get("groupName") or "").strip().lower()
    action = str(item.get("actionName") or item.get("jobType") or item.get("name") or "").strip().lower()
    start = int(timestamp(first_value(item, "startTime", "started", "start")) or 0)
    if not workflow and not action and not start:
        return None
    return (workflow, action, start)


def rest_job_key(item: Any) -> tuple[str, str] | None:
    """Identity of a jobs-database record that carries its NetWorker job id."""
    if not isinstance(item, dict) or item.get("restJobId") in (None, ""):
        return None
    return ("job", str(item["restJobId"]))


def merge_action_history(live: list[Any], history: list[Any]) -> list[Any]:
    """Merge live monitoringactions (running set) with completed job history.
    When the same run appears in both, prefer the terminal (completed) record
    over the live "running" one so finished jobs are counted correctly.

    Jobs-database records are the same job only when their job ids match
    (review C1). Several save sets of one client start in the same second, so
    keying them on action_dedup_key kept one of them and a failure could
    vanish. A live NWUI record has no job id: it still matches on
    action_dedup_key, and pairs with one record only."""
    by_key: dict[tuple[Any, ...], Any] = {}
    extras: list[Any] = []
    for item in live:
        key = rest_job_key(item) or action_dedup_key(item)
        if key is None:
            extras.append(item)
            continue
        by_key[key] = item
    for item in history:
        run_key = action_dedup_key(item)
        key = rest_job_key(item) or run_key
        if key is None:
            extras.append(item)
            continue
        existing = by_key.get(key)
        if existing is None and key != run_key and run_key in by_key:
            existing = by_key.pop(run_key)
            by_key[key] = existing
        if existing is None:
            by_key[key] = item
            continue
        existing_running = normalize_nwui_status(existing.get("status")) in ("running", "queued")
        incoming_running = normalize_nwui_status(item.get("status")) in ("running", "queued")
        if existing_running and not incoming_running:
            by_key[key] = item
    return list(by_key.values()) + extras


def rest_fallback_versions(config: ApiConfig) -> tuple[str, ...]:
    if config.api_version != "auto":
        return (config.api_version,)
    return API_VERSION_CANDIDATES


def nwui_rest_fallback_configs(config: ApiConfig) -> list[ApiConfig]:
    candidates: list[ApiConfig] = []
    seen: set[tuple[str, int]] = set()

    def add(host: str, port: int) -> None:
        key = (str(host or "").lower(), int(port))
        if not key[0] or key in seen:
            return
        seen.add(key)
        candidates.append(replace(config, rest_api_host=host, rest_api_port=port))

    # In NWUI mode the login/API host can be the NWUI front end, while
    # /nwrestapi is often exposed by the actual NetWorker/AuthC server.
    add(config.backup_server_host, config.backup_server_port)
    add(config.rest_api_host, config.rest_api_port)
    return candidates


def nwui_failure_records(
    config: ApiConfig,
    version: str,
    headers: dict[str, str],
    context: ssl.SSLContext,
    removed_fields: set[str],
) -> list[Any]:
    """The jobs database's completionStatus:"Failed" records with their
    `message`, the failure text the bulk history query leaves out (review
    C9). Optional: on any error the history goes without it."""
    path = dashboard_endpoints(config)["failedJobs"]
    for field in removed_fields:
        path = remove_rest_field_from_path(path, field)
    endpoint_host = authc_header_value(config.rest_api_host, config.rest_api_port)
    try:
        data = fetch_json(
            api_base_url_for_version(config, version) + path,
            headers,
            max(config.timeout_seconds, 120),
            context,
            f"nwuiFallback:failedJobs:{endpoint_host}:{version}",
            max_bytes=MAX_JOBS_RESPONSE_BYTES,
        )
    except RestApiError as exc:
        debug_log(f"NWUI failed-job text unavailable host={endpoint_host} version={version} error={exc.message}")
        return []
    return collection_from(data, "jobs")


def nwui_rest_fallback_items(
    config: ApiConfig,
    target: str,
    context: ssl.SSLContext,
    retained: dict[str, float] | None = None,
) -> tuple[list[Any], str]:
    """Items for `target` from the REST API. For "actions", `retained` (when
    given) receives "oldestStart": the oldest job in the whole jobs database,
    before the report-window filter."""
    if not config.password:
        raise RestApiError(502, "Direct REST fallback needs the current login password; reconnect to refresh this source.")
    paths = dashboard_endpoints(config)
    source_name = {"actions": "jobs", "clients": "clients"}.get(target, "policies")
    original_path = job_history_endpoint() if target == "actions" else paths[source_name]
    last_error: RestApiError | None = None
    attempts: list[str] = []
    for fallback_config in nwui_rest_fallback_configs(config):
        headers = build_headers(fallback_config)
        endpoint_host = authc_header_value(fallback_config.rest_api_host, fallback_config.rest_api_port)
        for version in rest_fallback_versions(fallback_config):
            path = original_path
            removed_fields: set[str] = set()
            query_stripped = False
            url = api_base_url_for_version(fallback_config, version) + path
            while True:
                try:
                    # The jobs database has no server-side time filter and can be
                    # large on busy servers; allow a higher response ceiling and a
                    # longer read timeout for it than for small resources.
                    is_jobs = target == "actions"
                    fetch_timeout = max(fallback_config.timeout_seconds, 120) if is_jobs else fallback_config.timeout_seconds
                    fetch_max_bytes = MAX_JOBS_RESPONSE_BYTES if is_jobs else MAX_RESPONSE_BYTES
                    data = fetch_json(
                        url,
                        headers,
                        fetch_timeout,
                        context,
                        f"nwuiFallback:{source_name}:{endpoint_host}:{version}",
                        max_bytes=fetch_max_bytes,
                    )
                    preferred_key = source_name
                    items = collection_from(data, preferred_key)
                    if target == "actions":
                        if _cfg.APP_DEBUG:
                            total_raw = len(items)
                            raw_completion = Counter(
                                str(job.get("completionStatus") or "").lower()
                                for job in items
                                if isinstance(job, dict)
                            )
                            debug_log(
                                f"REST jobs raw diagnostic source={source_name} version={version} "
                                f"totalRaw={total_raw} completionStatus={dict(raw_completion)}"
                            )
                            for idx, job in enumerate(items[:3]):
                                if isinstance(job, dict):
                                    fields = {k: job.get(k) for k in sorted(job.keys())}
                                    debug_log(
                                        f"REST jobs raw sample[{idx}] keys={sorted(job.keys())} "
                                        f"values={safe_log_text(json.dumps(fields, default=str), 900)}"
                                    )
                        # Filter to the report window FIRST (cheap timestamp
                        # check), then sort and project only the survivors. The
                        # jobs DB can hold tens of thousands of records; sorting
                        # and converting the whole set wastes CPU on a busy server.
                        if retained is not None:
                            retained["oldestStart"] = oldest_job_start(items)
                        in_window = [job for job in items if job_in_report_window(job, config)]
                        if any(is_failed_job(job) for job in in_window):
                            in_window = with_failure_messages(
                                in_window,
                                nwui_failure_records(fallback_config, version, headers, context, removed_fields),
                            )
                        items = [rest_job_as_nwui_action(job) for job in sort_jobs(in_window)]
                    return items, f"https://{endpoint_host}/nwrestapi/{version}{compact_path_for_log(path)}"
                except RestApiError as exc:
                    invalid_field = invalid_rest_query_field(exc.message, exc.body)
                    if (
                        target in ("actions", "clients")
                        and exc.status_code == 400
                        and invalid_field
                        and invalid_field not in removed_fields
                    ):
                        next_path = remove_rest_field_from_path(path, invalid_field)
                        if next_path != path:
                            removed_fields.add(invalid_field)
                            attempts.append(
                                f"{endpoint_host}/{version}: removed unsupported field {invalid_field}"
                            )
                            debug_log(
                                f"NWUI REST fallback retry source={source_name} host={endpoint_host} "
                                f"version={version} removedField={invalid_field}"
                            )
                            path = next_path
                            url = api_base_url_for_version(fallback_config, version) + path
                            continue
                    # If the server rejected the server-side time-window query
                    # (NQL syntax not supported on this version), drop the `q`
                    # filter once and retry unfiltered so smaller deployments
                    # still return data. On busy servers this may then hit the
                    # response-size guard, which is reported as a normal error.
                    if (
                        exc.status_code == 400
                        and not query_stripped
                        and "q=" in path
                    ):
                        next_path = strip_query_param(path, "q")
                        if next_path != path:
                            query_stripped = True
                            attempts.append(
                                f"{endpoint_host}/{version}: dropped time-window query after HTTP 400"
                            )
                            debug_log(
                                f"NWUI REST fallback retry source={source_name} host={endpoint_host} "
                                f"version={version} droppedTimeWindowQuery=1"
                            )
                            path = next_path
                            url = api_base_url_for_version(fallback_config, version) + path
                            continue
                    last_error = exc
                    attempts.append(f"{endpoint_host}/{version}: {safe_log_text(exc.message, 180)}")
                    if exc.status_code in (401, 403):
                        break
                    break
    if last_error:
        if attempts:
            raise RestApiError(
                last_error.status_code,
                f"{last_error.message} (direct REST fallback attempts: {' | '.join(attempts)})",
                last_error.body,
            ) from last_error
        raise last_error
    raise RestApiError(502, f"Direct REST fallback for {source_name} did not return data.")


def session_counts_for_jobs(jobs: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"total": 0, "failed": 0, "running": 0}
    for job in jobs:
        counts = job.get("_sessions") or {}
        totals["total"] += int(counts.get("total") or 0)
        totals["failed"] += int(counts.get("failed") or 0)
        totals["running"] += int(counts.get("running") or 0)
    return totals


def nwui_backup_activity_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "completed": 0,
        "successful": 0,
        "failed": 0,
        "active": 0,
        "warnings": 0,
    }
    for job in jobs:
        counts = job.get("_sessions") or {}
        success = int(counts.get("success") or 0)
        failed = int(counts.get("failed") or 0)
        canceled = int(counts.get("canceled") or 0)
        running = int(counts.get("running") or 0)
        waiting = int(counts.get("waiting") or 0)
        session_total = success + failed + canceled + running + waiting
        if session_total:
            failed_total = failed + canceled
            totals["successful"] += success
            totals["failed"] += failed_total
            totals["active"] += running + waiting
            totals["completed"] += success + failed_total
            if str(job.get("status") or "").lower() == "warning" and not failed_total:
                totals["warnings"] += 1
            continue

        status = str(job.get("status") or "").lower()
        if status == "succeeded":
            totals["successful"] += 1
            totals["completed"] += 1
        elif status == "failed":
            totals["failed"] += 1
            totals["completed"] += 1
        elif status == "warning":
            totals["warnings"] += 1
            totals["completed"] += 1
        elif status in ("running", "queued"):
            totals["active"] += 1
    return totals


def project_nwui_alert(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        return {"severity": "info", "time": "", "message": "", "resource": ""}
    return {
        "severity": str(item.get("severity") or item.get("level") or "info").lower(),
        "time": str(item.get("timestamp") or item.get("time") or ""),
        "message": str(item.get("message") or item.get("description") or "")[:260],
        "resource": str(item.get("source") or item.get("category") or item.get("name") or ""),
    }


def build_nwui_clients(jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Client rows from the jobs of the range, for when the REST API's client
    list is out of reach: one per client host that ran a job. Not per
    workflow: a workflow name is not a client (review C21)."""
    by_host: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = job.get("_client_host") or ""
        if not name:
            continue
        row = by_host.setdefault(
            name,
            {
                "hostname": name,
                "enabled": "Yes",
                "backupType": job.get("name", ""),
                "saveSets": 0,
                "protectionGroups": set(),
                "_failed": 0,
            },
        )
        sessions = job.get("_sessions") or {}
        row["saveSets"] += int(sessions.get("total") or 0)
        row["_failed"] += int(sessions.get("failed") or 0)
        if job.get("_group"):
            row["protectionGroups"].add(job["_group"])
    output = []
    for row in by_host.values():
        groups = ", ".join(sorted(row["protectionGroups"]))
        output.append(
            {
                "hostname": str(row["hostname"]),
                "enabled": "Warnings" if row["_failed"] else "Yes",
                "backupType": str(row["backupType"]),
                "saveSets": f"{row['saveSets']} sessions",
                "protectionGroups": groups,
            }
        )
    return sorted(output, key=lambda item: item["hostname"])


def merge_recoveries(monitored: list[Any], restores: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Recovery rows, newest first, from NWUI's recoveries monitor and the
    restores the jobs database holds, each restore once (review C4). A
    restore both list (same client, same start second) keeps the jobs
    database's record, which has its final status."""
    by_run: dict[tuple[str, int], tuple[float, dict[str, str]]] = {}
    unkeyed: list[tuple[float, dict[str, str]]] = []
    for item in monitored:
        started = timestamp(item.get("startTime") or item.get("start"))
        client = str(item.get("clientName") or item.get("client") or item.get("hostname") or "").strip().lower()
        row = (started, project_nwui_recovery(item))
        if started:
            by_run[(client, int(started))] = row
        else:
            unkeyed.append(row)
    for job in restores:
        started = float(job.get("_started_ts") or 0)
        row = (started, nwui_job_table_row(job))
        if started:
            by_run[(str(job.get("client") or "").strip().lower(), int(started))] = row
        else:
            unkeyed.append(row)
    rows = sorted([*by_run.values(), *unkeyed], key=lambda pair: pair[0], reverse=True)
    return [row for _, row in rows]


def newest_first(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Projected jobs by start time, newest first. Not by `started`: as a
    dd-mm-YYYY string it put the highest day of the month first (review C2)."""
    return sorted(jobs, key=lambda job: job.get("_started_ts") or 0, reverse=True)


def build_nwui_policies(policy_items: list[Any], jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
    stats: dict[str, dict[str, Any]] = {}
    for job in jobs:
        policy = job.get("policy") or ""
        if not policy:
            continue
        row = stats.setdefault(policy, {"actions": 0, "workflows": set(), "last": "", "last_ts": -1.0, "status": "unknown"})
        row["actions"] += 1
        if job.get("_workflow"):
            row["workflows"].add(job["_workflow"])
        started_ts = float(job.get("_started_ts") or 0)
        if job.get("started") and started_ts > row["last_ts"]:
            row["last"] = str(job.get("started") or "")
            row["last_ts"] = started_ts
            row["status"] = job.get("status") or "unknown"

    output = []
    for item in policy_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("policyName") or "")
        stat = stats.get(name, {})
        output.append(
            {
                "severity": str(item.get("status") or stat.get("status") or "active").lower(),
                "time": str(item.get("lastRun") or item.get("lastRunTime") or stat.get("last") or ""),
                "message": (
                    f"Policy {name}: {item.get('workflowCount') or len(stat.get('workflows', [])) or 0} workflows, "
                    f"{item.get('actionCount') or stat.get('actions') or 0} actions"
                ),
                "resource": name,
            }
        )
    if not output:
        for name, stat in stats.items():
            output.append(
                {
                    "severity": str(stat.get("status") or "unknown"),
                    "time": str(stat.get("last") or ""),
                    "message": f"Policy {name}: {len(stat.get('workflows', []))} workflows, {stat.get('actions', 0)} actions",
                    "resource": name,
                }
            )
    return sorted(output, key=lambda item: item["resource"])


def base_server_protection_detail(detail: Any) -> str:
    text = str(detail or "Last known Server Protection job")
    marker = " (last known"
    if marker in text:
        text = text.split(marker, 1)[0]
    return text.strip() or "Last known Server Protection job"


def last_known_server_protection(
    previous: dict[str, Any],
    refresh_error: str = "",
) -> dict[str, Any]:
    detail = f"{base_server_protection_detail(previous.get('detail'))} (last known)"
    return {
        **previous,
        "detail": detail,
        "_baseDetail": base_server_protection_detail(previous.get("detail") or previous.get("_baseDetail")),
        "_lastRefreshError": refresh_error,
    }


def refresh_server_protection_job_nwui(
    config: ApiConfig,
    cookie_jar: CookieJar,
    auth_headers: dict[str, str],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = ssl_context_for_api(config.verify_tls)
    opener = api_opener(context, HTTPCookieProcessor(cookie_jar))
    now = time.time()
    lookback_start = now - (7 * 24 * 60 * 60)
    try:
        items = nwui_monitoring_all_pages(
            replace(config, api_mode="nwui"),
            opener,
            auth_headers,
            "monitoringactions",
            start_ts=lookback_start,
            end_ts=now,
        )
        jobs = [job for job in (project_nwui_job(item) for item in items) if job]
        jobs = newest_first(jobs)
        status = maintenance_backup_status(jobs)
        if status.get("count"):
            status["_baseDetail"] = base_server_protection_detail(status.get("detail"))
            status["_lastRefreshError"] = ""
            return status
        if previous and previous.get("count"):
            return last_known_server_protection(previous)
        return status
    except RestApiError as exc:
        if previous and previous.get("count"):
            return last_known_server_protection(previous, exc.message)
        return {
            "status": "unknown",
            "label": "Unavailable",
            "detail": f"Server Protection refresh failed: {exc.message}",
            "count": 0,
        }




def build_dashboard_nwui(
    config: ApiConfig,
    cookie_jar: CookieJar | None = None,
    auth_headers: dict[str, str] | None = None,
    create_session: bool = True,
) -> tuple[int, dict[str, Any]]:
    from .sessions import create_dashboard_session  # late import: avoids circular module import
    context = ssl_context_for_api(config.verify_tls)
    sources: dict[str, dict[str, Any]] = {}
    backup_target = authc_header_value(config.backup_server_host, config.backup_server_port)
    debug_log(
        "NWUI dashboard request "
        f"apiBase={nwui_api_base_url(config)} "
        f"networkerServer={backup_target} "
        f"verifyTls={config.verify_tls} timeout={config.timeout_seconds}s"
    )

    cookie_jar = cookie_jar or CookieJar()
    opener = api_opener(context, HTTPCookieProcessor(cookie_jar))

    should_login = auth_headers is None
    login_info: dict[str, Any] = {"status": "reused", "hasToken": bool(auth_headers)}
    if auth_headers is None:
        auth_headers = {}
    try:
        if should_login:
            auth_headers, login_info = nwui_login(config, opener)
            source_path = "/nwui/api/login"
        else:
            source_path = "volatile-session"
        sources["nwuiLogin"] = {
            "ok": True,
            "path": source_path,
            "count": 1,
            "detail": f"Networker server target {backup_target}",
        }
    except RestApiError as exc:
        sources["nwuiLogin"] = {
            "ok": False,
            "path": "/nwui/api/login",
            "status": exc.status_code,
            "error": exc.message,
        }
        body = {
            "ok": False,
            "generatedAt": generated_at(),
            "target": {
                "restApiBase": nwui_api_base_url(config),
                "apiMode": "nwui",
                "backupServer": backup_target,
                "authcHeaderEnabled": False,
                "verifyTls": config.verify_tls,
                "reportRange": config.report_range,
            },
            "summary": {"health": "critical"},
            "serverHealth": unavailable_server_health("NWUI login failed before server health could be checked."),
            "serverProtectionJob": maintenance_backup_status([]),
            "maintenanceBackup": maintenance_backup_status([]),
            "sources": sources,
            "tables": {"jobs": [], "failedJobs": [], "recovery": [], "cloneJobs": [], "logs": [], "alerts": [], "clients": []},
            "error": exc.message,
        }
        return exc.status_code if exc.status_code in (401, 403) else 502, body

    raw_actions: list[Any] = []
    raw_policies: list[Any] = []
    raw_alerts: list[Any] = []
    raw_recoveries: list[Any] = []
    start_ts, end_ts, range_label = report_window(config)
    for source_name, endpoint_name, target in (
        ("monitoringActions", "monitoringactions", "actions"),
        ("monitoringPolicies", "monitoringpolicies", "policies"),
        ("monitoringAlerts", "monitoringalerts", "alerts"),
        ("monitoringRecoveries", "monitoringrecoveries", "recoveries"),
    ):
        try:
            paging: dict[str, Any] = {}
            items = nwui_monitoring_all_pages(
                config,
                opener,
                auth_headers,
                endpoint_name,
                start_ts=start_ts,
                end_ts=end_ts,
                paging=paging,
            )
            sources[source_name] = {"ok": True, "path": f"/nwui/api/{endpoint_name}", "count": len(items)}
            if paging.get("truncated"):
                listed = f"{paging['totalCount']}" if paging.get("totalCount") else "more"
                sources[source_name].update({
                    "truncated": True,
                    "totalCount": paging.get("totalCount") or None,
                    "detail": f"NWUI returned {len(items)} of {listed} items; the list is incomplete.",
                })
            if target == "actions":
                raw_actions = items
            elif target == "policies":
                raw_policies = items
            elif target == "alerts":
                raw_alerts = items
            elif target == "recoveries":
                raw_recoveries = items
        except RestApiError as exc:
            if target in ("actions", "policies"):
                try:
                    items, fallback_path = nwui_rest_fallback_items(config, target, context)
                    sources[source_name] = {
                        "ok": True,
                        "path": fallback_path,
                        "count": len(items),
                        "detail": f"Used direct REST fallback after /nwui/api/{endpoint_name} returned HTTP {exc.status_code}.",
                    }
                    if target == "actions":
                        raw_actions = items
                    else:
                        raw_policies = items
                    continue
                except RestApiError as fallback_exc:
                    if target == "policies":
                        sources[source_name] = {
                            "ok": True,
                            "path": f"/nwui/api/{endpoint_name}",
                            "count": 0,
                            "detail": (
                                "Optional policy summary unavailable; dashboard continues without policy rows. "
                                f"NWUI error: {safe_log_text(exc.message, 220)}; REST fallback: {safe_log_text(fallback_exc.message, 220)}"
                            ),
                        }
                        raw_policies = []
                        continue
                    exc = RestApiError(
                        exc.status_code,
                        f"{exc.message}; direct REST fallback also failed: {fallback_exc.message}",
                    )
            sources[source_name] = {
                "ok": False,
                "path": f"/nwui/api/{endpoint_name}",
                "status": exc.status_code,
                "error": exc.message,
                "userMessage": (
                    "Backup activity source is temporarily unavailable; server health and cached local snapshot data remain visible."
                    if target == "actions"
                    else "Source is temporarily unavailable."
                ),
            }

    # /nwui/api/monitoringactions is the LIVE activity monitor: it returns only
    # the currently-active workflow actions (status="Running"), not completed
    # historical runs, and it ignores the requested time window. Completed jobs
    # for the selected range live in the NetWorker jobs database, reachable via
    # nwrestapi /global/jobs. Merge that history in so finished backups show up.
    # A failure here must never break the live dashboard, but it must show:
    # without the history every completed count is zero, which reads as a quiet,
    # healthy range (review C10). So it is a warning source, never diagnostic
    # only, and a missing password (one that could not be decrypted) counts too.
    live_action_count = len(raw_actions)
    history_action_count = 0
    history_from_cache = False
    history_available = False
    oldest_retained = 0.0
    try:
        rest_history, history_path, history_from_cache, oldest_retained = cached_nwui_job_history(config, context)
        # Keep only completed/terminal runs (succeeded/failed/warning) from
        # the jobs DB. This drops running/queued (the live monitor already
        # provides those) and status-less records (empty completionStatus),
        # which are not real completed backups and would otherwise inflate
        # the totals as "unknown".
        rest_history = [
            item
            for item in rest_history
            if normalize_nwui_status(item.get("status")) in ("succeeded", "failed", "warning")
        ]
        history_action_count = len(rest_history)
        raw_actions = merge_action_history(raw_actions, rest_history)
        history_available = True
        sources["monitoringActionsHistory"] = {
            "ok": True,
            "path": history_path,
            "count": history_action_count,
            "cached": history_from_cache,
            "detail": (
                "Completed job history merged from the NetWorker jobs database"
                + (" (cached)." if history_from_cache else ".")
            ),
        }
    except RestApiError as exc:
        sources["monitoringActionsHistory"] = {
            "ok": False,
            "path": "/nwrestapi/global/jobs",
            "status": exc.status_code,
            "error": safe_log_text(exc.message, 300),
            "userMessage": f"{JOB_HISTORY_UNAVAILABLE} Live backup activity is still shown.",
            "severity": "warning",
        }

    # The Clients figure counts NetWorker's clients (review C21). It asks the
    # REST API that just answered for the history; without it, the client
    # hosts that ran a job stand in.
    rest_clients: list[Any] | None = None
    if history_available:
        try:
            rest_clients, clients_path, clients_from_cache = cached_nwui_clients(config, context)
            sources["clients"] = {
                "ok": True,
                "path": clients_path,
                "count": len(rest_clients),
                "cached": clients_from_cache,
            }
        except RestApiError as exc:
            sources["clients"] = {
                "ok": False,
                "path": "/nwrestapi/global/clients",
                "status": exc.status_code,
                "error": safe_log_text(exc.message, 300),
                "detail": "Clients are the client hosts that ran a job in this range.",
                "severity": "info",
                "diagnosticOnly": True,
            }

    jobs = [job for job in (project_nwui_job(item) for item in raw_actions) if job]
    jobs = newest_first(jobs)
    clone_jobs = [job for job in jobs if is_clone_job(job)]
    other_jobs = [job for job in jobs if not is_clone_job(job)]
    # The jobs database lists restores too. They are counted once, as
    # restores: not also as backups in the totals and the SLA (review C4).
    history_restores = [job for job in other_jobs if is_recovery_job(job)]
    backup_jobs = [job for job in other_jobs if not is_recovery_job(job)]
    if _cfg.APP_DEBUG:
        debug_log(
            "NWUI action merge: "
            f"liveActions={live_action_count} historyActions={history_action_count} "
            f"mergedActions={len(raw_actions)} historyCached={history_from_cache}"
        )
        raw_status = Counter(
            str(item.get("status") or "").lower()
            for item in raw_actions
            if isinstance(item, dict)
        )
        norm_status = Counter(str(job.get("status") or "unknown") for job in jobs)
        debug_log(
            "NWUI monitoringactions diagnostic: "
            f"window={display_datetime(start_ts)}..{display_datetime(end_ts)} "
            f"rawActions={len(raw_actions)} jobs={len(jobs)} "
            f"backup={len(backup_jobs)} clone={len(clone_jobs)} "
            f"rawStatus={dict(raw_status)} normalizedStatus={dict(norm_status)}"
        )
        for idx, sample in enumerate(raw_actions[:3]):
            if isinstance(sample, dict):
                debug_log(
                    f"NWUI raw action sample[{idx}] keys={sorted(sample.keys())} "
                    f"status={sample.get('status')!r} "
                    f"startTime={sample.get('startTime')!r} "
                    f"completionTime={sample.get('completionTime')!r} "
                    f"actionName={sample.get('actionName')!r} "
                    f"workflowName={sample.get('workflowName')!r}"
                )
    failed_jobs = [job for job in backup_jobs if str(job.get("status", "")).lower() in ("failed", "warning")]
    if rest_clients is None:
        clients = build_nwui_clients(backup_jobs)
    else:
        clients = [project_client(client) for client in rest_clients]
    alerts = [project_nwui_alert(item) for item in raw_alerts if isinstance(item, dict)]
    if not alerts:
        alerts = [
            {
                "severity": "critical" if job.get("status") == "failed" else "warning",
                "time": str(job.get("started") or ""),
                "message": f"{str(job.get('status')).title()} backup: {job.get('client', '')} / {job.get('name', '')}",
                "resource": str(job.get("policy") or "backup"),
            }
            for job in failed_jobs[:50]
        ]
    policy_alert_rows = build_nwui_policies(raw_policies, backup_jobs)
    recovery_jobs = [item for item in raw_recoveries if isinstance(item, dict) and not is_clone_job(item)]
    clone_recoveries = [item for item in raw_recoveries if isinstance(item, dict) and is_clone_job(item)]
    recovery_rows = merge_recoveries(recovery_jobs, history_restores)
    clone_recovery_rows = [project_nwui_recovery(item) for item in clone_recoveries]

    clone_session_counts = session_counts_for_jobs(clone_jobs)
    backup_activity = nwui_backup_activity_counts(backup_jobs)
    successful_jobs = backup_activity["successful"]
    failed_count = backup_activity["failed"]
    warning_count = backup_activity["warnings"]
    active_jobs = backup_activity["active"]
    recovery_failed = sum(1 for row in recovery_rows if row.get("status") == "failed")
    recovery_running = sum(1 for row in recovery_rows if row.get("status") in ("running", "queued"))
    clone_failed = sum(1 for job in clone_jobs if job.get("status") == "failed") + sum(
        1 for row in clone_recovery_rows if row.get("status") == "failed"
    )
    clone_running = sum(1 for job in clone_jobs if job.get("status") in ("running", "queued")) + sum(
        1 for row in clone_recovery_rows if row.get("status") in ("running", "queued")
    )
    critical_source_errors = sum(
        1
        for name, item in sources.items()
        if name in {"nwuiLogin", "monitoringActions"} and not item.get("ok")
    )
    warning_source_errors = sum(
        1
        for name, item in sources.items()
        if name not in {"nwuiLogin", "monitoringActions"}
        and not item.get("ok")
        and not item.get("diagnosticOnly")
    )
    health = (
        "critical"
        if failed_count or recovery_failed or clone_failed or critical_source_errors
        else ("warning" if warning_count or warning_source_errors else "ok")
    )

    tables = {
        "jobs": [nwui_job_table_row(job) for job in backup_jobs[:TABLE_LIMIT]],
        "failedJobs": [
            {
                "client": job.get("client", ""),
                "name": job.get("name", ""),
                "policy": job.get("policy", ""),
                "status": job.get("status", ""),
                "started": job.get("started", ""),
                "message": job.get("message") or job.get("_save_set", ""),
            }
            for job in failed_jobs[:TABLE_LIMIT]
        ],
        "recovery": recovery_rows[:TABLE_LIMIT],
        "cloneJobs": ([nwui_job_table_row(job) for job in clone_jobs] + clone_recovery_rows)[:TABLE_LIMIT],
        "logs": [
            row
            for row in (nwui_job_log_row(job) for job in (other_jobs + clone_jobs)[:TABLE_LIMIT])
            if row.get("message")
        ],
        "alerts": (alerts + policy_alert_rows)[:TABLE_LIMIT],
        "clients": clients[:TABLE_LIMIT],
    }
    # Every row behind each table, which keeps only its first TABLE_LIMIT
    # (review C17); exports say "80 of N".
    table_totals = {
        "jobs": len(backup_jobs),
        "failedJobs": len(failed_jobs),
        "recovery": len(recovery_rows),
        "cloneJobs": len(clone_jobs) + len(clone_recovery_rows),
        "alerts": len(alerts) + len(policy_alert_rows),
        "clients": len(clients),
    }
    server_health = load_server_health_nwui(config, opener, auth_headers)
    # Detected on every backup job of the range, not the 80-row table.
    maintenance_backup = maintenance_backup_status(backup_jobs)

    any_success = sources.get("nwuiLogin", {}).get("ok") and any(
        item.get("ok") for key, item in sources.items() if key != "nwuiLogin"
    )
    body = {
        "ok": bool(any_success),
        "generatedAt": generated_at(),
        "target": {
            "restApiBase": nwui_api_base_url(config),
            "apiMode": "nwui",
            "backupServer": backup_target,
            "authcHeaderEnabled": False,
            "verifyTls": config.verify_tls,
            "reportRange": config.report_range,
            "login": login_info,
        },
        "summary": add_sla_summary({
            "totalClients": len(clients),
            "totalJobs": backup_activity["completed"] + backup_activity["active"],
            "completedJobs": backup_activity["completed"],
            "successfulJobs": successful_jobs,
            "failedJobs": failed_count,
            "activeJobs": active_jobs,
            "recoveryJobs": len(recovery_rows),
            "recoveryFailed": recovery_failed,
            "recoveryRunning": recovery_running,
            "cloneJobs": len(clone_jobs) + len(clone_recoveries),
            "cloneFailed": clone_failed,
            "cloneRunning": clone_running,
            "cloneSessionTotal": clone_session_counts["total"],
            "cloneSessionFailed": clone_session_counts["failed"],
            "cloneSessionRunning": clone_session_counts["running"],
            "totalAlerts": len(alerts),
            "criticalAlerts": sum(1 for item in alerts if item.get("severity") == "critical"),
            "warningAlerts": sum(1 for item in alerts if item.get("severity") == "warning"),
            "policies": len(raw_policies),
            "range": config.report_range,
            "rangeLabel": range_label,
            "health": health,
            "historyAvailable": history_available,
            "coverage": report_coverage(start_ts, end_ts, oldest_retained),
        }),
        "serverHealth": server_health,
        "serverProtectionJob": maintenance_backup,
        "maintenanceBackup": maintenance_backup,
        "sources": sources,
        "tables": tables,
        "tableTotals": table_totals,
    }
    if not any_success:
        first_error = next((item.get("error") for item in sources.values() if item.get("error")), "")
        body["error"] = first_error or "NWUI login worked, but no monitoring endpoints returned data."
        return 502, body
    if create_session:
        body["sessionId"] = create_dashboard_session(config, cookie_jar, auth_headers, maintenance_backup)
    return 200, body
