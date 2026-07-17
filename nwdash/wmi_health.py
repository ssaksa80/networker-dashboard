"""Server health via WMI (PowerShell) and health payload helpers.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import socket
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .config import safe_log_text
from .models import ApiConfig

def unavailable_server_health(detail: str = "CPU/RAM metrics were not exposed by the tested endpoints.") -> dict[str, Any]:
    return {
        "status": "unknown",
        "label": "Unavailable",
        "detail": detail,
        "source": "",
        "cpuUsagePercent": None,
        "ramUsagePercent": None,
        "ramUsedGb": None,
        "ramFreeGb": None,
        "ramTotalGb": None,
        "cpuDetail": "",
        "ramDetail": "",
    }


def percent_from_any(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        match = re.search(r"(\d+(?:\.\d+)?)", value)
        if not match:
            return None
        value = match.group(1)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 1:
        number *= 100
    return max(0, min(100, round(number)))


def find_metric_value(data: Any, names: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        lowered = {str(key).lower(): value for key, value in data.items()}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
        for value in data.values():
            found = find_metric_value(value, names)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_metric_value(item, names)
            if found not in (None, ""):
                return found
    return None


def server_health_from_payload(data: Any, source: str) -> dict[str, Any] | None:
    cpu = percent_from_any(
        find_metric_value(
            data,
            (
                "cpuUsagePercent",
                "cpuPercent",
                "cpuUsage",
                "processorUsage",
                "systemCpuUsage",
                "cpu",
            ),
        )
    )
    ram = percent_from_any(
        find_metric_value(
            data,
            (
                "ramUsagePercent",
                "memoryUsagePercent",
                "memoryPercent",
                "memoryUsage",
                "usedMemoryPercent",
                "ram",
            ),
        )
    )
    if cpu is None and ram is None:
        return None

    status = "ok"
    if (cpu is not None and cpu >= 90) or (ram is not None and ram >= 90):
        status = "critical"
    elif (cpu is not None and cpu >= 75) or (ram is not None and ram >= 75):
        status = "warning"

    return {
        "status": status,
        "label": "Critical" if status == "critical" else ("Warning" if status == "warning" else "Healthy"),
        "detail": f"Metrics loaded from {source}.",
        "source": source,
        "cpuUsagePercent": cpu,
        "ramUsagePercent": ram,
        "ramUsedGb": None,
        "ramFreeGb": None,
        "ramTotalGb": None,
        "cpuDetail": "CPU utilization",
        "ramDetail": "Memory utilization",
    }




def wmi_target_host(config: ApiConfig) -> str:
    return config.backup_server_host or config.rest_api_host


def is_local_wmi_target(target: str) -> bool:
    host = str(target or "").strip().strip("[]").lower()
    if host in ("", ".", "localhost", "127.0.0.1", "::1"):
        return True
    local_names = {
        socket.gethostname().lower(),
        socket.getfqdn().lower(),
    }
    if host in local_names:
        return True
    try:
        target_ips = {info[4][0] for info in socket.getaddrinfo(host, None)}
        local_ips = {"127.0.0.1", "::1"}
        for name in local_names:
            try:
                local_ips.update(info[4][0] for info in socket.getaddrinfo(name, None))
            except socket.gaierror:
                continue
        return bool(target_ips & local_ips)
    except socket.gaierror:
        return False


def wmi_connectivity_hint(target: str) -> str:
    return (
        f"Check WMI/DCOM access to {target}: Windows Firewall WMI rules, RPC port 135 "
        "and dynamic RPC ports, and Remote WMI/DCOM permissions for the service account."
    )


def wmi_failure_hint(target: str, detail: str = "") -> str:
    lowered = str(detail or "").lower()
    if "access is denied" in lowered or "0x80070005" in lowered or "unauthorizedaccess" in lowered:
        return (
            f"WMI reached {target}, but Windows denied the account. Use DOMAIN\\user or {target}\\localadmin, "
            f"add the account to local Administrators on {target}, or grant DCOM Remote Launch/Activation and "
            r"WMI root\cimv2 Remote Enable, Execute Methods, and Enable Account permissions. "
            "For non-domain local accounts, Remote UAC filtering may also block WMI; use a domain service account "
            "or configure LocalAccountTokenFilterPolicy on the backup server."
        )
    if "user credentials cannot be used for local connections" in lowered:
        return (
            "Windows rejected explicit credentials for a local WMI target. Use localhost/the server hostname from "
            "the dashboard host and leave WMI username/password blank, or run the dashboard under the account that "
            "has local WMI access."
        )
    return wmi_connectivity_hint(target)


def clean_powershell_error(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    text = text.replace("#< CLIXML", "").strip()
    if "<Objs" in text:
        xml_text = text[text.find("<Objs") :]
        try:
            root = ET.fromstring(xml_text)
            messages = []
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1]
                stream = element.attrib.get("S", "")
                name = element.attrib.get("N", "")
                value_text = (element.text or "").strip()
                if not value_text:
                    continue
                if stream == "progress" or value_text == "Preparing modules for first use.":
                    continue
                if tag in {"S", "AV"} or name in {"Message", "ErrorRecord", "FullyQualifiedErrorId"}:
                    messages.append(value_text)
            if messages:
                return safe_log_text(" ".join(dict.fromkeys(messages)), 700)
        except ET.ParseError:
            pass
    text = re.sub(r"<Obj\b[^>]*S=\"progress\".*?</Obj>", " ", text, flags=re.DOTALL)
    text = re.sub(r"Preparing modules for first use\.", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return safe_log_text(text, 700)


def load_server_health_wmi(config: ApiConfig) -> dict[str, Any] | None:
    if not config.use_wmi_health:
        return None
    target = wmi_target_host(config)
    is_local_target = is_local_wmi_target(target)
    if not is_local_target and (not config.wmi_username or not config.wmi_password):
        return unavailable_server_health("WMI credentials were not provided.")

    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    executable = str(powershell) if powershell.exists() else "powershell.exe"
    script = r'''
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
$DebugPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"
$inputJson = [Console]::In.ReadToEnd()
$payload = $inputJson | ConvertFrom-Json
$target = $payload.host
if ($payload.isLocal) {
  $wmi = @{}
} elseif ($payload.useCredential) {
  $securePassword = ConvertTo-SecureString -String $payload.password -AsPlainText -Force
  $credential = New-Object System.Management.Automation.PSCredential($payload.username, $securePassword)
  $wmi = @{
    ComputerName = $target
    Credential = $credential
    Authentication = "PacketPrivacy"
    Impersonation = "Impersonate"
  }
} else {
  $wmi = @{
    ComputerName = $target
  }
}
$cpuSampleSeconds = 1
$processorStart = Get-WmiObject -Class Win32_PerfRawData_PerfOS_Processor @wmi -Filter "Name='_Total'"
Start-Sleep -Seconds $cpuSampleSeconds
$processorEnd = Get-WmiObject -Class Win32_PerfRawData_PerfOS_Processor @wmi -Filter "Name='_Total'"
$os = Get-WmiObject -Class Win32_OperatingSystem @wmi
$system = Get-WmiObject -Class Win32_PerfFormattedData_PerfOS_System @wmi
$totalKb = [double]$os.TotalVisibleMemorySize
$freeKb = [double]$os.FreePhysicalMemory
$cpuCounterDelta = [double]$processorEnd.PercentProcessorTime - [double]$processorStart.PercentProcessorTime
$cpuTimeDelta = [double]$processorEnd.Timestamp_Sys100NS - [double]$processorStart.Timestamp_Sys100NS
$cpuPercent = if ($cpuTimeDelta -gt 0) { [math]::Round((1 - ($cpuCounterDelta / $cpuTimeDelta)) * 100) } else { $null }
if ($cpuPercent -ne $null) {
  if ($cpuPercent -lt 0) { $cpuPercent = 0 }
  if ($cpuPercent -gt 100) { $cpuPercent = 100 }
}
$ramPercent = if ($totalKb -gt 0) { [math]::Round((($totalKb - $freeKb) / $totalKb) * 100) } else { $null }
[pscustomobject]@{
  host = $target
  cpuUsagePercent = if ($cpuPercent -ne $null) { [int]$cpuPercent } else { $null }
  cpuSampleSeconds = $cpuSampleSeconds
  ramUsagePercent = $ramPercent
  totalMemoryMb = if ($totalKb -gt 0) { [math]::Round($totalKb / 1024) } else { $null }
  freeMemoryMb = if ($freeKb -gt 0) { [math]::Round($freeKb / 1024) } else { $null }
  uptimeSeconds = [int64]$system.SystemUpTime
  osCaption = [string]$os.Caption
  lastBoot = [string]$os.LastBootUpTime
} | ConvertTo-Json -Compress
'''
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    payload = json.dumps(
        {
            "host": target,
            "username": config.wmi_username,
            "password": config.wmi_password,
            "isLocal": is_local_target,
            "useCredential": bool(config.wmi_username and config.wmi_password and not is_local_target),
        },
        ensure_ascii=True,
    )
    wmi_timeout = max(10, min(config.timeout_seconds, 120))
    try:
        completed = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded_script,
            ],
            input=payload,
            text=True,
            capture_output=True,
            timeout=wmi_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return unavailable_server_health(
            f"WMI query timed out after {wmi_timeout}s. {wmi_failure_hint(target)}"
        )
    except OSError as exc:
        return unavailable_server_health(f"WMI query could not start PowerShell: {safe_log_text(exc)}")

    if completed.returncode != 0:
        detail = clean_powershell_error(completed.stderr) or clean_powershell_error(completed.stdout) or "PowerShell WMI command failed."
        return unavailable_server_health(f"WMI query failed: {detail} {wmi_failure_hint(target, detail)}")

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return unavailable_server_health("WMI query returned non-JSON output.")

    health = server_health_from_payload(data, f"WMI {wmi_target_host(config)}")
    if not health:
        return unavailable_server_health("WMI query returned no CPU/RAM metrics.")
    health["detail"] = f"{data.get('osCaption') or 'Windows'} via WMI."
    sample_seconds = data.get("cpuSampleSeconds") or 1
    health["cpuDetail"] = f"Real-time WMI sample from {data.get('host') or wmi_target_host(config)} over {sample_seconds}s"
    total = data.get("totalMemoryMb")
    free = data.get("freeMemoryMb")
    if total is not None and free is not None:
        total_gb = gb_from_mb(total)
        free_gb = gb_from_mb(free)
        if total_gb is not None:
            health["ramTotalGb"] = total_gb
        if free_gb is not None:
            health["ramFreeGb"] = free_gb
        if total_gb is not None and free_gb is not None:
            used_gb = round(max(0.0, total_gb - free_gb), 1)
            health["ramUsedGb"] = used_gb
            health["ramDetail"] = f"{used_gb:g} GB used of {total_gb:g} GB ({health.get('ramUsagePercent')}%)"
    return health


def format_number_for_detail(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value or "")


def gb_from_mb(value: Any) -> float | None:
    try:
        mb = float(value)
    except (TypeError, ValueError):
        return None
    return round(mb / 1024, 1)
