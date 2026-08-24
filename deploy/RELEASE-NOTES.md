# Release notes

Operator-facing summary of what each release changes and anything to do or warn
people about before deploying it. Newest first.

Scope rules for this file:

- **What changed for a user or an operator**, not the commit list. `git log` is
  the commit list.
- **Any pre/post-upgrade step** — a hard refresh, a config to re-create, a URL
  to re-point.
- **Anything that will generate a support question** — a behaviour that looks
  like a regression, a capability someone loses, or a burst of email.

Every release ships as a full bundle (`nwdash-bundle-<ver>-win-x64.zip`) and
upgrades via `Setup-NWDash.cmd` — see [`INSTALL.md`](INSTALL.md). Each GitHub
release body carries the bundle's SHA-256. The release policy (what may ship
under which bump) is [`../RELEASING.md`](../RELEASING.md).

---

## v2.13.1 — Aptos/Arial typography everywhere

Patch tier.

- **Typography**: the dashboard, login and read-only pages, TV mode, scheduled
  email reports, the emailed snapshot image, and the Excel export now use
  **Aptos** with **Arial** fallback. No font files are shipped — the app stays
  fully offline; hosts without Aptos render Arial.
- Repaired a test-suite lint break that had CI red since 2026-07-21 (a
  pytest-style test file that never actually ran); its three cases now execute.

**CI note:** GitHub Actions is billing-blocked for this account. The workflow's
gates (ruff + the full 122-test suite) were run **locally** on Windows /
Python 3.14 for this release: both green.

**Deploying:** re-run `Setup-NWDash.cmd` (auto-detects the release). No
user-visible behaviour change beyond fonts.

---

## v2.13.0 — original email system restored + self-healing

Minor tier. **This is a deliberate wholesale revert — read before deploying.**

- The email/reporting redesigns of 2.9.0–2.12.0 repeatedly broke notification
  delivery. This release **removes all of them** and restores the proven
  v2.8.3 email engine exactly as it was: the **Email** button and its dialog —
  saved schedules (daily report + alert check), named profiles with ON/OFF
  toggles, quiet hours, digest mode, Send test, SMTP configuration — all
  persisted across restarts.
- **One enhancement — self-healing connection**: the old engine's single flaw
  was a schedule losing its connection after a service restart and waiting
  silently. Schedules now adopt the most recent working connection
  automatically (live dashboard session, else newest saved session) and keep
  sending. Each schedule shows a status chip — **live** / **reconnectable** /
  **waiting** — plus its last send result.

**Support-question warnings:**

- The **`/reports` page is gone**; email is managed via the **Email** button
  again. Anyone trained on the 2.11/2.12 Scheduled Reports page loses it.
- **TV displays must be re-pointed to `https://<host>:<port>/tv`** — the
  2.10.x tokenized `/tv/<token>` URL no longer works.

**Deploying:** run `Setup-NWDash.cmd` with the bundle beside it; hard refresh
once (Ctrl+F5). Previously saved schedules and SMTP settings are reused as-is
and self-heal on the next cycle after anyone opens the dashboard.

---

## v2.12.0 — fixes empty reports + Scheduled Reports page

Minor tier. *(Superseded: this reporting stack was removed in v2.13.0.)*

- **Fixed reports arriving empty** (right server name, every metric 0): the
  hand-entered reporting connection assumed auth mode / TLS / API version /
  backup-server address; any mismatch authenticated but returned no rows. The
  reporting connection became a **complete copy of the live dashboard
  connection** — set with one click ("Use my current dashboard connection"),
  validated immediately with a job count so an empty-report config is caught
  at setup, not in the inbox.
- New **Scheduled Reports page** (Account menu) holding the reporting
  connection, SMTP settings, and report groups.

**Deploying (historical):** hard refresh, then set the reporting connection
with the one-click button and re-check groups.

---

## v2.11.1 — checkbox fix + clear reporting-connection error

Patch tier. *(Superseded in v2.13.0.)*

- Checkboxes/radios rendered as large empty boxes (a global input style
  stretched them); now inline with their labels.
- **Send now** without a configured reporting connection produced a raw
  `[WinError 10049]` socket error; it now says plainly that the reporting
  connection isn't set, and a scheduled run in that state is marked unhealthy
  and raises an ops alert instead of failing cryptically.

---

## v2.11.0 — Scheduled Report Groups

Minor tier. *(Superseded in v2.13.0.)*

- Scheduled reports organised into independent **groups**: per-group section
  selection, recipients, cadence (daily / weekly Sun–Sat / monthly 1st),
  on/off toggle, reorder, and **Send now** with an optional `[TEST]` marker.
  Data pulled live at send time; failures send the last good report with a
  STALE banner plus an ops alert — never silent.
- All groups share **one reporting connection** (set under Account → TV /
  Display).

**Support-question warning (historical):** the previous per-job digest/alert
model was removed and jobs were **not migrated** — operators had to re-create
them as groups.

---

## v2.10.3 — config panels in a right-side drawer

Patch tier. Account-menu panels (Email/SMTP, Scheduled Reports, TV / Display)
open in a slide-in right drawer (close via X, backdrop, or Escape) instead of
appearing at the bottom of the dashboard. Hard refresh after upgrading.

---

## v2.10.2 — Account-menu buttons for Email/Reports/TV config

Patch tier. 2.10.1 moved the config panels onto the dashboard but left no menu
entry, so they were effectively hidden. Three **Account menu** buttons restore
access. Hard refresh after upgrading.

---

## v2.10.1 — SMTP settings UI + remove dead email modal

Patch tier.

- **Removed the dead Email Alert Automation modal** left behind by 2.9.0 —
  every button in it hit a removed endpoint and failed with HTTP 410.
- **Added an Email (SMTP) settings panel** (host, port, security, credentials,
  from address, ops-alert address) — 2.9.0 had shipped with no UI path to
  configure SMTP at all. Password stored encrypted, never returned to the
  browser.

---

## v2.10.0 — DSO TV wall: no-login display URL + 24/7 display feed

Minor tier. *(The tokenized URL was later removed in v2.13.0 — current TV mode
is `/tv` with a one-time login.)*

- **No-login wall URL** `https://<host>:<port>/tv/<token>`, generated and
  rotatable/revocable in the TV / Display panel. A leaked URL exposed only the
  read-only wall.
- **Display connection**: a dedicated NetWorker credential (encrypted at rest,
  DPAPI) rendered the wall 24/7 with nobody logged in.
- Documented setting the dashboard password via
  `Setup-NWDash.cmd -Upgrade -AuthPassword <pw>` (salted PBKDF2 hash;
  plaintext removed from the service env after first healthy boot).

---

## v2.9.0 — Scheduled Reports: credentialed, validated, no more silent failures

Minor tier. *(This whole redesign was reverted in v2.13.0.)*

- Fixed schedules **silently not sending** after a service restart (they had
  borrowed a live browser session that no longer existed, while the UI showed
  "Active").
- Replaced them with self-contained credentialed report jobs: own encrypted
  NetWorker credential, a hard save gate (live connect + render + SMTP test
  before a job could become Active), STALE-banner fallback plus a separate ops
  failure alert, per-job health in the UI.

**Support-question warning (historical):** the old automation engine and its
API were removed; existing schedules were **not** auto-imported and had to be
re-created once.

---

## v2.8.3 — DEDB-style deployment: NSSM service + zero-prerequisite bundle

Minor tier. The deployment model this repo still uses today.

- **Zero host prerequisites**: the bundle gained an embedded Python 3.12
  runtime and nssm.exe — Python no longer needs to exist on the target.
- **Real Windows service `NetWorkerDashboard`** (services.msc): crash
  auto-restart, rotated stdout/stderr logs, optional service account.
- **Safe upgrades**: pre-upgrade backup (newest 3 kept), health-gated start,
  automatic rollback with an error-log dump on failure; `-Rollback`;
  `-Check` dry-run.
- Installer moved **inside the bundle**; `Setup-NWDash.cmd` became a thin
  bootstrap. Legacy scheduled-task installs migrate automatically with data
  and config untouched.
- UI fix: dialogs no longer close accidentally on resize or on a drag that
  ends on the backdrop.

**Support-question warning:** upgrading from ≤2.8.2 required downloading BOTH
assets fresh — the bundle naming changed to `nwdash-bundle-*` and old
launchers look for the old name.

---

## v2.8.2 — resizable dialogs, consistent close behavior

Patch tier. Every dialog resizable by its corner grip with pinned header;
job-details drawer gets a drag handle; consistent X / Escape / click-outside
closing everywhere.

---

## v2.8.1 — email schedules follow the saved configuration

Patch tier. Stale schedules with drifted recipient lists kept sending (the
v2.6.2 duplicate key includes recipients, so drift dodged it). The saved email
configuration is now the source of truth: first boot after the update keeps
one schedule per type (newest wins, logged), syncs recipients/settings from
the saved config, and rewrites `automations.json`. Scheduling a report now
**replaces** any other schedule of the same type; named profiles are
unaffected. Watch the first-boot log for `reconcile: dropped stale ...` lines.

---

## v2.8.0 — TV wall-display mode

Minor tier. New `/tv` page (login once on the display): auto-resizes 1080p–8K
without scrolling, LIVE/OFFLINE indicator with auto-reconnect, SLA/activity
donuts, KPI cards, server health, snapshot-growth strip, jobs table that
always fills the remaining screen and auto-cycles Recent → Failed → Clients
every 15 s, single-click minimize (remembered), follows the saved theme, and a
TV button in the main dashboard.

---

## v2.7.0 — profile-based scheduling + modern UI

Minor tier. Email notification profiles became the scheduling unit: each saved
profile gets an **ON/OFF toggle** — ON arms a schedule from the profile's
stored settings (works with no live connection; the profile carries an
encrypted connection snapshot), OFF stops it. Toggle state survives restarts
and is derived from the live schedule registry, so it cannot drift. Re-saving
an ON profile re-arms it. Modernised automation dialog (status dots, summaries,
theme-matched effects).

---

## v2.6.2 — no duplicate email notifications

Patch tier.

- Re-scheduling the same report after a restart **added** a second schedule
  (duplicate detection was per browser session); both sent. Schedules now have
  an identity (type + recipients + SMTP host + sender) and scheduling replaces
  any same-identity schedule; existing duplicates in `data\` are healed on
  first boot (logged).
- **Behaviour change that generated questions:** "Save configuration" no
  longer silently arms a schedule — only "Schedule selected report" starts
  sending.

---

## v2.6.1 — bar/label overlap fix

Patch tier. Snapshot-comparison Before/After bars painted over their own
labels at scaled font sizes; label columns are now em-based.

---

## v2.6.0 — field fixes + persistence + themes + 4K

Minor tier, driven by the first production deployment.

- **Fixed:** Show more / Show all silently reset to 25 rows on every re-render
  (looked like dead buttons); Timeline/Heatmap empty because the backend's
  DD-MM-YYYY timestamps weren't parseable; snapshot-card delta pill overlap.
- **Email persistence overhaul:** schedules no longer vanish after restarts or
  updates — automations decoupled from live sessions, carry an encrypted
  connection snapshot, restore unconditionally, reconnect at send time, and
  are never auto-cancelled. Named email profiles added (encrypted in `data\`).
- **UI:** 4K/large-display scaling (verified to 3840 px); **15 one-click
  themes** via a swatch picker; email reports render in the same palette.

---

## v2.5.1 — keep-alive fix

Patch tier, from the first live deployment.

- **Keep-alive connection poisoning:** early-error POST responses (401/403/404)
  didn't drain the request body, corrupting the next request on the same
  connection (spurious 501s). All early-exit paths now drain safely.
- **CSRF bootstrap race:** the first theme save could fire before the token
  arrived, guaranteeing one 403+retry per page load. Mutating calls now wait
  for the bootstrap.

---

## v2.5.0 — hardened release

First release from the hardened codebase (feature set: REST/NWUI collectors,
WMI health, snapshots, email reports, alert automations).

- CSRF protection on all state-changing routes; outbound TLS verification
  fail-closed by default (per-profile override).
- Monolith split into the `nwdash\` package; UI extracted to real HTML/CSS/JS
  assets (byte-identical output); 27-test smoke suite incl. a subprocess HTTPS
  end-to-end; swallowed exceptions logged; loud DPAPI cross-account warning.
- Deployment at this point was still "extract + run with host Python 3.11+";
  the service/bundle model arrived in v2.8.3. Pre-hardening history lives on
  the `legacy/v2.5.0-history` branch; see `docs/REPORT-CARD.md` and
  `docs/PROJECT-STATE.md`.
