
    (function(){
      const _fetch = window.fetch;
      let csrfToken = "";
      async function refreshCsrfToken(){
        try {
          const r = await _fetch("/api/csrf", {cache: "no-store"});
          if (r.ok) { csrfToken = (await r.json()).csrfToken || ""; }
        } catch (_e) {}
        return csrfToken;
      }
      function isMutatingApi(args){
        const opts = args[1] || {};
        const method = String(opts.method || "GET").toUpperCase();
        const url = (args[0] && args[0].url) ? args[0].url : String(args[0] || "");
        return method !== "GET" && method !== "HEAD" && url.indexOf("/api/") !== -1;
      }
      function withCsrf(args){
        if (isMutatingApi(args) && csrfToken) {
          const opts = Object.assign({}, args[1] || {});
          opts.headers = Object.assign({}, opts.headers || {}, {"X-CSRF-Token": csrfToken});
          return [args[0], opts];
        }
        return args;
      }
      window.fetch = async function(...args){
        // A mutating call fired before the token bootstrap finished would be
        // rejected 403 (and needlessly retried); wait for the bootstrap once.
        if (isMutatingApi(args) && !csrfToken) { await csrfReady; }
        let resp = await _fetch.apply(this, withCsrf(args));
        const url = (args[0] && args[0].url) ? args[0].url : String(args[0] || "");
        if (resp.status === 403 && url.indexOf("/api/") !== -1) {
          // Stale/missing CSRF token (e.g. tab restored after server restart):
          // re-bootstrap once and retry the original request.
          const before = csrfToken;
          await refreshCsrfToken();
          if (csrfToken && csrfToken !== before) {
            resp = await _fetch.apply(this, withCsrf(args));
          }
        }
        try {
          if (resp.status === 401 && url.indexOf("/api/") !== -1) { location.reload(); }
        } catch (_e) {}
        return resp;
      };
      const csrfReady = refreshCsrfToken();
    })();
    function initCollapsibles(){
      var toggles = document.querySelectorAll('[data-toggle-target]');
      for (var i = 0; i < toggles.length; i++){
        (function(btn){
          var panel = document.getElementById(btn.getAttribute('data-toggle-target'));
          if (!panel) return;
          var key = 'collapse:' + btn.getAttribute('data-toggle-target');
          var open = false;
          try { open = localStorage.getItem(key) === 'open'; } catch (_e) {}
          panel.classList.toggle('open', open);
          btn.setAttribute('aria-expanded', open ? 'true' : 'false');
          btn.addEventListener('click', function(){
            var now = !panel.classList.contains('open');
            panel.classList.toggle('open', now);
            btn.setAttribute('aria-expanded', now ? 'true' : 'false');
            try { localStorage.setItem(key, now ? 'open' : 'closed'); } catch (_e) {}
          });
        })(toggles[i]);
      }
    }
    initCollapsibles();
    // Close a collapsible dropdown panel and keep its toggle + saved state in sync.
    function closeCollapsiblePanel(id) {
      var panel = document.getElementById(id);
      if (!panel || !panel.classList.contains('open')) return false;
      panel.classList.remove('open');
      var toggle = document.querySelector('[data-toggle-target="' + id + '"]');
      if (toggle) toggle.setAttribute('aria-expanded', 'false');
      try { localStorage.setItem('collapse:' + id, 'closed'); } catch (_e) {}
      return true;
    }
    // The account menu is a floating dropdown: any click outside it (or on one
    // of its action buttons) closes it. Inline collapsibles are left alone.
    document.addEventListener('click', function (event) {
      var menu = document.getElementById('accountMenu');
      if (!menu || !menu.classList.contains('open')) return;
      var toggle = document.querySelector('[data-toggle-target="accountMenu"]');
      if (toggle && toggle.contains(event.target)) return; // toggle handles itself
      if (menu.contains(event.target)) {
        if (event.target.closest && event.target.closest('button')) {
          closeCollapsiblePanel('accountMenu');
        }
        return;
      }
      closeCollapsiblePanel('accountMenu');
    });
    const form = document.getElementById("connectionForm");
    const topStatus = document.getElementById("topStatus");
    const discoverBtn = document.getElementById("discoverBtn");
    const refreshBtn = document.getElementById("refreshBtn");
    const manualRefreshBtn = document.getElementById("manualRefreshBtn");
    const exportBtn = document.getElementById("exportBtn");
    const showConnectionBtn = document.getElementById("showConnectionBtn");
    const dashReportRange = document.getElementById("dashReportRange");
    const customStartDate = document.getElementById("customStartDate");
    const customEndDate = document.getElementById("customEndDate");
    const dashCustomStartDate = document.getElementById("dashCustomStartDate");
    const dashCustomEndDate = document.getElementById("dashCustomEndDate");
    const autoRefreshMode = document.getElementById("autoRefreshMode");
    const refreshMinutes = document.getElementById("refreshMinutes");
    const themeSelect = document.getElementById("themeSelect");
    const clearBtn = document.getElementById("clearBtn");
    const alertConfigBtn = document.getElementById("alertConfigBtn");
    const alertAutomationModal = document.getElementById("alertAutomationModal");
    const alertModalCloseBtn = document.getElementById("alertModalCloseBtn");
    const alertScheduleBtn = document.getElementById("alertScheduleBtn");
    const alertTestBtn = document.getElementById("alertTestBtn");
    const alertStopBtn = document.getElementById("alertStopBtn");
    const emailSaveConfigBtn = document.getElementById("emailSaveConfigBtn");
    const emailScheduleType = document.getElementById("emailScheduleType");
    const alertAutomationStatus = document.getElementById("alertAutomationStatus");
    const smtpSecurity = document.getElementById("smtpSecurity");
    const smtpPort = document.getElementById("smtpPort");
    const smtpUsername = document.getElementById("smtpUsername");
    const smtpPassword = document.getElementById("smtpPassword");
    const notice = document.getElementById("notice");
    const healthGrid = document.getElementById("healthGrid");
    const generatedAt = document.getElementById("generatedAt");
    const tableTitle = document.getElementById("tableTitle");
    const tableMeta = document.getElementById("tableMeta");
    const emptyState = document.getElementById("emptyState");
    const tableWrap = document.getElementById("tableWrap");
    const tableHead = document.getElementById("tableHead");
    const tableBody = document.getElementById("tableBody");
    const mgmtConnection = document.getElementById("mgmtConnection");
    const mgmtStatus = document.getElementById("mgmtStatus");
    const mgmtApi = document.getElementById("mgmtApi");
    const mgmtBackupServer = document.getElementById("mgmtBackupServer");
    const mgmtUpdated = document.getElementById("mgmtUpdated");
    const mgmtRange = document.getElementById("mgmtRange");
    const mgmtDonut = document.getElementById("mgmtDonut");
    const mgmtSlaPie = document.getElementById("mgmtSlaPie");
    const mgmtSlaMeta = document.getElementById("mgmtSlaMeta");
    const mgmtBars = document.getElementById("mgmtBars");
    const mgmtRestorePanel = document.getElementById("mgmtRestorePanel");
    const mgmtClonePanel = document.getElementById("mgmtClonePanel");
    const snapshotSaveBtn    = document.getElementById("snapshotSaveBtn");
    const snapshotCompareBtn = document.getElementById("snapshotCompareBtn");
    const snapshotMeta       = document.getElementById("snapshotMeta");
    const snapshotGrid       = document.getElementById("snapshotGrid");
    const snapshotManageBtn  = document.getElementById("snapshotManageBtn");
    const snapshotExportBtn  = document.getElementById("snapshotExportBtn");
    const snapshotPanel      = document.getElementById("snapshotPanel");
    const snapshotPanelClose = document.getElementById("snapshotPanelCloseBtn");
    const autoSnapshotToggle = document.getElementById("autoSnapshotToggle");
    let activeSnapshotRange  = "7d";
    let snapshotHistoryCache = null;
    const SERVER_HEALTH_REFRESH_MS = 60000;

    const metrics = {
      clients: document.getElementById("metricClients"),
      success: document.getElementById("metricSuccess"),
      failed: document.getElementById("metricFailed"),
      failedRestores: document.getElementById("metricFailedRestores"),
      failedClones: document.getElementById("metricFailedClones"),
      active: document.getElementById("metricActive"),
      recovery: document.getElementById("metricRecovery"),
      alerts: document.getElementById("metricAlerts"),
    };

    const tableDefs = {
      jobs: {
        title: "Recent Jobs",
        columns: [
          ["client", "Client"],
          ["name", "Job"],
          ["policy", "Policy"],
          ["status", "Status"],
          ["started", "Started"],
          ["duration", "Duration"],
          ["size", "Size"],
          ["message", "Message"],
        ],
      },
      failedJobs: {
        title: "Failed Jobs",
        columns: [
          ["client", "Client"],
          ["name", "Job"],
          ["policy", "Policy"],
          ["started", "Started"],
          ["message", "Message"],
        ],
      },
      recovery: {
        title: "Restores",
        columns: [
          ["client", "Client"],
          ["name", "Restore"],
          ["policy", "Policy"],
          ["status", "Status"],
          ["started", "Started"],
          ["duration", "Duration"],
          ["message", "Message"],
        ],
      },
      cloneJobs: {
        title: "Clone Jobs",
        columns: [
          ["client", "Client"],
          ["name", "Clone Job"],
          ["policy", "Policy"],
          ["status", "Status"],
          ["started", "Started"],
          ["duration", "Duration"],
          ["message", "Message"],
        ],
      },
      logs: {
        title: "Log",
        columns: [
          ["priority", "Priority"],
          ["time", "Time"],
          ["source", "Source"],
          ["category", "Category"],
          ["message", "Message"],
        ],
      },
      alerts: {
        title: "Alerts",
        columns: [
          ["severity", "Severity"],
          ["time", "Time"],
          ["message", "Message"],
          ["resource", "Resource"],
        ],
      },
      clients: {
        title: "Clients",
        columns: [
          ["hostname", "Hostname"],
          ["enabled", "Enabled"],
          ["backupType", "Backup Type"],
          ["saveSets", "Save Sets"],
          ["protectionGroups", "Protection Groups"],
        ],
      },
    };

    let latestDashboard = null;
    let activeTable = "jobs";
    let sessionId = null;
    let refreshTimer = null;
    let healthRefreshTimer = null;

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
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }

    function formatNumber(value) {
      try {
        return new Intl.NumberFormat().format(numberValue(value));
      } catch (error) {
        return String(numberValue(value));
      }
    }

    function formatDecimal(value, digits = 1) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "--";
      return new Intl.NumberFormat(undefined, {
        minimumFractionDigits: numeric % 1 === 0 ? 0 : digits,
        maximumFractionDigits: digits,
      }).format(numeric);
    }

    function memoryUsageValue(health) {
      const total = Number(health.ramTotalGb);
      const used = Number(health.ramUsedGb);
      const free = Number(health.ramFreeGb);
      if (Number.isFinite(total) && total > 0) {
        const usedValue = Number.isFinite(used) ? used : (Number.isFinite(free) ? Math.max(0, total - free) : NaN);
        if (Number.isFinite(usedValue)) {
          return `${formatDecimal(usedValue)} / ${formatDecimal(total)} GB`;
        }
        return `${formatDecimal(total)} GB total`;
      }
      const ram = health.ramUsagePercent;
      return ram === null || ram === undefined ? "--" : `${formatNumber(ram)}%`;
    }

    function memoryUsageDetail(health) {
      const percent = health.ramUsagePercent;
      const free = Number(health.ramFreeGb);
      if (Number.isFinite(free) && percent !== null && percent !== undefined) {
        return `${formatDecimal(free)} GB free - ${formatNumber(percent)}% used`;
      }
      if (health.ramDetail) return health.ramDetail;
      return health.source || "No memory metric returned.";
    }

    function percentage(part, total) {
      const denominator = numberValue(total);
      if (!denominator) return 0;
      return Math.round((numberValue(part) / denominator) * 100);
    }

    function rangeLabelFromValue(value) {
      const labels = {
        "24h": "Last 24 Hours",
        "7d": "Last Week",
        "30d": "Last Month",
        "custom": "Custom Dates",
      };
      return labels[value] || text(value);
    }

    function syncCustomDateVisibility() {
      const isCustom = form.reportRange.value === "custom" || dashReportRange.value === "custom";
      document.querySelectorAll("[data-custom-range]").forEach((node) => {
        node.hidden = !isCustom;
      });
    }

    function syncRangeToToolbar() {
      dashReportRange.value = form.reportRange.value;
      dashCustomStartDate.value = customStartDate.value;
      dashCustomEndDate.value = customEndDate.value;
      syncCustomDateVisibility();
    }

    function syncRangeToForm() {
      form.reportRange.value = dashReportRange.value;
      customStartDate.value = dashCustomStartDate.value;
      customEndDate.value = dashCustomEndDate.value;
      syncCustomDateVisibility();
    }

    function setStatus(label, tone) {
      topStatus.textContent = label;
      const colors = {
        neutral: "rgba(255, 255, 255, 0.10)",
        ok: "rgba(24, 118, 74, 0.95)",
        warn: "rgba(169, 104, 0, 0.95)",
        bad: "rgba(189, 43, 58, 0.95)",
      };
      topStatus.style.background = colors[tone] || colors.neutral;
    }

    function setLoading(loading) {
      document.getElementById("connectBtn").disabled = loading;
      discoverBtn.disabled = loading;
      refreshBtn.disabled = loading;
      manualRefreshBtn.disabled = loading;
      exportBtn.disabled = loading;
      if (loading) {
        setStatus("Connecting...", "neutral");
      }
    }

    function getPayload() {
      const selectedRange = dashReportRange?.value || form.reportRange.value;
      const pw  = form.password.value;
      const wpw = form.wmiPassword.value;
      return {
        restApiHost: form.restApiHost.value.trim(),
        restApiPort: form.restApiPort.value.trim(),
        backupServerHost: form.backupServerHost.value.trim(),
        backupServerPort: form.backupServerPort.value.trim(),
        username: form.username.value.trim(),
        password: pw  === "(saved)" ? "__profile_password__" : pw,
        profileName: profileSelect.value || "",
        sessionId,
        apiMode: form.apiMode.value,
        apiVersion: form.apiVersion.value,
        reportRange: selectedRange,
        customStartDate: selectedRange === "custom" ? customStartDate.value : "",
        customEndDate: selectedRange === "custom" ? customEndDate.value : "",
        useWmiHealth: form.useWmiHealth.checked,
        wmiUsername: form.wmiUsername.value.trim(),
        wmiPassword: wpw === "(saved)" ? "__profile_password__" : wpw,
        timeoutSeconds: form.timeoutSeconds.value.trim(),
        useAuthcHeader: form.useAuthcHeader.checked,
        verifyTls: form.verifyTls.checked,
      };
    }

    function clearPassword() {
      form.password.value    = "";
      form.wmiPassword.value = "";
    }

    function statusTone(summary, failed) {
      if (failed) return "bad";
      const health = String(summary?.health || "").toLowerCase();
      if (health === "critical") return "bad";
      if (health === "warning") return "warn";
      return "ok";
    }

    function statusText(summary, failed) {
      if (failed) return "Connection issue";
      const health = String(summary?.health || "").toLowerCase();
      if (health === "critical") return "Connected - action required";
      if (health === "warning") return "Connected with warnings";
      return "Connection established";
    }

    const SNAPSHOT_METRIC_BAD_ON_GROWTH = new Set(["failedJobs", "totalAlerts"]);
    function snapshotColor(value, key) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric) || numeric === 0) return "var(--ink)";
      const badOnGrowth = SNAPSHOT_METRIC_BAD_ON_GROWTH.has(key);
      if (numeric > 0) return badOnGrowth ? "var(--red)" : "var(--green)";
      return badOnGrowth ? "var(--green)" : "var(--red)";
    }

    function renderSparklineSvg(values) {
      if (!values || values.length < 2) return "";
      const nums = values.map(Number);
      const min = Math.min(...nums), max = Math.max(...nums);
      const range = max - min || 1;
      const w = 64, h = 22;
      const step = w / (nums.length - 1);
      const pts = nums.map((v, i) => {
        const x = (i * step).toFixed(1);
        const y = (h - 2 - ((v - min) / range) * (h - 4)).toFixed(1);
        return `${x},${y}`;
      }).join(" ");
      return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" class="sparkline" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--brand)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
    }

    function renderSlaGaugeInline(data) {
      const el = document.getElementById("slaGaugeInline");
      if (!el) return;
      if (!data || !data.ok) { el.innerHTML = ""; return; }
      const slaArr = snapshotHistoryCache?.slaHistory;
      const latestSla = slaArr && slaArr.length ? slaArr[slaArr.length - 1].value : 0;
      const pct = Math.min(100, Math.max(0, Number(latestSla) || 0));
      const r = 30, cx = 38, cy = 38;
      const theta = (180 + (pct / 100) * 180) * Math.PI / 180;
      const ex = (cx + r * Math.cos(theta)).toFixed(2);
      const ey = (cy + r * Math.sin(theta)).toFixed(2);
      const color = pct >= 95 ? "var(--green)" : pct >= 85 ? "var(--amber)" : "var(--red)";
      const bgPath = `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`;
      const fgPath = pct > 0.1 ? `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${ex} ${ey}` : "";
      el.innerHTML = `<svg viewBox="0 0 76 44" width="76" height="44" class="sla-gauge-svg">
        <path d="${bgPath}" fill="none" stroke="var(--line)" stroke-width="5" stroke-linecap="round"/>
        ${fgPath ? `<path d="${fgPath}" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round"/>` : ""}
        <text x="${cx}" y="${cy - 8}" text-anchor="middle" font-size="11" font-weight="800" fill="${color}">${pct.toFixed(1)}%</text>
        <text x="${cx}" y="${cy + 2}" text-anchor="middle" font-size="8" fill="var(--muted)">SLA</text>
      </svg>`;
    }

    function renderSnapshotComparison(data) {
      snapshotMeta.textContent = data.message || (
        data.previousDate
          ? `Comparing ${data.previousDate} to ${data.currentDate}`
          : "No previous snapshot found"
      );
      if (!data.ok || !Array.isArray(data.metrics) || !data.metrics.length) {
        snapshotGrid.innerHTML = `
          <div class="snapshot-cell snapshot-empty">
            <span>No comparison available</span>
            <strong class="snap-note">${escapeHtml(data.message || "Save at least two snapshots to compare growth.")}</strong>
            <small>Use <strong>Save snapshot</strong> after each connection to track growth over time.</small>
          </div>`;
        return;
      }
      const prevDate = data.previousDate || "";
      const currDate = data.currentDate || "";
      const rangeLabel = data.range ? ` (${data.range})` : "";
      const history = snapshotHistoryCache?.history || {};
      snapshotGrid.innerHTML = `<div class="snapshot-header">${escapeHtml(prevDate)} &rarr; ${escapeHtml(currDate)}${escapeHtml(rangeLabel)}</div>` +
        data.metrics.map((item) => {
          const delta = Number(item.delta || 0);
          const sign = delta > 0 ? "+" : "";
          const pct = item.deltaPercent === null || item.deltaPercent === undefined ? "--" : `${sign}${formatDecimal(item.deltaPercent)}%`;
          const badOnGrowth = SNAPSHOT_METRIC_BAD_ON_GROWTH.has(item.key);
          const trend = delta === 0 ? "neutral" : (delta > 0 === !badOnGrowth ? "good" : "bad");
          const badgeClass = delta === 0 ? "neutral" : trend;
          const arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "→";
          const sparkVals = (history[item.key] || []).map((h) => h.value);
          const sparkSvg = renderSparklineSvg(sparkVals);
          const maxBar = Math.max(item.previous, item.current, 1);
          const prevW = ((item.previous / maxBar) * 100).toFixed(1);
          const currW = ((item.current  / maxBar) * 100).toFixed(1);
          const currColor = trend === "good" ? "var(--green)" : trend === "bad" ? "var(--red)" : "var(--brand)";
          return `
          <div class="snapshot-cell" data-trend="${trend}">
            <div class="snap-cell-head">
              <span>${escapeHtml(item.label)}</span>
              <span class="snap-badge ${badgeClass}">${arrow} ${pct}</span>
            </div>
            <strong style="color:${snapshotColor(delta, item.key)}">${sign}${formatNumber(delta)}</strong>
            <div class="snap-bars">
              <div class="snap-bar-row"><span>Before</span><div class="snap-bar-track"><div class="snap-bar" style="width:${prevW}%;background:var(--muted);opacity:0.5"></div></div><span>${formatNumber(item.previous)}</span></div>
              <div class="snap-bar-row"><span>After</span><div class="snap-bar-track"><div class="snap-bar" style="width:${currW}%;background:${currColor}"></div></div><span>${formatNumber(item.current)}</span></div>
            </div>
            ${sparkSvg ? `<div style="margin-top:4px">${sparkSvg}</div>` : ""}
          </div>`;
        }).join("");
    }

    async function loadSharedDashboard() {
      try {
        const response = await fetch("/api/current-dashboard", {cache: "no-store"});
        const data = await response.json();
        if (response.ok && data.ok && data.dashboard) {
          sessionId = data.sessionId || data.dashboard.sessionId || sessionId;
          renderDashboard(data.dashboard);
          snapshotMeta.textContent = data.snapshotSummary || "Shared dashboard session restored";
          setStatus("Shared session loaded", "ok");
        }
      } catch (error) {
        if (window.console) console.warn("No shared dashboard session available", error);
      }
    }

    function chartItems(summary, includeClients = false) {
      const rows = [
        {label: "Successful", value: numberValue(summary.successfulJobs), color: "var(--green)"},
        {label: "Failed", value: numberValue(summary.failedJobs), color: "var(--red)"},
        {label: "Running", value: numberValue(summary.activeJobs), color: "var(--blue)"},
        {label: "Restores", value: numberValue(summary.recoveryJobs), color: "var(--amber)"},
        {label: "Clones", value: numberValue(summary.cloneJobs), color: "#8a6fb0"},
        {label: "Alerts", value: numberValue(summary.totalAlerts), color: "#65747c"},
      ];
      if (includeClients) {
        rows.unshift({label: "Clients", value: numberValue(summary.totalClients), color: "#4f8f9e"});
      }
      return rows;
    }

    function conicGradient(items, total) {
      if (!total) return "conic-gradient(#d8e3e8 0deg 360deg)";
      let cursor = 0;
      const segments = [];
      items.forEach((item) => {
        const amount = Math.max(0, numberValue(item.value));
        if (!amount) return;
        const next = cursor + ((amount / total) * 360);
        segments.push(`${item.color} ${cursor.toFixed(2)}deg ${next.toFixed(2)}deg`);
        cursor = next;
      });
      if (segments.length && cursor < 360) {
        segments.push(`${items[0].color} ${cursor.toFixed(2)}deg 360deg`);
      }
      return `conic-gradient(${segments.join(", ")})`;
    }

    function renderDonut(summary) {
      const items = chartItems(summary).slice(0, 4);
      const total = items.reduce((sum, item) => sum + numberValue(item.value), 0);
      if (!total) {
        mgmtDonut.className = "chart-empty";
        mgmtDonut.textContent = "No backup or restore activity in this range";
        return;
      }

      const background = conicGradient(items, total);
      mgmtDonut.className = "";
      mgmtDonut.innerHTML = `
        <div class="donut-layout">
          <div class="donut-chart" style="--donut-bg: ${background}">
            <div class="donut-center">
              <div>
                <strong>${formatNumber(total)}</strong>
                <span>Activity</span>
              </div>
            </div>
          </div>
          <div class="legend-list">
            ${items.map((item) => `
              <div class="legend-item" style="--dot: ${item.color}">
                <span class="legend-dot"></span>
                <span>${escapeHtml(item.label)}</span>
                <strong>${formatNumber(item.value)} (${percentage(item.value, total)}%)</strong>
              </div>
            `).join("")}
          </div>
        </div>
      `;
    }

    function renderSlaPie(summary) {
      const total = numberValue(summary.slaTotalJobs ?? summary.totalJobs);
      const met = numberValue(summary.slaMetJobs ?? summary.successfulJobs);
      const missed = Math.max(0, numberValue(summary.slaMissedJobs ?? (total - met)));
      if (!total) {
        mgmtSlaPie.className = "chart-empty";
        const running = numberValue(summary.activeJobs);
        mgmtSlaPie.textContent = running
          ? `${running} job${running > 1 ? "s" : ""} currently running — SLA pending`
          : "No backup jobs ran in this range";
        mgmtSlaMeta.textContent = "Jobs ran";
        return;
      }

      const percent = numberValue(summary.slaPercent ?? Math.round((met / total) * 100));
      const items = [
        {label: "SLA met", value: met, color: "var(--green)"},
        {label: "Not met", value: missed, color: "var(--red)"},
      ];
      const background = conicGradient(items, total);
      mgmtSlaMeta.textContent = `${formatNumber(total)} jobs`;
      mgmtSlaPie.className = "";
      mgmtSlaPie.innerHTML = `
        <div class="donut-layout">
          <div class="donut-chart" style="--donut-bg: ${background}">
            <div class="donut-center">
              <div>
                <strong>${formatNumber(percent)}%</strong>
                <span>SLA</span>
              </div>
            </div>
          </div>
          <div class="legend-list">
            ${items.map((item) => `
              <div class="legend-item" style="--dot: ${item.color}">
                <span class="legend-dot"></span>
                <span>${escapeHtml(item.label)}</span>
                <strong>${formatNumber(item.value)} (${percentage(item.value, total)}%)</strong>
              </div>
            `).join("")}
          </div>
        </div>
      `;
    }

    function renderBars(summary) {
      const items = chartItems(summary, true);
      const hasData = Object.keys(summary || {}).length > 0;
      if (!hasData) {
        mgmtBars.className = "chart-empty";
        mgmtBars.textContent = "No management data yet";
        return;
      }

      const max = Math.max(1, ...items.map((item) => numberValue(item.value)));
      mgmtBars.className = "bar-chart";
      mgmtBars.innerHTML = items.map((item) => {
        const width = Math.max(2, Math.round((numberValue(item.value) / max) * 100));
        return `
          <div class="bar-row">
            <span>${escapeHtml(item.label)}</span>
            <span class="bar-track">
              <span class="bar-fill" style="--bar-width: ${width}%; --bar-color: ${item.color}"></span>
            </span>
            <span class="bar-value">${formatNumber(item.value)}</span>
          </div>
        `;
      }).join("");
    }

    function renderRecoveryPanel(summary) {
      const rangeLabel = summary.rangeLabel || rangeLabelFromValue(dashReportRange.value);
      const detailRows = summary.recoveryFailed === undefined
        ? `
          <div class="summary-row"><span>Policies</span><strong>${formatNumber(summary.policies)}</strong></div>
          <div class="summary-row"><span>Clients</span><strong>${formatNumber(summary.totalClients)}</strong></div>
          <div class="summary-row"><span>Critical alerts</span><strong>${formatNumber(summary.criticalAlerts)}</strong></div>
        `
        : `
          <div class="summary-row"><span>Failed restores</span><strong>${formatNumber(summary.recoveryFailed)}</strong></div>
          <div class="summary-row"><span>Running restores</span><strong>${formatNumber(summary.recoveryRunning)}</strong></div>
          <div class="summary-row"><span>Clone jobs excluded</span><strong>${formatNumber(summary.cloneJobs)}</strong></div>
        `;

      mgmtRestorePanel.className = "summary-list";
      mgmtRestorePanel.innerHTML = `
        <div class="summary-band">
          <strong>${formatNumber(summary.recoveryJobs)}</strong>
          <span>Restore jobs in ${escapeHtml(rangeLabel)}</span>
        </div>
        ${detailRows}
      `;
    }

    function renderClonePanel(summary) {
      const rangeLabel = summary.rangeLabel || rangeLabelFromValue(dashReportRange.value);
      mgmtClonePanel.className = "summary-list";
      mgmtClonePanel.innerHTML = `
        <div class="summary-band">
          <strong>${formatNumber(summary.cloneJobs)}</strong>
          <span>Clone jobs in ${escapeHtml(rangeLabel)}</span>
        </div>
        <div class="summary-row"><span>Failed clone jobs</span><strong>${formatNumber(summary.cloneFailed)}</strong></div>
        <div class="summary-row"><span>Running clone jobs</span><strong>${formatNumber(summary.cloneRunning)}</strong></div>
        <div class="summary-row"><span>Clone sessions</span><strong>${formatNumber(summary.cloneSessionTotal)}</strong></div>
      `;
    }

    function renderManagement(data, failed = false) {
      const summary = data?.summary || {};
      const target = data?.target || {};
      const tone = statusTone(summary, failed);
      mgmtConnection.className = `connection-line ${tone}`;
      mgmtStatus.textContent = statusText(summary, failed);
      mgmtApi.textContent = text(target.apiMode || form.apiMode.value).toUpperCase();
      mgmtBackupServer.textContent = text(target.backupServer || form.backupServerHost.value.trim());
      mgmtUpdated.textContent = text(data?.generatedAt);
      mgmtRange.textContent = text(summary.rangeLabel || rangeLabelFromValue(dashReportRange.value));
      renderDonut(summary);
      renderSlaPie(summary);
      renderBars(summary);
      renderRecoveryPanel(summary);
      renderClonePanel(summary);
    }

    function resetManagement() {
      mgmtConnection.className = "connection-line";
      mgmtStatus.textContent = "Not connected";
      mgmtApi.textContent = "--";
      mgmtBackupServer.textContent = "--";
      mgmtUpdated.textContent = "--";
      mgmtRange.textContent = "--";
      mgmtDonut.className = "chart-empty";
      mgmtDonut.textContent = "No backup data yet";
      mgmtSlaPie.className = "chart-empty";
      mgmtSlaPie.textContent = "No SLA data yet";
      mgmtSlaMeta.textContent = "Jobs ran";
      mgmtBars.className = "chart-empty";
      mgmtBars.textContent = "No management data yet";
      mgmtRestorePanel.className = "summary-list";
      mgmtRestorePanel.innerHTML = `
        <div class="summary-band">
          <strong>--</strong>
          <span>Restore jobs in selected range</span>
        </div>
      `;
      mgmtClonePanel.className = "summary-list";
      mgmtClonePanel.innerHTML = `
        <div class="summary-band">
          <strong>--</strong>
          <span>Clone jobs in selected range</span>
        </div>
      `;
    }

    function updateMetrics(summary) {
      metrics.clients.textContent = text(summary.totalClients);
      metrics.success.textContent = text(summary.successfulJobs);
      metrics.failed.textContent = text(summary.failedJobs);
      metrics.failedRestores.textContent = text(summary.recoveryFailed);
      metrics.failedClones.textContent = text(summary.cloneFailed);
      metrics.active.textContent = text(summary.activeJobs);
      metrics.recovery.textContent = text(summary.recoveryJobs);
      metrics.alerts.textContent = text(summary.totalAlerts);
    }

    function healthTone(value) {
      const numeric = numberValue(value);
      if (!numeric) return "";
      if (numeric >= 90) return "bad";
      if (numeric >= 75) return "warn";
      return "ok";
    }

    function healthMeter(value, color) {
      const numeric = Math.max(0, Math.min(100, numberValue(value)));
      if (!numeric) return "";
      return `
        <span class="health-meter" aria-hidden="true">
          <span class="health-meter-fill" style="--meter-width: ${numeric}%; --meter-color: ${color}"></span>
        </span>
      `;
    }

    function healthItem(label, value, detail = "", tone = "", meter = "") {
      return `
        <div class="health-item ${tone}">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
          ${meter}
          <small>${escapeHtml(detail)}</small>
        </div>
      `;
    }

    function renderServerHealth(data) {
      const health = data?.serverHealth || {};
      const maintenance = data?.serverProtectionJob || data?.maintenanceBackup || {};
      const cpu = health.cpuUsagePercent;
      const ram = health.ramUsagePercent;
      const statusTone = health.status === "ok" ? "ok" : (health.status === "warning" ? "warn" : (health.status === "critical" ? "bad" : ""));
      const maintenanceTone = maintenance.status === "failed"
        ? "bad"
        : (maintenance.status === "running" || maintenance.status === "queued" || maintenance.status === "warning" ? "warn" : (maintenance.status === "succeeded" ? "ok" : ""));

      healthGrid.innerHTML = [
        healthItem(
          "Server status",
          health.label || "Unavailable",
          health.detail || "CPU/RAM endpoint did not return data.",
          statusTone,
        ),
        healthItem(
          "CPU usage",
          cpu === null || cpu === undefined ? "--" : `${formatNumber(cpu)}%`,
          health.cpuDetail || health.source || "No CPU metric returned.",
          healthTone(cpu),
          healthMeter(cpu, "var(--blue)"),
        ),
        healthItem(
          "Memory usage",
          memoryUsageValue(health),
          memoryUsageDetail(health),
          healthTone(ram),
          healthMeter(ram, "var(--amber)"),
        ),
        healthItem(
          "Server Protection Job",
          maintenance.label || "Not found",
          maintenance.detail || "No Server Protection job found in this range.",
          maintenanceTone,
        ),
      ].join("");
    }

    function sourceNeedsVisibleWarning(item) {
      return item && !item.ok && item.displayWarning !== false && item.severity !== "info";
    }

    function failedSourceSummary(sources) {
      const failed = Object.entries(sources || {}).filter(([, item]) => sourceNeedsVisibleWarning(item));
      if (!failed.length) return "";
      return failed.map(([name, item]) => {
        const path = item.path || name;
        const status = item.status ? `HTTP ${item.status}` : "failed";
        const rawError = String(item.error || "");
        let error = item.userMessage || item.summary || "";
        if (!error) {
          if (path.includes("monitoringactions")) {
            error = "Backup activity source is temporarily unavailable; server health and cached local snapshot data remain visible.";
          } else if (rawError.includes("nwrestapi application was not found") || rawError.includes("HTTP 404")) {
            error = "REST endpoint route is unavailable on the selected host/port.";
          } else {
            error = "Source is temporarily unavailable.";
          }
        }
        return `${path} ${status}: ${error}`;
      }).join(" | ");
    }

    function setActiveTable(tableName) {
      activeTable = tableName;
      document.querySelectorAll(".tab").forEach((node) => {
        node.classList.toggle("active", node.dataset.table === tableName);
      });
    }

    function chooseVisibleTable() {
      const tables = latestDashboard?.tables || {};
      if ((tables[activeTable] || []).length) return;
      const fallback = ["jobs", "failedJobs", "recovery", "cloneJobs", "logs", "alerts", "clients"].find((name) => {
        return (tables[name] || []).length > 0;
      });
      if (fallback) {
        setActiveTable(fallback);
      }
    }

    function badgeClass(value) {
      const status = String(value || "").toLowerCase();
      if (status.includes("success") || status.includes("succeed") || status.includes("complete")) return "success";
      if (status.includes("fail") || status.includes("error") || status.includes("critical")) return "failed";
      if (status.includes("run") || status.includes("active") || status.includes("start")) return "running";
      if (status.includes("warn")) return "warning";
      return "";
    }

    function renderTable() {
      if (activeTable === "timeline" || activeTable === "heatmap") return;
      if (typeof timelineWrap !== "undefined") timelineWrap.classList.add("hidden");
      if (typeof heatmapWrap  !== "undefined") heatmapWrap.classList.add("hidden");
      const def = tableDefs[activeTable] || {title: activeTable, columns: []};
      const rows = latestDashboard?.tables?.[activeTable] || [];
      tableTitle.textContent = def.title;
      tableMeta.textContent = `${rows.length} rows`;
      tableHead.innerHTML = `<tr>${def.columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr>`;

      if (!rows.length) {
        tableWrap.classList.add("hidden");
        paginationBar.classList.add("hidden");
        emptyState.classList.remove("hidden");
        emptyState.textContent = latestDashboard ? "No records returned for this view." : "Enter connection details and connect.";
        return;
      }

      emptyState.classList.add("hidden");
      tableWrap.classList.remove("hidden");
      // Reset pagination only when the visible table actually changed.
      // renderTable() also re-runs for data refreshes of the SAME table (SSE
      // /api/stream pushes every SHARED_REFRESH_SECONDS, auto-refresh, manual
      // refresh); resetting here collapsed the list right back to 25 rows and
      // made the Show more / Show all buttons appear dead.
      if (pageLimitTable !== activeTable) {
        pageLimit = PAGE_SIZE;
        pageLimitTable = activeTable;
      }
      renderTablePage(rows, def);
    }

    function renderDashboard(data) {
      diffAndNotify(latestDashboard, data);
      latestDashboard = data;
      if (data.sessionId) {
        sessionId = data.sessionId;
      }
      updateMetrics(data.summary || {});
      renderManagement(data);
      renderServerHealth(data);
      // Update collapsed bar
      const host = data.target?.restApiBase || data.target?.backupServerBase || form.restApiHost?.value || "";
      const rl   = data.summary?.rangeLabel || "";
      updateCollapsedBar(host, rl);
      const rangeLabel = data.summary?.rangeLabel ? ` - ${data.summary.rangeLabel}` : "";
      generatedAt.textContent = data.generatedAt ? `Updated ${data.generatedAt}${rangeLabel}` : `Updated now${rangeLabel}`;

      const failedSources = Object.values(data.sources || {}).filter((item) => sourceNeedsVisibleWarning(item));
      if (data.stale && data.reportNotice) {
        notice.textContent = data.reportNotice;
        notice.classList.add("show");
      } else if (failedSources.length) {
        notice.textContent = `Backup data loaded with ${failedSources.length} source warning(s): ${failedSourceSummary(data.sources)}`;
        notice.classList.add("show");
      } else {
        notice.textContent = "";
        notice.classList.remove("show");
      }

      const health = data.summary?.health || "unknown";
      if (data.stale) setStatus("Using cached dashboard", "warn");
      else if (health === "critical") setStatus("Attention required", "bad");
      else if (health === "warning") setStatus("Connected with warnings", "warn");
      else setStatus("Connected", "ok");

      document.body.classList.add("connected");
      document.body.classList.remove("connection-open");
      document.getElementById("shareBtn").classList.remove("hidden");
      document.getElementById("logoutBtn").classList.remove("hidden");
      chooseVisibleTable();
      if (activeTable === "timeline") renderTimeline();
      else if (activeTable === "heatmap") renderHeatmap();
      else renderTable();
      refreshBtn.disabled = false;
      manualRefreshBtn.disabled = false;
      exportBtn.disabled = false;
      snapshotSaveBtn.disabled = false;
      snapshotCompareBtn.disabled = false;
      refreshSnapshotStatus();
      scheduleAutoRefresh();
      scheduleServerHealthRefresh();
    }

    function renderFailure(data, fallbackMessage) {
      latestDashboard = data || null;
      updateMetrics(data?.summary || {});
      renderManagement(data || {}, true);
      renderServerHealth(data || {});
      clearServerHealthRefresh();
      generatedAt.textContent = data?.generatedAt ? `Failed ${data.generatedAt}` : "Connection failed";
      tableWrap.classList.add("hidden");
      emptyState.classList.remove("hidden");
      emptyState.textContent = "REST API data was not returned.";
      tableMeta.textContent = "0 rows";

      const sourceErrors = Object.values(data?.sources || {})
        .filter((item) => !item.ok)
        .map((item) => `${item.path || "REST call"}: ${item.error || "failed"}`);
      notice.textContent = sourceErrors.length
        ? sourceErrors.join(" | ")
        : (fallbackMessage || "Unable to load dashboard.");
      notice.classList.add("show");
      setStatus("REST API failed", "bad");
    }

    function resetDashboard() {
      latestDashboard = null;
      sessionId = null;
      clearAutoRefresh();
      clearServerHealthRefresh();
      Object.values(metrics).forEach((node) => node.textContent = "--");
      resetManagement();
      generatedAt.textContent = "Waiting for connection";
      notice.textContent = "";
      notice.classList.remove("show");
      healthGrid.innerHTML = `
        <div class="health-item"><span>Server status</span><strong>Waiting</strong><small>Connect to load server health.</small></div>
        <div class="health-item"><span>CPU usage</span><strong>--</strong><small>Awaiting NetWorker health data.</small></div>
        <div class="health-item"><span>Memory usage</span><strong>--</strong><small>Awaiting NetWorker health data.</small></div>
        <div class="health-item"><span>Server Protection Job</span><strong>--</strong><small>No job data loaded.</small></div>
      `;
      tableWrap.classList.add("hidden");
      emptyState.classList.remove("hidden");
      emptyState.textContent = "Enter connection details and connect.";
      tableMeta.textContent = "0 rows";
      snapshotMeta.textContent = "No local snapshots loaded";
      snapshotGrid.innerHTML = `
        <div class="snapshot-cell">
          <span>Snapshot status</span>
          <strong>Waiting</strong>
          <small>Connect to NetWorker, then save a local snapshot.</small>
        </div>`;
      snapshotSaveBtn.disabled = true;
      snapshotCompareBtn.disabled = false;
      document.getElementById("shareBtn").classList.add("hidden");
      document.getElementById("logoutBtn").classList.add("hidden");
      setStatus("Not connected", "neutral");
      document.body.classList.remove("connected");
      document.body.classList.add("connection-open");
      refreshBtn.disabled = false;
    }

    function currentRefreshMs() {
      const minutes = Math.max(1, Math.min(1440, parseInt(refreshMinutes.value || "5", 10) || 5));
      refreshMinutes.value = String(minutes);
      return minutes * 60 * 1000;
    }

    function clearAutoRefresh() {
      if (refreshTimer) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
      }
    }

    function clearServerHealthRefresh() {
      if (healthRefreshTimer) {
        clearTimeout(healthRefreshTimer);
        healthRefreshTimer = null;
      }
    }

    function scheduleServerHealthRefresh() {
      clearServerHealthRefresh();
      if (!sessionId || !latestDashboard) return;
      healthRefreshTimer = setTimeout(refreshServerHealth, SERVER_HEALTH_REFRESH_MS);
    }

    async function refreshServerHealth() {
      if (!sessionId) return;
      try {
        const response = await fetch("/api/server-health", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({sessionId}),
          cache: "no-store",
        });
        const data = await response.json();
        if (response.ok && data.serverHealth) {
          latestDashboard = latestDashboard || {};
          latestDashboard.serverHealth = data.serverHealth;
          if (data.serverProtectionJob) {
            latestDashboard.serverProtectionJob = data.serverProtectionJob;
            latestDashboard.maintenanceBackup = data.serverProtectionJob;
          }
          renderServerHealth(data);
          const rangeLabel = latestDashboard.summary?.rangeLabel ? ` - ${latestDashboard.summary.rangeLabel}` : "";
          generatedAt.textContent = data.generatedAt ? `Updated ${data.generatedAt}${rangeLabel}` : generatedAt.textContent;
        }
      } catch (error) {
        if (window.console) console.warn("Server health refresh failed", error);
      } finally {
        scheduleServerHealthRefresh();
      }
    }

    function scheduleAutoRefresh() {
      clearAutoRefresh();
      if (autoRefreshMode.value !== "on" || !latestDashboard) return;
      if (!sessionId) return;
      refreshTimer = setTimeout(() => {
        loadDashboard({silent: true});
      }, currentRefreshMs());
    }

    // One-click theme picker. Names must stay in lockstep with the CSS
    // body[data-theme] blocks, the hidden #themeSelect options, and the
    // backend config.THEME_PALETTES (email/PNG report rendering).
    const THEMES = [
      ["default", "Default", "#126e82", "#eef3f6"],
      ["midnight", "Midnight", "#2aa6b8", "#101719"],
      ["graphite", "Graphite", "#3d5a5f", "#f1f2f3"],
      ["contrast", "High contrast", "#005fcc", "#ffffff"],
      ["ocean", "Ocean", "#087f8c", "#e8f4f6"],
      ["forest", "Forest", "#2f6f45", "#eef5ef"],
      ["ruby", "Ruby", "#9f2d55", "#f8eef1"],
      ["steel", "Steel", "#425c78", "#edf1f5"],
      ["arctic", "Arctic", "#0d7891", "#edf7f8"],
      ["citrus", "Citrus", "#617d18", "#f5f7ec"],
      ["harbor", "Harbor", "#235f73", "#eef3f4"],
      ["ember", "Ember", "#8d4a36", "#f6f1ee"],
      ["violet", "Violet", "#6d3fbf", "#f2effa"],
      ["sandstone", "Sandstone", "#8a6d3b", "#f5efe4"],
      ["carbon", "Carbon", "#4dd0e1", "#000000"],
    ];
    const themePicker = document.getElementById("themePicker");

    function renderThemePicker() {
      if (!themePicker) return;
      themePicker.innerHTML = THEMES.map(([name, label, brand, bg]) => `
        <button type="button" class="theme-chip" data-theme="${name}" title="${label}" aria-label="Theme: ${label}">
          <span class="theme-chip-dot" style="--chip-bg:${bg};--chip-brand:${brand}"></span>
          <span class="theme-chip-name">${label}</span>
        </button>`).join("");
      themePicker.querySelectorAll(".theme-chip").forEach((chip) => {
        chip.addEventListener("click", () => applyTheme(chip.dataset.theme));
      });
    }

    function syncThemeChips(value) {
      if (!themePicker) return;
      themePicker.querySelectorAll(".theme-chip").forEach((chip) => {
        chip.classList.toggle("active", chip.dataset.theme === value);
      });
    }

    function applyTheme(theme) {
      const value = theme || "default";
      document.body.dataset.theme = value === "default" ? "" : value;
      themeSelect.value = value;
      syncThemeChips(value);
      try {
        localStorage.setItem("nw_dashboard_theme", value);
      } catch (error) {}
      // Persist the current theme server-side so background-scheduled report
      // emails use the live theme dynamically (fire-and-forget).
      try {
        fetch("/api/ui-theme", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({theme: value}),
          cache: "no-store",
        }).catch(() => {});
      } catch (error) {}
    }

    function syncSmtpSecurityFields() {
      const isPlainSmtp = smtpSecurity.value === "none";
      if (isPlainSmtp) {
        smtpPort.value = "25";
        smtpUsername.value = "";
        smtpPassword.value = "";
      } else if (!smtpPort.value || smtpPort.value === "25") {
        smtpPort.value = smtpSecurity.value === "ssl" ? "465" : "587";
      }
      smtpUsername.disabled = isPlainSmtp;
      smtpPassword.disabled = isPlainSmtp;
      smtpUsername.placeholder = isPlainSmtp ? "Disabled for SMTP without authentication" : "";
      smtpPassword.placeholder = isPlainSmtp ? "Disabled for SMTP without authentication" : "";
    }

    let emailConfigCache = null;

    function syncEmailTypeFields() {
      const isDaily = emailScheduleType.value === "daily_report";
      const intervalEl = document.getElementById("alertIntervalMinutes");
      const reportTimeEl = document.getElementById("dailyReportTime");
      if (intervalEl) {
        intervalEl.disabled = isDaily;
        const lbl = intervalEl.closest("label");
        if (lbl) lbl.classList.toggle("is-disabled", isDaily);
      }
      if (reportTimeEl) {
        reportTimeEl.disabled = !isDaily;
        const lbl = reportTimeEl.closest("label");
        if (lbl) lbl.classList.toggle("is-disabled", !isDaily);
      }
      const quietStart = document.getElementById("quietStart");
      const quietEnd = document.getElementById("quietEnd");
      const digestEl = document.getElementById("emailDigest");
      [quietStart, quietEnd, digestEl].forEach(el => {
        if (!el) return;
        el.disabled = isDaily;
        const lbl = el.closest("label");
        if (lbl) lbl.classList.toggle("is-disabled", isDaily);
      });
    }

    function applyEmailTypeBlock() {
      const c = emailConfigCache;
      if (!c) return;
      const smtpToEl = document.getElementById("smtpTo");
      syncEmailTypeFields();
      if (emailScheduleType.value === "daily_report") {
        smtpToEl.value = c.dailyReport.recipients || "";
        document.getElementById("dailyReportTime").value = c.dailyReport.reportTime || "08:00";
        // NOTE: do NOT touch themeSelect here. The report theme is dynamic and
        // follows the current dashboard theme (persisted server-side); the email
        // modal must never override the shared dashboard theme control.
      } else {
        smtpToEl.value = c.alert.recipients || "";
        document.getElementById("alertTrigger").value = c.alert.trigger || "critical";
        document.getElementById("alertIntervalMinutes").value = c.alert.intervalMinutes || 60;
      }
    }

    function applyEmailConfig() {
      const c = emailConfigCache;
      if (!c) return;
      document.getElementById("smtpHost").value = c.smtp.host || "";
      smtpPort.value = c.smtp.port || "587";
      smtpSecurity.value = c.smtp.security || "starttls";
      smtpUsername.value = c.smtp.username || "";
      document.getElementById("smtpFrom").value = c.smtp.from || "";
      smtpPassword.value = "";
      smtpPassword.placeholder = c.smtp.passwordSaved ? "Saved — leave blank to keep" : "";
      applyEmailTypeBlock();
      syncSmtpSecurityFields();
    }

    async function loadEmailConfigIntoForm() {
      try {
        const r = await fetch("/api/email-config", {cache: "no-store"});
        const d = await r.json();
        if (r.ok && d.ok) {
          emailConfigCache = d;
          applyEmailConfig();
        }
      } catch (_) { /* keep current form values */ }
    }

    let currentEmailAutomationId = "";

    function _emailEscape(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function editEmailRow(id, rows) {
      const s = (rows || []).find(r => r.automationId === id);
      if (!s) return;
      currentEmailAutomationId = id;
      document.getElementById("emailScheduleType").value = s.scheduleType || "alert";
      document.getElementById("alertIntervalMinutes").value = s.intervalMinutes || 60;
      document.getElementById("dailyReportTime").value = s.reportTime || "08:00";
      document.getElementById("alertTrigger").value = s.trigger || "critical";
      document.getElementById("smtpTo").value = s.recipients || "";
      if (s.smtpHost) document.getElementById("smtpHost").value = s.smtpHost;
      if (s.smtpPort) smtpPort.value = s.smtpPort;
      if (s.smtpSecurity) smtpSecurity.value = s.smtpSecurity;
      if (s.smtpUsername) smtpUsername.value = s.smtpUsername;
      if (s.smtpFrom) document.getElementById("smtpFrom").value = s.smtpFrom;
      document.getElementById("quietStart").value = s.quietStart || "";
      document.getElementById("quietEnd").value = s.quietEnd || "";
      document.getElementById("emailDigest").checked = s.digest !== false;
      syncSmtpSecurityFields();
      applyEmailTypeBlock();
      alertAutomationStatus.textContent = "Editing schedule " + id + " - update fields and click Schedule or Save.";
    }

    async function deleteEmailRow(id) {
      try {
        await fetch("/api/alert-automation", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "stop", sessionId, automationId: id}),
          cache: "no-store",
        });
      } catch (_e) {}
      if (currentEmailAutomationId === id) currentEmailAutomationId = "";
      refreshEmailScheduleList();
    }

    async function toggleEmailRow(id, active) {
      try {
        await fetch("/api/alert-automation", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "set_enabled", sessionId, automationId: id, enabled: active}),
          cache: "no-store",
        });
      } catch (_e) {}
      refreshEmailScheduleList();
    }

    async function refreshEmailScheduleList() {
      // Schedules outlive dashboard sessions (they persist server-side and
      // reconnect on their own), so the list is fetched even before connect —
      // otherwise restored schedules would look "vanished" after a restart.
      const list = document.getElementById("emailScheduleList");
      if (!list) return;
      try {
        const r = await fetch("/api/alert-automation", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "list", sessionId}),
          cache: "no-store",
        });
        const data = await r.json();
        if (!r.ok || !data.ok) { list.innerHTML = ""; const cnt1 = document.getElementById("emailScheduleCount"); if (cnt1) cnt1.textContent = "0"; return; }
        const rows = data.schedules || [];
        if (!rows.length) { list.innerHTML = ""; document.getElementById("emailScheduleCount").textContent = "0"; return; }
        document.getElementById("emailScheduleCount").textContent = rows.length;
        list.innerHTML = rows.map(s => {
          const typeLabel = s.scheduleType === "daily_report" ? "Daily report" : "Alert check";
          const cadence = s.scheduleType === "daily_report"
            ? ("at " + (s.reportTime || "08:00"))
            : ("every " + (s.intervalMinutes || 0) + " min");
          const paused = s.enabled === false;
          // Schedules survive restarts/session loss; show how each one will run.
          const linkNote = s.sessionLive === false
            ? (s.reconnectable ? " · reconnects automatically" : " · waiting for a connection")
            : "";
          return '<div class="email-row' + (paused ? " is-disabled" : "") + '" data-id="' + _emailEscape(s.automationId) + '">'
            + '<label class="em-toggle"><input type="checkbox" class="em-active" data-id="' + _emailEscape(s.automationId) + '"' + (paused ? "" : " checked") + '> Active</label>'
            + '<strong>' + _emailEscape(typeLabel) + '</strong>'
            + '<span class="em-meta">' + _emailEscape(s.recipients || "") + ' &middot; '
            + _emailEscape(cadence) + ' &middot; ' + _emailEscape(s.trigger || "") + (paused ? ' &middot; (paused)' : '') + _emailEscape(linkNote) + '</span>'
            + '<div class="em-actions">'
            + '<button type="button" class="ghost em-edit" data-id="' + _emailEscape(s.automationId) + '">Edit</button>'
            + '<button type="button" class="ghost em-del" data-id="' + _emailEscape(s.automationId) + '">Delete</button>'
            + '</div></div>';
        }).join("");
        list.querySelectorAll(".em-edit").forEach(b => b.addEventListener("click", () => editEmailRow(b.getAttribute("data-id"), rows)));
        list.querySelectorAll(".em-del").forEach(b => b.addEventListener("click", () => deleteEmailRow(b.getAttribute("data-id"))));
        list.querySelectorAll(".em-active").forEach(b => b.addEventListener("change", () => toggleEmailRow(b.getAttribute("data-id"), b.checked)));
      } catch (_e) { list.innerHTML = ""; const cnt2 = document.getElementById("emailScheduleCount"); if (cnt2) cnt2.textContent = "0"; }
    }

    // ── Named email notification profiles (form presets saved server-side) ──
    // Loading a profile fills the form; the password field carries the
    // "(saved)" sentinel, which the server swaps for the stored password.
    const EMAIL_PROFILE_PW_SAVED = "(saved)";
    let loadedEmailProfileName = "";
    let emailProfilesCache = {};
    const emailProfileCards = document.getElementById("emailProfileCards");
    const emailProfileNameInput = document.getElementById("emailProfileName");
    const emailProfileSaveBtn = document.getElementById("emailProfileSaveBtn");

    function emailProfileSummary(p) {
      const typeLabel = p.scheduleType === "daily_report" ? "Daily report" : "Alert check";
      const recips = String(p.smtpTo || "").split(/[;,]/).map(s => s.trim()).filter(Boolean);
      const recipLabel = recips.length
        ? (recips[0] + (recips.length > 1 ? " +" + (recips.length - 1) : ""))
        : "no recipients";
      const cadence = p.scheduleType === "daily_report"
        ? "at " + (p.reportTime || "08:00")
        : "every " + (p.intervalMinutes || 60) + " min";
      return typeLabel + " · " + recipLabel + " · " + cadence;
    }

    // Profile CARD list: name + summary + status dot + animated ON/OFF switch
    // (toggle = schedule/stop that profile) + Edit/Delete.
    function renderEmailProfileCards(profiles) {
      emailProfilesCache = profiles || {};
      if (!emailProfileCards) return;
      const names = Object.keys(emailProfilesCache).sort((a, b) => a.localeCompare(b));
      if (!names.length) {
        emailProfileCards.innerHTML = '<div class="ep-empty">No saved profiles yet — fill the form and save one below.</div>';
        return;
      }
      emailProfileCards.innerHTML = names.map(n => {
        const p = emailProfilesCache[n] || {};
        const on = p.enabled === true;
        return '<div class="ep-card' + (on ? " is-on" : "") + '" data-name="' + _emailEscape(n) + '">'
          + '<span class="ep-dot' + (on ? " on" : "") + '" aria-hidden="true"></span>'
          + '<div class="ep-card-main">'
          + '<strong class="ep-card-name">' + _emailEscape(n) + '</strong>'
          + '<span class="ep-card-sub">' + _emailEscape(emailProfileSummary(p)) + '</span>'
          + '</div>'
          + '<div class="ep-card-actions">'
          + '<button type="button" class="ghost ep-btn ep-edit" data-name="' + _emailEscape(n) + '">&#9998; Edit</button>'
          + '<button type="button" class="ghost ep-btn ep-del" data-name="' + _emailEscape(n) + '">&#128465; Delete</button>'
          + '<label class="ep-switch" title="' + (on ? "Scheduled - click to stop" : "Off - click to schedule") + '">'
          + '<input type="checkbox" class="ep-toggle" data-name="' + _emailEscape(n) + '"' + (on ? " checked" : "")
          + ' aria-label="Toggle schedule for profile ' + _emailEscape(n) + '">'
          + '<span class="ep-slider" aria-hidden="true"></span>'
          + '</label>'
          + '</div></div>';
      }).join("");
      emailProfileCards.querySelectorAll(".ep-edit").forEach(b =>
        b.addEventListener("click", () => loadEmailProfile(b.getAttribute("data-name"))));
      emailProfileCards.querySelectorAll(".ep-del").forEach(b =>
        b.addEventListener("click", () => deleteEmailProfile(b.getAttribute("data-name"))));
      emailProfileCards.querySelectorAll(".ep-toggle").forEach(t =>
        t.addEventListener("change", () => toggleEmailProfile(t.getAttribute("data-name"), t.checked, t)));
    }

    async function toggleEmailProfile(name, enabled, inputEl) {
      // Optimistic: the checkbox has already animated; reconcile from the
      // server response (or revert on failure).
      const card = inputEl && inputEl.closest ? inputEl.closest(".ep-card") : null;
      if (card) card.classList.toggle("is-on", enabled);
      if (inputEl) inputEl.disabled = true;
      try {
        const data = await emailProfileRequest({
          action: "toggle-profile", profileName: name, enabled: !!enabled, sessionId,
        });
        renderEmailProfileCards(data.profiles || emailProfilesCache);
        alertAutomationStatus.textContent = data.message
          || ('Profile "' + name + '" ' + (enabled ? "scheduled." : "stopped."));
        setStatus(enabled ? "Profile scheduled" : "Profile stopped", enabled ? "ok" : "neutral");
      } catch (error) {
        if (inputEl) { inputEl.checked = !enabled; inputEl.disabled = false; }
        if (card) card.classList.toggle("is-on", !enabled);
        alertAutomationStatus.textContent = error.message || "Toggling email profile failed";
        setStatus("Profile toggle failed", "bad");
      }
      refreshEmailScheduleList();
    }

    async function emailProfileRequest(body) {
      const r = await fetch("/api/alert-automation", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
        cache: "no-store",
      });
      const data = await r.json();
      if (!r.ok || data.ok === false) {
        throw new Error(data.error || ("Email profile request failed with HTTP " + r.status));
      }
      return data;
    }

    async function refreshEmailProfileList() {
      try {
        const data = await emailProfileRequest({action: "list-profiles"});
        renderEmailProfileCards(data.profiles || {});
      } catch (_e) { /* keep the current card list */ }
    }

    function applyEmailProfileToForm(name, p) {
      if (!p) return;
      document.getElementById("smtpHost").value = p.smtpHost || "";
      smtpPort.value = p.smtpPort || "587";
      smtpSecurity.value = p.smtpSecurity || "starttls";
      smtpUsername.value = p.smtpUsername || "";
      document.getElementById("smtpFrom").value = p.smtpFrom || "";
      document.getElementById("smtpTo").value = p.smtpTo || "";
      document.getElementById("emailScheduleType").value = p.scheduleType || "alert";
      document.getElementById("alertIntervalMinutes").value = p.intervalMinutes || 60;
      document.getElementById("dailyReportTime").value = p.reportTime || "08:00";
      document.getElementById("alertTrigger").value = p.trigger || "critical";
      document.getElementById("quietStart").value = p.quietStart || "";
      document.getElementById("quietEnd").value = p.quietEnd || "";
      document.getElementById("emailDigest").checked = p.digest !== false;
      // "(saved)" sentinel = keep the profile's stored password on submit.
      smtpPassword.value = p.smtpPassword === EMAIL_PROFILE_PW_SAVED ? EMAIL_PROFILE_PW_SAVED : "";
      loadedEmailProfileName = name;
      if (emailProfileNameInput) emailProfileNameInput.value = name;
      syncSmtpSecurityFields();
      syncEmailTypeFields();
      alertAutomationStatus.textContent =
        'Loaded email profile "' + name + '" - review, then Schedule, Save, or Send test.';
    }

    async function saveEmailProfile() {
      const name = ((emailProfileNameInput && emailProfileNameInput.value) || "").trim()
        || loadedEmailProfileName || "";
      if (!name) {
        alertAutomationStatus.textContent = "Enter a profile name before saving.";
        setStatus("Profile name required", "warn");
        return;
      }
      const payload = alertAutomationPayload("save-profile");
      payload.profileName = name;
      if (emailProfileSaveBtn) emailProfileSaveBtn.disabled = true;
      try {
        const data = await emailProfileRequest(payload);
        renderEmailProfileCards(data.profiles || emailProfilesCache);
        loadedEmailProfileName = name;
        alertAutomationStatus.textContent = data.message || ('Email profile "' + name + '" saved.');
        setStatus("Email profile saved", "ok");
        // Re-saving an ON profile re-arms its schedule server-side.
        refreshEmailScheduleList();
      } catch (error) {
        alertAutomationStatus.textContent = error.message || "Saving email profile failed";
        setStatus("Email profile save failed", "bad");
      } finally {
        if (emailProfileSaveBtn) emailProfileSaveBtn.disabled = false;
      }
    }

    async function loadEmailProfile(profileName) {
      const name = (profileName || "").trim()
        || ((emailProfileNameInput && emailProfileNameInput.value) || "").trim();
      if (!name) {
        alertAutomationStatus.textContent = "Pick a saved profile to load.";
        return;
      }
      try {
        const data = await emailProfileRequest({action: "get-profile", profileName: name});
        applyEmailProfileToForm(name, data.profile || null);
      } catch (error) {
        alertAutomationStatus.textContent = error.message || "Loading email profile failed";
        setStatus("Email profile load failed", "bad");
      }
    }

    async function deleteEmailProfile(profileName) {
      const name = (profileName || "").trim()
        || ((emailProfileNameInput && emailProfileNameInput.value) || "").trim();
      if (!name) {
        alertAutomationStatus.textContent = "Pick a saved profile to delete.";
        return;
      }
      if (!window.confirm('Delete email profile "' + name + '"?')) return;
      try {
        const data = await emailProfileRequest({action: "delete-profile", profileName: name});
        renderEmailProfileCards(data.profiles || {});
        if (loadedEmailProfileName === name) loadedEmailProfileName = "";
        if (emailProfileNameInput && emailProfileNameInput.value === name) emailProfileNameInput.value = "";
        alertAutomationStatus.textContent = data.message || ('Email profile "' + name + '" deleted.');
        setStatus("Email profile deleted", "neutral");
      } catch (error) {
        alertAutomationStatus.textContent = error.message || "Deleting email profile failed";
        setStatus("Email profile delete failed", "bad");
      }
    }

    function openAlertAutomationModal() {
      alertAutomationModal.classList.add("open");
      alertAutomationModal.setAttribute("aria-hidden", "false");
      syncSmtpSecurityFields();
      loadEmailConfigIntoForm();
      applyEmailTypeBlock();
      refreshEmailScheduleList();
      refreshEmailProfileList();
      setTimeout(() => document.getElementById("smtpHost").focus(), 0);
    }

    function closeAlertAutomationModal() {
      alertAutomationModal.classList.remove("open");
      alertAutomationModal.setAttribute("aria-hidden", "true");
      smtpPassword.value = "";
      currentEmailAutomationId = "";
      loadedEmailProfileName = "";
      alertConfigBtn.focus();
    }

    function alertAutomationPayload(action) {
      const payload = {
        action,
        sessionId,
        automationId: currentEmailAutomationId || "",
        // Lets the server resolve the "(saved)" password sentinel against the
        // loaded profile's stored password.
        emailProfileName: loadedEmailProfileName || "",
        smtpHost: document.getElementById("smtpHost").value.trim(),
        smtpPort: smtpPort.value.trim(),
        smtpSecurity: smtpSecurity.value,
        smtpUsername: smtpSecurity.value === "none" ? "" : smtpUsername.value.trim(),
        smtpPassword: smtpSecurity.value === "none" ? "" : smtpPassword.value,
        smtpFrom: document.getElementById("smtpFrom").value.trim(),
        smtpTo: document.getElementById("smtpTo").value.trim(),
        intervalMinutes: document.getElementById("alertIntervalMinutes").value.trim(),
        trigger: document.getElementById("alertTrigger").value,
        scheduleType: document.getElementById("emailScheduleType").value,
        reportTime: document.getElementById("dailyReportTime").value.trim(),
        quietStart: document.getElementById("quietStart").value.trim(),
        quietEnd: document.getElementById("quietEnd").value.trim(),
        digest: document.getElementById("emailDigest").checked,
        theme: themeSelect.value || "default",
      };
      if (action === "test" && payload.scheduleType === "daily_report" && latestDashboard) {
        payload.dashboard = {
          generatedAt: latestDashboard.generatedAt,
          target: latestDashboard.target || {},
          summary: latestDashboard.summary || {},
          serverHealth: latestDashboard.serverHealth || {},
          serverProtectionJob: latestDashboard.serverProtectionJob || latestDashboard.maintenanceBackup || {},
          theme: payload.theme,
        };
      }
      return payload;
    }

    function smtpDebugSummary(debug) {
      if (!debug || typeof debug !== "object") return "";
      const lines = [
        `SMTP stage: ${debug.stage || "unknown"}`,
        `SMTP host: ${debug.host || "--"}:${debug.port || "--"}`,
        `SMTP security: ${debug.security || "none"}`,
        `SMTP auth: ${debug.usernameProvided ? "enabled" : "disabled"}`,
        `Recipients: ${debug.recipientCount || 0}`,
      ];
      if (debug.detail) lines.unshift(debug.detail);
      return lines.join("\\n");
    }

    async function submitAlertAutomation(action) {
      // Saving config does not need a live session; scheduling/testing does.
      if (!sessionId && action !== "stop" && action !== "save") {
        alertAutomationStatus.textContent = "Connect before scheduling email automations";
        setStatus("Connect first", "warn");
        return;
      }
      const payload = alertAutomationPayload(action);
      alertScheduleBtn.disabled = true;
      alertTestBtn.disabled = true;
      alertStopBtn.disabled = true;
      emailSaveConfigBtn.disabled = true;
      try {
        const response = await fetch("/api/alert-automation", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        const data = await response.json();
        if (!response.ok) {
          const debugText = smtpDebugSummary(data.smtpDebug);
          const message = data.error || `Alert automation failed with HTTP ${response.status}`;
          throw new Error(debugText ? `${message}\\n${debugText}` : message);
        }
        // Persisting (save, or start which also saves) returns the refreshed
        // config — update the cache so the per-type recipients stay in sync.
        if (data.config) emailConfigCache = data.config;
        const successDebug = action === "test" ? smtpDebugSummary(data.smtpDebug) : "";
        alertAutomationStatus.textContent = successDebug
          ? `${data.message || "Alert automation updated"}\\n${successDebug}`
          : data.message || "Alert automation updated";
        if (action === "test") setStatus("Test email sent", "ok");
        if (action === "start") setStatus(payload.scheduleType === "daily_report" ? "Report scheduled" : "Alerts scheduled", "ok");
        if (action === "stop") setStatus("Schedule stopped", "neutral");
        if (action === "save") setStatus("Configuration saved", "ok");
        if (action === "start" || action === "save" || action === "stop") {
          currentEmailAutomationId = "";
          refreshEmailScheduleList();
        }
      } catch (error) {
        alertAutomationStatus.textContent = error.message || "Alert automation failed";
        setStatus("Email automation failed", "bad");
      } finally {
        smtpPassword.value = "";
        alertScheduleBtn.disabled = false;
        alertTestBtn.disabled = false;
        alertStopBtn.disabled = false;
        emailSaveConfigBtn.disabled = false;
      }
    }

    // Quietly load saved snapshots and render the growth/status panel. Called on
    // connect and on page load so the panel reflects stored snapshots instead of
    // staying on the disconnected "Waiting" placeholder.
    async function refreshSnapshotStatus() {
      try {
        if (!snapshotHistoryCache) { await loadSnapshotHistory(); }
        const r = await fetch(`/api/snapshots?range=${encodeURIComponent(activeSnapshotRange)}`, {cache: "no-store"});
        const data = await r.json();
        const count = snapshotHistoryCache?.dates?.length
          ?? (snapshotHistoryCache?.history ? Object.keys(snapshotHistoryCache.history).length : 0);
        if (data && data.ok && Array.isArray(data.metrics) && data.metrics.length) {
          renderSnapshotComparison(data);
        } else if (count > 0) {
          snapshotMeta.textContent = `${count} local snapshot(s) saved`;
          snapshotGrid.innerHTML = `
            <div class="snapshot-cell snapshot-empty">
              <span>Snapshot status</span>
              <strong class="snap-note">${count} snapshot(s) saved</strong>
              <small>Save another (or wait for tomorrow's auto-save) to compare growth.</small>
            </div>`;
        } else {
          snapshotMeta.textContent = latestDashboard ? "No local snapshots yet" : "No local snapshots loaded";
          snapshotGrid.innerHTML = `
            <div class="snapshot-cell snapshot-empty">
              <span>Snapshot status</span>
              <strong class="snap-note">${latestDashboard ? "Ready — no snapshots yet" : "Waiting"}</strong>
              <small>${latestDashboard ? "Use <strong>Save snapshot</strong> or enable Auto-save daily to start tracking growth." : "Connect to NetWorker, then save a local snapshot."}</small>
            </div>`;
        }
      } catch (e) { /* leave existing panel content */ }
    }

    async function saveLocalSnapshot() {
      if (!latestDashboard) {
        setStatus("Connect first", "warn");
        snapshotMeta.textContent = "Load dashboard data before saving a local snapshot.";
        return;
      }
      snapshotSaveBtn.disabled = true;
      try {
        const response = await fetch("/api/snapshots", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            range: activeSnapshotRange,
            dashboard: {
              ok: latestDashboard.ok,
              generatedAt: latestDashboard.generatedAt,
              target: latestDashboard.target || {},
              summary: latestDashboard.summary || {},
              serverHealth: latestDashboard.serverHealth || {},
              serverProtectionJob: latestDashboard.serverProtectionJob || latestDashboard.maintenanceBackup || {},
            },
          }),
          cache: "no-store",
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `Snapshot save failed with HTTP ${response.status}`);
        snapshotMeta.textContent = data.message || "Snapshot saved.";
        snapshotHistoryCache = null;
        await loadSnapshotHistory();
        renderSnapshotComparison(data.comparison || {ok: false, message: "Snapshot saved. Save another snapshot later to compare growth."});
        renderSlaGaugeInline(data.comparison || {});
        setStatus("Snapshot saved", "ok");
      } catch (error) {
        snapshotMeta.textContent = error.message || "Unable to save local snapshot.";
        setStatus("Snapshot failed", "bad");
      } finally {
        snapshotSaveBtn.disabled = false;
      }
    }

    async function loadSnapshotHistory() {
      try {
        const r = await fetch("/api/snapshots?action=history", {cache: "no-store"});
        if (r.ok) snapshotHistoryCache = await r.json();
      } catch (_) {}
    }

    async function compareLocalSnapshots() {
      const originalText = snapshotCompareBtn.textContent;
      snapshotCompareBtn.disabled = true;
      snapshotCompareBtn.textContent = "Comparing...";
      snapshotMeta.textContent = "Comparing local snapshots...";
      snapshotGrid.innerHTML = `
        <div class="snapshot-cell snapshot-empty">
          <span>Comparison running</span>
          <strong class="snap-note">Checking saved snapshot history.</strong>
          <small>Comparing the latest saved snapshot with the nearest previous snapshot in the selected range.</small>
        </div>`;
      try {
        const [resp] = await Promise.all([
          fetch(`/api/snapshots?range=${encodeURIComponent(activeSnapshotRange)}`, {cache: "no-store"}),
          snapshotHistoryCache ? Promise.resolve() : loadSnapshotHistory(),
        ]);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `Snapshot compare failed with HTTP ${resp.status}`);
        renderSnapshotComparison(data);
        renderSlaGaugeInline(data);
        if (data.ok) {
          setStatus("Snapshot compared", "ok");
          showToast(data.message || "Snapshot comparison updated");
        } else {
          setStatus("No comparison available", "warn");
          showToast(data.message || "Save at least two snapshots to compare growth");
        }
      } catch (error) {
        snapshotMeta.textContent = error.message || "Unable to compare local snapshots.";
        renderSnapshotComparison({ok: false, message: snapshotMeta.textContent});
        setStatus("Snapshot compare failed", "bad");
        showToast(snapshotMeta.textContent);
      } finally {
        snapshotCompareBtn.disabled = false;
        snapshotCompareBtn.textContent = originalText || "Compare growth";
      }
    }

    async function loadDashboard(options = {}) {
      const payload = getPayload();
      if (!payload.password && !payload.sessionId) {
        setStatus("Password required", "warn");
        notice.textContent = "Enter the password once to connect. Auto-refresh uses a volatile server session after login.";
        notice.classList.add("show");
        return;
      }

      if (!options.silent) {
        setLoading(true);
        notice.textContent = "";
        notice.classList.remove("show");
      }

      try {
        const response = await fetch("/api/dashboard", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        const data = await response.json();
        if (!response.ok) {
          // 401 from server means session expired server-side.
          // If silent refresh, keep showing last good data and note reconnect happening.
          if (response.status === 401 && options.silent && latestDashboard) {
            setStatus("Reconnecting…", "warn");
            notice.textContent = "Session refreshing automatically — no action needed.";
            notice.classList.add("show");
            // Retry with password if available in form, else wait for next cycle
            const retryPayload = getPayload();
            if (retryPayload.password) {
              sessionId = null;
              const retryResp = await fetch("/api/dashboard", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(retryPayload),
                cache: "no-store",
              });
              if (retryResp.ok) {
                const retryData = await retryResp.json();
                renderDashboard(retryData);
                return;
              }
            }
            scheduleAutoRefresh();
            return;
          }
          renderFailure(data, data.error || data.message || `HTTP ${response.status}`);
          return;
        }
        renderDashboard(data);
      } catch (error) {
        if (options.silent && latestDashboard) {
          if (window.console) console.warn("Dashboard auto-refresh failed; keeping last successful data", error);
          scheduleAutoRefresh();
          return;
        }
        setStatus("Connection failed", "bad");
        notice.textContent = error.message || "Unable to load dashboard.";
        notice.classList.add("show");
      } finally {
        payload.password = "";
        clearPassword();
        if (!options.silent) {
          setLoading(false);
        }
      }
    }

    async function exportReport() {
      const payload = getPayload();
      if (!payload.password && !payload.sessionId && latestDashboard) {
        payload.dashboard = latestDashboard;
      }
      if (!payload.password && !payload.sessionId && !payload.dashboard) {
        setStatus("Password required", "warn");
        notice.textContent = "Reconnect before exporting if the in-memory session is no longer available.";
        notice.classList.add("show");
        return;
      }
      setLoading(true);
      try {
        const response = await fetch("/api/export", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.error || `Export failed with HTTP ${response.status}`);
        }
        const blob = await response.blob();
        const disposition = response.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : "networker_dashboard_report.xlsx";
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
        setStatus("Report exported", "ok");
      } catch (error) {
        setStatus("Export failed", "bad");
        notice.textContent = error.message || "Unable to export Excel report.";
        notice.classList.add("show");
      } finally {
        payload.password = "";
        clearPassword();
        setLoading(false);
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      loadDashboard();
    });

    refreshBtn.addEventListener("click", () => {
      loadDashboard();
    });

    manualRefreshBtn.addEventListener("click", () => {
      loadDashboard();
    });

    discoverBtn.addEventListener("click", () => {
      loadDashboard();
    });

    exportBtn.addEventListener("click", () => {
      exportReport();
    });

    alertConfigBtn.addEventListener("click", () => {
      openAlertAutomationModal();
    });

    alertModalCloseBtn.addEventListener("click", () => {
      closeAlertAutomationModal();
    });

    // Backdrop dismiss that survives resizing/drag. A bare "click" fires on the
    // common ancestor of pointerdown+pointerup, so grabbing the .modal-panel
    // resize grip (down on panel) and releasing over the backdrop would target
    // the backdrop and falsely close. Only dismiss when BOTH the press and the
    // release land directly on the backdrop element itself.
    function bindBackdropDismiss(modalEl, closeFn) {
      let downOnBackdrop = false;
      modalEl.addEventListener("pointerdown", (event) => {
        downOnBackdrop = (event.target === modalEl);
      });
      modalEl.addEventListener("click", (event) => {
        if (downOnBackdrop && event.target === modalEl) closeFn();
        downOnBackdrop = false;
      });
    }

    bindBackdropDismiss(alertAutomationModal, closeAlertAutomationModal);

    // Single document-level Escape handler: closes only the topmost open
    // popup per key press (ordered by stacking: drawer > dropdown > modals).
    function closeTopmostPopup() {
      if (jobDetailDrawer.classList.contains("open")) { closeJobDrawer(); return true; }
      const accountMenu = document.getElementById("accountMenu");
      if (accountMenu && accountMenu.classList.contains("open")) return closeCollapsiblePanel("accountMenu");
      if (snapshotPanel.classList.contains("open")) { closeSnapshotPanel(); return true; }
      if (alertAutomationModal.classList.contains("open")) { closeAlertAutomationModal(); return true; }
      if (shareModal.classList.contains("open")) { closeShareModal(); return true; }
      if (addServerModal.classList.contains("open")) { closeAddServerModal(); return true; }
      return false;
    }

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && closeTopmostPopup()) event.preventDefault();
    });

    alertScheduleBtn.addEventListener("click", () => {
      submitAlertAutomation("start");
    });

    alertTestBtn.addEventListener("click", () => {
      submitAlertAutomation("test");
    });

    alertStopBtn.addEventListener("click", () => {
      submitAlertAutomation("stop");
    });

    emailSaveConfigBtn.addEventListener("click", () => {
      submitAlertAutomation("save");
    });

    if (emailProfileSaveBtn) emailProfileSaveBtn.addEventListener("click", () => { saveEmailProfile(); });

    // Switching Email type swaps to that type's separately-saved recipients and
    // settings without losing the other type's values.
    emailScheduleType.addEventListener("change", applyEmailTypeBlock);

    snapshotSaveBtn.addEventListener("click", () => { saveLocalSnapshot(); });
    snapshotCompareBtn.addEventListener("click", () => { compareLocalSnapshots(); });

    snapshotExportBtn.addEventListener("click", async () => {
      try {
        const r = await fetch("/api/snapshots?action=export", {cache: "no-store"});
        const csv = await r.text();
        const blob = new Blob([csv], {type: "text/csv"});
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `networker_snapshots_${new Date().toISOString().slice(0,10)}.csv`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast("Snapshots exported");
      } catch (e) { showToast("Export failed: " + e.message); }
    });

    snapshotManageBtn.addEventListener("click", openSnapshotPanel);
    snapshotPanelClose.addEventListener("click", closeSnapshotPanel);
    bindBackdropDismiss(snapshotPanel, closeSnapshotPanel);

    autoSnapshotToggle.addEventListener("change", async () => {
      const enabled = autoSnapshotToggle.checked;
      try {
        const resp = await fetch("/api/snapshots", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "auto-config", enabled}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!enabled) {
          showToast("Auto-snapshot disabled");
        } else {
          const messages = {
            saved: "Auto-snapshot enabled — snapshot saved now",
            exists: "Auto-snapshot enabled — today already captured",
            "no-dashboard": "Auto-snapshot enabled — will capture once connected",
          };
          showToast(messages[data.result] || "Auto-snapshot enabled");
          if (data.summary) snapshotMeta.textContent = data.summary;
          if (data.result === "saved") { snapshotHistoryCache = null; refreshSnapshotStatus(); }
        }
      } catch (e) { showToast("Failed to update auto-snapshot setting"); }
    });

    async function openSnapshotPanel() {
      snapshotPanel.setAttribute("aria-hidden", "false");
      snapshotPanel.classList.add("open");
      document.getElementById("snapshotPanelBody").innerHTML = "<p style='color:var(--muted);padding:8px'>Loading…</p>";
      try {
        const r = await fetch("/api/snapshots?action=list", {cache: "no-store"});
        const json = await r.json();
        renderSnapshotPanelList(json.snapshots || []);
      } catch (e) {
        document.getElementById("snapshotPanelBody").innerHTML = `<p style='color:var(--red)'>Failed to load: ${escapeHtml(e.message)}</p>`;
      }
    }

    function closeSnapshotPanel() {
      snapshotPanel.setAttribute("aria-hidden", "true");
      snapshotPanel.classList.remove("open");
    }

    function renderSnapshotPanelList(snapshots) {
      const body = document.getElementById("snapshotPanelBody");
      if (!snapshots.length) {
        body.innerHTML = "<p style='color:var(--muted);padding:8px'>No snapshots saved yet.</p>";
        return;
      }
      body.innerHTML = `<table class="snap-panel-table">
        <thead><tr><th>Date</th><th>Server</th><th>Health</th><th>SLA %</th><th>Note</th><th></th></tr></thead>
        <tbody>${snapshots.map((s) => `
          <tr data-date="${escapeHtml(s.date)}">
            <td><strong>${escapeHtml(s.date)}</strong></td>
            <td class="cell-muted cell-small">${escapeHtml(s.server || "—")}</td>
            <td class="cell-small">${escapeHtml(s.health || "—")}</td>
            <td class="cell-small">${Number(s.slaPercent || 0).toFixed(1)}%</td>
            <td><button class="snap-panel-annotation" data-date="${escapeHtml(s.date)}" title="Click to edit note">${escapeHtml(s.annotation || "Add note…")}</button></td>
            <td><button class="ghost snap-del-btn" data-date="${escapeHtml(s.date)}" type="button">Delete</button></td>
          </tr>`).join("")}
        </tbody>
      </table>`;
      body.querySelectorAll(".snap-del-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const date = btn.dataset.date;
          if (!confirm(`Delete snapshot for ${date}?`)) return;
          try {
            const r = await fetch("/api/snapshots", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({action: "delete", date}),
            });
            const j = await r.json();
            renderSnapshotPanelList(j.snapshots || []);
            snapshotHistoryCache = null;
            compareLocalSnapshots();
            showToast(`Snapshot ${date} deleted`);
          } catch (e) { showToast("Delete failed: " + e.message); }
        });
      });
      body.querySelectorAll(".snap-panel-annotation").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const date = btn.dataset.date;
          const note = prompt("Add a note for this snapshot:", btn.textContent === "Add note…" ? "" : btn.textContent);
          if (note === null) return;
          try {
            await fetch("/api/snapshots", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({action: "annotate", date, note}),
            });
            btn.textContent = note || "Add note…";
            showToast("Note saved");
          } catch (e) { showToast("Save failed: " + e.message); }
        });
      });
    }

    // Load auto-snapshot state on init
    fetch("/api/snapshots?action=auto-config", {cache: "no-store"})
      .then((r) => r.json())
      .then((j) => { if (j.ok) autoSnapshotToggle.checked = !!j.enabled; })
      .catch(() => {});

    // Show any already-saved snapshots on load (independent of connection state).
    refreshSnapshotStatus();

    document.getElementById("snapRangeTabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".snap-tab");
      if (!btn) return;
      document.querySelectorAll(".snap-tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      activeSnapshotRange = btn.dataset.range || "7d";
      compareLocalSnapshots();
    });

    showConnectionBtn.addEventListener("click", () => {
      document.body.classList.toggle("connection-open");
    });

    dashReportRange.addEventListener("change", () => {
      syncRangeToForm();
      if (latestDashboard) loadDashboard();
    });

    form.reportRange.addEventListener("change", () => {
      syncRangeToToolbar();
    });

    dashCustomStartDate.addEventListener("change", () => {
      syncRangeToForm();
      if (latestDashboard && dashReportRange.value === "custom") loadDashboard();
    });

    dashCustomEndDate.addEventListener("change", () => {
      syncRangeToForm();
      if (latestDashboard && dashReportRange.value === "custom") loadDashboard();
    });

    customStartDate.addEventListener("change", syncRangeToToolbar);
    customEndDate.addEventListener("change", syncRangeToToolbar);

    autoRefreshMode.addEventListener("change", scheduleAutoRefresh);
    refreshMinutes.addEventListener("change", scheduleAutoRefresh);
    themeSelect.addEventListener("change", () => applyTheme(themeSelect.value));
    smtpSecurity.addEventListener("change", syncSmtpSecurityFields);

    clearBtn.addEventListener("click", () => {
      form.reset();
      form.restApiPort.value = "9090";
      form.backupServerPort.value = "9090";
      form.apiMode.value = "auto";
      form.apiVersion.value = "auto";
      form.reportRange.value = "24h";
      customStartDate.value = "";
      customEndDate.value = "";
      syncRangeToToolbar();
      form.timeoutSeconds.value = "30";
      form.useWmiHealth.checked = true;
      form.useAuthcHeader.checked = true;
      form.verifyTls.checked = true;
      smtpPassword.value = "";
      syncSmtpSecurityFields();
      alertAutomationStatus.textContent = "Not scheduled";
      clearPassword();
      resetDashboard();
    });

    document.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => {
        const tbl = button.dataset.table;
        setActiveTable(tbl);
        if (tbl === "timeline") {
          renderTimeline();
        } else if (tbl === "heatmap") {
          renderHeatmap();
        } else {
          renderTable();
        }
      });
    });

    // ── Timeline (Gantt) ─────────────────────────────────────────────────────
    const timelineWrap = document.getElementById("timelineWrap");
    const tlTooltip    = document.getElementById("tlTooltip");

    function tlStatusClass(status) {
      const s = String(status || "").toLowerCase();
      if (s.includes("success") || s.includes("succeed") || s.includes("complete")) return "success";
      if (s.includes("fail") || s.includes("error") || s.includes("critical")) return "failed";
      if (s.includes("run") || s.includes("active") || s.includes("start")) return "running";
      if (s.includes("warn")) return "warning";
      return "unknown";
    }

    // The backend formats job timestamps for display as strftime
    // "%d-%m-%Y %H:%M:%S %Z" (e.g. "18-07-2026 08:45:12 +04"), which
    // new Date() cannot parse — every job came back NaN, leaving the Timeline
    // empty ("No parseable timestamps") and the Heatmap without job recency.
    // Parse DD-MM-YYYY [HH:MM[:SS]] positionally as LOCAL time (the server
    // formats in its own local zone and viewers are on the same site) and
    // ignore any trailing timezone token ("+04", "Arabian Standard Time", …).
    // Anything else (ISO strings, epoch numbers) falls back to new Date().
    const DDMMYYYY_TS_RE = /^(\d{1,2})-(\d{1,2})-(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?(?:\s|$)/;
    function parseTs(str) {
      if (!str && str !== 0) return NaN;
      const m = typeof str === "string" ? DDMMYYYY_TS_RE.exec(str.trim()) : null;
      if (m) {
        const day = Number(m[1]), month = Number(m[2]), year = Number(m[3]);
        if (day >= 1 && day <= 31 && month >= 1 && month <= 12) {
          const d = new Date(year, month - 1, day,
            Number(m[4] || 0), Number(m[5] || 0), Number(m[6] || 0));
          return isNaN(d.getTime()) ? NaN : d.getTime();
        }
      }
      const d = new Date(str);
      return isNaN(d.getTime()) ? NaN : d.getTime();
    }

    function parseDurationMs(val) {
      if (!val) return 0;
      const s = String(val);
      let total = 0;
      const d = s.match(/(\d+)d/); if (d) total += parseInt(d[1]) * 86400000;
      const h = s.match(/(\d+)h/); if (h) total += parseInt(h[1]) * 3600000;
      const m = s.match(/(\d+)m/); if (m) total += parseInt(m[1]) * 60000;
      const sec = s.match(/(\d+)s/); if (sec) total += parseInt(sec[1]) * 1000;
      if (total === 0 && /^\d+$/.test(s)) total = parseInt(s) * 1000;
      return total;
    }

    function renderTimeline() {
      tableWrap.classList.add("hidden");
      emptyState.classList.add("hidden");
      heatmapWrap.classList.add("hidden");
      paginationBar.classList.add("hidden");
      timelineWrap.classList.remove("hidden");

      const jobs = latestDashboard?.tables?.jobs || [];
      if (!jobs.length) {
        timelineWrap.innerHTML = '<p style="padding:16px;color:var(--muted)">No job data to display.</p>';
        return;
      }

      // Build per-client job list
      const clientMap = new Map();
      let minTs = Infinity, maxTs = -Infinity;
      jobs.forEach((job) => {
        const ts = parseTs(job.started);
        if (isNaN(ts)) return;
        const dur = parseDurationMs(job.duration) || 600000;
        const end = ts + dur;
        minTs = Math.min(minTs, ts);
        maxTs = Math.max(maxTs, end);
        const key = job.client || "Unknown";
        if (!clientMap.has(key)) clientMap.set(key, []);
        clientMap.get(key).push({...job, _ts: ts, _end: end});
      });

      if (minTs === Infinity) {
        timelineWrap.innerHTML = '<p style="padding:16px;color:var(--muted)">No parseable timestamps in job data.</p>';
        return;
      }

      const clients = [...clientMap.keys()].sort();
      const ROW_H = 28, ROW_GAP = 6, LABEL_W = 160, AXIS_H = 28, PAD = 12;
      const totalW = Math.max(700, timelineWrap.clientWidth || 900) - PAD * 2;
      const chartW = totalW - LABEL_W;
      const totalH = clients.length * (ROW_H + ROW_GAP) + AXIS_H + PAD;
      const spanMs = maxTs - minTs || 3600000;

      function xOf(ts) { return LABEL_W + ((ts - minTs) / spanMs) * chartW; }

      // Axis ticks (up to 6)
      const tickCount = Math.min(6, Math.floor(chartW / 80));
      const tickLines = [];
      for (let i = 0; i <= tickCount; i++) {
        const ts = minTs + (i / tickCount) * spanMs;
        const x = xOf(ts);
        const label = new Date(ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
        tickLines.push(`<line x1="${x}" y1="${AXIS_H - 6}" x2="${x}" y2="${totalH}" stroke="var(--line)" stroke-width="1"/>`);
        tickLines.push(`<text class="tl-axis-label" x="${x}" y="${AXIS_H - 10}" text-anchor="middle">${escapeHtml(label)}</text>`);
      }

      const bars = [];
      clients.forEach((client, rowIdx) => {
        const y = AXIS_H + PAD / 2 + rowIdx * (ROW_H + ROW_GAP);
        const labelY = y + ROW_H / 2 + 4;
        bars.push(`<text class="tl-client-label" x="${LABEL_W - 8}" y="${labelY}" text-anchor="end">${escapeHtml(client.length > 22 ? client.slice(0, 20) + "…" : client)}</text>`);
        clientMap.get(client).forEach((job) => {
          const x1 = xOf(job._ts);
          const x2 = xOf(job._end);
          const bw = Math.max(4, x2 - x1);
          const cls = tlStatusClass(job.status);
          const tip = `${client}\n${job.name || "Job"}\nStatus: ${job.status}\nStarted: ${job.started}\nDuration: ${job.duration}`;
          bars.push(`<rect class="tl-bar ${cls}" x="${x1.toFixed(1)}" y="${y}" width="${bw.toFixed(1)}" height="${ROW_H}" data-tip="${escapeHtml(tip)}"/>`);
        });
      });

      timelineWrap.innerHTML = `
        <div class="timeline-svg-container">
          <svg width="${totalW}" height="${totalH}" style="display:block">
            ${tickLines.join("")}
            ${bars.join("")}
          </svg>
        </div>`;

      timelineWrap.querySelectorAll(".tl-bar").forEach((el) => {
        el.addEventListener("mousemove", (e) => {
          tlTooltip.style.display = "block";
          tlTooltip.style.left = (e.clientX + 14) + "px";
          tlTooltip.style.top  = (e.clientY - 8) + "px";
          tlTooltip.textContent = el.dataset.tip;
        });
        el.addEventListener("mouseleave", () => { tlTooltip.style.display = "none"; });
      });
    }

    // ── Heatmap ──────────────────────────────────────────────────────────────
    const heatmapWrap = document.getElementById("heatmapWrap");
    const hmTooltip   = document.getElementById("hmTooltip");

    function renderHeatmap() {
      tableWrap.classList.add("hidden");
      emptyState.classList.add("hidden");
      timelineWrap.classList.add("hidden");
      paginationBar.classList.add("hidden");
      heatmapWrap.classList.remove("hidden");

      const clients = latestDashboard?.tables?.clients || [];
      const jobs    = latestDashboard?.tables?.jobs    || [];

      if (!clients.length) {
        heatmapWrap.innerHTML = '<p style="padding:16px;color:var(--muted)">No client data to display.</p>';
        return;
      }

      // Map client hostname → latest job status
      const statusMap = new Map();
      const lastJobTs = new Map();
      jobs.forEach((job) => {
        const key = (job.client || "").toLowerCase();
        const ts  = parseTs(job.started) || 0;
        if (!lastJobTs.has(key) || ts > lastJobTs.get(key)) {
          lastJobTs.set(key, ts);
          statusMap.set(key, {status: job.status, started: job.started, name: job.name});
        }
      });

      const legend = `
        <div class="heatmap-legend">
          <span><span class="heatmap-legend-dot" style="background:var(--green)"></span>Success</span>
          <span><span class="heatmap-legend-dot" style="background:var(--red)"></span>Failed</span>
          <span><span class="heatmap-legend-dot" style="background:var(--blue)"></span>Running</span>
          <span><span class="heatmap-legend-dot" style="background:var(--amber)"></span>Warning</span>
          <span><span class="heatmap-legend-dot" style="background:var(--line)"></span>No recent job</span>
        </div>`;

      const cells = clients.map((c) => {
        const key  = (c.hostname || "").toLowerCase();
        const info = statusMap.get(key);
        const cls  = info ? tlStatusClass(info.status) : "none";
        const init = (c.hostname || "?").charAt(0).toUpperCase();
        const tip  = info
          ? `${c.hostname}\nLast job: ${info.name || "—"}\nStatus: ${info.status}\nStarted: ${info.started}`
          : `${c.hostname}\nNo backup job in current window`;
        return `<div class="heatmap-cell ${cls}" data-tip="${escapeHtml(tip)}" title="">${escapeHtml(init)}</div>`;
      }).join("");

      heatmapWrap.innerHTML = legend + `<div class="heatmap-grid">${cells}</div>`;

      heatmapWrap.querySelectorAll(".heatmap-cell").forEach((el) => {
        el.addEventListener("mousemove", (e) => {
          hmTooltip.style.display = "block";
          hmTooltip.style.left = (e.clientX + 14) + "px";
          hmTooltip.style.top  = (e.clientY - 8) + "px";
          hmTooltip.textContent = el.dataset.tip;
        });
        el.addEventListener("mouseleave", () => { hmTooltip.style.display = "none"; });
      });
    }

    // ── Multi-server ─────────────────────────────────────────────────────────
    const addServerBtn        = document.getElementById("addServerBtn");
    const addServerModal      = document.getElementById("addServerModal");
    const addServerModalClose = document.getElementById("addServerModalCloseBtn");
    const addServerCancelBtn  = document.getElementById("addServerCancelBtn");
    const addServerConnectBtn = document.getElementById("addServerConnectBtn");
    const addServerStatus     = document.getElementById("addServerStatus");
    const multiServerSection  = document.getElementById("multiServerSection");
    const serverCards         = document.getElementById("serverCards");
    const multiServerMeta     = document.getElementById("multiServerMeta");

    const extraServers = [];  // [{sessionId, host, summary}]

    function openAddServerModal() {
      addServerModal.classList.add("open");
      addServerModal.setAttribute("aria-hidden", "false");
      addServerStatus.textContent = "";
      setTimeout(() => document.getElementById("asHost").focus(), 0);
    }
    function closeAddServerModal() {
      addServerModal.classList.remove("open");
      addServerModal.setAttribute("aria-hidden", "true");
    }

    async function connectExtraServer() {
      const host     = document.getElementById("asHost").value.trim();
      const port     = document.getElementById("asPort").value.trim() || "9090";
      const username = document.getElementById("asUsername").value.trim();
      const password = document.getElementById("asPassword").value;
      const apiMode  = document.getElementById("asApiMode").value;
      const verifyTls = document.getElementById("asVerifyTls").checked;
      if (!host || !username || !password) {
        addServerStatus.textContent = "Host, username, and password are required.";
        return;
      }
      addServerConnectBtn.disabled = true;
      addServerStatus.textContent = "Connecting…";
      try {
        const payload = {
          restApiHost: host, restApiPort: parseInt(port, 10) || 9090,
          backupServerHost: "", backupServerPort: 9090,
          username, password, apiMode, apiVersion: "auto",
          reportRange: "24h", customStartDate: "", customEndDate: "",
          useWmiHealth: false, wmiUsername: "", wmiPassword: "",
          timeoutSeconds: 30, verifyTls, useAuthcHeader: true,
        };
        const resp = await fetch("/api/dashboard", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        extraServers.push({sessionId: data.sessionId, host, summary: data.summary || {}});
        document.getElementById("asPassword").value = "";
        closeAddServerModal();
        renderServerCards();
      } catch (err) {
        addServerStatus.textContent = err.message || "Connection failed.";
      } finally {
        addServerConnectBtn.disabled = false;
      }
    }

    function serverCardBadge(summary) {
      const h = String(summary?.health || "unknown").toLowerCase();
      if (h === "ok" || h === "good") return ["ok", "OK"];
      if (h === "critical" || h === "bad") return ["bad", "Critical"];
      if (h === "warning" || h === "warn") return ["warn", "Warning"];
      return ["load", "Unknown"];
    }

    function renderServerCards() {
      if (!extraServers.length) {
        multiServerSection.classList.add("hidden");
        return;
      }
      multiServerSection.classList.remove("hidden");
      multiServerMeta.textContent = `${extraServers.length} server${extraServers.length !== 1 ? "s" : ""}`;
      serverCards.innerHTML = extraServers.map((srv, idx) => {
        const [badgeCls, badgeLabel] = serverCardBadge(srv.summary);
        const s = srv.summary;
        return `<div class="server-card">
          <div class="server-card-head">
            <span class="server-card-host">${escapeHtml(srv.host)}</span>
            <span class="server-card-badge ${badgeCls}">${escapeHtml(badgeLabel)}</span>
          </div>
          <div class="server-card-stats">
            <span>Jobs</span><strong>${numberValue(s.totalJobs)}</strong>
            <span>Failed</span><strong>${numberValue(s.failedJobs)}</strong>
            <span>Active</span><strong>${numberValue(s.activeJobs)}</strong>
            <span>Alerts</span><strong>${numberValue(s.totalAlerts)}</strong>
          </div>
          <button class="server-card-remove" data-idx="${idx}">Remove</button>
        </div>`;
      }).join("");
      serverCards.querySelectorAll(".server-card-remove").forEach((btn) => {
        btn.addEventListener("click", () => {
          extraServers.splice(parseInt(btn.dataset.idx, 10), 1);
          renderServerCards();
        });
      });
    }

    addServerBtn.addEventListener("click", openAddServerModal);
    addServerModalClose.addEventListener("click", closeAddServerModal);
    addServerCancelBtn.addEventListener("click", closeAddServerModal);
    bindBackdropDismiss(addServerModal, closeAddServerModal);
    addServerConnectBtn.addEventListener("click", connectExtraServer);

    // ── Share ─────────────────────────────────────────────────────────────────
    const shareBtn           = document.getElementById("shareBtn");
    const shareModal         = document.getElementById("shareModal");
    const shareModalClose    = document.getElementById("shareModalCloseBtn");
    const generateShareToken = document.getElementById("generateShareTokenBtn");
    const revokeShareToken   = document.getElementById("revokeShareTokenBtn");
    const shareTokenSection  = document.getElementById("shareTokenSection");
    const shareUrlInput      = document.getElementById("shareUrlInput");
    const copyShareUrlBtn    = document.getElementById("copyShareUrlBtn");
    const shareModalStatus   = document.getElementById("shareModalStatus");

    let currentShareToken = null;

    function openShareModal() {
      shareModal.classList.add("open");
      shareModal.setAttribute("aria-hidden", "false");
      shareModalStatus.textContent = "";
    }
    function closeShareModal() {
      shareModal.classList.remove("open");
      shareModal.setAttribute("aria-hidden", "true");
    }

    async function doGenerateShareToken() {
      if (!sessionId) {
        shareModalStatus.textContent = "Connect to a NetWorker server first.";
        return;
      }
      generateShareToken.disabled = true;
      shareModalStatus.textContent = "Generating…";
      try {
        const resp = await fetch("/api/share", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({sessionId, action: "create"}),
          cache: "no-store",
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        currentShareToken = data.token;
        const viewUrl = `${location.origin}/view/${data.token}`;
        shareUrlInput.value = viewUrl;
        shareTokenSection.classList.remove("hidden");
        revokeShareToken.classList.remove("hidden");
        generateShareToken.textContent = "Regenerate";
        shareModalStatus.textContent = "Link valid for 24 hours.";
      } catch (err) {
        shareModalStatus.textContent = err.message || "Failed to generate link.";
      } finally {
        generateShareToken.disabled = false;
      }
    }

    async function doRevokeShareToken() {
      if (!currentShareToken) return;
      revokeShareToken.disabled = true;
      try {
        await fetch("/api/share", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({token: currentShareToken, action: "revoke"}),
          cache: "no-store",
        });
        currentShareToken = null;
        shareTokenSection.classList.add("hidden");
        revokeShareToken.classList.add("hidden");
        generateShareToken.textContent = "Generate Link";
        shareModalStatus.textContent = "Link revoked.";
        shareUrlInput.value = "";
      } catch (err) {
        shareModalStatus.textContent = "Revoke failed.";
      } finally {
        revokeShareToken.disabled = false;
      }
    }

    document.getElementById("logoutBtn").addEventListener("click", () => {
      stopSSE();
      profileSelect.value = "";
      clearPassword();
      resetDashboard();
    });
    shareBtn.addEventListener("click", openShareModal);
    // TV wall-display mode (v2.8.0): opens /tv in a new tab; the route reuses
    // the same session cookie so no extra sign-in is needed.
    document.getElementById("tvModeBtn").addEventListener("click", () => {
      window.open("/tv", "_blank", "noopener");
    });
    shareModalClose.addEventListener("click", closeShareModal);
    bindBackdropDismiss(shareModal, closeShareModal);
    generateShareToken.addEventListener("click", doGenerateShareToken);
    revokeShareToken.addEventListener("click", doRevokeShareToken);
    copyShareUrlBtn.addEventListener("click", () => {
      if (!shareUrlInput.value) return;
      navigator.clipboard.writeText(shareUrlInput.value).then(() => {
        copyShareUrlBtn.textContent = "Copied!";
        setTimeout(() => { copyShareUrlBtn.textContent = "Copy"; }, 2000);
      }).catch(() => {
        shareUrlInput.select();
        document.execCommand("copy");
      });
    });

    // ── Connection Profiles ───────────────────────────────────────────────────
    const profileSelect    = document.getElementById("profileSelect");
    const profileSaveBtn   = document.getElementById("profileSaveBtn");
    const profileDeleteBtn = document.getElementById("profileDeleteBtn");
    const PROFILES_KEY     = "nw_dashboard_profiles";

    function loadProfiles() {
      try { return JSON.parse(localStorage.getItem(PROFILES_KEY) || "{}"); }
      catch (e) { return {}; }
    }
    function saveProfiles(profiles) {
      try { localStorage.setItem(PROFILES_KEY, JSON.stringify(profiles)); } catch (e) {}
    }
    async function fetchProfiles() {
      try {
        const r = await fetch("/api/profiles");
        if (r.ok) {
          const j = await r.json();
          if (j.profiles) { saveProfiles(j.profiles); refreshProfileList(); }
        }
      } catch (e) {}
    }
    function refreshProfileList() {
      const profiles = loadProfiles();
      const current = profileSelect.value;
      profileSelect.innerHTML = '<option value="">— Select saved profile —</option>';
      Object.keys(profiles).sort().forEach((name) => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        profileSelect.appendChild(opt);
      });
      if (current && profiles[current]) profileSelect.value = current;
    }
    function profileFormValues() {
      return {
        restApiHost: form.restApiHost.value.trim(),
        restApiPort: form.restApiPort.value.trim(),
        backupServerHost: form.backupServerHost.value.trim(),
        backupServerPort: form.backupServerPort.value.trim(),
        username: form.username.value.trim(),
        apiMode: form.apiMode.value,
        apiVersion: form.apiVersion.value,
        reportRange: form.reportRange.value,
        timeoutSeconds: form.timeoutSeconds.value,
        useWmiHealth: form.useWmiHealth.checked,
        wmiUsername: form.wmiUsername.value.trim(),
        verifyTls: form.verifyTls.checked,
        useAuthcHeader: form.useAuthcHeader.checked,
      };
    }
    function applyProfile(profile) {
      if (!profile) return;
      form.restApiHost.value      = profile.restApiHost      || "";
      form.restApiPort.value      = profile.restApiPort      || "9090";
      form.backupServerHost.value = profile.backupServerHost || "";
      form.backupServerPort.value = profile.backupServerPort || "9090";
      form.username.value         = profile.username         || "";
      form.apiMode.value          = profile.apiMode          || "auto";
      form.apiVersion.value       = profile.apiVersion       || "auto";
      form.reportRange.value      = profile.reportRange      || "24h";
      form.timeoutSeconds.value   = profile.timeoutSeconds   || "30";
      form.useWmiHealth.checked   = !!profile.useWmiHealth;
      form.wmiUsername.value      = profile.wmiUsername      || "";
      form.verifyTls.checked      = !!profile.verifyTls;
      form.useAuthcHeader.checked = profile.useAuthcHeader !== false;
      // "(saved)" means server has encrypted password — keep as-is; getPayload() sends sentinel
      form.password.value    = profile.password    || "";
      form.wmiPassword.value = profile.wmiPassword || "";
      syncRangeToToolbar();
    }
    profileSelect.addEventListener("change", () => {
      const profiles = loadProfiles();
      applyProfile(profiles[profileSelect.value]);
    });
    profileSaveBtn.addEventListener("click", async () => {
      const name = prompt("Profile name:", profileSelect.value || form.restApiHost.value || "My Server");
      if (!name) return;
      const data = profileFormValues();
      const pw  = form.password.value;
      const wpw = form.wmiPassword.value;
      if (pw  && pw  !== "(saved)") data.password    = pw;
      if (wpw && wpw !== "(saved)") data.wmiPassword = wpw;
      try {
        const resp = await fetch("/api/profiles", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ action: "save", name, data }),
        });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || "Save failed");
        saveProfiles(json.profiles || {});
        refreshProfileList();
        profileSelect.value = name;
        showToast(`Profile "${name}" saved`);
      } catch (e) { showToast("Profile save failed: " + e.message); }
    });
    profileDeleteBtn.addEventListener("click", async () => {
      const name = profileSelect.value;
      if (!name) return;
      if (!confirm(`Delete profile "${name}"?`)) return;
      try {
        const resp = await fetch("/api/profiles", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ action: "delete", name }),
        });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || "Delete failed");
        saveProfiles(json.profiles || {});
        refreshProfileList();
        showToast(`Profile "${name}" deleted`);
      } catch (e) { showToast("Profile delete failed: " + e.message); }
    });
    fetchProfiles();

    // ── Collapsed connection bar ──────────────────────────────────────────────
    const connCollapsedBar = document.getElementById("connCollapsedBar");
    const collapsedHost    = document.getElementById("collapsedHost");
    const collapsedRange   = document.getElementById("collapsedRange");
    const collapsedEditBtn = document.getElementById("collapsedEditBtn");

    function updateCollapsedBar(host, rangeLabel) {
      collapsedHost.textContent  = host || "—";
      collapsedRange.textContent = rangeLabel ? `· ${rangeLabel}` : "";
    }
    collapsedEditBtn.addEventListener("click", () => {
      document.body.classList.toggle("connection-open");
    });

    // ── SSE live push ─────────────────────────────────────────────────────────
    let sseSource = null;
    function startSSE() {
      if (sseSource) return;
      if (!window.EventSource) return;
      sseSource = new EventSource("/api/stream");
      sseSource.addEventListener("dashboard", (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data && data.ok && latestDashboard) {
            diffAndNotify(latestDashboard, data);
            renderDashboard(data);
          }
        } catch (_) {}
      });
      sseSource.addEventListener("error", () => {
        sseSource.close();
        sseSource = null;
        setTimeout(startSSE, 15000);
      });
    }
    function stopSSE() {
      if (sseSource) { sseSource.close(); sseSource = null; }
    }

    // ── Browser push notifications ────────────────────────────────────────────
    let notifyPermission = (typeof Notification !== "undefined") ? Notification.permission : "denied";
    function requestNotifyPermission() {
      if (typeof Notification === "undefined" || notifyPermission === "granted") return;
      Notification.requestPermission().then((p) => { notifyPermission = p; });
    }
    function sendBrowserNotification(title, body, tag) {
      if (notifyPermission !== "granted" || typeof Notification === "undefined") return;
      try {
        new Notification(title, {body, tag, icon: "/favicon.ico"});
      } catch (_) {}
    }

    // ── "What changed" toast ──────────────────────────────────────────────────
    const changeToast = document.getElementById("changeToast");
    let toastTimer    = null;

    function showToast(message, tone) {
      changeToast.textContent = message;
      changeToast.className   = `change-toast show ${tone || ""}`;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { changeToast.className = "change-toast"; }, 6000);
    }

    function diffAndNotify(prev, next) {
      if (!prev || !next) return;
      const ps = prev.summary || {}, ns = next.summary || {};
      const prevFailed = Number(ps.failedJobs || 0);
      const nextFailed = Number(ns.failedJobs || 0);
      const prevAlerts = Number(ps.totalAlerts || 0);
      const nextAlerts = Number(ns.totalAlerts || 0);
      const prevHealth = String(ps.health || ""), nextHealth = String(ns.health || "");
      if (nextFailed > prevFailed) {
        const delta = nextFailed - prevFailed;
        const msg = `⚠ ${delta} new failure${delta > 1 ? "s" : ""} detected`;
        showToast(msg, "bad");
        sendBrowserNotification("NetWorker Alert", msg, "failures");
        document.title = `(${nextFailed}) NetWorker Dashboard`;
      } else if (prevFailed > 0 && nextFailed === 0) {
        showToast("✓ All failures resolved", "ok");
        document.title = "NetWorker Dashboard";
      } else if (nextAlerts > prevAlerts) {
        showToast(`⚠ ${nextAlerts - prevAlerts} new alert(s)`, "warn");
      } else if (nextHealth !== prevHealth && nextHealth === "ok" && prevHealth !== "") {
        showToast("✓ Dashboard status is now healthy", "ok");
      }
    }

    // ── Pagination ────────────────────────────────────────────────────────────
    const paginationBar  = document.getElementById("paginationBar");
    const paginationMeta = document.getElementById("paginationMeta");
    const showMoreBtn    = document.getElementById("showMoreBtn");
    const showAllBtn     = document.getElementById("showAllBtn");
    const showLessBtn    = document.getElementById("showLessBtn");
    const PAGE_SIZE      = 25;
    let   pageLimit      = PAGE_SIZE;
    // Which table pageLimit currently belongs to; renderTable() resets the
    // limit when this differs from activeTable (i.e. on a real tab switch).
    let   pageLimitTable = null;

    function renderTablePage(rows, def) {
      const showing = Math.min(pageLimit, rows.length);
      const visible = rows.slice(0, showing);
      tableBody.innerHTML = visible.map((row) => {
        return `<tr data-row='${escapeHtml(JSON.stringify(row))}'>${def.columns.map(([key]) => {
          const value = row[key];
          if (key === "status" || key === "severity" || key === "priority") {
            return `<td><span class="badge ${badgeClass(value)}">${escapeHtml(value)}</span></td>`;
          }
          const muted = value ? "" : " cell-muted";
          return `<td class="${muted}">${escapeHtml(value)}</td>`;
        }).join("")}</tr>`;
      }).join("");
      // Wire row clicks for drawer
      tableBody.querySelectorAll("tr").forEach((tr) => {
        tr.addEventListener("click", () => {
          try { openJobDrawer(JSON.parse(tr.dataset.row || "{}")); } catch (_) {}
        });
      });
      const more = showing < rows.length;
      paginationMeta.textContent = more
        ? `Showing ${showing} of ${rows.length}`
        : `Showing all ${rows.length}`;
      // Keep the bar visible whenever the list is expandable so the user always
      // has a control: "Show more/all" while collapsed, "Show less" once
      // expanded. Previously the bar vanished after "Show all", leaving a long
      // list with no way to collapse back — a scroll trap on touch devices.
      if (rows.length <= PAGE_SIZE) {
        paginationBar.classList.add("hidden");
      } else {
        paginationBar.classList.remove("hidden");
        showMoreBtn.classList.toggle("hidden", !more);
        showAllBtn.classList.toggle("hidden", !more);
        showLessBtn.classList.toggle("hidden", more);
        showMoreBtn.disabled = false;
        showAllBtn.disabled  = false;
        showLessBtn.disabled = false;
      }
    }

    // Re-render the active table at the CURRENT pageLimit without resetting it.
    // renderTable() always forces pageLimit back to PAGE_SIZE (correct for
    // tab-switch / data refresh), so the pagination buttons must bypass it —
    // otherwise their pageLimit change is overwritten and they do nothing.
    function repaginate() {
      if (activeTable === "timeline" || activeTable === "heatmap") return;
      const def  = tableDefs[activeTable] || {title: activeTable, columns: []};
      const rows = latestDashboard?.tables?.[activeTable] || [];
      if (!rows.length) return;
      renderTablePage(rows, def);
    }

    showMoreBtn.addEventListener("click", () => {
      pageLimit += PAGE_SIZE;
      repaginate();
    });
    showAllBtn.addEventListener("click", () => {
      pageLimit = Infinity;
      repaginate();
    });
    showLessBtn.addEventListener("click", () => {
      pageLimit = PAGE_SIZE;
      repaginate();
      // Collapsing a long list leaves the viewport scrolled far down; bring the
      // jobs section back into view so the page isn't stuck below the fold
      // (touch devices can't easily drag back up past a tall table).
      tableTitle.scrollIntoView({behavior: "smooth", block: "start"});
    });

    // ── Job detail drawer ─────────────────────────────────────────────────────
    const jobDetailDrawer = document.getElementById("jobDetailDrawer");
    const drawerOverlay   = document.getElementById("drawerOverlay");
    const drawerBody      = document.getElementById("drawerBody");
    const drawerTitle     = document.getElementById("drawerTitle");
    const drawerCloseBtn  = document.getElementById("drawerCloseBtn");

    function openJobDrawer(row) {
      drawerTitle.textContent = row.name || row.client || "Job Details";
      const fields = [
        ["Client",    row.client],
        ["Job Name",  row.name],
        ["Policy",    row.policy],
        ["Status",    row.status],
        ["Started",   row.started],
        ["Duration",  row.duration],
        ["Size",      row.size],
        ["Message",   row.message || "—"],
      ];
      drawerBody.innerHTML = fields.map(([label, value]) => `
        <div class="detail-field">
          <div class="detail-label">${escapeHtml(label)}</div>
          <div class="detail-value">${escapeHtml(value || "—")}</div>
        </div>`).join("") +
        `<button class="ghost detail-copy-btn" id="drawerCopyBtn">Copy all to clipboard</button>`;
      document.getElementById("drawerCopyBtn").addEventListener("click", () => {
        const text = fields.map(([l, v]) => `${l}: ${v || "—"}`).join("\n");
        navigator.clipboard.writeText(text).catch(() => {});
        document.getElementById("drawerCopyBtn").textContent = "Copied!";
        setTimeout(() => { const b = document.getElementById("drawerCopyBtn"); if (b) b.textContent = "Copy all to clipboard"; }, 2000);
      });
      jobDetailDrawer.classList.add("open");
      drawerOverlay.classList.add("open");
    }
    function closeJobDrawer() {
      jobDetailDrawer.classList.remove("open");
      drawerOverlay.classList.remove("open");
    }
    drawerCloseBtn.addEventListener("click", closeJobDrawer);
    drawerOverlay.addEventListener("click", closeJobDrawer);
    // Escape close is handled by the shared closeTopmostPopup keydown handler.

    // Left-edge drag handle: adjusts drawer width (session-only), clamped to
    // 320px .. 90vw. CSS resize is unreliable on fixed right-anchored panels.
    const drawerResizeHandle = document.getElementById("drawerResizeHandle");
    let drawerDragState = null;
    drawerResizeHandle.addEventListener("pointerdown", (e) => {
      drawerDragState = {
        startX: e.clientX,
        startWidth: jobDetailDrawer.getBoundingClientRect().width,
      };
      drawerResizeHandle.classList.add("dragging");
      try { drawerResizeHandle.setPointerCapture(e.pointerId); } catch (_err) {}
      e.preventDefault();
    });
    drawerResizeHandle.addEventListener("pointermove", (e) => {
      if (!drawerDragState) return;
      const maxW = Math.round(window.innerWidth * 0.9);
      const next = Math.min(
        Math.max(drawerDragState.startWidth + (drawerDragState.startX - e.clientX), 320),
        maxW
      );
      jobDetailDrawer.style.width = next + "px";
    });
    const endDrawerDrag = () => {
      drawerDragState = null;
      drawerResizeHandle.classList.remove("dragging");
    };
    drawerResizeHandle.addEventListener("pointerup", endDrawerDrag);
    drawerResizeHandle.addEventListener("pointercancel", endDrawerDrag);

    refreshBtn.disabled = false;
    syncRangeToToolbar();
    renderThemePicker();
    applyTheme((() => {
      try { return localStorage.getItem("nw_dashboard_theme") || "default"; }
      catch (error) { return "default"; }
    })());
    syncSmtpSecurityFields();
    exportBtn.disabled = true;
    snapshotSaveBtn.disabled = true;
    requestNotifyPermission();
    fetchProfiles();
    compareLocalSnapshots();
    loadSharedDashboard();
    startSSE();
    initScheduledReports();
    initDisplayConfig();

    function reportHealthBadge(state) {
      const map = {healthy: "ok", unhealthy: "bad", never_run: "idle"};
      const cls = map[state] || "idle";
      return `<span class="health-badge health-${cls}">${state.replace("_", " ")}</span>`;
    }

    async function renderReportJobs() {
      const list = document.getElementById("reportJobsList");
      if (!list) return;
      const r = await fetch("/api/report-jobs", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action: "list"}),
      });
      const data = await r.json();
      const notice = document.getElementById("reportLegacyNotice");
      if (notice) notice.hidden = !data.legacyMigrationNeeded;
      list.innerHTML = (data.jobs || []).map(j => `
        <div class="report-job">
          <div class="rj-main">
            <strong>${j.kind}</strong> &rarr; ${j.recipients.join(", ")}
            &middot; ${j.kind === "digest" ? "at " + j.reportTime : "every " + j.intervalMinutes + " min"}
            ${reportHealthBadge(j.health.state)}
          </div>
          <div class="rj-sub">last: ${j.health.lastResult || "—"} &middot; next: ${
            j.health.nextRun ? new Date(j.health.nextRun * 1000).toLocaleString() : "—"}</div>
          <button data-del="${j.id}" type="button">Delete</button>
        </div>`).join("") || "<p>No scheduled reports yet.</p>";
      list.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", async () => {
        await fetch("/api/report-jobs", {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "delete", id: b.getAttribute("data-del")})});
        renderReportJobs();
      }));
    }

    async function submitReportJob(ev) {
      ev.preventDefault();
      const f = ev.target;
      const err = document.getElementById("reportFormError");
      err.textContent = "Validating (connect + render + SMTP)…";
      const payload = {
        action: "create", kind: "digest",
        recipients: f.recipients.value, reportTime: f.reportTime.value,
        credential: {
          rest_api_host: f.rest_api_host.value, rest_api_port: Number(f.rest_api_port.value),
          backup_server_host: f.rest_api_host.value, backup_server_port: Number(f.rest_api_port.value),
          username: f.username.value, password: f.password.value, api_mode: "nwui",
        },
      };
      const r = await fetch("/api/report-jobs", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)});
      const data = await r.json();
      if (!data.ok) {
        const failed = Object.entries(data.checks || {}).filter(([, v]) => !v).map(([k]) => k).join(", ");
        err.textContent = `Not saved — failed: ${failed || "validation"}. ${data.message || ""}`;
        return;
      }
      err.textContent = "";
      f.reset(); f.hidden = true;
      renderReportJobs();
    }

    function initScheduledReports() {
      const panel = document.getElementById("scheduledReportsPanel");
      if (!panel) return;
      document.getElementById("reportAddBtn").addEventListener("click",
        () => { document.getElementById("reportJobForm").hidden = false; });
      document.getElementById("reportJobForm").addEventListener("submit", submitReportJob);
      renderReportJobs();
    }

    async function renderDisplayConfig() {
      const panel = document.getElementById("tvDisplayPanel");
      if (!panel) return;
      const r = await fetch("/api/display-config", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action: "get"})});
      const d = await r.json();
      const urlBox = document.getElementById("tvDisplayUrl");
      urlBox.value = d.token ? `${location.origin}/tv/${d.token}` : "(revoked — click Rotate to create one)";
      const st = document.getElementById("tvConnState");
      st.textContent = d.hasConnection ? "connection set" : "not set";
      st.className = "health-badge " + (d.hasConnection ? "health-ok" : "health-idle");
    }

    async function _displayAction(action) {
      await fetch("/api/display-config", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action})});
      renderDisplayConfig();
    }

    async function submitDisplayConn(ev) {
      ev.preventDefault();
      const f = ev.target, err = document.getElementById("tvConnError");
      err.textContent = "Validating connection…";
      const r = await fetch("/api/display-config", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action: "set-connection", credential: {
          rest_api_host: f.rest_api_host.value, rest_api_port: Number(f.rest_api_port.value),
          backup_server_host: f.rest_api_host.value, backup_server_port: Number(f.rest_api_port.value),
          username: f.username.value, password: f.password.value, api_mode: "nwui"}})});
      const d = await r.json();
      if (!d.ok) { err.textContent = "Not saved — " + (d.message || "validation failed"); return; }
      err.textContent = ""; f.reset(); renderDisplayConfig();
    }

    function initDisplayConfig() {
      const panel = document.getElementById("tvDisplayPanel");
      if (!panel) return;
      document.getElementById("tvRotateBtn").addEventListener("click", () => _displayAction("rotate"));
      document.getElementById("tvRevokeBtn").addEventListener("click", () => _displayAction("revoke"));
      document.getElementById("tvConnForm").addEventListener("submit", submitDisplayConn);
      renderDisplayConfig();
    }
