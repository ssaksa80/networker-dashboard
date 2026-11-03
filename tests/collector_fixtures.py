"""Hand-written, scrubbed NetWorker answers for the collector tests (review T4).

Both collectors run for real. Only the network is replaced: REST mode by a
fetch_json that answers each dashboard source by its label, NWUI mode by an
NWUI monitoring feed and a /nwrestapi stand-in that answers by URL. Every
host is a placeholder (example.com) and every record is made up.
"""
import json
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest import mock
from urllib.parse import parse_qs, urlparse

from nwdash import nwui, restapi
from nwdash.models import ApiConfig, RestApiError

HOUR = 60 * 60
DAY = 24 * HOUR


def config(api_mode="rest", report_range="24h", start="", end=""):
    return ApiConfig(
        rest_api_host="nw.example.com", rest_api_port=9090,
        backup_server_host="nw.example.com", backup_server_port=9090,
        username="administrator", password="fixture-pw", api_mode=api_mode, api_version="v3",
        report_range=report_range, custom_start_date=start, custom_end_date=end,
        use_wmi_health=False, wmi_username="", wmi_password="",
        timeout_seconds=30, verify_tls=True, use_authc_header=False,
    )


def iso(ts):
    """A NetWorker time with its UTC offset, as /global/jobs writes it."""
    return datetime.fromtimestamp(ts, UTC).isoformat()


def ago(hours):
    return iso(time.time() - hours * HOUR)


def rest_job(
    job_id,
    client="fs01.example.com",
    status="Succeeded",
    start=None,
    end=None,
    name="C:\\",
    action="backup",
    workflow="Gold-Filesystems",
    state=None,
    message=None,
):
    """One /global/jobs record with the fields the dashboard asks for. An
    action job names no client host (client=None)."""
    job = {
        "id": job_id,
        "startTime": start or ago(2),
        "name": name,
        "policyActionName": action,
        "workflowName": workflow,
        "level": "incr",
    }
    if status is not None:
        job["completionStatus"] = status
    if client is not None:
        job["clientHostname"] = client
    if end is not None:
        job["endTime"] = end
    if state is not None:
        job["state"] = state
    if message is not None:
        job["message"] = message
    return job


def restore_job(job_id, client="fs01.example.com", status="Succeeded", start=None, **kwargs):
    """A recover job as the jobs database lists it."""
    return rest_job(job_id, client, status, start, name="recover", action="", workflow="", **kwargs)


def server_protection_job(job_id, status="Succeeded", start=None, **kwargs):
    return rest_job(job_id, "nw.example.com", status, start, name="bootstrap",
                    action="server db backup", workflow="Server backup", **kwargs)


def nwui_action(status="Running", start=None, workflow="Gold-Filesystems", action="backup", **extra):
    """One /nwui/api/monitoringactions entry: a workflow action, no client."""
    item = {
        "workflowName": workflow,
        "actionName": action,
        "policyName": "Gold",
        "status": status,
        "startTime": start or ago(1),
        "duration": 120000,
        "jobCmd": f"nsrworkflow -p Gold -w {workflow} -A {action} -s nw.example.com",
        "jobData": {
            "successfulInputCount": 0, "failedInputCount": 0, "waitingInputCount": 0,
            "runningInputCount": 1, "canceledInputCount": 0,
        },
    }
    item.update(extra)
    return item


def nwui_recovery(status="Succeeded", start=None, client="fs01.example.com"):
    """One /nwui/api/monitoringrecoveries entry."""
    return {"clientName": client, "recoverType": "File system", "status": status,
            "startTime": start or ago(1), "saveSet": "C:\\Users"}


def client_record(hostname):
    """One /global/clients record."""
    return {"hostname": hostname, "backupType": "Filesystem", "saveSets": ["All"],
            "protectionGroups": ["Gold-Filesystems"], "enabled": True}


def rejects_field(field):
    """The HTTP 400 a NetWorker answers for a field its jobs resource lacks."""
    message = f"HTTP 400 Bad Request: The {field} field is not valid"
    return RestApiError(400, message, message)


def query_fields(url):
    query = parse_qs(urlparse(url).query)
    return query["fl"][0].split(",") if "fl" in query else []


def only_requested(records, url):
    """What NetWorker sends back: only the fields the query's `fl` names."""
    fields = query_fields(url)
    if not fields:
        return [dict(record) for record in records]
    return [{name: value for name, value in record.items() if name in fields} for record in records]


class RestServer:
    """The /nwrestapi answers of one REST-mode dashboard build, by source.
    The failed query answers the jobs whose completion status is "Failed"
    unless `failed` says otherwise. `rejected` names jobs fields this
    NetWorker does not know."""

    def __init__(self, jobs=(), failed=None, clients=(), alerts=(), policies=(), rejected=()):
        if failed is None:
            failed = [job for job in jobs if job.get("completionStatus") == "Failed"]
        self.answers = {
            "clients": {"clients": list(clients)},
            "jobs": {"jobs": list(jobs)},
            "failedJobs": {"jobs": list(failed)},
            "alerts": {"alerts": list(alerts)},
            "policies": {"protectionPolicies": list(policies)},
        }
        self.rejected = set(rejected)
        self.urls = []

    def fetch(self, url, _headers, _timeout, _context, label, max_bytes=0):
        self.urls.append(url)
        for field in query_fields(url):
            if field in self.rejected:
                raise rejects_field(field)
        answer = json.loads(json.dumps(self.answers[label]))
        key = next(iter(answer))
        return {key: only_requested(answer[key], url)}

    def build(self, cfg=None):
        with mock.patch.object(restapi, "fetch_json", self.fetch), \
                mock.patch.object(restapi, "load_server_health_rest", return_value={"status": "ok"}):
            return restapi.build_dashboard_rest(cfg or config())[1]


class NwuiServer:
    """The answers of one NWUI-mode dashboard build: the NWUI monitoring feeds
    and, for the history, failure text and clients, the /nwrestapi behind it.
    Each /nwrestapi answer is a list of records or an exception to raise."""

    def __init__(self, live=(), recoveries=(), history=(), failed=None, clients=(), rejected=()):
        self.live = list(live)
        self.recoveries = list(recoveries)
        self.history = history
        self.failed = failed
        self.clients = clients
        self.rejected = set(rejected)
        self.urls = []

    def pages(self, _config, _opener, _headers, endpoint_name, start_ts=None, end_ts=None, paging=None):
        if endpoint_name == "monitoringactions":
            return list(self.live)
        if endpoint_name == "monitoringrecoveries":
            return list(self.recoveries)
        return []

    def answer_for(self, url):
        parsed = urlparse(url)
        if parsed.path.endswith("/global/clients"):
            return self.clients, "clients"
        if parsed.path.endswith("/global/jobs"):
            if "q" in parse_qs(parsed.query):
                if self.failed is None:
                    return [job for job in self.history if job.get("completionStatus") == "Failed"], "jobs"
                return self.failed, "jobs"
            return self.history, "jobs"
        return [], "items"

    def fetch(self, url, _headers, _timeout, _context, _label, max_bytes=0):
        self.urls.append(url)
        for field in query_fields(url):
            if field in self.rejected:
                raise rejects_field(field)
        answer, key = self.answer_for(url)
        if isinstance(answer, Exception):
            raise answer
        records = only_requested(answer, url)
        return {"count": len(records), key: records}

    def urls_for(self, path):
        return [url for url in self.urls if urlparse(url).path.endswith(path)]

    def build(self, cfg=None):
        with ExitStack() as stack:
            for name, value in (
                ("nwui_monitoring_all_pages", self.pages),
                ("fetch_json", self.fetch),
                ("load_server_health_nwui", lambda *_args: {"status": "ok"}),
            ):
                stack.enter_context(mock.patch.object(nwui, name, value))
            stack.enter_context(mock.patch.dict(nwui._JOBS_HISTORY_CACHE, clear=True))
            if hasattr(nwui, "_CLIENTS_CACHE"):
                stack.enter_context(mock.patch.dict(nwui._CLIENTS_CACHE, clear=True))
            status, body = nwui.build_dashboard_nwui(
                cfg or config("nwui"), cookie_jar=None,
                auth_headers={"Authorization": "Bearer fixture"}, create_session=False,
            )
        body["_status"] = status
        return body
