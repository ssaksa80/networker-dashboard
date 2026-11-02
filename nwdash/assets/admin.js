    // Admin console (/admin), after DEDB's: boot asks the server which screen to
    // show (sign in, first run, type the password again, or refused), then a tab
    // strip of sections. Every /api/admin call needs the admin window open; a
    // 403 "unlock_required" brings back the password card, the gateway's 401
    // (X-NWDash-Login: required) the sign-in.
    (function () {
      "use strict";
      const $ = (sel, root) => (root || document).querySelector(sel);
      const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
      const D = window.NWDesign;
      const esc = D.esc;
      const body = document.body;
      const motionOn = () => D.motionOn();

      const TABS = [
        ["health", "Health"], ["sessions", "Sessions"], ["audit", "Audit"], ["logs", "Logs"],
        ["connections", "Connections"], ["email", "Email"], ["schedules", "Schedules"],
        ["snapshots", "Snapshots"], ["shares", "Share links"],
        ["appearance", "Appearance"], ["security", "Security"], ["tls", "TLS"],
      ];
      const THEMES = [
        ["default", "Default", "#eef3f6", "#126e82"], ["midnight", "Midnight", "#101719", "#2aa6b8"],
        ["graphite", "Graphite", "#f1f2f3", "#3d5a5f"], ["contrast", "High contrast", "#ffffff", "#005fcc"],
        ["ocean", "Ocean", "#e8f4f6", "#087f8c"], ["forest", "Forest", "#eef5ef", "#2f6f45"],
        ["ruby", "Ruby", "#f8eef1", "#9f2d55"], ["steel", "Steel", "#edf1f5", "#425c78"],
        ["arctic", "Arctic", "#edf7f8", "#0d7891"], ["citrus", "Citrus", "#f5f7ec", "#617d18"],
        ["harbor", "Harbor", "#eef3f4", "#235f73"], ["ember", "Ember", "#f6f1ee", "#8d4a36"],
        ["violet", "Violet", "#f2effa", "#6d3fbf"], ["sandstone", "Sandstone", "#f5efe4", "#8a6d3b"],
        ["carbon", "Carbon", "#000000", "#4dd0e1"],
      ];

      let csrf = "";
      let state = {access: "", passwordSet: false, unlocked: false, unlockedUntil: 0, unlockSeconds: 900, client: "", version: ""};
      let tab = "health";
      const dirty = {};
      let continuedWithoutPassword = false;
      let setupCode = "";  // no password set: the code from the server's data folder
      let expiredNotice = "";

      // ── Talking to the server ───────────────────────────────────────────
      class GateError extends Error {}
      async function refreshCsrf() {
        try {
          const r = await fetch("/api/csrf", {cache: "no-store"});
          if (r.ok) csrf = (await r.json()).csrfToken || "";
        } catch (_e) { /* signed out */ }
      }
      // Only the gateway's "not signed in" 401 carries X-NWDash-Login (review F2),
      // and only that one goes back to the sign-in. Any other 401 is a password
      // typed here being wrong (the current password on Security, the unlock
      // card), so the caller shows its error.
      async function rebootIfSignedOut(r) {
        if (r.status !== 401 || r.headers.get("X-NWDash-Login") !== "required") return false;
        expiredNotice = "Your session has expired. Please sign in again.";
        await boot();
        return true;
      }
      // `background` marks timed refreshes: only reads a person asked for (and
      // every change) keep the admin window open, so an unattended console locks.
      async function api(method, path, payload, retried, background) {
        const opts = {method, cache: "no-store", headers: {}};
        if (method !== "GET") {
          opts.headers["Content-Type"] = "application/json";
          opts.body = JSON.stringify(payload || {});
          if (csrf) opts.headers["X-CSRF-Token"] = csrf;
        } else if (!background) {
          opts.headers["X-NWDash-Activity"] = "1";
        }
        if (setupCode) opts.headers["X-NWDash-Setup-Code"] = setupCode;
        const r = await fetch(path, opts);
        let data = {};
        try { data = await r.json(); } catch (_e) { data = {}; }
        if (await rebootIfSignedOut(r)) throw new GateError("signed out");
        if (r.status === 403 && data.code === "unlock_required") {
          state.unlocked = false;
          showGate("stepup");
          throw new GateError("locked");
        }
        if (r.status === 403 && data.code === "setup_code_required" && continuedWithoutPassword) {
          setupCode = "";
          continuedWithoutPassword = false;
          showGate("setup");
          throw new GateError("setup code");
        }
        if (r.status === 403 && method !== "GET" && !retried && !data.code) {
          await refreshCsrf();
          return api(method, path, payload, true, background);
        }
        if (!r.ok || data.ok === false) {
          const err = new Error(data.error || data.message || ("Request failed (HTTP " + r.status + ")."));
          err.data = data;
          throw err;
        }
        if (!background && path.indexOf("/api/admin/") === 0 && state.passwordSet && state.unlocked) {
          state.unlockedUntil = Math.floor(Date.now() / 1000) + state.unlockSeconds;
        }
        return data;
      }
      const get = (path, background) => api("GET", path, null, false, background);
      const post = (path, payload) => api("POST", path, payload);
      function fail(e) { if (!(e instanceof GateError)) toast(e.message || String(e), "bad"); }

      // ── Small UI pieces ─────────────────────────────────────────────────
      function toast(message, kind) {
        const t = document.createElement("div");
        t.className = "adm-toast";
        t.dataset.k = kind || "ok";
        t.textContent = message;
        $("#admToasts").appendChild(t);
        setTimeout(() => { t.style.transition = "opacity .4s"; t.style.opacity = "0"; setTimeout(() => t.remove(), 450); }, 4200);
      }
      // In-page confirm (DEDB ConfirmOverlay): window.confirm is never used.
      function confirmBox(title, message, okLabel, danger) {
        return new Promise((resolve) => {
          const ov = document.createElement("div");
          ov.className = "adm-overlay";
          ov.innerHTML = '<div class="adm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="dlgT"><h4 id="dlgT">' + esc(title) + "</h4><p>" + esc(message) +
            '</p><div class="acts"><button type="button" class="btn ghost" data-x>Cancel</button><button type="button" class="btn ' + (danger ? "solid-danger" : "") + '" data-ok>' + esc(okLabel) + "</button></div></div>";
          document.body.appendChild(ov);
          let fired = false;
          const done = (value) => { if (fired) return; fired = true; ov.remove(); resolve(value); };
          ov.addEventListener("click", (e) => { if (e.target === ov) done(false); });
          $("[data-x]", ov).addEventListener("click", () => done(false));
          $("[data-ok]", ov).addEventListener("click", () => done(true));
          ov.addEventListener("keydown", (e) => { if (e.key === "Escape") done(false); });
          setTimeout(() => $("[data-ok]", ov).focus(), 20);
        });
      }
      // Slide to confirm (DEDB SlideToConfirm): drag to the end, or arrow keys.
      function slide(el, onDone) {
        const knob = $(".knob", el), fill = $(".fill", el), label = $(".lbl", el), start = label.textContent;
        let dragging = false, done = false, pct = 0;
        const max = () => el.clientWidth - 50;
        const draw = (x) => { knob.style.left = (4 + x) + "px"; fill.style.width = (x + 50) + "px"; };
        const finish = async () => {
          done = true; el.classList.add("done"); draw(max()); label.textContent = "Working…"; knob.textContent = "✓"; el.setAttribute("aria-valuenow", "100");
          try { await onDone(); label.textContent = "Done ✓"; } catch (e) { fail(e); label.textContent = "Not done"; }
          setTimeout(reset, 2400);
        };
        const reset = () => { done = false; pct = 0; el.classList.remove("done"); draw(0); knob.textContent = "!"; label.textContent = start; el.setAttribute("aria-valuenow", "0"); };
        knob.addEventListener("pointerdown", (e) => { if (done) return; dragging = true; try { knob.setPointerCapture(e.pointerId); } catch (_e) { /* old engines */ } });
        knob.addEventListener("pointermove", (e) => {
          if (!dragging) return;
          const r = el.getBoundingClientRect();
          const x = Math.max(0, Math.min(max(), e.clientX - r.left - 25));
          draw(x);
          if (x >= max() * 0.97) { dragging = false; finish(); }
        });
        const up = () => { if (!dragging) return; dragging = false; draw(0); };
        knob.addEventListener("pointerup", up);
        knob.addEventListener("pointercancel", up);
        el.addEventListener("keydown", (e) => {
          if (done) return;
          if (e.key === "ArrowRight" || e.key === "ArrowUp") {
            e.preventDefault(); pct = Math.min(100, pct + 8); draw(pct / 100 * max()); el.setAttribute("aria-valuenow", String(pct));
            if (pct >= 100) finish();
          } else if (e.key === "Escape") { pct = 0; draw(0); }
        });
      }
      const pill = (text, kind) => '<span class="pill" data-k="' + kind + '">' + esc(text) + "</span>";
      function when(value) {
        if (!value) return "—";
        const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
        if (Number.isNaN(d.getTime())) return String(value);
        return d.toLocaleString([], {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"});
      }
      // Certificate dates are years apart: the year, not the time of day.
      function day(value) {
        if (!value) return "—";
        const d = new Date(value);
        return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleDateString([], {day: "2-digit", month: "short", year: "numeric"});
      }
      function duration(seconds) {
        const s = Math.max(0, Math.floor(seconds));
        if (s >= 86400) return Math.floor(s / 86400) + " d " + Math.floor(s % 86400 / 3600) + " h";
        if (s >= 3600) return Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " min";
        if (s >= 60) return Math.floor(s / 60) + " min";
        return s + " s";
      }
      function browserName(agent) {
        const a = String(agent || "");
        if (!a) return "—";
        const name = /Edg\//.test(a) ? "Edge" : /Chrome\//.test(a) ? "Chrome" : /Firefox\//.test(a) ? "Firefox" : /Safari\//.test(a) ? "Safari" : /python|curl|urllib/i.test(a) ? "Script" : "Browser";
        const os = /Windows/.test(a) ? "Windows" : /Mac OS/.test(a) ? "macOS" : /Android/.test(a) ? "Android" : /iPhone|iPad/.test(a) ? "iOS" : /Linux/.test(a) ? "Linux" : "";
        return os ? name + " · " + os : name;
      }
      function download(name, text, type) {
        const url = URL.createObjectURL(new Blob([text], {type: type || "text/plain"}));
        const a = document.createElement("a");
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      const csvCell = (v) => '"' + String(v === undefined || v === null ? "" : v).replace(/"/g, '""') + '"';
      function busy(button, on, label) {
        if (on) { button.dataset.label = button.textContent; button.textContent = label || "Saving…"; button.disabled = true; }
        else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
      }
      function strength(input, bar, hint, min) {
        const v = input.value;
        let s = 0;
        if (v.length >= min) s++;
        if (v.length >= 16) s++;
        if (/[A-Z]/.test(v) && /[a-z]/.test(v)) s++;
        if (/\d/.test(v)) s++;
        if (/[^A-Za-z0-9]/.test(v)) s++;
        bar.style.width = (v ? Math.max(10, s * 20) : 0) + "%";
        bar.style.background = ["var(--red)", "var(--red)", "var(--amber)", "var(--amber)", "var(--green)", "var(--green)"][s];
        hint.textContent = !v ? "At least " + min + " characters." : (v.length < min ? (min - v.length) + " more characters needed." : ["Weak", "Weak", "Fair", "Good", "Strong", "Very strong"][s] + ".");
      }

      // ── Theme, design, title bar ────────────────────────────────────────
      function applyTheme(theme) {
        let value = theme;
        try { value = localStorage.getItem("nw_dashboard_theme") || theme; } catch (_e) { /* private window */ }
        body.dataset.theme = !value || value === "default" ? "" : value;
      }
      function renderTitle() {
        $("#admHost").textContent = location.host + (state.client ? " · you are " + state.client : "");
        $("#admVersion").textContent = "NetWorker Dashboard " + (state.version || "");
        const signedIn = state.access === "ok";
        $("#admSignOut").hidden = !signedIn;
        $("#admLock").hidden = !(signedIn && state.unlocked);
        $("#admWho").innerHTML = state.access === "ok" ? "<i>A</i>Signed in" : (state.access === "open" ? "<i>!</i>No password · on this server" : "");
        const info = D.DESIGNS[body.dataset.design] || {};
        const scheme = $("#admScheme");
        scheme.hidden = !info.hasScheme;
        const dark = getComputedStyle(body).colorScheme.indexOf("dark") >= 0;
        scheme.innerHTML = '<svg class="nw-ic" aria-hidden="true"><use href="#nw-i-' + (dark ? "sun" : "moon") + '"/></svg>';
        scheme.title = dark ? "Switch to light colours" : "Switch to dark colours";
        scheme.setAttribute("aria-label", scheme.title);
        tickWindow();
      }
      function tickWindow() {
        const chip = $("#admWindow");
        if (!(state.passwordSet && state.access === "ok" && state.unlocked)) { chip.hidden = true; return; }
        const left = state.unlockedUntil - Math.floor(Date.now() / 1000);
        if (left <= 0) {
          chip.hidden = true;
          state.unlocked = false;
          if (body.dataset.view === "console") showGate("stepup");
          return;
        }
        chip.hidden = false;
        chip.classList.toggle("soon", left < 120);
        chip.innerHTML = '<svg class="nw-ic" aria-hidden="true"><use href="#nw-i-unlock"/></svg>Admin changes unlocked · ' + (left >= 60 ? Math.ceil(left / 60) + " min" : left + " s");
      }
      setInterval(tickWindow, 1000);
      document.addEventListener("nw:design", renderTitle);
      $("#admScheme").addEventListener("click", () => D.setScheme(body.dataset.scheme === "alt" ? "base" : "alt"));
      $("#admSignOut").addEventListener("click", async () => {
        try { await fetch("/api/logout", {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: "{}"}); } catch (_e) { /* already out */ }
        csrf = "";
        await boot();
      });
      $("#admLock").addEventListener("click", async () => {
        try { await post("/api/admin/lock", {}); } catch (e) { fail(e); }
        state.unlocked = false;
        showGate("stepup");
      });

      // ── Gate cards ──────────────────────────────────────────────────────
      const GATES = {
        signin: () => ({
          title: "NetWorker Admin Console", sub: "Sign in with the dashboard password",
          body: (expiredNotice ? '<div class="banner" data-k="warn">' + esc(expiredNotice) + "</div>" : "") +
            '<label class="f">Password<input id="gPw" type="password" autocomplete="current-password" required></label>' +
            '<button class="btn" type="submit">Sign in</button><p>Five wrong passwords in five minutes lock this address out for five minutes.</p>',
          submit: async (form) => {
            const r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password: $("#gPw", form).value, admin: true})});
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.error || "Sign-in failed.");
            csrf = data.csrfToken || "";
            expiredNotice = "";
            await boot();
          },
        }),
        setup: () => ({
          title: "Set up the admin console", sub: "No dashboard password is set yet",
          body: '<div class="wsteps"><span class="on">1 · Password</span><span>2 · Console</span></div>' +
            "<p>With no password, the admin console opens only on the server itself (you are on " + esc(state.client || "this server") +
            "). Setting one turns on sign-in for everyone who opens the dashboard.</p>" +
            '<label class="f">Setup code<input id="gCode" class="mono" autocomplete="one-time-code" spellcheck="false" placeholder="XXXX-XXXX-XXXX">' +
            "<small>In <span class=\"mono\">data\\" + esc(state.setupCodeFile || "setup-code.txt") + "</span> in the dashboard's folder on the server. A new one is made at every restart.</small></label>" +
            '<label class="f">New password<input id="gNew" type="password" autocomplete="new-password"><span class="strength"><i id="gBar"></i></span><small id="gHint">At least 12 characters.</small></label>' +
            '<label class="f">Confirm password<input id="gConf" type="password" autocomplete="new-password"></label>' +
            '<button class="btn" type="submit">Set password and continue</button>' +
            '<button class="btn ghost" type="button" data-continue>Continue without a password</button>',
          submit: async (form) => {
            const pw = $("#gNew", form).value;
            if (pw !== $("#gConf", form).value) throw new Error("The two passwords do not match.");
            const r = await fetch("/api/admin/setup", {method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({password: pw, setupCode: $("#gCode", form).value.trim()})});
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.error || "The password could not be set.");
            csrf = data.csrfToken || "";
            toast("Password set. Everyone now signs in to open the dashboard.");
            await boot();
          },
          init: (form) => {
            $("#gNew", form).addEventListener("input", () => strength($("#gNew", form), $("#gBar", form), $("#gHint", form), 12));
            $("[data-continue]", form).addEventListener("click", async () => {
              // The code stands in for a password: check it before opening.
              setupCode = $("#gCode", form).value.trim();
              try {
                await get("/api/admin/overview");
              } catch (err) {
                setupCode = "";
                if (!(err instanceof GateError)) toast(err.message, "bad");
                return;
              }
              continuedWithoutPassword = true;
              showConsole();
            });
          },
        }),
        stepup: () => ({
          title: "Confirm it's you", sub: "Admin changes need your password again",
          body: "<p>No admin change has been made for " + Math.round(state.unlockSeconds / 60) +
            " minutes, so the console has locked. Type the password to unlock it for the next " + Math.round(state.unlockSeconds / 60) + " minutes.</p>" +
            '<label class="f">Password<input id="gPw" type="password" autocomplete="current-password" required></label>' +
            '<button class="btn" type="submit">Unlock</button><a class="btn ghost" href="/">Back to the dashboard</a>',
          submit: async (form) => {
            const r = await fetch("/api/admin/unlock", {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify({password: $("#gPw", form).value})});
            const data = await r.json().catch(() => ({}));
            if (await rebootIfSignedOut(r)) return;
            if (!r.ok) throw new Error(data.error || "Unlock failed.");
            state.unlocked = true;
            state.unlockedUntil = data.unlockedUntil || Math.floor(Date.now() / 1000) + state.unlockSeconds;
            showConsole();
          },
        }),
        denied: () => ({
          title: "Admin access required", sub: "This address cannot open the admin console",
          body: "<p>This dashboard has no password, and you are connecting from " + esc(state.client || "another machine") +
            ", not from the server. Open <span class=\"mono\">/admin</span> on the server itself and set a password there; then you can sign in from anywhere.</p>" +
            '<a class="btn" href="/">Back to the dashboard</a>',
        }),
      };
      function showGate(kind) {
        const g = GATES[kind]();
        body.dataset.view = "gate";
        $("#admConsole").hidden = true;
        $("#admGate").hidden = false;
        $("#admBanner").hidden = true;
        const card = $("#admGateCard");
        card.innerHTML = '<div class="gh"><b id="admGateTitle">' + esc(g.title) + "</b><small>" + esc(g.sub) + "</small></div>" +
          (g.submit ? '<form novalidate>' + g.body + "</form>" : '<div class="gb">' + g.body + "</div>");
        const form = $("form", card);
        if (form) {
          if (g.init) g.init(form);
          form.addEventListener("submit", async (e) => {
            e.preventDefault();
            const button = $('button[type="submit"]', form);
            busy(button, true, "Working…");
            try { await g.submit(form); } catch (err) { toast(err.message, "bad"); } finally { if (button.isConnected) busy(button, false); }
          });
          const first = $("input", form);
          if (first) setTimeout(() => first.focus(), 30);
        }
        renderTitle();
      }

      // ── Console and tabs ────────────────────────────────────────────────
      function renderTabs() {
        $("#admTabs").innerHTML = TABS.map(([key, label]) =>
          '<button type="button" class="adm-tab" role="tab" data-tab="' + key + '" aria-selected="' + (key === tab) + '">' + esc(label) +
          (dirty[key] ? '<span class="dot" title="Unsaved changes"></span>' : "") + "</button>").join("");
      }
      $("#admTabs").addEventListener("click", (e) => {
        const b = e.target.closest(".adm-tab");
        if (b) requestTab(b.dataset.tab);
      });
      async function requestTab(next) {
        if (next === tab) return;
        if (dirty[tab]) {
          const ok = await confirmBox("Unsaved changes", "You have unsaved edits on this screen. Leaving now will discard them.", "Discard changes");
          if (!ok) return;
          dirty[tab] = false;
        }
        showTab(next);
      }
      function showTab(next) {
        tab = next;
        try { history.replaceState(null, "", "#" + next); } catch (_e) { /* file: */ }
        $$(".adm-panel").forEach((p) => { p.hidden = p.dataset.tab !== next; });
        renderTabs();
        const panel = $('.adm-panel[data-tab="' + next + '"]');
        if (motionOn() && panel.animate) {
          panel.animate([{opacity: 0, transform: "translateX(40px)"}, {opacity: 1, transform: "none"}],
            {duration: body.dataset.motion === "subtle" ? 200 : 450, easing: "cubic-bezier(.22,1,.36,1)"});
        }
        window.scrollTo(0, 0);
        (LOADERS[next] || (() => {}))();
      }
      function showConsole() {
        body.dataset.view = "console";
        $("#admGate").hidden = true;
        $("#admConsole").hidden = false;
        const banner = $("#admBanner");
        banner.hidden = state.passwordSet;
        banner.textContent = "No dashboard password is set: anyone on this server can make admin changes, and the dashboard is open to anyone who can reach it. Set one under Security.";
        renderTitle();
        const fromHash = (location.hash || "").slice(1);
        showTab(TABS.some((t) => t[0] === fromHash) ? fromHash : tab);
      }
      window.addEventListener("beforeunload", (e) => {
        if (Object.values(dirty).some(Boolean)) { e.preventDefault(); e.returnValue = ""; }
      });
      function trackDirty(form, key) {
        const mark = () => { if (!dirty[key]) { dirty[key] = true; renderTabs(); } };
        form.addEventListener("input", mark);
        form.addEventListener("change", mark);
      }
      function clean(key) { dirty[key] = false; renderTabs(); }

      // ── Health ──────────────────────────────────────────────────────────
      const ST_STEPS = [
        {id: "cert", label: "Certificate"}, {id: "listener", label: "HTTPS listener"}, {id: "data", label: "Data folder"},
        {id: "networker", label: "NetWorker servers"}, {id: "smtp", label: "Mail server"}, {id: "log", label: "Log file"},
      ];
      const stFlow = $("#stFlow");
      D.buildFlow(stFlow, ST_STEPS);
      let stShown = 0;
      function stDial(value, color) { stShown = value; D.setDial($("#stArc"), $("#stPct"), value, color); }
      function stLine(text, cls) {
        const log = $("#stLog");
        const d = document.createElement("div");
        d.className = (cls || "") + (motionOn() ? " new" : "");
        d.innerHTML = '<span class="t">' + esc(new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"})) + "</span> " + esc(text);
        log.appendChild(d);
        log.scrollTop = log.scrollHeight;
      }
      const sleep = (ms) => new Promise((r) => setTimeout(r, motionOn() ? ms : 0));
      async function runSelfTest() {
        const btn = $("#stRun");
        btn.disabled = true;
        $("#stLog").textContent = "";
        ST_STEPS.forEach((s) => D.setNode(stFlow, s.id, "idle"));
        const run = D.cssVar("--p-run");
        stDial(0, run);
        $("#stCap").textContent = "Running";
        $("#stState").textContent = "Running…";
        stLine("self-test started");
        // Light the steps while the server works; the answer then settles each.
        let i = 0;
        const scan = setInterval(() => {
          if (i < ST_STEPS.length) { D.setNode(stFlow, ST_STEPS[i].id, "run", "Checking"); i++; }
        }, motionOn() ? 220 : 1);
        let result;
        try { result = await post("/api/admin/self-test", {}); } catch (e) {
          clearInterval(scan); btn.disabled = false; fail(e);
          ST_STEPS.forEach((s) => D.setNode(stFlow, s.id, "idle")); $("#stCap").textContent = "Not run"; return;
        }
        clearInterval(scan);
        for (let k = 0; k < result.steps.length; k++) {
          const step = result.steps[k];
          await sleep(260);
          D.setNode(stFlow, step.id, step.state, {ok: "OK", warn: "Check", bad: "Failed"}[step.state]);
          stLine(step.label + ": " + step.detail, step.state);
          const target = (k + 1) / result.steps.length * 100;
          D.tween(stShown, target, 0.35, (v) => stDial(v, run));
        }
        await sleep(380);
        const color = result.failures ? D.cssVar("--p-bad") : (result.warnings ? D.cssVar("--p-warn") : D.cssVar("--p-ok"));
        stDial(100, color);
        const summary = result.failures ? result.failures + " failed" : (result.warnings ? "Passed · " + result.warnings + " to check" : "Passed");
        $("#stCap").textContent = result.failures ? result.failures + " failed" : (result.warnings ? result.warnings + " to check" : "Passed");
        $("#stState").textContent = summary + " · " + when(result.finishedAt);
        stLine("self-test finished: " + summary, result.failures ? "bad" : (result.warnings ? "warn" : "ok"));
        btn.disabled = false;
      }
      $("#stRun").addEventListener("click", runSelfTest);
      async function loadHealth() {
        try {
          const o = await get("/api/admin/overview");
          const age = o.lastRefreshAgeSeconds;
          $("#hlService").innerHTML = [
            ["Service", pill("Running · " + duration(o.uptimeSeconds), "ok")],
            ["Version", esc(o.version + " · Python " + o.python + (o.embedded ? " (embedded)" : ""))],
            ["Dashboard refresh", age === null ? pill("Not connected yet", "warn") : (o.lastRefreshError ? pill(o.lastRefreshError, "warn") : pill("OK · " + duration(age) + " ago", "ok"))],
            ["Live viewers", esc(o.liveViewers)],
            ["NetWorker sessions", esc(o.connectionSessions) + " · " + esc(o.profiles) + " saved profile(s)"],
            ["Email schedules", esc(o.schedules.total) + (o.schedules.paused ? " · " + esc(o.schedules.paused) + " paused" : "")],
            ["Worker threads", esc(o.threads) + " of " + esc(o.maxConnections)],
          ].map((r) => "<div><span>" + r[0] + "</span><span>" + r[1] + "</span></div>").join("");
          const tls = o.tls || {};
          $("#hlSecurity").innerHTML = [
            ["Gateway password", o.passwordSet ? pill("Set", "ok") : pill("Not set", "bad")],
            ["Host allow-list", o.allowlist.enabled ? pill("Enforced · " + o.allowlist.entries + " entr" + (o.allowlist.entries === 1 ? "y" : "ies"), "ok") : pill("Off: any host", "warn")],
            ["TLS certificate", !tls.ok ? pill("Unreadable", "bad") : pill((tls.selfSigned ? "Self-signed · " : "") + (tls.daysLeft !== undefined && tls.daysLeft !== null ? tls.daysLeft + " days left" : "valid"), tls.selfSigned || tls.daysLeft < 30 ? "warn" : "ok")],
            ["Cross-site requests", pill("Refused", "ok")],
            ["Data folder", o.dataFreePercent === null ? "—" : esc(o.dataFreePercent + "% of the disk free")],
            ["Audit entries today", esc(o.auditEntriesToday)],
          ].map((r) => "<div><span>" + r[0] + "</span><span>" + r[1] + "</span></div>").join("");
        } catch (e) { fail(e); }
      }

      // ── Sessions ────────────────────────────────────────────────────────
      async function loadSessions() {
        try {
          const data = await get("/api/admin/sessions");
          const rows = data.sessions || [];
          $("#sessTable tbody").innerHTML = rows.map((s) => "<tr><td class=\"mono\">" + esc(s.ip || "—") + "</td><td>" + esc(browserName(s.agent)) + "</td><td>" + esc(when(s.signedIn)) +
            "</td><td>" + esc(when(s.expires)) + "</td><td>" + (s.adminOpen ? pill("Unlocked", "run") : "—") + '</td><td class="end">' +
            (s.current ? pill("This browser", "ok") : '<button type="button" class="btn ghost sm" data-revoke="' + esc(s.id) + '">Revoke</button>') + "</td></tr>").join("") ||
            '<tr class="empty-row"><td colspan="6">' + (state.passwordSet ? "No one is signed in." : "No password is set, so there are no sign-in sessions.") + "</td></tr>";
          $("#sessSlide").setAttribute("aria-disabled", rows.filter((s) => !s.current).length ? "false" : "true");
        } catch (e) { fail(e); }
      }
      $("#sessTable").addEventListener("click", async (e) => {
        const b = e.target.closest("[data-revoke]");
        if (!b) return;
        try { await post("/api/admin/sessions/revoke", {id: b.dataset.revoke}); toast("Session revoked."); loadSessions(); } catch (err) { fail(err); }
      });
      slide($("#sessSlide"), async () => {
        const r = await post("/api/admin/sessions/revoke-others", {});
        toast(r.ended ? "Signed out " + r.ended + " other session(s)." : "There were no other sessions.");
        loadSessions();
      });

      // ── Audit ───────────────────────────────────────────────────────────
      let auditRows = [];
      const today = () => { const d = new Date(); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
      $("#auDate").value = today();
      async function loadAudit() {
        const sel = $("#auAction");
        const params = new URLSearchParams({date: $("#auDate").value, action: sel.value, q: $("#auQuery").value.trim()});
        try {
          const data = await get("/api/admin/audit?" + params.toString());
          auditRows = data.entries || [];
          const current = sel.value;
          sel.innerHTML = '<option value="">All actions</option>' + (data.actions || []).map((a) => "<option" + (a === current ? " selected" : "") + ">" + esc(a) + "</option>").join("");
          $("#auTable tbody").innerHTML = auditRows.map((r) => '<tr><td class="mono">' + esc(when(r.ts)) + '</td><td class="mono">' + esc(r.ip || "—") + "</td><td>" + esc(r.action) +
            "</td><td>" + esc(r.item || "—") + "</td><td>" + esc(r.field || "—") + '</td><td class="muted">' + esc(r.old || "—") + "</td><td>" + esc(r.new || "—") + "</td></tr>").join("") ||
            '<tr class="empty-row"><td colspan="7">No entries for this day.</td></tr>';
          $("#auCount").textContent = auditRows.length + (auditRows.length === 1 ? " entry" : " entries");
        } catch (e) { fail(e); }
      }
      $("#auDate").addEventListener("change", loadAudit);
      $("#auAction").addEventListener("change", loadAudit);
      let auditTimer = 0;
      $("#auQuery").addEventListener("input", () => { clearTimeout(auditTimer); auditTimer = setTimeout(loadAudit, 300); });
      $("#auExport").addEventListener("click", () => {
        const head = ["When", "Address", "Session", "Action", "Item", "Field", "Old value", "New value"];
        const lines = [head.map(csvCell).join(",")].concat(auditRows.map((r) => [r.ts, r.ip, r.session, r.action, r.item, r.field, r.old, r.new].map(csvCell).join(",")));
        download("audit-" + ($("#auDate").value || "all") + ".csv", lines.join("\r\n"), "text/csv");
      });

      // ── Logs ────────────────────────────────────────────────────────────
      let logPaused = false;
      let logTimer = 0;
      let logLines = [];
      // `background` is the 4-second auto-refresh: it must not keep the admin
      // window open, or a console left on this tab would never lock.
      async function loadLogs(background) {
        clearTimeout(logTimer);
        if (tab !== "logs" || body.dataset.view !== "console") return;
        const params = new URLSearchParams({level: $("#lgLevel").value, q: $("#lgQuery").value.trim(), lines: "400"});
        try {
          const data = await get("/api/admin/logs?" + params.toString(), background === true);
          const before = new Set(logLines.map((l) => l.ts + l.msg));
          logLines = data.lines || [];
          const box = $("#lgBox");
          const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
          box.innerHTML = logLines.map((l) => '<div class="' + esc(l.level) + (before.size && !before.has(l.ts + l.msg) && motionOn() ? " new" : "") + '"><span class="t">' +
            esc(when(l.ts)) + "</span> " + esc(l.level.padEnd(7)) + " " + esc(l.msg) + (l.client ? ' <span class="t">' + esc(l.client) + "</span>" : "") + "</div>").join("") ||
            '<div class="t">' + (data.missing ? "The log file has not been written yet." : "No lines at this level.") + "</div>";
          if (!logPaused && (atBottom || !before.size)) box.scrollTop = box.scrollHeight;
          $("#lgCount").textContent = logLines.length + " lines · " + (data.file || "");
        } catch (e) { fail(e); }
        if (!logPaused) logTimer = setTimeout(() => loadLogs(true), 4000);
      }
      $("#lgLevel").addEventListener("change", () => { logLines = []; loadLogs(); });
      let logQueryTimer = 0;
      $("#lgQuery").addEventListener("input", () => { clearTimeout(logQueryTimer); logQueryTimer = setTimeout(() => { logLines = []; loadLogs(); }, 300); });
      $("#lgPause").addEventListener("click", () => {
        logPaused = !logPaused;
        $("#lgPause").textContent = logPaused ? "Resume" : "Pause";
        if (!logPaused) loadLogs();
      });
      $("#lgDownload").addEventListener("click", () => {
        download("networker_dashboard-" + today() + ".log", logLines.map((l) => l.ts + " " + l.level + " " + l.msg).join("\r\n"));
      });

      // ── Connections ─────────────────────────────────────────────────────
      let profiles = {};
      let editing = "";
      const cpForm = $("#cpForm");
      trackDirty(cpForm, "connections");
      function fillProfile(name) {
        editing = name;
        const p = profiles[name] || {};
        $("#cpTitle").textContent = name ? 'Edit "' + name + '"' : "New profile";
        $("#cpName").value = name || "";
        $("#cpName").readOnly = !!name;
        $("#cpHost").value = p.restApiHost || "";
        $("#cpPort").value = p.restApiPort || "9090";
        $("#cpMode").value = p.apiMode || "auto";
        $("#cpVersion").value = p.apiVersion || "auto";
        $("#cpBackupHost").value = p.backupServerHost || "";
        $("#cpBackupPort").value = p.backupServerPort || "9090";
        $("#cpUser").value = p.username || "";
        $("#cpPass").value = "";
        $("#cpPass").placeholder = p.password ? "Saved · leave blank to keep" : "Not saved";
        $("#cpVerify").checked = p.verifyTls !== false;
        $("#cpDelete").hidden = !name;
        clean("connections");
        renderProfiles();
      }
      function renderProfiles() {
        const names = Object.keys(profiles).sort();
        $("#cpTable tbody").innerHTML = names.map((n) => {
          const p = profiles[n];
          return '<tr class="click' + (n === editing ? " sel" : "") + '" tabindex="0" data-name="' + esc(n) + '"><td><b>' + esc(n) + '</b></td><td class="mono">' + esc((p.restApiHost || "—") + ":" + (p.restApiPort || "")) +
            "</td><td>" + esc((p.apiMode || "auto") + " · " + (p.apiVersion || "auto")) + "</td><td>" + (p.verifyTls === false ? pill("Not verified", "warn") : pill("Verified", "ok")) +
            "</td><td>" + (p.password ? pill("Saved", "ok") : pill("Not saved", "warn")) + "</td></tr>";
        }).join("") || '<tr class="empty-row"><td colspan="5">No saved profiles yet.</td></tr>';
      }
      async function loadConnections() {
        try {
          const data = await get("/api/profiles");
          profiles = data.profiles || {};
          if (!dirty.connections) fillProfile(editing && profiles[editing] ? editing : (Object.keys(profiles).sort()[0] || ""));
          else renderProfiles();
        } catch (e) { fail(e); }
      }
      async function pickProfile(name) {
        if (dirty.connections && !(await confirmBox("Unsaved changes", "Discard your edits to this profile?", "Discard changes"))) return;
        fillProfile(name);
      }
      $("#cpTable").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-name]"); if (tr) pickProfile(tr.dataset.name); });
      $("#cpTable").addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-name]"); if (tr && e.key === "Enter") pickProfile(tr.dataset.name); });
      $("#cpNew").addEventListener("click", () => pickProfile(""));
      cpForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const name = $("#cpName").value.trim();
        if (!name) { toast("Give the profile a name.", "bad"); return; }
        if (!editing && profiles[name]) { toast('A profile named "' + name + '" already exists.', "bad"); return; }
        // Start from the stored record so fields this form does not show survive;
        // "(saved)" passwords tell the server to keep the stored secret.
        const data = Object.assign({}, profiles[editing] || {}, {
          restApiHost: $("#cpHost").value.trim(), restApiPort: $("#cpPort").value.trim(),
          apiMode: $("#cpMode").value, apiVersion: $("#cpVersion").value,
          backupServerHost: $("#cpBackupHost").value.trim(), backupServerPort: $("#cpBackupPort").value.trim(),
          username: $("#cpUser").value.trim(), verifyTls: $("#cpVerify").checked,
        });
        if ($("#cpPass").value) data.password = $("#cpPass").value;
        const button = $("#cpSave");
        busy(button, true);
        try {
          const r = await post("/api/profiles", {action: "save", name, data});
          profiles = r.profiles || profiles;
          toast('Profile "' + name + '" saved.' + (profiles[name] && !profiles[name].password && (profiles[editing] || {}).password ? " Its server or user changed, so type the password again." : ""));
          dirty.connections = false;
          fillProfile(name);
        } catch (err) { fail(err); } finally { busy(button, false); }
      });
      $("#cpDelete").addEventListener("click", async () => {
        if (!editing) return;
        if (!(await confirmBox('Delete "' + editing + '"?', "Sessions using this profile are signed out of NetWorker. Schedules keep their own saved connection.", "Delete profile", true))) return;
        try {
          const r = await post("/api/profiles", {action: "delete", name: editing});
          profiles = r.profiles || {};
          toast("Profile deleted.");
          dirty.connections = false;
          fillProfile(Object.keys(profiles).sort()[0] || "");
        } catch (e) { fail(e); }
      });

      // ── Email ───────────────────────────────────────────────────────────
      const emForm = $("#emForm");
      trackDirty(emForm, "email");
      let emailCfg = null;
      let sharedSessionId = "";
      async function loadEmail() {
        try {
          const cfg = await get("/api/email-config");
          emailCfg = cfg;
          if (!dirty.email) {
            $("#emHost").value = cfg.smtp.host;
            $("#emPort").value = cfg.smtp.port;
            $("#emSecurity").value = cfg.smtp.security;
            $("#emUser").value = cfg.smtp.username;
            $("#emPass").value = "";
            $("#emPass").placeholder = cfg.smtp.passwordSaved ? "Saved · leave blank to keep" : "Not saved";
            $("#emFrom").value = cfg.smtp.from;
            $("#emAlertTo").value = cfg.alert.recipients;
            $("#emTrigger").value = cfg.alert.trigger;
            $("#emInterval").value = cfg.alert.intervalMinutes;
            $("#emReportTo").value = cfg.dailyReport.recipients;
            $("#emTime").value = cfg.dailyReport.reportTime;
            clean("email");
          }
          const shared = await get("/api/current-dashboard").catch(() => ({}));
          sharedSessionId = shared.sessionId || "";
          const list = await post("/api/alert-automation", {action: "list-profiles"});
          const names = Object.keys(list.profiles || {}).sort();
          $("#epTable tbody").innerHTML = names.map((n) => {
            const p = list.profiles[n];
            const type = (p.scheduleType || p.emailScheduleType) === "daily_report" ? "Daily report" : "Alerts";
            return '<tr><td><button type="button" class="switch" role="switch" aria-checked="' + !!p.enabled + '" data-profile="' + esc(n) + '" aria-label="Switch ' + esc(n) + ' on or off"></button></td><td><b>' +
              esc(n) + '</b><div class="muted small">' + esc(p.smtpTo || "") + "</div></td><td>" + esc(type) + "</td></tr>";
          }).join("") || '<tr class="empty-row"><td colspan="3">No email profiles yet.</td></tr>';
        } catch (e) { fail(e); }
      }
      $("#epTable").addEventListener("click", async (e) => {
        const b = e.target.closest("[data-profile]");
        if (!b) return;
        const on = b.getAttribute("aria-checked") !== "true";
        try {
          const r = await post("/api/alert-automation", {action: "toggle-profile", profileName: b.dataset.profile, enabled: on, sessionId: sharedSessionId});
          toast(r.message || (on ? "Profile switched on." : "Profile switched off."));
          loadEmail();
        } catch (err) { fail(err); }
      });
      emForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const common = {
          action: "save", smtpHost: $("#emHost").value.trim(), smtpPort: $("#emPort").value.trim(), smtpSecurity: $("#emSecurity").value,
          smtpUsername: $("#emUser").value.trim(), smtpFrom: $("#emFrom").value.trim(),
        };
        const button = $("#emSave");
        busy(button, true);
        try {
          // The SMTP server is shared; alert and report recipients are saved per
          // type. The password goes with the first save only.
          await post("/api/alert-automation", Object.assign({}, common, {scheduleType: "alert", smtpPassword: $("#emPass").value, smtpTo: $("#emAlertTo").value,
            trigger: $("#emTrigger").value, intervalMinutes: $("#emInterval").value}));
          await post("/api/alert-automation", Object.assign({}, common, {scheduleType: "daily_report", smtpTo: $("#emReportTo").value, reportTime: $("#emTime").value.trim(),
            theme: (emailCfg && emailCfg.dailyReport && emailCfg.dailyReport.theme) || "default"}));
          toast("Email settings saved. Nothing new is scheduled.");
          clean("email");
          loadEmail();
        } catch (err) { fail(err); } finally { busy(button, false); }
      });

      // ── Schedules ───────────────────────────────────────────────────────
      async function loadSchedules(glowId) {
        try {
          const r = await post("/api/alert-automation", {action: "list"});
          const rows = r.schedules || [];
          $("#scTable tbody").innerHTML = rows.map((s) => {
            const type = s.scheduleType === "daily_report" ? "Daily report" : "Alerts";
            const cadence = s.scheduleType === "daily_report" ? "Daily " + s.reportTime : "Every " + s.intervalMinutes + " min";
            const next = s.enabled && s.nextRunAt ? when(s.nextRunAt) : (s.enabled ? "—" : "paused");
            const last = String(s.lastResult || "Not run yet");
            const kind = /fail|error|refused|rejected/i.test(last) ? "bad" : (s.lastRun ? "ok" : "warn");
            return '<tr' + (s.automationId === glowId ? ' class="glow"' : "") + '><td><button type="button" class="switch" role="switch" aria-checked="' + !!s.enabled + '" data-sched="' + esc(s.automationId) +
              '" aria-label="Pause or resume this schedule"></button></td><td>' + pill(type, s.scheduleType === "daily_report" ? "run" : "warn") + (s.profileName ? '<div class="muted small">' + esc(s.profileName) + "</div>" : "") +
              "</td><td>" + esc(cadence) + "</td><td>" + esc(s.recipients) + '</td><td class="mono">' + esc(next) + "</td><td>" + pill(last.length > 60 ? last.slice(0, 58) + "…" : last, kind) +
              '</td><td class="end"><button type="button" class="btn ghost sm danger" data-stop="' + esc(s.automationId) + '">Stop</button></td></tr>';
          }).join("") || '<tr class="empty-row"><td colspan="7">Nothing is scheduled. Schedules are started from the dashboard\'s Email window.</td></tr>';
        } catch (e) { fail(e); }
      }
      $("#scTable").addEventListener("click", async (e) => {
        const sw = e.target.closest("[data-sched]");
        const stop = e.target.closest("[data-stop]");
        try {
          if (sw) {
            const on = sw.getAttribute("aria-checked") !== "true";
            const r = await post("/api/alert-automation", {action: "set_enabled", automationId: sw.dataset.sched, enabled: on});
            toast(r.message || "Saved.");
            loadSchedules(sw.dataset.sched);
          } else if (stop) {
            if (!(await confirmBox("Stop this schedule?", "It stops sending and is removed. You can schedule it again from the dashboard's Email window.", "Stop schedule", true))) return;
            const r = await post("/api/alert-automation", {action: "stop", automationId: stop.dataset.stop});
            toast(r.message || "Schedule stopped.");
            loadSchedules();
          }
        } catch (err) { fail(err); }
      });

      // ── Snapshots ───────────────────────────────────────────────────────
      async function loadSnapshots(glowFirst) {
        try {
          const [list, auto] = await Promise.all([get("/api/snapshots?action=list"), get("/api/snapshots?action=auto-config")]);
          $("#snAuto").setAttribute("aria-checked", String(!!auto.enabled));
          const rows = list.snapshots || [];
          $("#snTable tbody").innerHTML = rows.map((s, i) => "<tr" + (glowFirst && i === 0 ? ' class="glow"' : "") + "><td><b>" + esc(s.date) + "</b></td><td>" + esc(s.server || "—") + "</td><td>" +
            (s.health ? pill(s.health, s.health === "critical" ? "bad" : (s.health === "warning" ? "warn" : "ok")) : "—") + '</td><td class="num">' + esc(s.slaPercent) + "%</td><td>" + esc(when(s.savedAt)) +
            "</td><td>" + esc(s.annotation || "") + '</td><td class="end"><button type="button" class="btn ghost sm danger" data-del="' + esc(s.date) + '">Delete</button></td></tr>').join("") ||
            '<tr class="empty-row"><td colspan="7">No snapshots yet.</td></tr>';
        } catch (e) { fail(e); }
      }
      $("#snAuto").addEventListener("click", async (e) => {
        const on = e.currentTarget.getAttribute("aria-checked") !== "true";
        try { const r = await post("/api/snapshots", {action: "auto-config", enabled: on}); toast(on ? "Automatic snapshot on." + (r.result === "saved" ? " Today's is saved." : "") : "Automatic snapshot off."); loadSnapshots(); } catch (err) { fail(err); }
      });
      $("#snNow").addEventListener("click", async () => {
        try { const r = await post("/api/snapshots", {action: "save"}); toast(r.message || "Snapshot saved."); loadSnapshots(true); } catch (e) { fail(e); }
      });
      $("#snExport").addEventListener("click", async () => {
        try {
          const r = await fetch("/api/snapshots?action=export", {cache: "no-store"});
          if (!r.ok) throw new Error("Export failed (HTTP " + r.status + ").");
          download("networker_snapshots.csv", await r.text(), "text/csv");
        } catch (e) { fail(e); }
      });
      $("#snTable").addEventListener("click", async (e) => {
        const b = e.target.closest("[data-del]");
        if (!b) return;
        if (!(await confirmBox("Delete the snapshot of " + b.dataset.del + "?", "It disappears from the growth chart. This cannot be undone.", "Delete", true))) return;
        try { await post("/api/snapshots", {action: "delete", date: b.dataset.del}); toast("Snapshot deleted."); loadSnapshots(); } catch (err) { fail(err); }
      });

      // ── Share links ─────────────────────────────────────────────────────
      async function loadShares() {
        try {
          const data = await get("/api/admin/shares");
          const rows = data.links || [];
          $("#shTable tbody").innerHTML = rows.map((l) => '<tr><td class="mono">/view/' + esc(l.hint) + "</td><td>" + esc(l.server || "—") + "</td><td>" + esc(when(l.created)) + "</td><td>" + esc(when(l.expires)) +
            '</td><td class="end"><button type="button" class="btn ghost sm danger" data-link="' + esc(l.id) + '">Revoke</button></td></tr>').join("") ||
            '<tr class="empty-row"><td colspan="5">No live share links.</td></tr>';
          $("#shSlide").setAttribute("aria-disabled", rows.length ? "false" : "true");
        } catch (e) { fail(e); }
      }
      $("#shTable").addEventListener("click", async (e) => {
        const b = e.target.closest("[data-link]");
        if (!b) return;
        if (!(await confirmBox("Revoke this link?", "Anyone using it sees an expired page at the next refresh.", "Revoke", true))) return;
        try { await post("/api/admin/shares/revoke", {id: b.dataset.link}); toast("Link revoked."); loadShares(); } catch (err) { fail(err); }
      });
      slide($("#shSlide"), async () => {
        const r = await post("/api/admin/shares/revoke-all", {});
        toast(r.revoked ? "Revoked " + r.revoked + " link(s)." : "There were no links.");
        loadShares();
      });

      // ── Appearance ──────────────────────────────────────────────────────
      let appearance = {};
      const apForm = $("#apForm");
      function renderAppearance() {
        $("#apDesigns").innerHTML = D.ORDER.map((key) => {
          const d = D.DESIGNS[key];
          return '<button type="button" class="design-card" data-ap-design="' + key + '" aria-pressed="' + (appearance.design === key) + '"><span class="preview">' +
            d.preview.map((c) => '<i style="background:' + c + '"></i>').join("") + "</span>" + esc(d.label) + "<small>" + esc(d.blurb) + "</small></button>";
        }).join("");
        $("#apMotion").innerHTML = ["full", "subtle", "off"].map((m) => '<button type="button" class="seg-btn" data-ap-motion="' + m + '" aria-pressed="' + (appearance.motion === m) + '">' + m.charAt(0).toUpperCase() + m.slice(1) + "</button>").join("");
        $("#apSidebar").innerHTML = [["dock", "Docked"], ["float", "Floating"]].map(([v, l]) => '<button type="button" class="seg-btn" data-ap-sidebar="' + v + '" aria-pressed="' + (appearance.sidebar === v) + '">' + l + "</button>").join("");
        $("#apThemes").innerHTML = THEMES.map(([key, label, bg, brand]) => '<button type="button" class="theme-sw" data-ap-theme="' + key + '" aria-pressed="' + (appearance.theme === key) +
          '"><span><i style="background:' + bg + '"></i><i style="background:' + brand + '"></i></span>' + esc(label) + "</button>").join("");
      }
      async function loadAppearance() {
        try {
          const data = await get("/api/admin/appearance");
          if (!dirty.appearance) { appearance = {design: data.design, motion: data.motion, sidebar: data.sidebar, theme: data.theme}; }
          renderAppearance();
        } catch (e) { fail(e); }
      }
      apForm.addEventListener("click", (e) => {
        const b = e.target.closest("button[type=button]");
        if (!b) return;
        const keys = {apDesign: "design", apMotion: "motion", apSidebar: "sidebar", apTheme: "theme"};
        for (const k in keys) {
          if (b.dataset[k]) { appearance[keys[k]] = b.dataset[k]; dirty.appearance = true; renderTabs(); renderAppearance(); return; }
        }
      });
      apForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const button = $("#apSave");
        busy(button, true);
        try {
          const r = await post("/api/admin/appearance", appearance);
          appearance = {design: r.design, motion: r.motion, sidebar: r.sidebar, theme: r.theme};
          clean("appearance");
          renderAppearance();
          toast("Saved. Viewers who have not picked their own design now get " + D.DESIGNS[r.design].label + ".");
        } catch (err) { fail(err); } finally { busy(button, false); }
      });

      // ── Security ────────────────────────────────────────────────────────
      const pwForm = $("#pwForm");
      trackDirty(pwForm, "security");
      let security = null;
      let allowDraft = [];
      $("#pwNew").addEventListener("input", () => strength($("#pwNew"), $("#pwBar"), $("#pwHint"), (security && security.passwordMinLength) || 12));
      function renderAllow() {
        const view = security.allowlist;
        const editable = view.editable;
        $("#alChips").innerHTML = allowDraft.map((h, i) => '<span class="chip mono">' + esc(h) + (editable ? '<button type="button" data-rm="' + i + '" aria-label="Remove ' + esc(h) + '">×</button>' : "") + "</span>").join("") ||
          '<span class="muted small">Empty: the dashboard may connect to any host.</span>';
        $("#alEdit").hidden = !editable;
        $("#alState").outerHTML = '<span id="alState">' + (view.enabled ? pill("Enforced", "ok") : pill("Off", "warn")) + "</span>";
        $("#alMsg").textContent = view.source === "startup"
          ? "Set when the service starts (--allowed-hosts or DASHBOARD_ALLOWED_HOSTS), so it is read-only here. Change it with Setup-NWDash.cmd -AllowedHosts."
          : "The dashboard refuses to connect to a NetWorker server that is not listed. An empty list allows any host.";
      }
      async function loadSecurity() {
        try {
          security = await get("/api/admin/security");
          allowDraft = security.allowlist.entries.slice();
          const set = security.passwordSet;
          $("#pwTitle").textContent = set ? "Gateway password" : "Set a gateway password";
          $("#pwState").outerHTML = '<span id="pwState">' + (set ? pill("Set", "ok") : pill("Not set", "bad")) + "</span>";
          $("#pwCurWrap").hidden = !set;
          $("#pwSave").textContent = set ? "Change password" : "Set password";
          $("#pwNote").textContent = set ? "Signs out every other session." : "Everyone then signs in to open the dashboard.";
          if (!dirty.security) { $("#pwCur").value = ""; $("#pwNew").value = ""; $("#pwConfirm").value = ""; $("#pwBar").style.width = "0"; }
          renderAllow();
          $("#secRows").innerHTML = [
            ["Session lifetime", esc(security.lifetimeHours) + " hours"],
            ["Sign-in lockout", "5 wrong passwords in 5 minutes"],
            ["Admin changes lock after", esc(security.unlockMinutes) + " minutes idle"],
            ["CSRF token on every change", pill(set ? "Required" : "Needs a password", set ? "ok" : "warn")],
            ["Cross-site requests", pill("Refused", "ok")],
          ].concat(security.passwordFromStartup ? [["Password source", pill("Service start-up setting", "warn")]] : [])
            .map((r) => "<div><span>" + r[0] + "</span><span>" + r[1] + "</span></div>").join("");
        } catch (e) { fail(e); }
      }
      pwForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const next = $("#pwNew").value;
        const min = security.passwordMinLength || 12;
        if (next.length < min) { toast("Use at least " + min + " characters.", "bad"); return; }
        if (next !== $("#pwConfirm").value) { toast("The two new passwords do not match.", "bad"); return; }
        const button = $("#pwSave");
        if (!security.passwordSet) {
          if (!(await confirmBox("Turn on sign-in?", "Everyone who opens the dashboard will need this password, TV walls included.", "Set password"))) return;
          busy(button, true);
          try {
            const r = await fetch("/api/admin/setup", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password: next})});
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.error || "The password could not be set.");
            csrf = data.csrfToken || "";
            clean("security");
            toast("Password set. Sign-in is now on.");
            await boot();
          } catch (err) { fail(err); } finally { busy(button, false); }
          return;
        }
        if (!(await confirmBox("Change the gateway password?", "Every other session is signed out, TV walls included. Share links keep working.", "Change password"))) return;
        busy(button, true);
        try {
          const r = await post("/api/admin/password", {current: $("#pwCur").value, new: next});
          clean("security");
          toast("Password changed. " + (r.sessionsEnded || 0) + " other session(s) signed out.");
          if (r.warning) toast(r.warning, "warn");
          loadSecurity();
        } catch (err) { fail(err); } finally { busy(button, false); }
      });
      $("#alChips").addEventListener("click", (e) => {
        const b = e.target.closest("[data-rm]");
        if (!b) return;
        allowDraft.splice(Number(b.dataset.rm), 1);
        dirty.security = true; renderTabs(); renderAllow();
      });
      function addHost() {
        const v = $("#alInput").value.trim().toLowerCase();
        if (!v) return;
        if (!/^[a-z0-9_.:\/[\]-]+$/.test(v)) { $("#alMsg").textContent = '"' + v + '" is not a host name, IP address or CIDR range.'; return; }
        if (allowDraft.indexOf(v) < 0) allowDraft.push(v);
        $("#alInput").value = "";
        dirty.security = true; renderTabs(); renderAllow();
      }
      $("#alAdd").addEventListener("click", addHost);
      $("#alInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addHost(); } });
      $("#alSave").addEventListener("click", async () => {
        if (!allowDraft.length && security.allowlist.entries.length &&
            !(await confirmBox("Allow any host?", "An empty list lets the dashboard connect to any NetWorker server name or address it is given.", "Save empty list", true))) return;
        try {
          const r = await post("/api/admin/allowlist", {entries: allowDraft});
          security.allowlist = r;
          allowDraft = r.entries.slice();
          clean("security");
          renderAllow();
          toast("Allow-list saved: " + r.entries.length + " entr" + (r.entries.length === 1 ? "y." : "ies."));
        } catch (e) { fail(e); }
      });

      // ── TLS ─────────────────────────────────────────────────────────────
      const tlsForm = $("#tlsForm");
      let tlsPrevious = null;
      function rowsHtml(rows) {
        return rows.map((r) => "<div><span>" + r[0] + "</span><span>" + r[1] + "</span></div>").join("");
      }
      function renderTls(t) {
        const banner = $("#tlsBanner");
        if (!t.ok) { banner.hidden = false; banner.dataset.k = "bad"; banner.textContent = t.error || "The certificate could not be read."; $("#tlsRows").innerHTML = ""; $("#tlsPrev").hidden = true; return; }
        const days = t.daysLeft;
        const warn = t.selfSigned || (typeof days === "number" && days < 30);
        banner.hidden = !warn;
        banner.dataset.k = typeof days === "number" && days < 7 ? "bad" : "warn";
        banner.textContent = t.auto ? "The built-in development certificate is in use: browsers warn until you replace it with one from your CA."
          : t.selfSigned ? "Self-signed certificate: browsers warn until you install one from your CA." : "The certificate expires in " + days + " days.";
        $("#tlsRows").innerHTML = rowsHtml([
          ["Subject", '<span class="mono">' + esc(t.subject || "—") + "</span>"],
          ["Also valid for", '<span class="mono">' + esc((t.altNames || []).join(", ") || "—") + "</span>"],
          ["Issuer", esc(t.selfSigned ? "Self-signed" : (t.issuer || "—"))],
          ["Valid", esc(day(t.validFrom) + " – " + day(t.validTo))],
          ["Days left", typeof days === "number" ? pill(days, days < 30 ? "warn" : "ok") : "—"],
          ["SHA-256", '<span class="mono small">' + esc(t.sha256 || "—") + "</span>"],
          ["Files", '<span class="mono">' + esc([t.file, t.keyFile].filter(Boolean).join(", ") || "—") + "</span>"],
        ]);
        tlsPrevious = t.previous || null;
        $("#tlsPrev").hidden = !tlsPrevious;
        if (tlsPrevious) {
          $("#tlsPrevRows").innerHTML = rowsHtml([
            ["Subject", '<span class="mono">' + esc(tlsPrevious.subject || "—") + "</span>"],
            ["Issuer", esc(tlsPrevious.selfSigned ? "Self-signed" : (tlsPrevious.issuer || "—"))],
            ["Valid until", esc(day(tlsPrevious.validTo))],
            ["SHA-256", '<span class="mono small">' + esc(tlsPrevious.sha256 || "—") + "</span>"],
          ]);
        }
        tlsForm.hidden = t.canReplace === false;
      }
      async function loadTls() {
        if (!dirty.tls) { $("#tlsCert").value = ""; $("#tlsKey").value = ""; $("#tlsMsg").textContent = ""; }
        try { renderTls(await get("/api/admin/tls")); } catch (e) { fail(e); }
      }
      // Files are read here and sent as text in the JSON body, like every other change.
      $$("[data-pick]", tlsForm).forEach((b) => b.addEventListener("click", () => $("#" + b.dataset.pick).click()));
      [["#tlsCertFile", "#tlsCert"], ["#tlsKeyFile", "#tlsKey"]].forEach(([input, area]) => {
        $(input).addEventListener("change", async () => {
          const file = $(input).files[0];
          $(input).value = "";
          if (!file) return;
          if (/\.(pfx|p12)$/i.test(file.name)) { toast("A .pfx file needs converting to PEM first: see the commands under the form.", "bad"); return; }
          if (file.size > 64 * 1024) { toast(file.name + " is too large for a certificate or key.", "bad"); return; }
          $(area).value = await file.text();
          dirty.tls = true; renderTabs();
        });
      });
      async function sendTls(confirmName) {
        const btn = $("#tlsSave");
        btn.disabled = true;
        $("#tlsMsg").textContent = "Checking…";
        try {
          const r = await post("/api/admin/tls", {certificate: $("#tlsCert").value, key: $("#tlsKey").value, confirmName});
          $("#tlsCert").value = "";
          $("#tlsKey").value = "";
          clean("tls");
          $("#tlsMsg").textContent = "";
          toast("Certificate replaced: new connections use it now. Reload to see it in this browser." + (r.warning ? " " + r.warning : ""), r.warning ? "warn" : "ok");
          renderTls(r.tls);
        } catch (e) {
          $("#tlsMsg").textContent = "";
          if (!confirmName && e.data && e.data.code === "name_not_covered") {
            if (await confirmBox("Use a certificate for other names?", e.message, "Use it anyway", true)) await sendTls(true);
            return;
          }
          fail(e);
        } finally { btn.disabled = false; }
      }
      tlsForm.addEventListener("submit", (e) => { e.preventDefault(); sendTls(false); });
      $("#tlsRestore").addEventListener("click", async () => {
        if (!tlsPrevious) return;
        const ok = await confirmBox("Put back the previous certificate?", "New connections get " + (tlsPrevious.subject || "the previous certificate") +
          " again. The current one is kept as the previous, so you can swap back.", "Put it back");
        if (!ok) return;
        try {
          const r = await post("/api/admin/tls/restore", {});
          toast("Previous certificate put back: new connections use it now.", r.warning ? "warn" : "ok");
          renderTls(r.tls);
        } catch (e) { fail(e); }
      });
      trackDirty(tlsForm, "tls");

      const LOADERS = {
        health: loadHealth, sessions: loadSessions, audit: loadAudit, logs: loadLogs, connections: loadConnections, email: loadEmail,
        schedules: () => loadSchedules(), snapshots: () => loadSnapshots(), shares: loadShares, appearance: loadAppearance, security: loadSecurity, tls: loadTls,
      };
      trackDirty(apForm, "appearance");

      // ── Boot: which screen? ─────────────────────────────────────────────
      async function boot() {
        try {
          const r = await fetch("/api/admin/state", {cache: "no-store"});
          state = Object.assign(state, await r.json());
        } catch (_e) {
          body.dataset.view = "gate";
          toast("The dashboard service is not answering.", "bad");
          return;
        }
        if (state.access === "ok" || state.access === "open") {
          if (!csrf) await refreshCsrf();
          fetch("/api/ui-theme", {cache: "no-store"}).then((x) => (x.ok ? x.json() : null)).then((p) => applyTheme(p && p.theme)).catch(() => applyTheme(""));
        } else {
          applyTheme("");
        }
        if (state.access === "denied") return showGate("denied");
        if (state.access === "login") return showGate("signin");
        if (state.access === "open" && !continuedWithoutPassword) return showGate("setup");
        if (state.access === "ok" && !state.unlocked) return showGate("stepup");
        showConsole();
      }
      boot();
    })();
