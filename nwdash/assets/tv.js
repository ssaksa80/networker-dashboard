/* NetWorker TV wall-display mode (v2.8.0).
   Live app page: same-origin cookie auth, /api/current-dashboard bootstrap,
   /api/stream SSE re-render, /api/snapshots comparison strip.
   Faithful port of the validated tv_final prototype behaviors. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var CYCLE_MS = (typeof window.__TV_CYCLE_MS__ === "number" && window.__TV_CYCLE_MS__ > 0)
    ? window.__TV_CYCLE_MS__ : 15000; /* auto-cycle 15s per the design spec */
  var RECONNECT_MS = 15000;
  var POLL_MS = 15000;
  var SNAP_REFRESH_MS = 5 * 60 * 1000;
  var MIN_KEY = "nw_tv_jobs_min";
  var CIRC = 2 * Math.PI * 46; /* donut circle r=46 in the 120x120 viewBox */

  var latestDashboard = null;
  var lastReceivedMs = 0;
  var updatedAtText = "";
  var sseSource = null;
  var pollTimer = null;
  var viewIndex = 0;

  /* DSO wall: when opened as /tv/<token> this page runs WITHOUT login and reads
     the read-only display feed instead of the cookie-gated APIs. */
  var _dtm = location.pathname.match(/\/tv\/([0-9a-f]{32})/);
  var DISPLAY_TOKEN = _dtm ? _dtm[1] : "";
  function dashboardUrl() {
    return DISPLAY_TOKEN ? ("/api/display/" + DISPLAY_TOKEN) : "/api/current-dashboard";
  }

  /* ── helpers (same semantics as app.js) ────────────────────────────── */
  function text(value) {
    if (value === null || value === undefined || value === "") return "--";
    return String(value);
  }
  function escapeHtml(value) {
    return text(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }
  function numberValue(value) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  function formatNumber(value) {
    try { return new Intl.NumberFormat().format(numberValue(value)); }
    catch (error) { return String(numberValue(value)); }
  }
  function formatDecimal(value, digits) {
    var numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    var d = digits === undefined ? 1 : digits;
    return new Intl.NumberFormat(undefined, {
      minimumFractionDigits: numeric % 1 === 0 ? 0 : d,
      maximumFractionDigits: d,
    }).format(numeric);
  }
  function percentage(part, total) {
    var denominator = numberValue(total);
    if (!denominator) return 0;
    return Math.round((numberValue(part) / denominator) * 100);
  }
  var DDMMYYYY_TS_RE = /^(\d{2})-(\d{2})-(\d{4})[ T](\d{2}):(\d{2})(?::(\d{2}))?/;
  function parseTs(str) {
    if (!str && str !== 0) return NaN;
    var m = typeof str === "string" ? DDMMYYYY_TS_RE.exec(str.trim()) : null;
    if (m) {
      var day = Number(m[1]), month = Number(m[2]), year = Number(m[3]);
      if (day >= 1 && day <= 31 && month >= 1 && month <= 12) {
        var d = new Date(year, month - 1, day, Number(m[4] || 0), Number(m[5] || 0), Number(m[6] || 0));
        return isNaN(d.getTime()) ? NaN : d.getTime();
      }
    }
    var parsed = new Date(str);
    return isNaN(parsed.getTime()) ? NaN : parsed.getTime();
  }
  function pillClass(value) {
    var status = String(value || "").toLowerCase();
    if (status.includes("success") || status.includes("succeed") || status.includes("complete")) return "ok3";
    if (status.includes("fail") || status.includes("error") || status.includes("critical")) return "bad3";
    if (status.includes("run") || status.includes("active") || status.includes("start")) return "run3";
    return "un3";
  }

  /* ── theme: same data-theme system as the main SPA ─────────────────── */
  function applyTheme(theme) {
    document.body.dataset.theme = String(theme || "default");
  }
  try { applyTheme(localStorage.getItem("nw_dashboard_theme") || "default"); } catch (error) {}
  fetch("/api/ui-theme", { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (data) { if (data && data.ok) applyTheme(data.theme || "default"); })
    .catch(function () {});

  /* ── header clock + freshness line ─────────────────────────────────── */
  function tickClock() {
    $("clock").textContent = new Date().toLocaleTimeString();
    var baseMs = parseTs(updatedAtText);
    if (!Number.isFinite(baseMs) || isNaN(baseMs)) baseMs = lastReceivedMs;
    var ago = $("agoline");
    if (ago && baseMs) {
      var seconds = Math.max(0, Math.round((Date.now() - baseMs) / 1000));
      var label = seconds < 60 ? seconds + "s ago"
        : seconds < 3600 ? Math.floor(seconds / 60) + "m " + (seconds % 60) + "s ago"
        : Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m ago";
      ago.textContent = label;
    }
  }
  setInterval(tickClock, 1000);
  tickClock();

  /* ── LIVE indicator (pulses only while the SSE stream is connected) ── */
  function setLive(connected) {
    $("liveWrap").classList.toggle("off", !connected);
    $("liveText").textContent = connected ? "LIVE" : "OFFLINE";
  }

  /* ── donuts ─────────────────────────────────────────────────────────── */
  function donutSvg(items, total, centerTop, centerBottom) {
    var segments = "";
    var offset = 0;
    if (total > 0) {
      items.forEach(function (item) {
        var amount = Math.max(0, numberValue(item.value));
        if (!amount) return;
        var len = (amount / total) * CIRC;
        segments += '<circle cx="60" cy="60" r="46" fill="none" stroke="' + item.color +
          '" stroke-width="15" stroke-dasharray="' + len.toFixed(2) + " " + (CIRC - len).toFixed(2) +
          '" stroke-dashoffset="' + (-offset).toFixed(2) + '" transform="rotate(-90 60 60)"/>';
        offset += len;
      });
    }
    return '<circle cx="60" cy="60" r="46" fill="none" stroke="var(--surface2)" stroke-width="15"/>' +
      segments +
      '<text x="60" y="57" text-anchor="middle" font-size="15" font-weight="780" fill="var(--ink)" font-family="Inter,system-ui">' + escapeHtml(centerTop) + "</text>" +
      '<text x="60" y="73" text-anchor="middle" font-size="8.5" fill="var(--muted)" font-family="Inter,system-ui">' + escapeHtml(centerBottom) + "</text>";
  }
  function legendHtml(items, total) {
    return items.map(function (item) {
      return '<span><i style="background:' + item.color + '"></i>' + escapeHtml(item.label) +
        " <b>" + formatNumber(item.value) + " (" + percentage(item.value, total) + "%)</b></span>";
    }).join("");
  }

  function renderMix(summary) {
    /* Same series as app.js chartItems().slice(0, 4) */
    var items = [
      { label: "Successful", value: numberValue(summary.successfulJobs), color: "var(--green)" },
      { label: "Failed", value: numberValue(summary.failedJobs), color: "var(--red)" },
      { label: "Running", value: numberValue(summary.activeJobs), color: "var(--blue)" },
      { label: "Restores", value: numberValue(summary.recoveryJobs), color: "var(--amber)" },
    ];
    var total = items.reduce(function (sum, item) { return sum + item.value; }, 0);
    $("mixRange").textContent = text(summary.rangeLabel);
    $("mixDonut").innerHTML = donutSvg(items, total, formatNumber(total), "ACTIVITY");
    $("mixLeg").innerHTML = legendHtml(items, total);
  }

  function renderSla(summary) {
    var total = numberValue(summary.slaTotalJobs !== undefined ? summary.slaTotalJobs : summary.totalJobs);
    var met = numberValue(summary.slaMetJobs !== undefined ? summary.slaMetJobs : summary.successfulJobs);
    var missed = Math.max(0, numberValue(summary.slaMissedJobs !== undefined ? summary.slaMissedJobs : (total - met)));
    var percent = numberValue(summary.slaPercent !== undefined ? summary.slaPercent : (total ? Math.round((met / total) * 100) : 0));
    var items = [
      { label: "SLA met", value: met, color: "var(--green)" },
      { label: "Not met", value: missed, color: "var(--red)" },
    ];
    $("slaMeta").textContent = total ? formatNumber(total) + " jobs" : "No jobs ran";
    $("slaDonut").innerHTML = donutSvg(items, total, total ? formatNumber(percent) + "%" : "--", "SLA");
    $("slaLeg").innerHTML = legendHtml(items, total);
  }

  function renderOverview(summary) {
    /* Same series as app.js chartItems(summary, true) */
    var items = [
      { label: "Clients", value: numberValue(summary.totalClients), color: "var(--blue)" },
      { label: "Successful", value: numberValue(summary.successfulJobs), color: "var(--green)" },
      { label: "Failed", value: numberValue(summary.failedJobs), color: "var(--red)" },
      { label: "Running", value: numberValue(summary.activeJobs), color: "var(--blue)" },
      { label: "Restores", value: numberValue(summary.recoveryJobs), color: "var(--amber)" },
      { label: "Clones", value: numberValue(summary.cloneJobs), color: "var(--brand)" },
      { label: "Alerts", value: numberValue(summary.totalAlerts), color: "var(--red)" },
    ];
    var max = Math.max(1, ...items.map(function (item) { return item.value; }));
    $("ovRows").innerHTML = items.map(function (item) {
      var width = Math.max(2, Math.round((item.value / max) * 100));
      return '<div class="ovrow"><span>' + escapeHtml(item.label) + '</span><div class="ovtrack"><i style="width:' +
        width + "%;background:" + item.color + '"></i></div><b>' + formatNumber(item.value) + "</b></div>";
    }).join("");
  }

  function renderRecovery(summary) {
    var rangeLabel = text(summary.rangeLabel);
    $("recoveryPanel").innerHTML =
      '<div class="bigbox"><div class="bignum">' + formatNumber(summary.recoveryJobs) +
      '</div><div class="cap">Restore jobs in ' + escapeHtml(rangeLabel) + "</div></div>" +
      '<div class="mini"><span>Failed restores</span><b>' + formatNumber(summary.recoveryFailed) + "</b></div>" +
      '<div class="mini"><span>Running restores</span><b>' + formatNumber(summary.recoveryRunning) + "</b></div>";
  }

  function renderClones(summary) {
    var rangeLabel = text(summary.rangeLabel);
    $("clonePanel").innerHTML =
      '<div class="bigbox"><div class="bignum">' + formatNumber(summary.cloneJobs) +
      '</div><div class="cap">Clone jobs in ' + escapeHtml(rangeLabel) + "</div></div>" +
      '<div class="mini"><span>Failed clone jobs</span><b>' + formatNumber(summary.cloneFailed) + "</b></div>" +
      '<div class="mini"><span>Clone sessions</span><b>' + formatNumber(summary.cloneSessionTotal) + "</b></div>";
  }

  /* ── 9 KPI cards (main SPA metric grid + Clone Jobs) ────────────────── */
  function renderKpis(summary) {
    var kpis = [
      ["Clients", summary.totalClients, "--blue"],
      ["Successful Jobs", summary.successfulJobs, "--green"],
      ["Failed Backups", summary.failedJobs, "--red"],
      ["Failed Restores", summary.recoveryFailed, "--red"],
      ["Failed Clones", summary.cloneFailed, "--red"],
      ["Active Jobs", summary.activeJobs, "--blue"],
      ["Recovery Jobs", summary.recoveryJobs, "--amber"],
      ["Alerts", summary.totalAlerts, "--amber"],
      ["Clone Jobs", summary.cloneJobs, "--brand"],
    ];
    $("kpis").innerHTML = kpis.map(function (k) {
      return '<div class="kb" style="--c:var(' + k[2] + ')"><div class="lab">' + k[0] +
        '</div><div class="val">' + escapeHtml(text(k[1])) + "</div></div>";
    }).join("");
  }

  /* ── server health strip ────────────────────────────────────────────── */
  function memoryUsageValue(health) {
    var total = Number(health.ramTotalGb);
    var used = Number(health.ramUsedGb);
    var free = Number(health.ramFreeGb);
    if (Number.isFinite(total) && total > 0) {
      var usedValue = Number.isFinite(used) ? used : (Number.isFinite(free) ? Math.max(0, total - free) : NaN);
      if (Number.isFinite(usedValue)) return formatDecimal(usedValue) + " / " + formatDecimal(total) + " GB";
      return formatDecimal(total) + " GB total";
    }
    var ram = health.ramUsagePercent;
    return ram === null || ram === undefined ? "--" : formatNumber(ram) + "%";
  }
  function memoryUsageDetail(health) {
    var percent = health.ramUsagePercent;
    var free = Number(health.ramFreeGb);
    if (Number.isFinite(free) && percent !== null && percent !== undefined) {
      return formatDecimal(free) + " GB free · " + formatNumber(percent) + "% used";
    }
    if (health.ramDetail) return health.ramDetail;
    return health.source || "No memory metric returned.";
  }
  function toneColor(tone) {
    return tone === "ok" ? "var(--green)" : tone === "warn" ? "var(--amber)" : tone === "bad" ? "var(--red)" : "var(--ink)";
  }
  function renderHealth(data) {
    var health = (data && data.serverHealth) || {};
    var maintenance = (data && (data.serverProtectionJob || data.maintenanceBackup)) || {};
    var cpu = health.cpuUsagePercent;
    var ram = health.ramUsagePercent;
    var statusTone = health.status === "ok" ? "ok" : health.status === "warning" ? "warn" : health.status === "critical" ? "bad" : "";
    var maintTone = maintenance.status === "failed" ? "bad"
      : (maintenance.status === "running" || maintenance.status === "queued" || maintenance.status === "warning") ? "warn"
      : maintenance.status === "succeeded" ? "ok" : "";
    var cpuWidth = Math.max(2, Math.min(100, numberValue(cpu)));
    var ramWidth = Math.max(2, Math.min(100, numberValue(ram)));
    $("healthStrip").innerHTML =
      '<div class="card hc"><div class="t">Server status</div><div class="v" style="color:' + toneColor(statusTone) + '">' +
        escapeHtml(health.label || "Unavailable") + '</div><div class="cap">' +
        escapeHtml(health.detail || "CPU/RAM endpoint did not return data.") + "</div></div>" +
      '<div class="card hc"><div class="t">CPU usage</div><div class="v">' +
        (cpu === null || cpu === undefined ? "--" : formatNumber(cpu) + "%") +
        '</div><div class="meter"><i style="width:' + cpuWidth + '%;background:var(--brand)"></i></div><div class="cap">' +
        escapeHtml(health.cpuDetail || health.source || "No CPU metric returned.") + "</div></div>" +
      '<div class="card hc"><div class="t">Memory usage</div><div class="v">' + escapeHtml(memoryUsageValue(health)) +
        '</div><div class="meter"><i style="width:' + ramWidth + '%;background:var(--amber)"></i></div><div class="cap">' +
        escapeHtml(memoryUsageDetail(health)) + "</div></div>" +
      '<div class="card hc"><div class="t">Server Protection Job</div><div class="v" style="color:' + toneColor(maintTone) + '">' +
        escapeHtml(maintenance.label || "Not found") + '</div><div class="cap">' +
        escapeHtml(maintenance.detail || "No Server Protection job found in this range.") + "</div></div>";
  }

  /* ── snapshot growth strip (GET /api/snapshots?range=7d) ───────────── */
  function renderSnapshots(data) {
    var strip = $("snapStrip");
    if (!data || !data.ok || !Array.isArray(data.metrics) || !data.metrics.length) {
      strip.classList.add("hidden");
      return;
    }
    strip.classList.remove("hidden");
    $("snapCap").textContent = "Local Snapshot Growth · comparing " + text(data.previousDate) + " → " + text(data.currentDate);
    var grid = $("snapgrid");
    grid.style.gridTemplateColumns = "repeat(" + data.metrics.length + ",1fr)";
    var BAD_ON_GROWTH = { failedJobs: 1, totalAlerts: 1 };
    grid.innerHTML = data.metrics.map(function (item) {
      var delta = Number(item.delta || 0);
      var sign = delta > 0 ? "+" : "";
      var pct = item.deltaPercent === null || item.deltaPercent === undefined ? "--" : sign + formatDecimal(item.deltaPercent) + "%";
      var badOnGrowth = !!BAD_ON_GROWTH[item.key];
      var trend = delta === 0 ? "fl" : ((delta > 0) === !badOnGrowth ? "up" : "dn");
      var bcls = trend === "up" ? "b-up" : trend === "dn" ? "b-dn" : "b-fl";
      var col = trend === "up" ? "var(--green)" : trend === "dn" ? "var(--red)" : "var(--muted)";
      var edge = trend === "up" ? "--green" : trend === "dn" ? "--red" : "--line";
      var maxBar = Math.max(numberValue(item.previous), numberValue(item.current), 1);
      var prevW = ((numberValue(item.previous) / maxBar) * 100).toFixed(1);
      var currW = ((numberValue(item.current) / maxBar) * 100).toFixed(1);
      return '<div class="card sc" style="--sc:var(' + edge + ')"><div class="h"><span class="n">' + escapeHtml(item.label) +
        '</span><span class="badge ' + bcls + '">' + escapeHtml(pct) + '</span></div><div class="d" style="color:' + col + '">' +
        sign + formatNumber(delta) + "</div>" +
        '<div class="ba"><span>Before</span><div class="tk"><i style="width:' + prevW + '%;background:var(--muted);opacity:.5"></i></div><span>' + formatNumber(item.previous) + "</span></div>" +
        '<div class="ba"><span>After</span><div class="tk"><i style="width:' + currW + '%;background:' + col + '"></i></div><span>' + formatNumber(item.current) + "</span></div></div>";
    }).join("");
  }
  function refreshSnapshots() {
    fetch("/api/snapshots?range=7d", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(renderSnapshots)
      .catch(function () { $("snapStrip").classList.add("hidden"); });
  }

  /* ── bottom table: auto-cycle Recent jobs → Failed jobs → Clients ──── */
  var VIEWS = [
    {
      key: "jobs", title: "Recent Jobs", tab: "Recent Jobs",
      grid: "13% 17% 7% 9% 20% 8% 7% 19%",
      columns: [["client", "Client"], ["name", "Job"], ["policy", "Policy"], ["status", "Status"],
                ["started", "Started"], ["duration", "Duration"], ["size", "Size"], ["message", "Message"]],
    },
    {
      key: "failedJobs", title: "Failed Jobs", tab: "Failed Jobs",
      grid: "14% 19% 10% 19% 38%",
      columns: [["client", "Client"], ["name", "Job"], ["policy", "Policy"], ["started", "Started"], ["message", "Message"]],
    },
    {
      key: "clients", title: "Clients", tab: "Clients",
      grid: "16% 8% 13% 31% 32%",
      columns: [["hostname", "Hostname"], ["enabled", "Enabled"], ["backupType", "Backup Type"],
                ["saveSets", "Save Sets"], ["protectionGroups", "Protection Groups"]],
    },
  ];

  function renderTabs() {
    $("jtabs").innerHTML = VIEWS.map(function (view, index) {
      return '<span class="jtab' + (index === viewIndex ? " on" : "") + '">' + view.tab + "</span>";
    }).join("");
  }

  function cellHtml(view, row, colIndex) {
    var key = view.columns[colIndex][0];
    var value = row ? row[key] : "";
    if (!row) return "<span></span>";
    if (key === "status") return '<span><span class="st ' + pillClass(value) + '">' + escapeHtml(text(value)) + "</span></span>";
    if (colIndex === 0) return "<span><b>" + escapeHtml(text(value)) + "</b></span>";
    return "<span>" + escapeHtml(text(value)) + "</span>";
  }

  function renderJobsTable() {
    var view = VIEWS[viewIndex];
    var rows = (latestDashboard && latestDashboard.tables && latestDashboard.tables[view.key]) || [];
    var rowsVar = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--rows"), 10) || 14;
    document.querySelector(".tblbody").style.setProperty("--tcols", view.grid);
    $("jt").textContent = view.title;
    renderTabs();
    $("thead").innerHTML = view.columns.map(function (col) { return "<span>" + col[1] + "</span>"; }).join("");
    var visible = rows.slice(0, rowsVar);
    $("rowsof").textContent = visible.length + " of " + rows.length + " rows";
    if (!rows.length) {
      $("tb").innerHTML = '<div class="tbempty">No records in this view yet.</div>';
      return;
    }
    var html = "";
    for (var i = 0; i < rowsVar; i += 1) {
      var row = visible[i] || null;
      html += '<div class="trow">';
      for (var c = 0; c < view.columns.length; c += 1) html += cellHtml(view, row, c);
      html += "</div>";
    }
    $("tb").innerHTML = html;
  }

  $("cycsec").textContent = String(Math.round(CYCLE_MS / 1000));
  setInterval(function () {
    if (document.body.classList.contains("jobs-min")) return; /* pause cycling while minimized */
    if (!latestDashboard) return;
    viewIndex = (viewIndex + 1) % VIEWS.length;
    renderJobsTable();
  }, CYCLE_MS);

  /* ── jobs minimize: single click on the header (or the − button) ───── */
  function setMinimized(min, persist) {
    document.body.classList.toggle("jobs-min", min);
    $("minbtn").innerHTML = min ? "&#9633;" : "&#8722;";
    if (persist) {
      try { localStorage.setItem(MIN_KEY, min ? "1" : "0"); } catch (error) {}
    }
  }
  $("jhead").addEventListener("click", function (ev) {
    if (ev.target.closest(".jtab")) return; /* tab clicks don't minimize */
    setMinimized(!document.body.classList.contains("jobs-min"), true);
  });
  try { if (localStorage.getItem(MIN_KEY) === "1") setMinimized(true, false); } catch (error) {}

  /* ── full render from a dashboard payload ──────────────────────────── */
  function renderAll(dashboard) {
    latestDashboard = dashboard;
    lastReceivedMs = Date.now();
    document.body.classList.remove("no-data");
    var summary = dashboard.summary || {};
    var target = dashboard.target || {};
    updatedAtText = String(dashboard.generatedAt || updatedAtText || "");
    $("connPill").textContent = "Connected";
    $("connPill").classList.add("ok");
    $("brandConn").textContent = "Connection established";
    $("brandApi").textContent = text(target.apiMode).toUpperCase();
    $("brandServer").textContent = text(target.backupServer || target.restApiHost);
    $("brandUpdated").textContent = text(dashboard.generatedAt);
    $("connline").innerHTML = "Connected to <b>" + escapeHtml(text(target.restApiBase || target.backupServerBase)) +
      "</b> · " + escapeHtml(text(summary.rangeLabel)) + " · Updated <b id=\"agoline\">--</b>";
    renderMix(summary);
    renderSla(summary);
    renderOverview(summary);
    renderRecovery(summary);
    renderClones(summary);
    renderKpis(summary);
    renderHealth(dashboard);
    renderJobsTable();
    tickClock();
  }

  function showWaiting() {
    document.body.classList.add("no-data");
    $("connPill").textContent = "Waiting";
    $("connPill").classList.remove("ok");
    $("connline").textContent = "Waiting for dashboard data — connect from the main dashboard.";
  }

  /* ── data: initial fetch + poll fallback ───────────────────────────── */
  function loadCurrentDashboard() {
    return fetch(dashboardUrl(), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.ok && data.dashboard) {
          if (DISPLAY_TOKEN && data.theme) applyTheme(data.theme);
          renderAll(data.dashboard);
          if (data.updatedAt) updatedAtText = String(data.updatedAt);
          return true;
        }
        return false;
      })
      .catch(function () { return false; });
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(function () {
      loadCurrentDashboard().then(function (got) {
        if (!got && !latestDashboard) showWaiting();
        schedulePoll();
      });
    }, POLL_MS);
  }

  /* ── SSE live push (re-render on dashboard events) ─────────────────── */
  function startSSE() {
    if (DISPLAY_TOKEN) { return; }
    if (sseSource || !window.EventSource) return;
    sseSource = new EventSource("/api/stream");
    sseSource.onopen = function () { setLive(true); };
    sseSource.addEventListener("dashboard", function (event) {
      try {
        var data = JSON.parse(event.data);
        if (data && data.ok) {
          setLive(true);
          renderAll(data);
        }
      } catch (error) {}
    });
    sseSource.onerror = function () {
      if (sseSource) { sseSource.close(); sseSource = null; }
      setLive(false);
      setTimeout(startSSE, RECONNECT_MS); /* auto-reconnect */
    };
  }

  /* ── boot ───────────────────────────────────────────────────────────── */
  renderTabs();
  loadCurrentDashboard().then(function (got) {
    if (!got) showWaiting();
    schedulePoll();
  });
  refreshSnapshots();
  setInterval(refreshSnapshots, SNAP_REFRESH_MS);
  if (DISPLAY_TOKEN) { setInterval(function () { loadCurrentDashboard(); }, 60000); }
  startSSE();
})();
