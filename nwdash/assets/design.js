    // ═══════════════════════════════════════════════════════════════════════
    // Designs, sidebar and glow-flow motion. Shared by the dashboard
    // and /admin; the dashboard-only parts start only when #navSidebar exists.
    //
    // A viewer's own design, motion, sidebar and light/dark choices are kept in
    // this browser (localStorage) and win over the defaults the admin console
    // saves (GET /api/ui-theme). Motion is plain CSS plus a small tween helper:
    // no library, so it works on the air-gapped hosts under the page CSP.
    // ═══════════════════════════════════════════════════════════════════════
    (function () {
      "use strict";

      var DESIGNS = {
        daylight: {label: "Daylight Glow", blurb: "Today's look and themes, with glow on what is live.", preview: ["#eef3f6", "#126e82", "#18764a"], hasScheme: false},
        aurora: {label: "Aurora Flow", blurb: "Dark navy glass with the strongest glow.", preview: ["#070d22", "#39d0ff", "#3ee08f"], hasScheme: true},
        command: {label: "Command Deck", blurb: "Carbon black, neon status colours, dense.", preview: ["#05070a", "#22d3ee", "#f472b6"], hasScheme: true},
        dedb: {label: "DEDB Navy", blurb: "DEDB's navy title bar, tab strip and Arial.", preview: ["#f3f5fa", "#16275b", "#39d0ff"], hasScheme: true},
      };
      var ORDER = ["daylight", "aurora", "command", "dedb"];
      var MOTION = ["full", "subtle", "off"];
      var SIDEBAR = ["dock", "float"];
      var KEYS = {design: "nw_design", motion: "nw_motion", sidebar: "nw_sidebar", scheme: "nw_scheme", rail: "nw_sidebar_rail", pos: "nw_sidebar_pos"};
      var body = document.body;
      var reduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
      var defaults = {design: "daylight", motion: "full", sidebar: "dock"};

      function load(key) { try { return localStorage.getItem(key); } catch (_e) { return null; } }
      function store(key, value) {
        try {
          if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
        } catch (_e) { /* private window: the choice lasts until reload */ }
      }
      function pick(value, allowed, fallback) { return allowed.indexOf(value) >= 0 ? value : fallback; }
      function motionOn() { return body.dataset.motion !== "off"; }
      function esc(value) {
        return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, function (c) {
          return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
        });
      }
      function num(value) { var n = Number(value); return Number.isFinite(n) ? n : 0; }
      function fmt(value) {
        try { return new Intl.NumberFormat().format(num(value)); } catch (_e) { return String(num(value)); }
      }

      // ── Applying a design ────────────────────────────────────────────────
      function apply() {
        var design = pick(load(KEYS.design), ORDER, defaults.design);
        var motion = reduced ? "off" : pick(load(KEYS.motion), MOTION, defaults.motion);
        var scheme = DESIGNS[design].hasScheme && load(KEYS.scheme + ":" + design) === "alt" ? "alt" : "base";
        var changed = body.dataset.design !== design || body.dataset.scheme !== scheme;
        body.dataset.design = design;
        body.dataset.scheme = scheme;
        body.dataset.motion = motion;
        if (changed) intro();
        document.dispatchEvent(new CustomEvent("nw:design", {detail: {design: design, scheme: scheme, motion: motion}}));
      }
      function setDesign(design) { store(KEYS.design, pick(design, ORDER, defaults.design)); apply(); }
      function setMotion(motion) { store(KEYS.motion, pick(motion, MOTION, defaults.motion)); apply(); }
      function setScheme(scheme) { store(KEYS.scheme + ":" + body.dataset.design, scheme === "alt" ? "alt" : null); apply(); }
      function resetToDefaults() { store(KEYS.design, null); store(KEYS.motion, null); apply(); }

      // Replays the entrance: cards rise in order. Elements that are hidden
      // now (before the first connection) rise when they first appear.
      function intro() {
        var targets = document.querySelectorAll("[data-nw-rise]");
        for (var i = 0; i < targets.length; i++) targets[i].style.setProperty("--nw-i", String(i % 14));
        body.classList.remove("nw-intro");
        void body.offsetWidth;
        body.classList.add("nw-intro");
      }

      // ── Tween (the GSAP part, by hand) ───────────────────────────────────
      function easeOut(t) { return 1 - Math.pow(1 - t, 3); }
      function tween(from, to, seconds, onUpdate) {
        if (!motionOn() || seconds <= 0) { onUpdate(to); return {kill: function () {}}; }
        var ms = seconds * 1000 * (body.dataset.motion === "subtle" ? 0.5 : 1);
        var start = performance.now();
        var frame = 0;
        var dead = false;
        function step(now) {
          if (dead) return;
          var p = Math.min(1, (now - start) / ms);
          onUpdate(from + (to - from) * easeOut(p));
          if (p < 1) frame = requestAnimationFrame(step);
        }
        frame = requestAnimationFrame(step);
        return {kill: function () { dead = true; cancelAnimationFrame(frame); }};
      }

      // Counts a number element up to its new text. Non-numbers are left as set.
      function countTo(el, finalText) {
        var plain = String(finalText).replace(/,/g, "");
        if (!/^\d+$/.test(plain)) { el.textContent = finalText; el.dataset.nwShown = ""; return; }
        var to = Number(plain);
        var from = el.dataset.nwShown === undefined || el.dataset.nwShown === "" ? 0 : Number(el.dataset.nwShown);
        if (el._nwTween) el._nwTween.kill();
        el.dataset.nwShown = String(to);
        var grouped = String(finalText).indexOf(",") >= 0;
        el._nwTween = tween(from, to, from === to ? 0 : 1.1, function (v) {
          el.textContent = v === to ? finalText : (grouped ? fmt(Math.round(v)) : String(Math.round(v)));
        });
      }

      // ── Glowing orb flow (shared with the admin console) ─────────────────
      function buildFlow(row, nodes, onPick) {
        row.textContent = "";
        nodes.forEach(function (node, i) {
          if (i > 0) {
            var link = document.createElement("div");
            link.className = "nw-link";
            link.dataset.state = "idle";
            link.setAttribute("aria-hidden", "true");
            link.appendChild(document.createElement("i"));
            row.appendChild(link);
          }
          var b = document.createElement("button");
          b.type = "button";
          b.className = "nw-node";
          b.setAttribute("role", "listitem");
          b.dataset.id = node.id;
          b.dataset.state = "idle";
          b.innerHTML = '<span class="nw-orb"><i></i></span><b>' + esc(node.label) + "</b><small>Waiting</small>";
          if (onPick) b.addEventListener("click", function () { onPick(node.id); });
          row.appendChild(b);
        });
      }
      var WORD = {idle: "Waiting", run: "Running", ok: "Done", bad: "Failed", warn: "Check"};
      function setNode(row, id, state, word) {
        var b = row.querySelector('.nw-node[data-id="' + id + '"]');
        if (!b) return;
        if (b.dataset.state !== state) b.dataset.state = state;
        b.querySelector("small").textContent = word || WORD[state];
        b.setAttribute("aria-label", b.querySelector("b").textContent + ": " + (word || WORD[state]));
        var prev = b.previousElementSibling;
        if (prev && prev.classList.contains("nw-link")) {
          var linkState = state === "idle" ? "idle" : state;
          if (prev.dataset.state !== linkState) prev.dataset.state = linkState;
        }
      }
      var ARC = 2 * Math.PI * 96;
      function setDial(arc, numEl, value, color) {
        arc.setAttribute("stroke-dasharray", String(ARC));
        arc.setAttribute("stroke-dashoffset", String(ARC * (1 - Math.max(0, Math.min(100, value)) / 100)));
        numEl.textContent = String(Math.round(value));
        if (color) arc.closest(".nw-dial").style.setProperty("--dc", color);
      }
      function cssVar(name) { return getComputedStyle(body).getPropertyValue(name).trim(); }

      window.NWDesign = {
        DESIGNS: DESIGNS, ORDER: ORDER, apply: apply, setDesign: setDesign, setMotion: setMotion,
        setScheme: setScheme, resetToDefaults: resetToDefaults, tween: tween, countTo: countTo,
        buildFlow: buildFlow, setNode: setNode, setDial: setDial, cssVar: cssVar, motionOn: motionOn,
        esc: esc, defaults: defaults, onDashboard: function () {}, onReset: function () {},
      };

      apply();
      fetch("/api/ui-theme", {cache: "no-store"}).then(function (r) { return r.ok ? r.json() : null; }).then(function (prefs) {
        if (!prefs) return;
        defaults.design = pick(prefs.design, ORDER, defaults.design);
        defaults.motion = pick(prefs.motion, MOTION, defaults.motion);
        defaults.sidebar = pick(prefs.sidebar, SIDEBAR, defaults.sidebar);
        apply();
        document.dispatchEvent(new CustomEvent("nw:defaults", {detail: defaults}));
      }).catch(function () {});

      var sidebar = document.getElementById("navSidebar");
      if (!sidebar) return;

      // ════════════════════════ Dashboard only ════════════════════════════
      var fab = document.getElementById("navFab");
      var floatBtn = document.getElementById("navFloatBtn");
      var collapseBtn = document.getElementById("navCollapseBtn");
      var grip = document.getElementById("navGrip");
      var narrow = window.matchMedia ? window.matchMedia("(max-width: 860px)") : {matches: false};

      function icon(name) { return '<svg class="nw-ic" aria-hidden="true"><use href="#nw-i-' + name + '"/></svg>'; }

      // FLIP: measure, change, then animate from the old box to the new one.
      function flip(el, mutate) {
        var a = el.getBoundingClientRect();
        mutate();
        var b = el.getBoundingClientRect();
        if (!motionOn() || !el.animate || !a.width || !b.width) return;
        el.animate([
          {transform: "translate(" + (a.left - b.left) + "px," + (a.top - b.top) + "px) scale(" + (a.width / b.width) + "," + (a.height / b.height) + ")", transformOrigin: "top left"},
          {transform: "none", transformOrigin: "top left"},
        ], {duration: body.dataset.motion === "subtle" ? 260 : 520, easing: "cubic-bezier(.22,1,.36,1)"});
      }

      function sidebarMode() { return body.dataset.sidebar === "float" ? "float" : "dock"; }
      function syncSidebarButtons() {
        var floating = sidebarMode() === "float";
        floatBtn.innerHTML = icon(floating ? "dock" : "float");
        floatBtn.title = floating ? "Dock the sidebar" : "Float the sidebar";
        floatBtn.setAttribute("aria-label", floatBtn.title);
        collapseBtn.innerHTML = icon(floating ? "min" : "chev");
        collapseBtn.title = floating ? "Minimise to a button" : (body.classList.contains("nav-rail") ? "Show labels" : "Collapse to icons");
        collapseBtn.setAttribute("aria-label", collapseBtn.title);
        collapseBtn.setAttribute("aria-expanded", body.classList.contains("nav-rail") ? "false" : "true");
      }
      function placeFloating() {
        var pos = (load(KEYS.pos) || "").split(",");
        if (pos.length === 2 && Number.isFinite(Number(pos[0])) && Number.isFinite(Number(pos[1]))) {
          var x = Math.max(6, Math.min(window.innerWidth - 120, Number(pos[0])));
          var y = Math.max(6, Math.min(window.innerHeight - 120, Number(pos[1])));
          sidebar.style.setProperty("--fx", x + "px");
          sidebar.style.setProperty("--fy", y + "px");
          body.classList.add("nav-moved");
        } else {
          sidebar.style.removeProperty("--fx");
          sidebar.style.removeProperty("--fy");
          body.classList.remove("nav-moved");
        }
      }
      function setSidebar(mode, animate) {
        var go = function () {
          body.dataset.sidebar = mode;
          body.classList.remove("nav-min");
          fab.hidden = true;
          if (mode === "float") { body.classList.remove("nav-rail"); placeFloating(); }
          else { body.classList.remove("nav-moved"); body.classList.toggle("nav-rail", load(KEYS.rail) === "1"); }
          syncSidebarButtons();
        };
        if (animate) flip(sidebar, go); else go();
      }
      setSidebar(pick(load(KEYS.sidebar), SIDEBAR, defaults.sidebar), false);
      document.addEventListener("nw:defaults", function () {
        if (!load(KEYS.sidebar)) setSidebar(defaults.sidebar, false);
      });

      floatBtn.addEventListener("click", function () {
        var next = sidebarMode() === "float" ? "dock" : "float";
        store(KEYS.sidebar, next);
        setSidebar(next, true);
      });
      collapseBtn.addEventListener("click", function () {
        if (sidebarMode() === "float") {
          body.classList.add("nav-min");
          fab.hidden = false;
          fab.focus();
          return;
        }
        var rail = !body.classList.contains("nav-rail");
        if (rail) body.classList.remove("connection-open");
        body.classList.toggle("nav-rail", rail);
        store(KEYS.rail, rail ? "1" : null);
        syncSidebarButtons();
      });
      fab.addEventListener("click", function () {
        body.classList.remove("nav-min");
        fab.hidden = true;
        if (motionOn() && sidebar.animate) {
          sidebar.animate([{opacity: 0, transform: "translate(-10px, 30px) scale(.92)"}, {opacity: 1, transform: "none"}],
            {duration: 420, easing: "cubic-bezier(.22,1,.36,1)"});
        }
      });

      // Dragging the floating sidebar by its header; a double-click parks it.
      var drag = null;
      grip.addEventListener("pointerdown", function (e) {
        if (sidebarMode() !== "float" || narrow.matches || e.target.closest("button")) return;
        var r = sidebar.getBoundingClientRect();
        drag = {dx: e.clientX - r.left, dy: e.clientY - r.top, w: r.width};
        sidebar.classList.add("dragging");
        try { grip.setPointerCapture(e.pointerId); } catch (_e) { /* old engines */ }
      });
      grip.addEventListener("pointermove", function (e) {
        if (!drag) return;
        var x = Math.max(6, Math.min(window.innerWidth - drag.w - 6, e.clientX - drag.dx));
        var y = Math.max(6, Math.min(window.innerHeight - 120, e.clientY - drag.dy));
        sidebar.style.setProperty("--fx", x + "px");
        sidebar.style.setProperty("--fy", y + "px");
        if (x > 30 || y > 30) body.classList.add("nav-moved");
      });
      function endDrag() {
        if (!drag) return;
        drag = null;
        sidebar.classList.remove("dragging");
        if (body.classList.contains("nav-moved")) {
          var r = sidebar.getBoundingClientRect();
          store(KEYS.pos, Math.round(r.left) + "," + Math.round(r.top));
        }
      }
      grip.addEventListener("pointerup", endDrag);
      grip.addEventListener("pointercancel", endDrag);
      grip.addEventListener("dblclick", function () {
        if (sidebarMode() !== "float") return;
        store(KEYS.pos, null);
        flip(sidebar, placeFloating);
      });

      // Opening the connection form widens the sidebar (and leaves icon mode).
      new MutationObserver(function () {
        if (body.classList.contains("connection-open") && body.classList.contains("nav-rail")) {
          body.classList.remove("nav-rail");
          syncSidebarButtons();
        }
        if (body.classList.contains("connection-open") && body.classList.contains("nav-min")) {
          body.classList.remove("nav-min");
          fab.hidden = true;
        }
      }).observe(body, {attributes: true, attributeFilter: ["class"]});

      // Narrow screens: the sidebar slides over the page.
      var openBtn = document.getElementById("navOpenBtn");
      var scrim = document.getElementById("navScrim");
      function closeDrawer() { body.classList.remove("nav-open"); openBtn.setAttribute("aria-expanded", "false"); }
      openBtn.addEventListener("click", function () {
        var open = !body.classList.contains("nav-open");
        body.classList.toggle("nav-open", open);
        openBtn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      scrim.addEventListener("click", closeDrawer);
      document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeDrawer(); });

      // ── Navigation ───────────────────────────────────────────────────────
      var navLinks = sidebar.querySelectorAll("[data-nav-go], [data-nav-table]");
      function markCurrent(link) {
        for (var i = 0; i < navLinks.length; i++) navLinks[i].removeAttribute("aria-current");
        if (link) link.setAttribute("aria-current", "true");
      }
      for (var n = 0; n < navLinks.length; n++) {
        navLinks[n].addEventListener("click", function () {
          var link = this;
          markCurrent(link);
          var table = link.getAttribute("data-nav-table");
          var target;
          if (table) {
            var tab = document.querySelector('.tab[data-table="' + table + '"]');
            if (tab) tab.click();
            target = document.getElementById("tableTitle");
          } else {
            var id = link.getAttribute("data-nav-go");
            target = id === "top" ? null : document.getElementById(id);
          }
          if (target) target.scrollIntoView({behavior: motionOn() ? "smooth" : "auto", block: "start"});
          else window.scrollTo({top: 0, behavior: motionOn() ? "smooth" : "auto"});
          if (narrow.matches) closeDrawer();
        });
      }
      sidebar.addEventListener("click", function (e) {
        if (narrow.matches && e.target.closest(".nav-list button")) closeDrawer();
      });

      // ── The design toggle ────────────────────────────────────────────────
      var designBtn = document.getElementById("designBtn");
      var designMenu = document.getElementById("designMenu");
      var schemeBtn = document.getElementById("schemeBtn");
      function renderDesignMenu() {
        var d = body.dataset.design;
        var info = DESIGNS[d];
        designBtn.querySelector(".swatch").innerHTML = info.preview.map(function (c) { return '<i style="background:' + c + '"></i>'; }).join("");
        designBtn.querySelector(".label").textContent = info.label;
        designMenu.innerHTML =
          '<h3>Design</h3><div class="design-grid">' + ORDER.map(function (key) {
            var item = DESIGNS[key];
            return '<button type="button" class="design-card" data-design-pick="' + key + '" aria-pressed="' + (key === d) + '">' +
              '<span class="preview">' + item.preview.map(function (c) { return '<i style="background:' + c + '"></i>'; }).join("") + "</span>" +
              esc(item.label) + "<small>" + esc(item.blurb) + "</small></button>";
          }).join("") + "</div>" +
          '<div class="design-row"><span>Motion</span><span class="seg">' + MOTION.map(function (m) {
            return '<button type="button" class="seg-btn" data-motion-pick="' + m + '" aria-pressed="' + (body.dataset.motion === m) + '"' + (reduced ? " disabled" : "") + ">" + m.charAt(0).toUpperCase() + m.slice(1) + "</button>";
          }).join("") + "</span></div>" +
          (info.hasScheme ? '<div class="design-row"><span>Colours</span><span class="seg">' +
            '<button type="button" class="seg-btn" data-scheme-pick="base" aria-pressed="' + (body.dataset.scheme !== "alt") + '">' + (d === "dedb" ? "Light" : "Dark") + "</button>" +
            '<button type="button" class="seg-btn" data-scheme-pick="alt" aria-pressed="' + (body.dataset.scheme === "alt") + '">' + (d === "dedb" ? "Dark" : "Light") + "</button></span></div>"
            : '<p class="design-note">Daylight follows the theme picked under View settings.</p>') +
          (reduced ? '<p class="design-note">Your system asks for reduced motion, so motion stays off.</p>' : "") +
          '<p class="design-note">Saved in this browser. The admin console sets the default for everyone else' +
          (load(KEYS.design) ? ' · <button type="button" class="seg-btn" data-design-reset style="display:inline;padding:0 4px;min-height:0">Use the default</button>' : "") + "</p>";
        schemeBtn.hidden = !info.hasScheme;
        var dark = getComputedStyle(body).colorScheme.indexOf("dark") >= 0;
        schemeBtn.innerHTML = icon(dark ? "sun" : "moon");
        schemeBtn.title = dark ? "Switch to light colours" : "Switch to dark colours";
        schemeBtn.setAttribute("aria-label", schemeBtn.title);
      }
      function closeMenu() { designMenu.hidden = true; designBtn.setAttribute("aria-expanded", "false"); }
      designBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        var open = designMenu.hidden;
        renderDesignMenu();
        designMenu.hidden = !open;
        designBtn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      designMenu.addEventListener("click", function (e) {
        e.stopPropagation();
        var t = e.target.closest("button");
        if (!t) return;
        if (t.dataset.designPick) setDesign(t.dataset.designPick);
        else if (t.dataset.motionPick) setMotion(t.dataset.motionPick);
        else if (t.dataset.schemePick) setScheme(t.dataset.schemePick);
        else if (t.hasAttribute("data-design-reset")) resetToDefaults();
        renderDesignMenu();
      });
      document.addEventListener("click", function () { if (!designMenu.hidden) closeMenu(); });
      document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !designMenu.hidden) { closeMenu(); designBtn.focus(); } });
      schemeBtn.addEventListener("click", function () { setScheme(body.dataset.scheme === "alt" ? "base" : "alt"); });
      document.addEventListener("nw:design", function () { renderDesignMenu(); applyPipeMotion(); repaintHero(); });
      renderDesignMenu();

      // ── Protection flow hero ─────────────────────────────────────────────
      var hero = document.getElementById("flowHero");
      var fhFlow = document.getElementById("fhFlow");
      var fhSteps = document.getElementById("fhSteps");
      var fhState = document.getElementById("fhState");
      var fhArc = document.getElementById("fhArc");
      var fhPct = document.getElementById("fhPct");
      var fhCap = document.getElementById("fhCap");
      var fhRibbon = document.getElementById("fhRibbon");
      var fhPipe = document.getElementById("fhPipe");
      var fhHud = document.getElementById("fhHud");
      var NODES = [
        {id: "clients", label: "Clients"},
        {id: "policies", label: "Policies"},
        {id: "backups", label: "Backups"},
        {id: "clones", label: "Clones"},
        {id: "restores", label: "Restores"},
        {id: "sla", label: "SLA"},
      ];
      var picked = null;
      var flow = null;
      var dialShown = 0;
      var dialTween = null;
      var lastData = null;
      buildFlow(fhFlow, NODES, function (id) { picked = id; renderOrbs(); });

      function activity(total, failed, running) {
        total = num(total); failed = num(failed); running = num(running);
        var state = running > 0 ? "run" : (failed > 0 ? "bad" : (total > 0 ? "ok" : "idle"));
        var word = running > 0 ? fmt(running) + " running" : (failed > 0 ? fmt(failed) + " failed" : (total > 0 ? fmt(total) + " done" : "None"));
        return {state: state, word: word};
      }

      // Everything the hero shows, from the dashboard payload alone.
      function computeFlow(data) {
        var s = (data && data.summary) || {};
        var ok = num(s.successfulJobs), failed = num(s.failedJobs), running = num(s.activeJobs);
        var slaTotal = num(s.slaTotalJobs !== undefined ? s.slaTotalJobs : s.totalJobs);
        var slaMet = num(s.slaMetJobs !== undefined ? s.slaMetJobs : s.successfulJobs);
        var slaPct = slaTotal ? num(s.slaPercent !== undefined ? s.slaPercent : Math.round(slaMet / slaTotal * 100)) : null;
        var rate = ok + failed ? ok / (ok + failed) * 100 : null;
        var backups = activity(ok + failed + running, failed, running);
        var clones = activity(s.cloneJobs, s.cloneFailed, s.cloneRunning);
        var restores = activity(s.recoveryJobs, s.recoveryFailed, s.recoveryRunning);
        var slaState = slaPct === null ? "idle" : (slaPct >= 98 ? "ok" : (slaPct >= 90 ? "warn" : "bad"));
        return {
          summary: s,
          dial: slaPct !== null ? slaPct : (rate !== null ? rate : 0),
          dialLabel: slaPct !== null ? "SLA met" : (rate !== null ? "Success rate" : "No jobs yet"),
          running: running + num(s.cloneRunning) + num(s.recoveryRunning),
          failed: failed + num(s.cloneFailed) + num(s.recoveryFailed),
          nodes: {
            clients: {state: num(s.totalClients) ? "ok" : "idle", word: s.totalClients === undefined ? "—" : fmt(s.totalClients)},
            policies: {state: num(s.policies) ? "ok" : "idle", word: s.policies === undefined ? "—" : fmt(s.policies)},
            backups: backups,
            clones: clones,
            restores: restores,
            sla: {state: slaState, word: slaPct === null ? "No jobs" : slaPct + "%"},
          },
          detail: {
            clients: [["Protected clients", num(s.totalClients), num(s.totalClients), "ok"]],
            policies: [["Policies", num(s.policies), num(s.policies), "ok"], ["Critical alerts", num(s.criticalAlerts), Math.max(1, num(s.totalAlerts)), "bad"]],
            backups: [["Succeeded", ok, ok + failed + running, "ok"], ["Failed", failed, ok + failed + running, "bad"], ["Running", running, ok + failed + running, "run"]],
            clones: [["Clone jobs", num(s.cloneJobs), num(s.cloneJobs), "ok"], ["Failed", num(s.cloneFailed), num(s.cloneJobs), "bad"], ["Running", num(s.cloneRunning), num(s.cloneJobs), "run"]],
            restores: [["Restores", num(s.recoveryJobs), num(s.recoveryJobs), "ok"], ["Failed", num(s.recoveryFailed), num(s.recoveryJobs), "bad"], ["Running", num(s.recoveryRunning), num(s.recoveryJobs), "run"]],
            sla: [["SLA met", slaMet, slaTotal, "ok"], ["Not met", Math.max(0, slaTotal - slaMet), slaTotal, "bad"]],
          },
        };
      }

      function renderOrbs(fromZero) {
        if (!flow) return;
        var current = picked;
        if (!current) {
          current = "backups";
          for (var i = 0; i < NODES.length; i++) {
            if (flow.nodes[NODES[i].id].state === "run" || flow.nodes[NODES[i].id].state === "bad") { current = NODES[i].id; break; }
          }
        }
        NODES.forEach(function (node) {
          var n = flow.nodes[node.id];
          setNode(fhFlow, node.id, n.state, n.word);
          fhFlow.querySelector('.nw-node[data-id="' + node.id + '"]').setAttribute("aria-current", node.id === current ? "true" : "false");
        });
        // Live work first (cyan), then the SLA level: met, near (amber) or missed.
        var sla = flow.nodes.sla.state;
        var color = flow.running > 0 ? cssVar("--p-run")
          : cssVar(sla === "bad" ? "--p-bad" : (sla === "warn" ? "--p-warn" : (sla === "idle" ? "--p-idle" : "--p-ok")));
        if (dialTween) dialTween.kill();
        var from = fromZero ? 0 : dialShown;
        dialTween = tween(from, flow.dial, fromZero ? 1.4 : 0.6, function (v) { dialShown = v; setDial(fhArc, fhPct, v, color); });
        fhCap.textContent = flow.running > 0 ? fmt(flow.running) + " running" : flow.dialLabel;
        var rows = flow.detail[current];
        var label = NODES.filter(function (x) { return x.id === current; })[0].label;
        fhSteps.innerHTML = '<div class="fh-eyebrow">' + esc(label) + "</div>" + rows.map(function (r) {
          var pct = r[2] ? Math.round(r[1] / r[2] * 100) : 0;
          var c = {ok: "var(--p-ok)", bad: "var(--p-bad)", run: "var(--p-run)"}[r[3]];
          return '<div class="nw-step"><span>' + esc(r[0]) + '</span><span class="n">' + fmt(r[1]) + (r[2] && r[2] !== r[1] ? " / " + fmt(r[2]) : "") +
            '</span><div class="bar"><i style="width:' + pct + "%;--bc:" + c + '"></i></div></div>';
        }).join("");
      }

      function statusClass(status) {
        var s = String(status || "").toLowerCase();
        if (/fail|error|critical|abort/.test(s)) return "bad";
        if (/run|active|start|progress|queued/.test(s)) return "run";
        if (/warn/.test(s)) return "warn";
        return "ok";
      }
      function timeText(ms, withDate) {
        var d = new Date(ms);
        var t = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
        return withDate ? d.toLocaleDateString([], {day: "2-digit", month: "short"}) + " " + t : t;
      }
      function renderRibbon(data) {
        var jobs = (data && data.tables && data.tables.jobs) || [];
        var parse = typeof window.parseTs === "function" ? window.parseTs : function (v) { var d = new Date(v); return d.getTime(); };
        var dur = typeof window.parseDurationMs === "function" ? window.parseDurationMs : function () { return 0; };
        var now = Date.now();
        var lanes = {};
        var min = Infinity, max = -Infinity;
        jobs.forEach(function (job) {
          var start = parse(job.started);
          if (!Number.isFinite(start)) return;
          var cls = statusClass(job.status);
          var end = cls === "run" ? Math.max(now, start + 60000) : start + (dur(job.duration) || 300000);
          var key = job.policy || "No policy";
          (lanes[key] = lanes[key] || []).push({s: start, e: end, c: cls});
          min = Math.min(min, start);
          max = Math.max(max, end);
        });
        var names = Object.keys(lanes).sort(function (a, b) { return lanes[b].length - lanes[a].length; }).slice(0, 6);
        if (!names.length) {
          fhRibbon.innerHTML = '<p class="rb-empty">' + (data ? "No job start times in this range to draw." : "Connect to a NetWorker server to see when jobs ran.") + "</p>";
          return;
        }
        max = Math.max(max, Math.min(now, max + 3600000));
        var span = Math.max(60000, max - min);
        var withDate = span > 30 * 3600000;
        var ticks = [];
        for (var t = 0; t <= 4; t++) ticks.push("<span>" + esc(timeText(min + span * t / 4, withDate)) + "</span>");
        var x = function (ms) { return Math.max(0, Math.min(100, (ms - min) / span * 100)); };
        var html = '<div class="rb-axis"><span></span><div>' + ticks.join("") + "</div><span></span></div>" + '<div class="rb-lanes">';
        if (now >= min && now <= max) {
          html += '<div class="rb-nowcol"><div class="rb-now" style="left:' + x(now).toFixed(2) + '%"><span>now</span></div></div>';
        }
        names.forEach(function (name) {
          var segs = lanes[name];
          var running = segs.filter(function (g) { return g.c === "run"; }).length;
          var failed = segs.filter(function (g) { return g.c === "bad"; }).length;
          html += '<div class="rb-row"><span title="' + esc(name) + '">' + esc(name) + '</span><div class="rb-track">' + segs.map(function (g) {
            return '<i class="rb-seg ' + g.c + '" style="left:' + x(g.s).toFixed(2) + "%;width:" + Math.max(0.4, x(g.e) - x(g.s)).toFixed(2) + '%"></i>';
          }).join("") + '</div><span class="rb-n">' + (running ? running + " run" : (failed ? failed + " failed" : segs.length + " jobs")) + "</span></div>";
        });
        fhRibbon.innerHTML = html + "</div>";
      }

      var PIPE_NODES = [
        {id: "srv", x: 20, y: 115, l: "Server", w: 130},
        {id: "policies", x: 220, y: 115, l: "Policies"},
        {id: "backups", x: 410, y: 115, l: "Backups", w: 150},
        {id: "clones", x: 640, y: 55, l: "Clones", w: 140},
        {id: "failed", x: 640, y: 175, l: "Failed", w: 140},
        {id: "restores", x: 830, y: 55, l: "Restores", w: 110},
      ];
      var WIRES = [
        ["pw1", "M150 115 C 185 115, 190 115, 220 115", "policies"],
        ["pw2", "M340 115 C 375 115, 380 115, 410 115", "backups"],
        ["pw3", "M560 115 C 600 115, 600 55, 640 55", "clones"],
        ["pw4", "M560 115 C 600 115, 600 175, 640 175", "failed"],
        ["pw5", "M780 55 C 805 55, 805 55, 830 55", "restores"],
      ];
      var NS = "http://www.w3.org/2000/svg";
      function buildPipe() {
        var defs = document.createElementNS(NS, "defs");
        WIRES.forEach(function (w) {
          var p = document.createElementNS(NS, "path");
          p.setAttribute("id", w[0]);
          p.setAttribute("d", w[1]);
          defs.appendChild(p);
        });
        fhPipe.appendChild(defs);
        WIRES.forEach(function (w, wi) {
          var g = document.createElementNS(NS, "g");
          g.dataset.wire = w[2];
          g.setAttribute("class", "off");
          var path = document.createElementNS(NS, "path");
          path.setAttribute("class", "wire");
          path.setAttribute("d", w[1]);
          g.appendChild(path);
          var count = w[2] === "failed" ? 1 : 4;
          for (var k = 0; k < count; k++) {
            var c = document.createElementNS(NS, "circle");
            c.setAttribute("r", w[2] === "failed" ? "3.5" : "3");
            c.setAttribute("class", "pt" + (k % 2 ? " x" : ""));
            var am = document.createElementNS(NS, "animateMotion");
            var d = w[2] === "failed" ? 3.2 : 1.6 + wi * 0.15;
            am.setAttribute("dur", d + "s");
            am.setAttribute("repeatCount", "indefinite");
            am.setAttribute("begin", (-k * d / count).toFixed(2) + "s");
            var mp = document.createElementNS(NS, "mpath");
            mp.setAttribute("href", "#" + w[0]);
            am.appendChild(mp);
            c.appendChild(am);
            g.appendChild(c);
          }
          fhPipe.appendChild(g);
        });
        PIPE_NODES.forEach(function (n) {
          var g = document.createElementNS(NS, "g");
          g.setAttribute("class", "pn");
          g.dataset.id = n.id;
          var w = n.w || 120;
          g.innerHTML = '<rect x="' + n.x + '" y="' + (n.y - 34) + '" width="' + w + '" height="68" rx="8"/>' +
            '<text class="sm" x="' + (n.x + 12) + '" y="' + (n.y - 14) + '">' + esc(n.l.toUpperCase()) + "</text>" +
            '<text class="big" x="' + (n.x + 12) + '" y="' + (n.y + 16) + '">—</text>';
          fhPipe.appendChild(g);
        });
        applyPipeMotion();
      }
      function applyPipeMotion() {
        if (!fhPipe.pauseAnimations) return;
        if (motionOn() && body.dataset.design === "command") fhPipe.unpauseAnimations(); else fhPipe.pauseAnimations();
      }
      function renderPipe(data) {
        var s = (data && data.summary) || {};
        var t = data && data.target ? data.target : {};
        function setN(id, text, color) {
          var g = fhPipe.querySelector('.pn[data-id="' + id + '"]');
          if (!g) return;
          var big = g.querySelector(".big");
          var host = text.length > 12 ? text.slice(0, 11) + "…" : text;
          big.textContent = host;
          g.style.setProperty("--c", color);
        }
        var health = s.health || (data ? "ok" : "");
        var server = String(t.backupServer || t.restApiBase || "").replace(/^https?:\/\//, "").split(/[:/]/)[0];
        setN("srv", data ? (server || "online") : "—", !data ? "var(--p-idle)" : (health === "critical" ? "var(--p-bad)" : (health === "warning" ? "var(--p-warn)" : "var(--p-ok)")));
        setN("policies", s.policies === undefined ? "—" : fmt(s.policies), num(s.policies) ? "var(--p-ok)" : "var(--p-idle)");
        var total = num(s.successfulJobs) + num(s.failedJobs) + num(s.activeJobs);
        setN("backups", data ? fmt(num(s.successfulJobs)) + "/" + fmt(total) : "—", num(s.activeJobs) ? "var(--p-run)" : (total ? "var(--p-ok)" : "var(--p-idle)"));
        setN("failed", data ? fmt(num(s.failedJobs)) : "—", num(s.failedJobs) ? "var(--p-bad)" : "var(--p-idle)");
        setN("clones", data ? fmt(num(s.cloneJobs)) : "—", num(s.cloneRunning) ? "var(--nw-clone)" : (num(s.cloneJobs) ? "var(--p-ok)" : "var(--p-idle)"));
        setN("restores", data ? fmt(num(s.recoveryJobs)) : "—", num(s.recoveryRunning) ? "var(--p-run)" : (num(s.recoveryJobs) ? "var(--p-ok)" : "var(--p-idle)"));
        var on = {
          policies: !!data,
          backups: num(s.activeJobs) > 0 || total > 0,
          clones: num(s.cloneRunning) > 0,
          failed: num(s.failedJobs) > 0,
          restores: num(s.recoveryRunning) > 0,
        };
        var colors = {policies: "var(--p-ok)", backups: num(s.activeJobs) ? "var(--p-run)" : "var(--p-ok)", clones: "var(--nw-clone)", failed: "var(--p-bad)", restores: "var(--p-run)"};
        var groups = fhPipe.querySelectorAll("g[data-wire]");
        for (var i = 0; i < groups.length; i++) {
          var key = groups[i].dataset.wire;
          groups[i].setAttribute("class", on[key] ? "" : "off");
          groups[i].style.setProperty("--c", colors[key]);
        }
        fhHud.innerHTML =
          "<div>Running now<b>" + (data ? fmt(num(s.activeJobs) + num(s.cloneRunning) + num(s.recoveryRunning)) : "—") + "</b></div>" +
          "<div>Clients<b>" + (s.totalClients === undefined ? "—" : fmt(s.totalClients)) + "</b></div>" +
          "<div>Alerts<b>" + (s.totalAlerts === undefined ? "—" : fmt(s.totalAlerts)) + "</b></div>" +
          "<div>SLA<b>" + (s.slaPercent === undefined ? "—" : esc(s.slaPercent) + "%") + "</b></div>";
      }
      buildPipe();

      function repaintHero() {
        if (flow) renderOrbs(false);
      }

      function heroState(data) {
        if (!data) return "Not connected";
        var s = data.summary || {};
        var bits = [];
        if (s.rangeLabel) bits.push(s.rangeLabel);
        if (data.generatedAt) bits.push("updated " + data.generatedAt);
        if (num(s.activeJobs)) bits.push(fmt(s.activeJobs) + " running");
        if (num(s.failedJobs)) bits.push(fmt(s.failedJobs) + " failed");
        return bits.join(" · ");
      }

      // Metrics: count up to the new values and flag non-zero failure tiles.
      function animateMetrics(first) {
        var cards = document.querySelectorAll(".metric");
        for (var i = 0; i < cards.length; i++) {
          var strong = cards[i].querySelector("strong");
          if (!strong) continue;
          var finalText = strong.textContent;
          var before = strong.dataset.nwShown;
          countTo(strong, finalText);
          var value = Number(String(finalText).replace(/,/g, ""));
          if (cards[i].dataset.tone === "red" && value > 0) cards[i].setAttribute("data-hot", "");
          else cards[i].removeAttribute("data-hot");
          if (!first && before !== undefined && before !== "" && before !== strong.dataset.nwShown && motionOn()) {
            cards[i].classList.remove("nw-flash");
            void cards[i].offsetWidth;
            cards[i].classList.add("nw-flash");
          }
        }
      }
      document.addEventListener("animationend", function (e) {
        if (e.animationName === "nwFlash" && e.target.classList) e.target.classList.remove("nw-flash");
      }, true);

      function setBadge(id, value) {
        var el = document.getElementById(id);
        if (!el) return;
        el.textContent = fmt(value);
        el.hidden = !num(value);
      }

      var firstData = true;
      window.NWDesign.onDashboard = function (data) {
        lastData = data;
        flow = computeFlow(data);
        renderOrbs(firstData);
        renderRibbon(data);
        renderPipe(data);
        fhState.textContent = heroState(data);
        animateMetrics(firstData);
        var s = data.summary || {};
        setBadge("navBadgeFailed", s.failedJobs);
        setBadge("navBadgeAlerts", s.criticalAlerts !== undefined ? s.criticalAlerts : s.totalAlerts);
        firstData = false;
      };
      window.NWDesign.onReset = function () {
        lastData = null;
        flow = computeFlow(null);
        renderOrbs(true);
        renderRibbon(null);
        renderPipe(null);
        fhState.textContent = heroState(null);
        setBadge("navBadgeFailed", 0);
        setBadge("navBadgeAlerts", 0);
        firstData = true;
      };
      window.NWDesign.onReset();
      if (hero) hero.dataset.nwRise = "";
      var risers = document.querySelectorAll(".management-card, .metric, .dashboard > .section, .dashboard-toolbar");
      for (var r = 0; r < risers.length; r++) risers[r].setAttribute("data-nw-rise", "");
      intro();
    })();
