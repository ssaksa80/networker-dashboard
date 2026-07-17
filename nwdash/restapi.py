"""NetWorker REST API client utilities and the REST dashboard builder.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import base64
import concurrent.futures
import email.utils
import html as html_lib
import json
import re
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import Request, urlopen

from .config import (
    APP_VERSION,
    CUSTOM_REPORT_RANGE,
    DEFAULT_REPORT_RANGE,
    MAX_JOBS_RESPONSE_BYTES,
    MAX_RESPONSE_BYTES,
    REPORT_RANGES,
    TABLE_LIMIT,
    debug_log,
)
from .models import ApiConfig, BadRequest, RestApiError, generated_at
from .wmi_health import load_server_health_wmi, server_health_from_payload, unavailable_server_health

def host_for_url(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def authc_header_value(host: str, port: int) -> str:
    return f"{host_for_url(host)}:{port}"


def report_range_label(report_range: str) -> str:
    return REPORT_RANGES.get(report_range, REPORT_RANGES[DEFAULT_REPORT_RANGE])[0]


def report_range_days(report_range: str) -> int:
    return REPORT_RANGES.get(report_range, REPORT_RANGES[DEFAULT_REPORT_RANGE])[1]


def parse_dashboard_date(value: str) -> datetime:
    text = str(value or "").strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise BadRequest("Custom dates must use DD-MM-YYYY format.")


def display_date(value: str) -> str:
    try:
        return parse_dashboard_date(value).strftime("%d-%m-%Y")
    except BadRequest:
        return str(value or "")


def parse_custom_date_window(start_date: str, end_date: str) -> tuple[float, float]:
    if not start_date or not end_date:
        raise BadRequest("Custom date range requires both start and end dates.")
    start_dt = parse_dashboard_date(start_date)
    end_dt = parse_dashboard_date(end_date) + timedelta(days=1)
    if end_dt <= start_dt:
        raise BadRequest("Custom end date must be on or after the start date.")
    return start_dt.timestamp(), end_dt.timestamp()


def report_window(config: ApiConfig) -> tuple[float, float, str]:
    if config.report_range == CUSTOM_REPORT_RANGE:
        start_ts, end_ts = parse_custom_date_window(
            config.custom_start_date,
            config.custom_end_date,
        )
        return start_ts, end_ts, f"{display_date(config.custom_start_date)} to {display_date(config.custom_end_date)}"
    end_ts = time.time()
    start_ts = end_ts - (report_range_days(config.report_range) * 24 * 60 * 60)
    return start_ts, end_ts, report_range_label(config.report_range)


def in_report_window(value: Any, config: ApiConfig) -> bool:
    ts = timestamp(value)
    start_ts, end_ts, _ = report_window(config)
    return bool(ts and start_ts <= ts <= end_ts)


def in_report_range(value: Any, report_range: str) -> bool:
    ts = timestamp(value)
    end_ts = time.time()
    start_ts = end_ts - (report_range_days(report_range) * 24 * 60 * 60)
    return bool(ts and start_ts <= ts <= end_ts)


def display_datetime(value: Any) -> str:
    ts = timestamp(value)
    if ts:
        try:
            return datetime.fromtimestamp(ts).astimezone().strftime("%d-%m-%Y %H:%M:%S %Z")
        except (OSError, OverflowError, ValueError):
            pass
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = timestamp(text)
    if parsed:
        try:
            return datetime.fromtimestamp(parsed).astimezone().strftime("%d-%m-%Y %H:%M:%S %Z")
        except (OSError, OverflowError, ValueError):
            pass
    return text




def add_sla_summary(summary: dict[str, Any]) -> dict[str, Any]:
    met = int(summary.get("successfulJobs") or 0)
    missed = int(summary.get("failedJobs") or 0)
    total = met + missed
    summary["slaTotalJobs"] = total
    summary["slaMetJobs"] = met
    summary["slaMissedJobs"] = missed
    summary["slaPercent"] = round((met / total) * 100, 2) if total else 0
    return summary




def maintenance_backup_status(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    housekeeping_keywords = ("expiration", "expire", "retention cleanup", "cleanup", "staging", "recycle")
    server_backup_names = {
        "server backup",
        "nmc server backup",
        "nmc server backup vm",
        "bootstrap maintenance backup",
    }

    def clean(value: Any) -> str:
        return " ".join(str(value or "").lower().split())

    def is_server_protection_job(job: dict[str, Any]) -> bool:
        identity_values = [
            clean(job.get(key))
            for key in (
                "policy",
                "policyName",
                "protectionPolicyName",
                "workflowName",
                "_workflow",
                "groupName",
                "_group",
            )
        ]
        action_values = [
            clean(job.get(key))
            for key in ("client", "clientHostname", "name", "actionName", "policyActionName", "_save_set")
        ]
        if any("server protection" in value for value in identity_values):
            return True
        if any(value == "server protection" for value in action_values):
            return True
        return any(value in server_backup_names for value in action_values + identity_values)

    matches = []
    for job in jobs:
        action_text = " ".join(
            clean(job.get(key))
            for key in ("name", "actionName", "policyActionName", "_workflow", "_group")
        )
        is_housekeeping = any(keyword in action_text for keyword in housekeeping_keywords)
        if is_server_protection_job(job) and not is_housekeeping:
            matches.append(job)

    if not matches:
        return {
            "status": "unknown",
            "label": "Not found",
            "detail": "No Server Protection job found in this range.",
            "count": 0,
        }

    latest = matches[0]
    raw_status = str(latest.get("status") or "unknown").lower()
    if any(word in raw_status for word in ("success", "succeed", "complete", "ok")):
        status = "succeeded"
    elif any(word in raw_status for word in ("fail", "error", "critical")):
        status = "failed"
    elif any(word in raw_status for word in ("run", "active", "start")):
        status = "running"
    elif any(word in raw_status for word in ("queue", "wait", "pending")):
        status = "queued"
    elif "warn" in raw_status:
        status = "warning"
    else:
        status = raw_status
    label = status.title() if status else "Unknown"
    return {
        "status": status,
        "label": label,
        "detail": (
            f"{latest.get('name') or 'Maintenance job'} on {latest.get('client') or 'server'}"
            f" at {latest.get('started') or 'unknown time'}"
        ),
        "count": len(matches),
    }


def api_base_url(config: ApiConfig) -> str:
    version = "v3" if config.api_version == "auto" else config.api_version
    return (
        f"https://{host_for_url(config.rest_api_host)}:{config.rest_api_port}"
        f"/nwrestapi/{version}"
    )


def api_base_url_for_version(config: ApiConfig, version: str) -> str:
    return (
        f"https://{host_for_url(config.rest_api_host)}:{config.rest_api_port}"
        f"/nwrestapi/{version}"
    )


def nwui_api_base_url(config: ApiConfig) -> str:
    return f"https://{host_for_url(config.rest_api_host)}:{config.rest_api_port}/nwui/api"


def origin_url(config: ApiConfig) -> str:
    return f"https://{host_for_url(config.rest_api_host)}:{config.rest_api_port}"


def endpoint(path: str, query: dict[str, str] | None = None) -> str:
    if query:
        return f"{path}?{urlencode(query)}"
    return path


# NetWorker /global/jobs field list (the `fl` query param). NetWorker rejects
# unknown fields with HTTP 400 ("The <field> field is not valid"), so this set
# is limited to fields the jobs resource actually exposes. Fields such as
# elapsedTime/policyName/saveBytes/transferredBytes are NOT valid job query
# fields on NetWorker and were removed. nwui_rest_fallback_items() still
# auto-strips any field a given NetWorker version rejects, as a safety net.
#
# `message` is deliberately EXCLUDED from the bulk list: it carries multi-KB of
# job-log text per record and on a busy server the jobs DB holds tens of
# thousands of jobs (observed: 36,031 jobs / 11.5 MB, almost entirely message
# text). Dropping it cuts the response by ~10x and removes the per-record log
# cleaning that was making each refresh time out. Failure detail still comes
# from the small, completionStatus:"Failed"-filtered failedJobs query below.
JOB_QUERY_FIELDS = (
    "clientHostname",
    "startTime",
    "completionStatus",
    "name",
    "policyActionName",
    "workflowName",
    "level",
)


def dashboard_endpoints(config: ApiConfig | None = None) -> dict[str, str]:
    # NOTE: NetWorker Query Language (the `q` param) supports only field:value
    # equality — it has NO range/comparison operators, so the report-time window
    # CANNOT be applied server-side (a startTime>=... query is rejected with
    # HTTP 400). The jobs database is naturally bounded by NetWorker's completed-
    # job retention, and the exact report window is enforced client-side by
    # in_report_window(). `config` is accepted for signature stability.
    job_fields = ",".join(JOB_QUERY_FIELDS)
    # The failed set is small (filtered to completionStatus:"Failed"), so it can
    # afford to include the verbose `message` field for failure detail.
    failed_fields = ",".join((*JOB_QUERY_FIELDS, "message"))
    return {
        "clients": endpoint(
            "/global/clients",
            {
                "fl": "hostname,backupType,saveSets,protectionGroups,enabled,aliases",
            },
        ),
        "jobs": endpoint("/global/jobs", {"fl": job_fields}),
        "failedJobs": endpoint(
            "/global/jobs",
            {
                "q": 'completionStatus:"Failed"',
                "fl": failed_fields,
            },
        ),
        "alerts": endpoint("/global/alerts"),
        "policies": endpoint("/global/protectionpolicies"),
    }


def build_headers(config: ApiConfig) -> dict[str, str]:
    token = base64.b64encode(f"{config.username}:{config.password}".encode("utf-8")).decode(
        "ascii"
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Basic {token}",
        "User-Agent": f"networker-dashboard/{APP_VERSION}",
    }
    if config.use_authc_header and config.backup_server_host:
        headers["X-NW-AUTHC-BASE-URL"] = authc_header_value(
            config.backup_server_host,
            config.backup_server_port,
        )
    return headers


def ssl_context_for_api(verify_tls: bool) -> ssl.SSLContext:
    if verify_tls:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def read_limited(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RestApiError(502, "REST API response exceeded dashboard safety limit.")
    return data


def compact_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    query_keys = ",".join(sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}))
    compact = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if query_keys:
        compact += f"?queryKeys={query_keys}"
    return compact


def compact_path_for_log(path: str) -> str:
    parsed = urlparse(path)
    query_keys = ",".join(sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}))
    compact = parsed.path or path
    if query_keys:
        compact += f"?queryKeys={query_keys}"
    return compact


def strip_html_for_error(body: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", body)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def looks_like_missing_nwrestapi(body: str) -> bool:
    lowered = html_lib.unescape(body).lower()
    return (
        "/nwrestapi/" in lowered
        and "is not available" in lowered
        and ("apache tomcat" in lowered or "http status 404" in lowered)
    )


def describe_http_error(status_code: int, reason: str, body: str, url: str) -> str:
    if status_code == 404 and looks_like_missing_nwrestapi(body):
        base = compact_url_for_log(url).split("?queryKeys=", 1)[0]
        return (
            f"HTTP 404 from NetWorker/Tomcat: the nwrestapi application was not found at {base}. "
            "Check that REST API server IP/port points to the NetWorker REST API host, not only an "
            "AuthC/Tomcat host. On the NetWorker REST API host verify the nwrestapi webapp exists "
            "and restapi.log is updating: Linux /nsr/authc/webapps/nwrestapi/ and "
            "/nsr/logs/restapi/restapi.log; Windows C:\\Program Files\\EMC NetWorker\\nsr\\authc-server\\tomcat\\webapps\\nwrestapi "
            "and C:\\Program Files\\EMC NetWorker\\nsr\\logs\\restapi\\restapi.log."
        )

    message = f"HTTP {status_code} {reason}".strip()
    clean_body = strip_html_for_error(body)
    if clean_body:
        message = f"{message}: {clean_body[:260]}"
    return message


def invalid_rest_query_field(message: str, body: str = "") -> str:
    text = f"{message}\n{body}"
    match = re.search(r"The\s+([A-Za-z0-9_.-]+)\s+field\s+is\s+not\s+valid", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"field\s+error:\s*([A-Za-z0-9_.-]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def remove_rest_field_from_path(path: str, field_name: str) -> str:
    if not field_name:
        return path
    parsed = urlparse(path)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    changed = False
    updated: list[tuple[str, str]] = []
    for key, value in query:
        if key != "fl":
            updated.append((key, value))
            continue
        fields = [field for field in value.split(",") if field and field.lower() != field_name.lower()]
        if len(fields) != len([field for field in value.split(",") if field]):
            changed = True
        updated.append((key, ",".join(fields)))
    if not changed:
        return path
    return parsed._replace(query=urlencode(updated)).geturl()


def strip_query_param(path: str, param_name: str) -> str:
    """Remove a single query parameter (e.g. the NQL `q` time filter) from a
    path, leaving the rest of the query string intact."""
    parsed = urlparse(path)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != param_name]
    return parsed._replace(query=urlencode(query)).geturl()


def describe_url_error(exc: BaseException) -> str:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            "REST API TLS certificate verification failed. Import the NetWorker REST API "
            "CA certificate into this host trust store, use a CA-signed certificate, or "
            "turn off 'Verify REST API TLS certificate' for lab/self-signed testing."
        )
    if isinstance(reason, ssl.SSLError):
        return f"REST API TLS handshake failed: {reason}"
    if isinstance(reason, TimeoutError) or isinstance(exc, (TimeoutError, socket.timeout)):
        return "REST API connection timed out. Check the REST API host, port, firewall, and routing."
    if isinstance(reason, ConnectionRefusedError):
        return "REST API connection refused. Check that NetWorker REST API is listening on the selected host and port."
    if isinstance(reason, OSError):
        return f"REST API network error: {reason}"
    return f"REST API connection failed: {reason}"


def fetch_json(
    url: str,
    headers: dict[str, str],
    timeout: int,
    context: ssl.SSLContext,
    label: str,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> Any:
    request = Request(url, headers=headers, method="GET")
    started = time.monotonic()
    debug_log(f"REST GET start source={label} url={compact_url_for_log(url)} timeout={timeout}s")
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            try:
                raw = read_limited(response, max_bytes)
            except RestApiError:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                debug_log(
                    f"REST GET too-large source={label} limitBytes={max_bytes} "
                    f"elapsedMs={elapsed_ms}"
                )
                raise
            elapsed_ms = int((time.monotonic() - started) * 1000)
            debug_log(
                f"REST GET ok source={label} status={response.status} "
                f"bytes={len(raw)} elapsedMs={elapsed_ms}"
            )
            if not raw:
                return None
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, "replace")
            return json.loads(text)
    except HTTPError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = exc.read(8192).decode("utf-8", "replace")
        message = describe_http_error(exc.code, exc.reason, body, url)
        debug_log(
            f"REST GET http-error source={label} status={exc.code} "
            f"elapsedMs={elapsed_ms} error={message}"
        )
        raise RestApiError(exc.code, message, body[:8192]) from exc
    except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        message = describe_url_error(exc)
        debug_log(
            f"REST GET network-error source={label} elapsedMs={elapsed_ms} "
            f"error={message}"
        )
        raise RestApiError(502, message) from exc
    except json.JSONDecodeError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        debug_log(
            f"REST GET json-error source={label} elapsedMs={elapsed_ms} "
            f"error={exc}"
        )
        raise RestApiError(502, f"REST API did not return JSON: {exc}") from exc


def collection_from(data: Any, preferred_key: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (preferred_key, "items", "results", "resources", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def stringify(value: Any, max_len: int = 220) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        text = ", ".join(stringify(item, 80) for item in value[:8])
        if len(value) > 8:
            text += f", +{len(value) - 8} more"
    elif isinstance(value, dict):
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    else:
        text = str(value)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def networker_group_name_from_output(text: str) -> str:
    patterns = (
        r"\bGroup\s+(.+?)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d{5,}",
        r"\bfor workflow '([^']+)'",
        r"\bStarting workflow '([^']+)'",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip(" .")
        if value and "%" not in value:
            return value
    return ""


def clean_networker_record_body(body: str, group_name: str = "") -> str:
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return ""

    waiting_match = re.search(
        r"\bwaiting\s+for\s+(\d+)\s+jobs\s+\((\d+)\s+awaiting\s+restart\)\s+to\s+complete\b",
        body,
        flags=re.IGNORECASE,
    )
    if waiting_match:
        prefix = f"Group {group_name} " if group_name else ""
        return (
            f"{prefix}waiting for {waiting_match.group(1)} jobs "
            f"({waiting_match.group(2)} awaiting restart) to complete."
        ).strip()

    nested_message = re.search(
        r"\bUnable to handle job (?:add|monitor) message:\s+(?:\d+\s+){3,}(.+?)(?=\s+\d+\s+\d+\s+\d+\s+\S+\s+NSR\b|$)",
        body,
        flags=re.IGNORECASE,
    )
    if nested_message:
        nested = re.sub(r"\s+", " ", nested_message.group(1)).strip(" .")
        nested = re.sub(r"\s+\d+\s+\d+\s+\d+\s+\S+.*$", "", nested).strip(" .")
        if nested:
            return f"{nested}."

    sentence_match = re.match(r"(.+?\.)\s+(.+)$", body)
    sentence = sentence_match.group(1) if sentence_match else body
    arg_tail = sentence_match.group(2) if sentence_match else ""
    sentence = re.sub(r"\\[rn]", " ", sentence)
    sentence = re.sub(r"\s+", " ", sentence).strip(" .")
    if not sentence:
        return ""

    if "%" in sentence:
        placeholders = re.findall(r"%[sdu]", sentence)
        arg_tokens = arg_tail.split()
        if arg_tokens and arg_tokens[0].isdigit() and int(arg_tokens[0]) == len(placeholders):
            arg_tokens = arg_tokens[1:]
        numeric_needed = sum(1 for placeholder in placeholders if placeholder in ("%d", "%u"))
        string_needed = sum(1 for placeholder in placeholders if placeholder == "%s")
        numeric_values: list[str] = []
        while arg_tokens and len(numeric_values) < numeric_needed and re.fullmatch(r"\d+", arg_tokens[0]):
            numeric_values.append(arg_tokens.pop(0))
        while arg_tokens and re.fullmatch(r"\d+", arg_tokens[-1]):
            arg_tokens.pop()
        string_values: list[str] = []
        if string_needed == 1 and arg_tokens:
            string_values.append(" ".join(arg_tokens))
        elif string_needed > 1:
            string_values.extend(arg_tokens[:string_needed])
        if len(numeric_values) >= numeric_needed and len(string_values) >= string_needed:
            numeric_index = 0
            string_index = 0

            def replace_record_placeholder(match: re.Match[str]) -> str:
                nonlocal numeric_index, string_index
                placeholder = match.group(0)
                if placeholder in ("%d", "%u"):
                    value = numeric_values[numeric_index]
                    numeric_index += 1
                    return value
                value = string_values[string_index]
                string_index += 1
                return value

            rendered = re.sub(r"%[sdu]", replace_record_placeholder, sentence)
            rendered = re.sub(r"\s+", " ", rendered).strip(" .")
            if rendered:
                return f"{rendered}."

        generic_templates = (
            (r"^Started\s+''\s+job\s+with\s+jobid\s+\[%u\]", "Backup job started."),
            (r"^Action\s+''\s+has\s+initialized", "Action initialized."),
        )
        for pattern, replacement in generic_templates:
            if re.search(pattern, sentence, flags=re.IGNORECASE):
                return replacement
        sentence = re.sub(r"\s+with job id %u.*$", "", sentence, flags=re.IGNORECASE).strip(" .")
        sentence = re.sub(r"\s+with jobid \[%u\].*$", "", sentence, flags=re.IGNORECASE).strip(" .")
        sentence = re.sub(r"%[sdu]", "", sentence)
        sentence = re.sub(r"\s+", " ", sentence).strip(" .'")

    if re.fullmatch(r"\d{1,2}:\d{1,2}\s+\S+", sentence):
        return ""
    return f"{sentence}." if sentence and not sentence.endswith(".") else sentence


def extract_networker_record_messages(text: str, group_name: str = "") -> list[str]:
    marker_pattern = re.compile(
        r"\b(?P<source>[A-Za-z0-9_.-]+)\s+NSR\s+(?P<level>info|notice|warning|error|critical)\s+(?P<code>\d+)\s+",
        flags=re.IGNORECASE,
    )
    matches = list(marker_pattern.finditer(text))
    messages: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        message = clean_networker_record_body(text[start:end], group_name)
        if message:
            messages.append(message)
    return messages


def clean_networker_job_message(value: Any, fallback: str = "", group_name: str = "") -> str:
    text = stringify(value, 12000)
    if not text:
        return fallback
    had_suppressed_prefix = bool(re.search(r"\bsuppressed\s+\d+\s+bytes?\s+of\s+output\b", text, re.IGNORECASE))
    text = re.sub(r"\bsuppressed\s+\d+\s+bytes?\s+of\s+output\.?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return fallback

    record_messages = extract_networker_record_messages(text, group_name)
    if record_messages:
        preferred = [message for message in record_messages if "waiting for" in message.lower()]
        return (preferred or record_messages)[-1]

    rendered_waiting_match = re.search(
        r"\bGroup\s+(.+?)\s+waiting\s+for\s+(\d+)\s+jobs\s+\((\d+)\s+awaiting\s+restart\)\s+to\s+complete\b",
        text,
        flags=re.IGNORECASE,
    )
    if rendered_waiting_match:
        group = re.sub(r"\s+", " ", rendered_waiting_match.group(1)).strip(" .")
        if len(group) <= 80 and not re.search(r"\b(?:NSR|savegrp|Program Files)\b", group, re.IGNORECASE):
            return (
                f"Group {group} waiting for {rendered_waiting_match.group(2)} jobs "
                f"({rendered_waiting_match.group(3)} awaiting restart) to complete."
            )

    waiting_match = re.search(
        r"\bwaiting\s+for\s+(\d+)\s+jobs\s+\((\d+)\s+awaiting\s+restart\)\s+to\s+complete\b",
        text,
        flags=re.IGNORECASE,
    )
    if waiting_match:
        group = group_name or networker_group_name_from_output(text)
        prefix = f"Group {group} " if group else ""
        return (
            f"{prefix}waiting for {waiting_match.group(1)} jobs "
            f"({waiting_match.group(2)} awaiting restart) to complete."
        ).strip().capitalize() if not prefix else (
            f"{prefix}waiting for {waiting_match.group(1)} jobs "
            f"({waiting_match.group(2)} awaiting restart) to complete."
        )

    catalog_match = re.search(
        r"\bNSR\s+(?:info|notice|warning|error|critical)\s+\d+\s+(.+?)\.\s+(\d+)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if catalog_match:
        template = catalog_match.group(1).strip()
        arg_count = int(catalog_match.group(2) or 0)
        arg_tokens = catalog_match.group(3).split()
        placeholders = re.findall(r"%[sd]", template)
        numeric_needed = sum(1 for placeholder in placeholders if placeholder == "%d")
        string_needed = sum(1 for placeholder in placeholders if placeholder == "%s")
        numeric_values = []
        while arg_tokens and len(numeric_values) < numeric_needed and re.fullmatch(r"\d+", arg_tokens[0]):
            numeric_values.append(arg_tokens.pop(0))
        while arg_tokens and re.fullmatch(r"\d+", arg_tokens[-1]):
            arg_tokens.pop()
        string_values: list[str] = []
        if string_needed == 1 and arg_tokens:
            string_values.append(" ".join(arg_tokens))
        elif string_needed > 1:
            string_values.extend(arg_tokens[:string_needed])

        if len(placeholders) == arg_count and len(numeric_values) >= numeric_needed and len(string_values) >= string_needed:
            numeric_index = 0
            string_index = 0

            def replace_placeholder(match: re.Match[str]) -> str:
                nonlocal numeric_index, string_index
                placeholder = match.group(0)
                if placeholder == "%d":
                    value = numeric_values[numeric_index]
                    numeric_index += 1
                    return value
                value = string_values[string_index]
                string_index += 1
                return value

            rendered = re.sub(r"%[sd]", replace_placeholder, template)
            rendered = re.sub(r"\s+", " ", rendered).strip(" .")
            if rendered:
                return f"{rendered}."
        compact_template = re.sub(r"%[sd]", "", template)
        compact_template = re.sub(r"\s+", " ", compact_template).strip(" .")
        if compact_template:
            return f"{compact_template}."

    tokens = text.split()
    numeric_tokens = sum(1 for token in tokens if re.fullmatch(r"\d+", token))
    if had_suppressed_prefix or (len(tokens) >= 18 and numeric_tokens / max(1, len(tokens)) > 0.45):
        return fallback or "Verbose NetWorker job output suppressed."
    return stringify(text, 260)


def first_value(item: Any, *keys: str) -> Any:
    if not isinstance(item, dict):
        return ""
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return ""


def timestamp(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1000 if numeric > 100000000000 else numeric

    text = str(value).strip()
    if not text:
        return 0.0
    if text.isdigit():
        numeric = float(text)
        return numeric / 1000 if numeric > 100000000000 else numeric

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass

    try:
        dt = email.utils.parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, IndexError):
        pass

    for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def format_duration_seconds(total_seconds: Any) -> str:
    try:
        seconds = int(float(total_seconds))
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not days and not hours:
        parts.append(f"{seconds}s")
    return " ".join(parts[:3]) or "0s"


def _format_bytes(value: Any) -> str:
    """Convert raw byte count to human-readable string (KB/MB/GB/TB)."""
    if value in (None, "", [], {}):
        return ""
    try:
        b = float(value)
    except (TypeError, ValueError):
        return ""
    if b <= 0:
        return ""
    for unit, threshold in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= threshold:
            return f"{b / threshold:.1f} {unit}"
    return f"{int(b)} B"


def format_duration_value(value: Any) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, (int, float)):
        return format_duration_seconds(value)
    text = stringify(value, 80).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return format_duration_seconds(float(text))
    colon_match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
    if colon_match:
        hours = int(colon_match.group(1) or 0)
        minutes = int(colon_match.group(2))
        seconds = int(colon_match.group(3))
        return format_duration_seconds((hours * 3600) + (minutes * 60) + seconds)
    return text


def status_text(job: Any) -> str:
    return stringify(first_value(job, "completionStatus", "state", "status"), 80)


def is_failed_job(job: Any) -> bool:
    status = status_text(job).lower()
    return any(word in status for word in ("fail", "error", "critical"))


def is_success_job(job: Any) -> bool:
    status = status_text(job).lower()
    return any(word in status for word in ("success", "succeed", "completed"))


def is_active_job(job: Any) -> bool:
    status = status_text(job).lower()
    return any(word in status for word in ("run", "active", "start", "queued"))


def is_clone_job(job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    fields = [
        first_value(job, "name"),
        first_value(job, "policyActionName"),
        first_value(job, "actionName"),
        first_value(job, "workflowName"),
        first_value(job, "jobType"),
        first_value(job, "type"),
        first_value(job, "recoverType"),
        first_value(job, "policyActionName"),
        first_value(job, "message"),
        first_value(job, "jobCmd"),
        job.get("_action_type"),
        job.get("_workflow"),
        job.get("_group"),
        job.get("_save_set"),
    ]
    text = " ".join(stringify(field, 140).lower() for field in fields)
    return any(token in text for token in ("clone", "nsrclone"))


def is_recovery_job(job: Any) -> bool:
    if is_clone_job(job):
        return False
    fields = [
        first_value(job, "name"),
        first_value(job, "policyActionName"),
        first_value(job, "actionName"),
        first_value(job, "workflowName"),
        first_value(job, "message"),
    ]
    text = " ".join(stringify(field, 120).lower() for field in fields)
    return any(word in text for word in ("recover", "recovery", "restore"))


def alert_severity(alert: Any) -> str:
    return stringify(first_value(alert, "severity", "level", "priority", "type", "status"), 80)


def is_critical_alert(alert: Any) -> bool:
    return any(word in alert_severity(alert).lower() for word in ("critical", "severe", "fatal"))


def is_warning_alert(alert: Any) -> bool:
    return any(word in alert_severity(alert).lower() for word in ("warn", "minor", "medium"))


def networker_log_priority(status: Any, message: Any = "") -> str:
    text = f"{stringify(status, 80)} {stringify(message, 140)}".lower()
    if any(word in text for word in ("critical", "fatal", "failed", "failure", "error")):
        return "error"
    if any(word in text for word in ("warn", "waiting", "queued", "awaiting restart")):
        return "warning"
    return "info"


def networker_log_category(message: Any, default: str = "policy") -> str:
    text = stringify(message, 260).lower()
    if any(word in text for word in ("device", "volume", "save set", "saveset", "media", "ddboost", "ddclone")):
        return "media"
    if any(word in text for word in ("workflow", "action", "group", "policy")):
        return "policy"
    if any(word in text for word in ("client", "host")):
        return "client"
    return default or "event"


def networker_log_row(
    message: Any,
    time_value: Any = "",
    status: Any = "",
    category: str = "",
    source: str = "event",
) -> dict[str, str]:
    clean_message = clean_networker_job_message(message)
    return {
        "priority": networker_log_priority(status, clean_message),
        "time": display_datetime(time_value) if time_value else "",
        "source": source or "event",
        "category": networker_log_category(clean_message, category or "policy"),
        "message": clean_message,
    }


def project_job_log(job: Any) -> dict[str, str]:
    projected = project_job(job)
    return networker_log_row(
        projected.get("message") or projected.get("name"),
        first_value(job, "startTime", "started", "start"),
        projected.get("status"),
        "policy",
        "event",
    )


def project_job(job: Any) -> dict[str, str]:
    group_name = stringify(
        first_value(job, "workflowName", "groupName", "policyName", "policy", "protectionPolicyName"),
        140,
    )
    raw_message = first_value(job, "jobOutput", "message", "messages", "statusMessage", "errorMessage")
    raw_bytes = first_value(job, "saveBytes", "transferredBytes", "bytesTransferred", "savedSize", "dataTransferred")
    return {
        "client": stringify(first_value(job, "clientHostname", "client", "hostname"), 120),
        "name": stringify(first_value(job, "name", "policyActionName", "actionName"), 140),
        "policy": group_name,
        "status": status_text(job),
        "started": display_datetime(first_value(job, "startTime", "started", "start")),
        "duration": format_duration_value(first_value(job, "elapsedTime", "duration", "elapsed")),
        "size": _format_bytes(raw_bytes),
        "message": clean_networker_job_message(raw_message, "", group_name),
    }


def project_failed_job(job: Any) -> dict[str, str]:
    projected = project_job(job)
    return {
        "client": projected["client"],
        "name": projected["name"],
        "policy": projected["policy"],
        "started": projected["started"],
        "message": projected["message"],
    }


def project_alert(alert: Any) -> dict[str, str]:
    return {
        "severity": alert_severity(alert),
        "time": display_datetime(first_value(alert, "time", "timestamp", "date", "createdTime")),
        "message": stringify(first_value(alert, "message", "summary", "description"), 260),
        "resource": stringify(first_value(alert, "resource", "resourceName", "source", "name"), 160),
    }


def project_client(client: Any) -> dict[str, str]:
    return {
        "hostname": stringify(first_value(client, "hostname", "name", "clientHostname"), 160),
        "enabled": stringify(first_value(client, "enabled", "active", "status"), 80),
        "backupType": stringify(first_value(client, "backupType", "type"), 120),
        "saveSets": stringify(first_value(client, "saveSets", "savesets"), 260),
        "protectionGroups": stringify(first_value(client, "protectionGroups", "groups"), 260),
    }


def sort_jobs(items: list[Any]) -> list[Any]:
    return sorted(
        items,
        key=lambda item: timestamp(first_value(item, "startTime", "started", "start")),
        reverse=True,
    )


def load_server_health_rest(
    config: ApiConfig,
    base_url: str,
    headers: dict[str, str],
    context: ssl.SSLContext,
) -> dict[str, Any]:
    wmi_health = load_server_health_wmi(config)
    if config.use_wmi_health:
        return wmi_health or unavailable_server_health("WMI health collection did not return CPU/memory metrics.")
    if wmi_health and (
        wmi_health.get("cpuUsagePercent") is not None
        or wmi_health.get("ramUsagePercent") is not None
    ):
        return wmi_health
    wmi_detail = wmi_health.get("detail") if wmi_health else ""
    candidates = (
        "/global/serverstatistics",
        "/global/serverstatus",
        "/global/health",
        "/global/status",
        "/server/health",
        "/server/status",
    )
    errors = []
    for path in candidates:
        try:
            data = fetch_json(base_url + path, headers, config.timeout_seconds, context, "serverHealth")
            health = server_health_from_payload(data, path)
            if health:
                return health
        except RestApiError as exc:
            errors.append(f"{path}: HTTP {exc.status_code}")
    detail = "No CPU/RAM metric found."
    if errors:
        detail = "Health endpoint unavailable: " + "; ".join(errors[:3])
    if wmi_detail:
        detail = f"{wmi_detail} NetWorker fallback: {detail}"
    return unavailable_server_health(detail)


def load_server_health_nwui(config: ApiConfig, opener: Any, auth_headers: dict[str, str]) -> dict[str, Any]:
    from .nwui import nwui_get_json  # late import: avoids circular module import
    wmi_health = load_server_health_wmi(config)
    if config.use_wmi_health:
        return wmi_health or unavailable_server_health("WMI health collection did not return CPU/memory metrics.")
    if wmi_health and (
        wmi_health.get("cpuUsagePercent") is not None
        or wmi_health.get("ramUsagePercent") is not None
    ):
        return wmi_health
    wmi_detail = wmi_health.get("detail") if wmi_health else ""
    candidates = (
        "serverstatistics",
        "serverstatus",
        "system/status",
        "system/health",
        "monitoring/serverhealth",
        "monitoring/system",
        "health",
    )
    errors = []
    for path in candidates:
        try:
            data = nwui_get_json(config, opener, auth_headers, path)
            health = server_health_from_payload(data, f"/nwui/api/{path}")
            if health:
                return health
        except RestApiError as exc:
            errors.append(f"/nwui/api/{path}: HTTP {exc.status_code}")
    detail = "No CPU/RAM metric found."
    if errors:
        detail = "Health endpoint unavailable: " + "; ".join(errors[:3])
    if wmi_detail:
        detail = f"{wmi_detail} NetWorker fallback: {detail}"
    return unavailable_server_health(detail)


def build_dashboard_rest(config: ApiConfig) -> tuple[int, dict[str, Any]]:
    base_url = api_base_url(config)
    paths = dashboard_endpoints(config)
    headers = build_headers(config)
    context = ssl_context_for_api(config.verify_tls)
    debug_log(
        "Dashboard request "
        f"restApiBase={base_url} "
        f"backupServer={authc_header_value(config.backup_server_host, config.backup_server_port)} "
        f"authcHeaderEnabled={config.use_authc_header} "
        f"verifyTls={config.verify_tls} "
        f"timeout={config.timeout_seconds}s"
    )

    raw_results: dict[str, Any] = {}
    sources: dict[str, dict[str, Any]] = {}

    def load(name: str, path: str) -> tuple[str, Any]:
        is_jobs = name in ("jobs", "failedJobs")
        load_timeout = max(config.timeout_seconds, 120) if is_jobs else config.timeout_seconds
        load_max_bytes = MAX_JOBS_RESPONSE_BYTES if is_jobs else MAX_RESPONSE_BYTES
        return name, fetch_json(
            base_url + path, headers, load_timeout, context, name, max_bytes=load_max_bytes
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(paths))) as executor:
        futures = {
            executor.submit(load, name, path): (name, path)
            for name, path in paths.items()
        }
        for future in concurrent.futures.as_completed(futures):
            name, path = futures[future]
            try:
                _, data = future.result()
                raw_results[name] = data
                preferred = "jobs" if name == "failedJobs" else name
                sources[name] = {
                    "ok": True,
                    "path": path,
                    "count": len(collection_from(data, preferred)),
                }
                debug_log(
                    f"Dashboard source ok source={name} "
                    f"path={compact_path_for_log(path)} count={sources[name]['count']}"
                )
            except RestApiError as exc:
                sources[name] = {
                    "ok": False,
                    "path": path,
                    "status": exc.status_code,
                    "error": exc.message,
                }
                debug_log(
                    f"Dashboard source failed source={name} "
                    f"path={compact_path_for_log(path)} status={exc.status_code} error={exc.message}"
                )

    clients = collection_from(raw_results.get("clients"), "clients")
    _, _, range_label = report_window(config)
    jobs = [
        job for job in sort_jobs(collection_from(raw_results.get("jobs"), "jobs"))
        if in_report_window(first_value(job, "startTime", "started", "start"), config)
    ]
    failed_jobs_from_query = [
        job for job in sort_jobs(collection_from(raw_results.get("failedJobs"), "jobs"))
        if in_report_window(first_value(job, "startTime", "started", "start"), config)
    ]
    clone_jobs = [job for job in jobs if is_clone_job(job)]
    backup_jobs = [job for job in jobs if not is_clone_job(job)]
    failed_jobs_from_query = [job for job in failed_jobs_from_query if not is_clone_job(job)]
    failed_jobs = failed_jobs_from_query or [job for job in backup_jobs if is_failed_job(job)]
    alerts = [
        alert for alert in collection_from(raw_results.get("alerts"), "alerts")
        if not first_value(alert, "time", "timestamp", "date", "createdTime")
        or in_report_window(first_value(alert, "time", "timestamp", "date", "createdTime"), config)
    ]
    policies = collection_from(raw_results.get("policies"), "policies")
    recovery_jobs = [job for job in backup_jobs if is_recovery_job(job)]

    critical_alerts = sum(1 for alert in alerts if is_critical_alert(alert))
    warning_alerts = sum(1 for alert in alerts if is_warning_alert(alert))
    failed_count = len(failed_jobs)
    clone_failed = sum(1 for job in clone_jobs if is_failed_job(job))
    clone_running = sum(1 for job in clone_jobs if is_active_job(job))
    source_errors = sum(1 for item in sources.values() if not item.get("ok"))

    if failed_count or clone_failed or critical_alerts:
        health = "critical"
    elif warning_alerts or source_errors:
        health = "warning"
    else:
        health = "ok"

    summary = add_sla_summary({
        "totalClients": len(clients),
        "totalJobs": len(backup_jobs),
        "successfulJobs": sum(1 for job in backup_jobs if is_success_job(job)),
        "failedJobs": failed_count,
        "activeJobs": sum(1 for job in backup_jobs if is_active_job(job)),
        "recoveryJobs": len(recovery_jobs),
        "recoveryFailed": sum(1 for job in recovery_jobs if is_failed_job(job)),
        "recoveryRunning": sum(1 for job in recovery_jobs if is_active_job(job)),
        "cloneJobs": len(clone_jobs),
        "cloneFailed": clone_failed,
        "cloneRunning": clone_running,
        "cloneSessionTotal": 0,
        "cloneSessionFailed": 0,
        "cloneSessionRunning": 0,
        "totalAlerts": len(alerts),
        "criticalAlerts": critical_alerts,
        "warningAlerts": warning_alerts,
        "policies": len(policies),
        "range": config.report_range,
        "rangeLabel": range_label,
        "health": health,
    })

    tables = {
        "jobs": [project_job(job) for job in backup_jobs[:TABLE_LIMIT]],
        "failedJobs": [project_failed_job(job) for job in failed_jobs[:TABLE_LIMIT]],
        "recovery": [project_job(job) for job in recovery_jobs[:TABLE_LIMIT]],
        "cloneJobs": [project_job(job) for job in clone_jobs[:TABLE_LIMIT]],
        "logs": [
            row
            for row in (project_job_log(job) for job in (backup_jobs + clone_jobs)[:TABLE_LIMIT])
            if row.get("message")
        ],
        "alerts": [project_alert(alert) for alert in alerts[:TABLE_LIMIT]],
        "clients": [project_client(client) for client in clients[:TABLE_LIMIT]],
    }
    server_health = load_server_health_rest(config, base_url, headers, context)
    maintenance_backup = maintenance_backup_status(tables["jobs"] + tables["failedJobs"])

    any_success = any(item.get("ok") for item in sources.values())
    statuses = {item.get("status") for item in sources.values() if not item.get("ok")}
    response_status = 200
    if not any_success:
        if len(statuses) == 1 and next(iter(statuses)) in (401, 403):
            response_status = int(next(iter(statuses)))
        else:
            response_status = 502

    body = {
        "ok": any_success,
        "generatedAt": generated_at(),
        "target": {
            "restApiBase": base_url,
            "apiMode": "rest",
            "backupServer": authc_header_value(config.backup_server_host, config.backup_server_port),
            "authcHeaderEnabled": config.use_authc_header,
            "verifyTls": config.verify_tls,
            "reportRange": config.report_range,
        },
        "summary": summary,
        "serverHealth": server_health,
        "serverProtectionJob": maintenance_backup,
        "maintenanceBackup": maintenance_backup,
        "sources": sources,
        "tables": tables,
    }
    if not any_success:
        first_error = next((item.get("error") for item in sources.values() if item.get("error")), "")
        body["error"] = first_error or "All REST API calls failed."
    return response_status, body
