# NetWorker Dashboard — User Guide

**Audience:** backup operators and anyone who reads the dashboard.
**Scope:** everything you do in the browser — signing in, connecting to a
NetWorker server, reading the dashboard, snapshots, exports, sharing, email
notifications, themes, and the TV wall display.

> Installation and service operation are covered separately in
> [`INSTALL.md`](INSTALL.md) and
> [`NWDash-Deployment-SOP.md`](NWDash-Deployment-SOP.md).

> *Figure: the login page.*
> *(screenshot placeholder)*

---

## 1. Signing in

Browse `https://<host>:8443/` (your operator will give you the exact
address). The dashboard uses **one shared password** — there are no user
accounts. Enter it once; your session is remembered in this browser until it
expires or you sign out from the **Account** menu.

- With the default self-signed certificate the browser shows a trust warning
  once — your operator can install a real certificate to remove it.
- Repeated wrong passwords are rate-limited; wait a minute and retry.

---

## 2. Connecting to a NetWorker server

Click **Add Server** and fill in the connection dialog:

| Field | What to enter |
|---|---|
| Host | The NetWorker server's address |
| Connection mode | **Auto discover** (default — tries REST, falls back to NWUI), **NetWorker REST API**, or **NWUI API** for older servers |
| Ports | REST API port and backup-server port (default 9090) — leave the defaults unless your environment differs |
| Username / password | A NetWorker account with read access |
| Time window | Last 24 h by default; custom ranges use DD-MM-YYYY |
| **Verify REST API TLS certificate** | **On by default — leave it on.** Untick only for a server with a self-signed certificate you trust, and only for that profile |

The dashboard supports multiple servers — add each one and switch between
them from the server list.

> *Figure: the Add Server dialog with Auto discover selected.*
> *(screenshot placeholder)*

### Connection profiles

Save a connection as a named **profile** so you never re-type it. Passwords
in profiles are stored encrypted on the server and are never sent back to the
browser — a restored profile shows a masked password. Profiles survive
restarts and upgrades. (Keys are machine-bound: if the app is ever moved to a
new machine, saved passwords must be re-entered once — the dashboard will say
so.)

---

## 3. Reading the dashboard

The main view is built top-down:

- **KPI cards** — totals for the selected window: jobs, successes, failures,
  data moved, and so on. They update live (the page refreshes itself about
  once a minute; your place is kept).
- **Health blocks** — Backup SLA and activity donuts, management overview,
  recovery health, clone jobs, and (when configured) Windows server
  CPU/memory/disk via WMI.
- **Job tables** — tabs along the top of the table:
  **Recent Jobs · Failed Jobs · Restores · Clone Jobs · Logs · Alerts ·
  Clients · Timeline · Heatmap**.
  - **Timeline** lays jobs out over time; **Heatmap** shows job recency at a
    glance. Neither paginates.
  - The other tabs show 25 rows with **Show more / Show all** — your expanded
    view survives the auto-refresh.
- Click a job row for a **details drawer** (drag its left edge to widen it).

> *Figure: the dashboard — KPI row, SLA donuts, and the jobs table.*
> *(screenshot placeholder)*

---

## 4. Snapshots and comparison

The **Local Snapshot Growth** section keeps daily point-in-time copies of the
dashboard data on the dashboard host (nothing is sent anywhere).

- Ranges: **7 / 30 / 90 days**.
- **Compare growth** shows Before/After cards with a delta pill per metric —
  what changed between two snapshots.
- **Snapshot History** lists every stored snapshot; auto-save can be toggled.
- **Export CSV** downloads the snapshot series.

---

## 5. Excel export

The **Export** button on the dashboard downloads the current view as an
**Excel workbook** (`.xlsx`) — KPI summary plus the job tables, styled to
match the report emails (Aptos/Arial). Use it for ad-hoc hand-offs; for
recurring delivery use email notifications (§7).

---

## 6. Sharing a read-only view

**Share** (top bar) → **Generate Link** creates a read-only URL you can give
to anyone — no login needed, nothing clickable, no credentials exposed.

- **Copy** puts the link on your clipboard.
- **Revoke** invalidates every previously shared link immediately.

Share links show the dashboard as of when it renders — they are for viewing,
not administration.

> *Figure: the Share Read-Only View dialog.*
> *(screenshot placeholder)*

---

## 7. Email notifications

Open **Email** in the top bar — the **Email Alert Automation** dialog is the
one place all email is managed.

**Nothing sends unless you explicitly arm it.** Saving SMTP settings or a
profile stores configuration only; email goes out only for a schedule you
created or a profile you toggled **ON**.

### SMTP settings

Host, port, security, username/password, from address. The password is stored
encrypted and never shown again. **Send test** delivers a test message so you
can prove the relay works before arming anything.

### Two kinds of schedule

- **Daily report** — the full dashboard emailed at a time you choose
  (rendered as an image when the host has a headless Chrome/Edge, HTML
  otherwise).
- **Alert check** — periodically checks for failures/alerts and emails only
  when there is something to say.

### Profiles with ON/OFF toggles

Save a named **profile** (recipients + schedule settings) and arm it with its
**ON/OFF toggle**. ON arms a schedule from the profile's stored settings —
it works even with no live connection, because the profile carries its own
encrypted connection snapshot. OFF stops it. Toggle state survives restarts
and upgrades and can never drift from what is actually scheduled.

### Options

- **Quiet hours** — a window (may wrap past midnight) during which nothing is
  sent; suppressed alerts are held.
- **Digest mode** — batch alerts into one summary email instead of one per
  event.

### Self-healing (v2.13.0+)

If a schedule loses its NetWorker connection (for example after a service
restart), it automatically adopts the most recent working one — the live
dashboard session if anyone is connected, otherwise the newest saved session
— and keeps sending. Each schedule shows a status chip: **live** (connected
now), **reconnectable** (will heal on the next cycle), or **waiting** (no
usable connection has ever existed — connect once on the dashboard and it
arms itself). The last send result is shown next to each schedule.

> *Figure: the Email dialog — profile cards with ON/OFF toggles and status
> chips.*
> *(screenshot placeholder)*

---

## 8. Themes

The theme picker (paint-swatch row in the dashboard) offers **15 one-click
themes**, including dark, light, violet, sandstone, and carbon true-black.
One click applies and saves it — the choice persists for the installation,
and TV mode and scheduled email reports render in the same palette.

---

## 9. TV mode (wall display)

Click the **TV mode** button (or browse `https://<host>:<port>/tv`) on the
display device and log in once there.

- Auto-sizes to any screen from 1080p to 8K and never scrolls.
- LIVE/OFFLINE indicator with automatic reconnect; wall clock in the header.
- The jobs table always fills the remaining screen and **auto-cycles**
  Recent → Failed → Clients every 15 seconds.
- **Single click on the jobs table minimizes it** — the KPI and donut blocks
  grow to use the space; the choice is remembered across reloads.
- Follows the dashboard's saved theme.

> Since v2.13.0 the TV page is `/tv` with a one-time login on the display.
> The old `/tv/<token>` no-login URL no longer works.

> *Figure: TV mode on a wall display, jobs table minimized.*
> *(screenshot placeholder)*

---

## 10. Working with dialogs

Every dialog behaves the same way:

- **Resize** by dragging the corner grip — content scrolls inside, the header
  stays pinned.
- **Close** with the X, with **Escape** (topmost dialog first), or by a
  deliberate click on the dark backdrop — press *and* release must both be on
  the backdrop, so resizing or selecting text can never close a dialog by
  accident.
- The Account menu closes when you click elsewhere or pick an action.

---

## 11. If something looks wrong

| Symptom | Try |
|---|---|
| Page looks broken right after an upgrade | Hard refresh once — **Ctrl+F5** |
| "Verify TLS" errors when connecting | The NetWorker server has a self-signed/private certificate. Ask your operator whether to trust it; unticking verification is per-profile and deliberate |
| No data but the connection saved | Check the time window, the connection mode (try Auto discover), and the account's permissions on the NetWorker server |
| Schedule shows **waiting** | No usable connection has ever been saved — connect to the server once on the dashboard; the schedule heals itself on the next cycle |
| Locked out / password unknown | Only an operator can reset it (reinstall path) — see the Deployment SOP |
