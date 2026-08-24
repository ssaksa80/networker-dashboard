# Releasing NetWorker Dashboard

NetWorker Dashboard uses semver `X.Y.Z`. **The version bump is decided by
compatibility, not by "did code change".** This page is the single source of
truth a releaser follows. Architecture rules that constrain what a release may
change live in [`docs/PROJECT-STATE.md`](docs/PROJECT-STATE.md) — read it
before touching `nwdash/`.

## Tier table

| Bump | When | Deploy path | Risk |
|---|---|---|---|
| **PATCH `Z`** (`2.13.0 -> 2.13.1`) | Bug fix / small tweak, **application code only**. No new dependency, no embedded-Python change, no installer behaviour change. | Full bundle (there is no overlay tier — the bundle is small enough that every release ships one). Upgrade via `Setup-NWDash.cmd` / `install.ps1 -Upgrade`; backup + health-gate + auto-rollback. | Drop-in. |
| **MINOR `Y`** (`2.13.x -> 2.14.0`) | New, **backward-compatible** functionality. New optional dependency OK. Resets `Z` -> 0. | Full bundle, same upgrade path. Anything under `data\` must keep loading unchanged — persisted state is the compatibility surface (see below). | Additive, low. |
| **MAJOR `X`** (`2.x -> 3.0.0`) | A **breaking** change (see below). Resets `Y.Z` -> 0.0. | Full bundle + **mandatory upgrade notes** in `deploy/RELEASE-NOTES.md` (pre-steps, what operators lose, rollback). | Breaking; read the notes. |

## When is it MAJOR?

Bump the **MAJOR** for any of:

- **Breaking persisted state**: a `data\*.json` file format change that an
  existing install cannot load (the store under `data\` is this app's
  "schema" — it survives every upgrade, so it is the compatibility surface).
- **Breaking behaviour / config / API**: a removed or changed endpoint that
  saved links or TV displays depend on, a renamed CLI flag or env var, or a
  changed default that alters existing installs.
- **Embedded Python major/minor change** (the bundled runtime moves off
  CPython 3.12).
- **No clean rollback**: the upgrade cannot be reverted by `-Rollback`
  restoring the previous app + runtime.

A feature removal that operators must act on (the v2.13.0 email-engine revert
removed the 2.9.0–2.12.0 reporting stack) is at minimum a MINOR with loud
release notes; it was shipped as one deliberately, because the restored
behaviour was the previously documented one.

## The hard rule

> If a release changes what an **existing install keeps working with** —
> persisted `data\` formats, saved TV/share URLs, the service command line —
> it is not a patch. Bump the MINOR (or MAJOR) and say so in
> `deploy/RELEASE-NOTES.md` before the release is cut, not after.

## Release runbook

1. **Gates** — both must be green at the release commit:

   ```powershell
   ruff check .
   python -m unittest discover -s tests
   ```

   > **GitHub Actions is currently billing-blocked for this account**, so the
   > CI workflow (`.github/workflows/tests.yml`: ruff + the full unittest
   > suite on windows-latest / Python 3.14) does not run on push. Run the
   > same gates **locally** per the local-CI fallback policy and record the
   > result in the release notes ("gates run locally: both green"). A release
   > with unrun gates is not a release.

2. **Bump the version in BOTH files, in sync** — they must always agree:
   - `nwdash/config.py` → `APP_VERSION = "X.Y.Z"` (the build reads this; a
     mismatch ships a wrongly-named bundle)
   - `pyproject.toml` → `version = "X.Y.Z"`

3. **Release notes** — add a section to
   [`deploy/RELEASE-NOTES.md`](deploy/RELEASE-NOTES.md) for anything an
   operator or a user would notice. Not the commit list — what changed for
   them, any pre/post-upgrade step (hard refresh, re-create a config), and
   anything that will generate a support question. Newest first, headed
   `## vX.Y.Z — <summary>`.

4. **Build the bundle**:

   ```powershell
   pwsh -File deploy/build-bundle.ps1
   ```

   Output: `dist\nwdash-bundle-X.Y.Z-win-x64.zip`. The build fails loudly if
   `APP_VERSION` is unreadable or a repo file is missing from the ship
   allow-list — fix the allow-list in `build-bundle.ps1`, never work around it.

5. **Record the SHA-256** — every release body carries the bundle hash, so an
   operator on an air-gapped host can verify the copy they were handed:

   ```powershell
   Get-FileHash dist\nwdash-bundle-X.Y.Z-win-x64.zip -Algorithm SHA256
   ```

6. **Publish the release** with both assets — the bundle AND the bootstrap
   (operators need the pair side by side; `Setup-NWDash.cmd` finds the newest
   bundle next to itself):

   ```powershell
   gh release create vX.Y.Z `
     dist/nwdash-bundle-X.Y.Z-win-x64.zip `
     scripts/Setup-NWDash.cmd `
     --title "vX.Y.Z — <summary>" `
     --notes "<operator-facing notes, incl. the Bundle SHA-256 and the local-gates statement>"
   ```

7. **Verify the release page** — tag, both assets, SHA-256 in the notes.
   `Setup-NWDash.cmd` on hosts with `gh`/`GITHUB_TOKEN` will now auto-detect
   and download this version; air-gapped hosts take the zip by hand.

## Non-goals

- No auto-bumping — the releaser chooses the version; this page only says how
  to choose it.
- No patch-overlay tier. The whole bundle is a few tens of MB and the
  installer's `-Upgrade` already health-gates and auto-rolls-back; a separate
  overlay mechanism would add risk without saving meaningful time.
