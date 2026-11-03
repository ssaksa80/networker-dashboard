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

## v3.3.0 — reports that say when data is missing, safer reinstalls, and security fixes

Minor tier: reports and the dashboard now say when job history failed or covers
less than the range, a plain reinstall keeps the service's settings, and the
review's Medium security findings are fixed. Nothing under `data\` changes
format, and URLs and the service command line are the same.
[`RELEASING.md`](../RELEASING.md) lists a changed default that alters existing
installs under MAJOR. One change here can stop something working: a connection
profile saved by an older version now checks the NetWorker certificate. It is
fixed in the dashboard in a minute, with nothing lost, so it ships as a minor
with the notes below. `Setup-NWDash.cmd -Rollback` restores v3.2.0.

### Before you upgrade

- **Older connection profiles now check the NetWorker certificate.** A profile
  saved by an older version had no Verify TLS setting, and connected with its
  saved password and the certificate check off. It now counts as Verify TLS
  on. If the dashboard host doesn't trust that NetWorker server's certificate,
  Connect with the profile fails verification. Select the profile, untick
  **Verify TLS**, type the password and save it: the profile then keeps that
  choice.
- **Use this release's `Setup-NWDash.cmd`.** It no longer downloads a newer
  release on its own; it says when one exists. Add `-Update` to download it,
  and the download is used only if its SHA-256 matches both GitHub's digest and
  the release's **Bundle SHA-256** line. Older copies of the script still
  download unasked and unchecked. A bundle zip with an entry outside its folder
  is refused before anything is unpacked.
- **Installing asks for the dashboard password.** Run `Setup-NWDash.cmd`
  without `-AuthPassword` and type the password when the installer asks, after
  UAC (hidden, typed twice). Double-clicking `Setup-NWDash.cmd` now works
  instead of being refused. `-AuthPassword` still works for an unattended
  install from an elevated prompt. Given in a non-elevated prompt, it is asked
  for again in the elevated window, because passwords no longer cross UAC.
  `-ServicePassword` works the same way.
- **A reinstall keeps the service's settings, and opens fewer networks.**
  - Running the installer without `-Upgrade` on an existing install used to
    rebuild the service from that command line alone, dropping its port, bind,
    allow-list, account and `-AllowUnauthenticated`. Whatever the command line
    leaves out is now kept, and `-Check` lists it. To drop a bind pin, pass
    `-BindHost 0.0.0.0`.
  - A service that logs on as a named account needs `-ServicePassword` on a
    reinstall, because Windows never gives the password back. Without it the
    installer stops before changing anything and says what to pass, or to use
    `-Upgrade`. LocalSystem and `NT AUTHORITY\` accounts need none.
  - An install now opens the port on Domain and Private networks only, where it
    used to open every network. `-FirewallProfile Any` opens Public networks
    too. `-Upgrade` and `-Rollback` never touch the rule, so an upgraded host
    keeps its rule until its next install without `-Upgrade`. A host on a
    network Windows classifies as Public then stops being reachable there; the
    install log and `-Check` name each such network and how to open it.

### Fixes

- **A failed job-history fetch no longer looks like a quiet day.** In NWUI
  mode, when NetWorker's jobs database couldn't be fetched (its size cap, a
  timeout, a password that couldn't be read), the dashboard and the daily email
  showed zero completed jobs, a 0% SLA and health OK. The SLA now shows N/A,
  health is a warning at best, and the daily email has a **Data incomplete**
  banner with NetWorker's error.
- **Reports say what period their data covers.** NetWorker often keeps only a
  few days of jobs, so Last Week or Last Month could cover far less. Every
  email report has a **Data covers** row, a shorter range gets a **Shorter than
  requested** banner, and the dashboard's notice bar says the same.
- **Every save set counts, and "latest" means newest** (NWUI mode). Two save
  sets of one client started in the same second counted as one, so a failure
  beside a success could vanish. Jobs were sorted by day of the month, so the
  Jobs table, Server Protection and a policy's last run could show an older
  job.
- **A 1% CPU reading no longer shows as 100% and Critical.**
- **Saves get through files held by antivirus or a backup agent.** Sessions,
  schedules and email settings could be lost without a word. A save now
  retries for about 1.5 seconds, and one that still fails is in the log as
  `could not save <file>`. Two settings files can no longer be left cut short.
- **Pages load faster.** The dashboard page was 1.4 MB on every load, with the
  logo inlined five times. Its CSS, JS and logo, and the admin console's and
  TV's, are now separate files the browser caches, with new URLs at each
  upgrade. `/networker-logo.png` now serves the real logo.
- **Live updates don't wait for a slow viewer.** One viewer that stopped
  reading could hold up a Connect or a refresh for about 30 seconds.
- **An expired share link says so**, instead of showing a dashboard of zeros
  and fetching it every minute. The page no longer prints the link's token.
- **`-Rollback` uses up the backup it restores.** A second `-Rollback` used to
  restore nothing; it now goes one backup further back. A backup without the
  app or its runtime is refused before the service is stopped, and
  `-Check -Rollback` shows the same verdict. Empty backup folders left by
  earlier rollbacks are skipped, and the next `-Upgrade` removes them.

### Security

- **The dashboard password never enters the service's environment.** The
  installer stores only its salted hash in `data\auth.json`, and the bootstrap
  shows `********` instead of the password. `-Upgrade` removes a plaintext
  `DASHBOARD_AUTH_PASSWORD` that an earlier failed install left in the service
  environment. On such installs, a password changed in the admin console now
  survives a restart.
- **With an allow-list (`--allowed-hosts`), each NetWorker address is looked up
  once**, checked and connected to. A name could resolve to an allowed address
  when checked and to another when connecting.
- **Regenerate revokes the link on screen** even after the session changed (a
  reconnect, an automatic re-login). The old link used to stay valid for 24
  hours.
- A request with a malformed CSRF token gets an error answer, not a reset
  connection.

### Support notes

- **"Connected with warnings" in NWUI mode** when `/nwrestapi` can't be
  reached is intended: without the job history the dashboard can't know the
  range was healthy. When the SLA isn't measured, the plain-text **Backup SLA**
  row reads `N/A (0 met / 0 total)`.
- Alert schedules don't yet look at missing history or short coverage.
- A viewer that falls 8 updates behind during a burst is dropped and reconnects
  within 15 seconds; its page's own refresh fills in anything it missed.
- A password with a space or a double quote is split by `cmd.exe` when typed on
  `Setup-NWDash.cmd`'s command line. Let the installer ask for it instead.
- `-ServicePassword` still reaches `nssm.exe`'s command line once, when the
  installer sets the service account.
- Reload any dashboard, admin console or TV tab that was open during the
  upgrade, so it picks up the new page.

### Rollback

Run `Setup-NWDash.cmd -Rollback` from an elevated prompt. It restores v3.2.0,
with its faults. Saved data stays as it is. A profile saved with Verify TLS
unticked keeps that setting; an older profile without one goes back to
connecting with the certificate check off. The firewall rule stays as the last
install set it.

**Gates:** GATES-PENDING

---

## v3.2.0 — choose a daily report's range, a locked install folder, and safer upgrades

Minor tier: a new range control for daily reports, the install folder locked
against other local users, and fixes, including the certificate upload bug in
v3.1.0. Nothing under `data\` changes format, no key is rewritten, and URLs and
the service command line are the same. [`RELEASING.md`](../RELEASING.md) lists
a changed default that alters existing installs under MAJOR. The folder lock
changes who can open the folder, but nothing an install runs on stops working,
so it ships as a minor with the notes below. `Setup-NWDash.cmd -Rollback`
restores v3.1.0, with the effects listed under Rollback.

### Before you upgrade: the install folder gets locked

Until now the install folder inherited from `C:\`, and any local user who could
read a key file could unwrap it. The upgrade now sets these permissions, with
the service still running, and no password has to be entered again:

| Folder | SYSTEM, Administrators | Service account | Other users |
|---|---|---|---|
| `data\`, `.certs\`, `logs\` | Full control | Modify | None |
| `backups\` | Full control | None | None |
| The app and runtime | Full control | Read | Read |

- **Anything that reads these folders under an ordinary account loses access**,
  a log shipper or a monitoring agent for example. Run it as an administrator
  or SYSTEM, or read through the dashboard instead. The lock replaces each
  access list outright, so entries added by hand are removed.
- **Open `logs\`, `data\` and `.certs\` from an elevated prompt.** If Explorer
  offers "You don't currently have permission… Continue", choose **Cancel**:
  **Continue** adds your account to the folder's access list until the next
  `-Upgrade`.
- **Starting the app by hand** needs an elevated prompt too.
- **Keep `--cert` and `--key` in `.certs\` or a folder of their own.** The TLS
  tab writes beside them, and a key in the root of the install folder would end
  up readable by users.
- If the lock can't be set, the upgrade stops there with the service still
  running. On a drive root or a system folder it isn't set at all, and the
  installer warns.

### New

- **Choose a daily report's range.** The email dialog and the admin Email tab
  now have a 24 hours, 7 days or 30 days control that shows the saved value, and
  the schedule and profile lists show each report's range.
- **Saved email settings reach running schedules straight away**, with no
  restart. That covers the SMTP server, recipients, times and range, for alert
  and report schedules alike. Saving still never starts sending: it arms
  nothing, paused schedules stay paused, and a schedule armed from a profile
  follows its profile. The save says how many schedules it updated.
- **Health self-test** has a new Folder permissions step, which fails if the
  lock is lost.

### Fixes

- **Certificate upload is all or nothing.** The Known issue in v3.1.0 is fixed.
  If either file can't be written, both stay as they were, the error says
  "Nothing was replaced", and **Put it back** still has the pair it had.
- **A failed upgrade or rollback recovers.** If a file stays locked or the disk
  fills during the swap, the previous files go back, the service starts, and
  the installer exits non-zero with the cause. The service stays stopped only
  if the old files can't be put back either, because a half-restored app must
  not run. The error then names the backup folder to finish from by hand.
- **A daily report never uses cached data of another range.** If NetWorker has
  no job data and no saved dashboard covers the report's range, no email goes
  out, and the schedule's last result says why. **Send test** follows the same
  rule, and a test email with no data for its range says so.
- **A changed SMTP password reaches its schedules.** It used to miss any
  schedule that already had a password, even across a restart.
- **Dialogs open above the sidebar and the top bar.** The sidebar could cover
  part of the Email, Add server and Share dialogs, and on a phone the top bar
  could cover a dialog's close button.
- **Edit on a schedule** now shows that schedule's own recipients, time and
  range, not the email configuration's.

### Support notes

- **Check the range on the Schedules tab after upgrading.** A range set through
  the API on v3.1.0 stays, unless the email settings were saved afterwards: on
  v3.1.0 that save takes effect at the next restart, which this upgrade is.
  The resets described in the v3.1.0 notes no longer happen.
- **No daily report today?** Look at the schedule's last result. A skipped day
  isn't retried until the next run.
- **The email configuration's password now wins** over a schedule's own, once
  it's confirmed for that schedule's server, on a save or at a restart. If no
  password is saved for a new server, the save warns that schedules can't log
  in.
- While a dialog is open the sidebar is dimmed, and clicking it closes the
  dialog.
- Alert schedules still use the dashboard session's range.
- Reload any dashboard or admin console tab that was open during the upgrade,
  so it picks up the new page.

### Rollback

Run `Setup-NWDash.cmd -Rollback` from an elevated prompt. It restores v3.1.0,
with its faults and without the range control, and daily reports keep the range
they have. The folder lock stays: restored files take the current permissions,
so rolling back doesn't reopen the install.

**Gates:** GitHub Actions (lint, unit tests on 3.12.10 and 3.14, installer
tests, secret scan) green on the release PR. ruff 0.17.2 and the full 444-test
suite run **locally** on Windows, on Python 3.13, 3.14 and this bundle's
embedded Python 3.12.10, and the installer tests under Windows PowerShell 5.1:
all green. The bundle was booted in the installed layout on its own runtime, and
`-Upgrade` on a test host passed. The automated gates use no real NetWorker
server.

---

## v3.1.0 — replace the TLS certificate from the console, and daily reports every day

Minor tier: one new admin feature, plus fixes to daily reports and live
updates. Daily report schedules gain a `report_range` field under `data\`,
which v3.0.1 ignores. URLs and the service command line are the same.
[`RELEASING.md`](../RELEASING.md) lists a changed default that alters existing
installs under MAJOR. The move to 24-hour reports isn't one: 24 hours was
already the app's default range, and the old behaviour was a fault.
`Setup-NWDash.cmd -Rollback` restores v3.0.1, with the effects listed under
Rollback below.

### New: replace the certificate from the admin console

- On the admin console's **TLS** tab, paste or load a PEM certificate
  (intermediates after it) and its key, then choose **Check and use**. New
  connections get it straight away, with no restart. `--cert`/`--key` on the
  host still work.
- Before anything is replaced, the console checks that the key belongs to the
  certificate, that the dates are valid, and that the certificate covers the
  name you opened the console with. If it doesn't cover that name, you have to
  confirm. Take that prompt seriously: the dashboard sends HSTS, so a browser
  that trusted the old certificate won't let anyone click past a wrong one.
- The replaced pair is kept as `*.previous`, and **Put it back** restores it.
  If browsers refuse the new certificate and the console can't be reached,
  [`INSTALL.md`](INSTALL.md) section 6 has the recovery by hand.
- `*.previous` includes the old private key. If you're replacing a leaked key,
  delete both `*.previous` files once the new certificate works.
- Keys with a passphrase and `.pfx`/`.p12` files are refused, and the tab
  shows the `openssl` commands to convert them.
- Both changes go in the audit log, never with key material.

### Daily reports

- **Daily reports now arrive every day.** A report used to be skipped when the
  dashboard's content hadn't changed, so a stable, all-green estate got none
  after the first email. **Recipients will see more mail.** A second run on
  the same day for the same range is still skipped.
- **Daily reports now cover the last 24 hours by default.** They used to cover
  whatever range the dashboard session last used, which could be 30 days or
  the same frozen custom dates every day. **Every existing daily schedule
  moves to 24 hours on upgrade**, with no action needed, even if the dashboard
  was on 7 or 30 days.

### Fixes

- **Pages on different NetWorker servers stay separate.** A live update could
  switch a page to whichever connection refreshed last, and Export, Share and
  email then acted on that other server.
- **Errors are shown instead of a page reload.** A NetWorker server refusing
  the credentials, or an expired dashboard session, used to reload the page,
  and a page restored onto an expired session kept reloading. The page now
  keeps its last data and shows "Reconnect needed" with the server's message.
  It goes back to sign-in only when the dashboard login itself has ended.
- **Admin console.** A mistyped current password on the Security tab now shows
  its error instead of going back to the sign-in card, and a console whose
  login has ended no longer stays stuck on the unlock card.
- **False self-test failure on a CA-issued certificate.** The listener
  self-test at start-up and on Health failed when the certificate file didn't
  include its root CA. The service kept serving, but the warning was wrong.
- **TLS tab.** The Valid row now shows the year.

### Support notes

- **Other daily report ranges are API-only for now, and easy to lose.** To set
  7 or 30 days, call `POST /api/alert-automation` with `action` `start`,
  `scheduleType` `daily_report`, `reportRange` `7d` or `30d`, a dashboard
  `sessionId`, the SMTP fields and the schedule's other settings: `reportTime`,
  `theme`, `quietStart`, `quietEnd` and `digest`. Anything left out goes back
  to its default, so a report sent at 06:30 moves to 08:00. `action` `list`
  shows the current values. Neither the email dialog nor the admin Email tab
  has a control yet, so these put the range back to 24 hours:
  - **Schedule selected report** in the email dialog with the daily report type
    selected, straight away;
  - any **Save** on the admin Email tab, even an SMTP change, and **Save
    configuration** in the email dialog with the daily report type selected.
    These take effect at the next service restart, so reports keep their range
    until then and the change is easy to miss;
  - an email profile saved from the dialog, which always stores 24 hours.
- If live data isn't available when a daily report runs, it can still fall
  back to a cached dashboard of another range.
- Alert schedules still use the dashboard session's range.
- **Scripts that call `/api` directly:** an expired dashboard session now
  answers 409 `session_expired` (was 401), and `/api/dashboard` and
  `/api/export` answer 502 `networker_auth_failed` when the NetWorker server
  refuses the credentials. Not being signed in is still a 401, now marked
  `X-NWDash-Login: required`.
- Reload any dashboard or admin console tab that was open during the upgrade,
  so it picks up the new page.

### Known issue

- **A failed certificate upload can stop the next restart.** The service
  account needs write access to the certificate, the key and their folder. If
  the certificate file can be replaced but the key file can't (a read-only key
  file, for example), the console shows "The certificate files could not be
  written" and the service keeps serving the old certificate, but the two
  files on disk no longer match and the service won't start next time. After
  that error, copy the certificate's `.previous` file back over the
  certificate file before restarting. If the folder itself can't be written,
  the upload fails the same way but changes nothing.

### Rollback

`Setup-NWDash.cmd -Rollback` restores v3.0.1, and with it the faults this
release fixes:

- All-green sites stop getting daily reports again after at most one more
  email.
- Daily reports go back to the dashboard's last range and ignore a range set
  through the API. Upgrading again brings that range back, unless the email
  settings were saved on v3.0.1 in between.
- The false self-test failure comes back.

An uploaded certificate stays in place, because rollback doesn't touch
certificates.

**Gates:** GitHub Actions (lint, unit tests on 3.12.10 and 3.14, installer
tests, secret scan) green on the release PR and the release commit. ruff 0.17.2
and the full 322-test suite run **locally** on Windows, on Python 3.13 and on
this bundle's embedded Python 3.12.10: all green. The bundle was booted in the
installed layout on its own runtime. Not tested against a real NetWorker server.

---

## v3.0.1 — admin console security fixes

Patch tier: two security fixes in the admin console that v3.0.0 added.
Nothing under `data\` changes format, URLs and the service command line are
the same, and rollback is clean. Upgrade every v3.0.0 install.

### Fixes

- **An idle admin console now locks.** v3.0.0 extended the 15-minute admin
  window on every admin request, including the Logs tab's 4-second
  auto-refresh, so a console left open on Logs could still change the
  password and the allow-list for the whole sign-in (up to 12 hours). Now
  only changes and the pages you open count as activity; the auto-refresh
  does not.
- **No-password installs and a reverse proxy on the same server.** With no
  password set, v3.0.0 opened `/admin` to any request from the server's own
  address. A reverse proxy on that server (IIS ARR, nginx) connects from the
  same address, so anyone who could reach the proxy got the console and
  could set the first password, locking the operator out. Now:
  - a request that carries a proxy-forwarding header (`Forwarded`, `Via`,
    `X-Forwarded-*`, `X-Real-IP`, `X-Client-IP`, `X-Original-URL`,
    `X-ARR-Log-ID`) is never treated as coming from the server itself, and
  - the console asks for a **setup code** before it opens without a
    password. The service writes a new one to `data\setup-code.txt` at every
    start (the log shows only the path) and deletes it when the first
    password is set from the console. Wrong codes count toward the sign-in
    lockout.

### Support notes

- **No password yet?** Opening `/admin` on the server now asks for the setup
  code: copy it from `data\setup-code.txt` in the install folder
  (`C:\apps\networker-dashboard\data\` by default). Restarting the service
  replaces the code.
- An admin console left open asks for the password again after 15 minutes
  without a click, even on the Logs tab. That is the fix, not a fault.
- Reload any admin console tab that was open during the upgrade, so it picks
  up the new page.

### Rollback

`Setup-NWDash.cmd -Rollback` restores v3.0.0, and with it both issues. The
one new file, `data\setup-code.txt`, is ignored by v3.0.0.

**Gates:** ruff 0.17.2 and the full 252-test suite run **locally** on
Windows, on Python 3.14 and on this bundle's embedded Python 3.12.10: both
green. GitHub Actions runs the same gates on the release commit.

---

## v3.0.0 — new look, admin console, and a password for network installs

Major tier: a network install with no dashboard password no longer upgrades
or starts unless the operator says so (below). Everything else is additive,
and rollback is clean.

### Before you upgrade (mandatory)

- **Network installs need a dashboard password.** If the service listens on
  anything other than `127.0.0.1` and no password is stored, `Setup-NWDash.cmd`
  / `install.ps1 -Upgrade` now **refuses before stopping the service**, and the
  app itself exits with code 2 at start. Either:
  - set one: `Setup-NWDash.cmd -AuthPassword <password>` (stored as a salted
    hash; the plaintext is removed from the service environment after the
    first healthy boot), or
  - keep running open on an isolated network: `Setup-NWDash.cmd
    -AllowUnauthenticated` (sets `DASHBOARD_ALLOW_UNAUTHENTICATED=1`).
  Loopback-only installs are not affected.
- **Scripts that POST to the API** must send `Content-Type: application/json`
  (anything else gets 415), and browsers on another site are refused (403).
  The dashboard itself already does this.

### What's new

- **Four designs, switchable from the Design button** in the top bar:
  Daylight Glow (today's look, follows the 15 themes), Aurora Flow (dark
  navy glass), Command Deck (carbon, neon) and DEDB Navy (DEDB's look). Each
  viewer's choice is kept in their browser; the admin console sets the
  default (Daylight until changed). Motion has Full, Subtle and Off; PCs set
  to reduce motion always get Off. Nothing is loaded from the internet.
- **Protection flow** at the top of the dashboard: clients, policies,
  backups, clones, restores and SLA as glowing steps (or a job timeline per
  policy, or a pipeline, depending on the design).
- **New left sidebar** with the navigation, the actions that were under
  Account, and the connection form. It docks, collapses to icons, or floats
  (drag it by its header, double-click to park it, minimise it to a button).
- **Admin console at `/admin`** (also linked from the sidebar), laid out like
  DEDB's: Health with a self-test, Sessions, Audit, Logs, Connections, Email,
  Schedules, Snapshots, Share links, Appearance, Security and TLS. It asks
  for the dashboard password again before admin changes after 15 minutes
  idle. With no password set it opens only on the server itself, and offers
  to set one. (v3.0.1 fixes two security issues in this console; upgrade.)
- **Audit log**: sign-ins, failed sign-ins and changes (from the console and
  from the dashboard) with old and new values, in `data\audit.jsonl`.
  Passwords are never recorded.
- **Allow-list from the console**: when the service is not started with
  `-AllowedHosts` / `DASHBOARD_ALLOWED_HOSTS`, the NetWorker host allow-list
  can be edited under Security (`data\allowed_hosts.json`). A start-up
  allow-list still wins and shows read-only there.
- **Logout ends the session on the server**, not just in that browser
  (`data\auth_sessions.json`); the console's Sessions tab can sign others out.

### Fixes since v2.13.1

- The service no longer restart-loops when security software intercepts its
  own loopback TLS: the start-up self-test only warns now, and a real crash
  exits non-zero instead of reporting success.
- A slow or silent client can no longer stall new connections (TLS
  handshakes moved off the accept loop).
- A stored SMTP or profile password is dropped, not reused, when a save
  points it at a different server or account.
- The built-in development certificate was regenerated. Installs keep the
  certificate already in their `.certs` folder.

### Support notes

- **Everyone is signed in again once** after the upgrade if they used a
  password before: sessions issued by v2.13.1 carry no session id.
- The self-test's HTTPS-listener step can warn on hosts whose antivirus
  inspects loopback TLS; the dashboard is still serving.

### Rollback

`Setup-NWDash.cmd -Rollback` restores v2.13.1 cleanly: the new files under
`data\` (`audit.jsonl`, `allowed_hosts.json`, `auth_sessions.json`, the extra
keys in `ui_prefs.json`) are ignored by v2.13.1. A console-saved allow-list
stops applying until you set `-AllowedHosts` again.

**Gates:** GitHub Actions (tests + secret scan) green on the release commit.
The gates (ruff 0.17.2 + the full 246-test suite) were also run **locally**
on Windows, on Python 3.13 and on this bundle's embedded Python 3.12.10: both
green. The bundle was booted in the installed layout on its own runtime.

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
