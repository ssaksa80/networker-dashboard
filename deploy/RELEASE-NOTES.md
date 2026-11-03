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

## v3.3.0 — passwords off command lines, checked updates, and reports that say what they cover

Minor tier: fixes from the October review, several of which change what an
operator sees on the next install or upgrade. Nothing under `data\` changes
format. The installer now writes `data\auth.json` itself, in the form the app
always used, and saved dashboards gain a few fields that v3.2.0 ignores. URLs
and the service command line are the same. [`RELEASING.md`](../RELEASING.md)
lists a changed default that alters existing installs under MAJOR. The closest
change here is Verify TLS on older profiles, and Verify TLS has always been
documented as on by default, so the old behaviour was a fault.
`Setup-NWDash.cmd -Rollback` restores v3.2.0, with the effects listed under
Rollback.

### Before you upgrade: what operators will notice

Take the new `Setup-NWDash.cmd` from this release along with the zip, and
replace the old copy. The v3.2.0 one still runs this bundle, but it puts
passwords on command lines and installs updates it downloads without checking
them.

- **The firewall rule opens Domain and Private networks only.** It used to open
  the port on every network, Public included, which is how Windows marks a
  guest network or one it can't identify. `-Upgrade` and `-Rollback` never
  touch the rule, so an upgraded host keeps the one it has. The next install
  without `-Upgrade` makes the rule again, for Domain and Private only. **If the
  dashboard's network is classified Public, browsers lose the dashboard after
  that install**, unless you pass `-FirewallProfile Any` or reclassify the
  network as Private. `-Check` and the install log name each connected network
  the rule leaves closed. See [`INSTALL.md`](INSTALL.md), Firewall.
- **A plain reinstall keeps the service's settings.** Running
  `Setup-NWDash.cmd` without `-Upgrade` over an installed service used to
  rebuild it from that command line alone, dropping the port, bind address,
  allow-list, service account and `-AllowUnauthenticated` it ran with. Anything
  left off the command line is now kept from the installed service, along with
  environment variables added by hand, and what you pass wins. `-Check` lists
  what would be kept. **A service that logs on as a named account needs
  `-ServicePassword`**, because Windows never gives that password back. Without
  it the reinstall stops before it changes anything and says what to pass.
  `-Upgrade` needs none of this. See [`INSTALL.md`](INSTALL.md), Re-running the
  install.
- **`Setup-NWDash.cmd` downloads an update only with `-Update`, and checks
  it.** On a host with `gh` or a `GITHUB_TOKEN` it used to download a newer
  release by itself and install it unchecked. Now it says that a newer release
  exists and installs the bundle beside it. With `-Update` it downloads the
  newer zip to a temporary folder and uses it only if its SHA-256 matches both
  GitHub's digest for the file and the **Bundle SHA-256** line of the release
  notes. Otherwise it says why and carries on with the local bundle. This
  catches a damaged or altered download, not a forged release. A script that
  relied on the automatic download needs `-Update`. `-NoUpdate` still skips
  the check.
- **Older connection profiles are held to Verify TLS.** A profile saved without
  the Verify TLS setting connected from the dashboard with certificate checks
  off, and showed the box unticked. It now counts as ticked, as the admin
  console already showed it. **If the dashboard host doesn't trust that
  NetWorker server's certificate, Connect now fails with a certificate error**
  where it used to succeed. Either make the host trust it (add the issuing CA
  to the computer's Trusted Root Certification Authorities store), or untick
  Verify TLS, enter the password again and save the profile, which records the
  choice. Profiles that already have the setting don't change.
- **The dashboard password stays off command lines and out of the registry.**
  The installer now stores its hash in `data\auth.json` before the first start,
  so the service environment, which local users can read, never holds the
  password. `-Upgrade` removes one an earlier install left there.
  - A first install with no `-AuthPassword` asks for the password after UAC
    (hidden, typed twice) instead of refusing. A run that can't ask, such as an
    unattended one, is still refused.
  - A password given at a prompt that isn't elevated is asked for again in the
    elevated window, because UAC can't hand it over safely. The same goes for
    `-ServicePassword`. For an unattended install, run from an elevated prompt.
  - `Setup-NWDash.cmd` shows passwords as `********`.
  - A password changed in the admin console no longer goes back to the install
    password at the next restart. That happened wherever an install had left
    the password in the service environment.
  - Still on a command line: a password typed on `Setup-NWDash.cmd`'s own, and
    `-ServicePassword` on nssm's, which sets the service logon.

### Fixes

- **Reports say what they really cover.** NetWorker keeps job history only for
  its retention period, often a few days, so "Last Week" and "Last Month"
  covered those days while the label, the totals and the SLA claimed the whole
  range. Every daily email now has a "Data covers" row, and a "Shorter than
  requested" banner when the range is short. The dashboard says the same in its
  notice bar, with "(data from …)" after the range in the Updated line.
- **A missing job history shows as incomplete, not healthy.** When the
  jobs-database fetch failed (too large, a timeout, a saved password that
  couldn't be decrypted), the dashboard and the daily email showed zero
  completed jobs, a 0% SLA and health OK. They now show a warning and the SLA
  as N/A, and the email has a "Data incomplete" banner with NetWorker's error.
- **Every save set counts, and the latest job is the latest.** On servers read
  through NWUI, save sets of one client that started in the same second were
  merged into one, so a failure could vanish. Jobs were also ordered by day of
  the month, so the "latest" in Server Protection status, the job tables and a
  policy's last run could be an older one.
- **A 1% CPU or memory reading shows as 1%**, not 100% and Critical.
- **Live updates never wait on a slow viewer.** One browser that stopped
  reading could hold up a refresh for about 30 seconds, and two updates could
  garble each other. A viewer that falls behind is now disconnected, and its
  page reconnects by itself.
- **An expired share link says so.** It showed a dashboard of zeros, fetched
  again every minute, with the link's token printed at the bottom. It now says
  "This share link has expired or is invalid" and stops.
- **Regenerate revokes the share link it replaces**, even after a reconnect or
  an automatic re-login in between. The old link used to stay valid for its 24
  hours.
- **Pages load faster.** The dashboard page was about 1.4 MB on every load; it
  is now about 40 KB, and its scripts, styles and logo are files the browser
  keeps. Each release gives them new URLs, so no hard refresh is needed.
- **A save no longer fails silently on a held file.** Antivirus or a backup
  agent holding a file under `data\` could make a save of sessions, schedules
  or email settings fail without a word. Saves now retry for about 1.5 seconds,
  and one that still fails is logged as "could not save <file>".
- **A second `-Rollback` goes one backup further back.** It used to move the
  running app aside and restore nothing. A backup that lacks the app or its
  runtime is now refused before the service stops, and `-Check -Rollback` says
  so.
- **Security.** With `-AllowedHosts` set, the dashboard now connects to the
  address the allow-list approved: a DNS answer that changed between the check
  and the connection could send the NetWorker credential elsewhere. Refusals
  are logged with event `allowlist_connect_refused`. A request through a proxy
  and the WMI health check aren't covered. A malformed CSRF header gets a 403
  instead of a dropped connection. A zip entry that would unpack outside the
  bootstrap's staging folder stops the install before anything is unpacked.

### Support notes

- **"Connected with warnings" that doesn't clear**, on a server whose
  `/nwrestapi` can't be reached, is intended: without the job history the
  dashboard can't tell that the range was healthy. Alert schedules are
  unchanged.
- **"Shorter than requested" on a 7- or 30-day report** means NetWorker no
  longer holds the older jobs. The report covers what the server still keeps.
- **More jobs or failures than before the upgrade**, on a server read through
  NWUI: those are save sets the old merge folded together.
- **Dashboard unreachable after a reinstall:** check how Windows classifies the
  network (the firewall item above).
- **Connect fails with a certificate error on an older profile:** see the
  Verify TLS item above.
- Reload any dashboard, admin console or TV tab that was open during the
  upgrade, so it picks up the new page.

### Rollback

Run `Setup-NWDash.cmd -Rollback` from an elevated prompt. It restores v3.2.0,
with its faults. These stay as v3.3.0 left them:

- the firewall rule, which `-Rollback` never touches;
- the dashboard password: it is in `data\auth.json`, which v3.2.0 reads the
  same way, and it doesn't go back into the service environment;
- a Verify TLS choice saved on a profile. A profile still without one goes back
  to connecting from the dashboard with certificate checks off.

Keep the v3.3.0 `Setup-NWDash.cmd`: it runs older bundles too.

**Gates:** ruff 0.17.2 and the full 585-test suite run **locally** on Windows, on
Python 3.13, 3.14 and this bundle's embedded Python 3.12.10, and the installer
tests under Windows PowerShell 5.1: all green. The bundle was booted in the
installed layout on its own runtime. Not yet run on a real install or against a
real NetWorker server.

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
