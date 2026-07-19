<!-- verified-against-UI: 2026-07-13 | routes: /admin/backups -->

# Backup & Restore

JARVIS RD Assistant backs itself up **automatically** — a scheduled run every day, plus on-demand backups whenever you want one — and every archive is **encrypted** by default. Admins manage everything from a single page: **Admin → Backups** (`/admin/backups`). From that one page you can review and download backup points, adjust how long they are kept, restore the instance to an earlier point with one click, and even recover a brand-new server from an off-site copy — all in the browser.

This page is for **admins**. Regular users never see the Backups panel; during a restore they simply see a brief "restore in progress" message.

---

## The one key you must keep somewhere else

Backups are encrypted with a **backup encryption key** that is generated during installation and stored in the server's `secrets/` folder. For obvious reasons, that key is **excluded from its own backup archive** — the archive it unlocks does not contain it.

> **Keep a copy of the backup encryption key off-site, separate from your backup archives.** A password manager, a sealed envelope, a secret in a separate cloud account — anywhere that is *not* the server itself. If the server is ever lost, your off-site archives can only be decrypted with that key. **Losing the key makes every encrypted backup permanently unrecoverable.** No support process can get the data back.

Where to find the key file on the host is covered in the [Deployment Guide](../DEPLOYMENT.md#backup-restore); if someone else operates your server, ask them to confirm an off-site copy exists.

---

## What a backup contains

Each **restore point** is a consistent snapshot of everything the instance needs, captured together:

| Component | What it holds |
|-----------|---------------|
| **Main database** | Papers, users, notes, projects, settings, jobs |
| **AI model router database** | Model routing configuration and API keys |
| **Secrets** | The platform's credential files — required to decrypt and run a restored instance |
| **Search index (Qdrant)** | Vector search snapshots (best-effort — if this part is missing, a restore rebuilds the index automatically) |

Because archives contain platform secrets, treat downloaded backup files with the same care as passwords — store them somewhere private.

---

## The Backups panel

<!-- screenshot: /admin/backups — status line, Run backup now button, and a list of restore point cards with Complete and Encrypted badges -->

At the top of the page a status line shows when the last backup ran and how many restore points exist. If the most recent backup attempt failed, a warning appears here instead — check the backup service if you see it.

### Run a backup now

Click **Run backup now** and confirm. The backup runs in the background — a "Backup running…" banner appears, and the new restore point shows up in the list when it finishes (this can take a few minutes). On-demand backups are in addition to the automatic daily run.

### Review and download restore points

Each restore point appears as a card, newest first, showing:

- **When** it was taken and its total size.
- A **Complete / Incomplete** badge — an incomplete point is missing a required archive and cannot be restored.
- An **Encrypted** badge — the default. A "Not encrypted" badge means the backup key was not configured when the point was taken.
- Badges for each component it covers.
- How long it will be kept under the current retention policy.

Click **Details** on a card to expand a per-file table with a **Download** button for each archive. Download a full set periodically and keep it **off-site** (together with — but separate from — your off-site key copy): off-site archives plus the off-site key are what make total-server-loss recovery possible.

### Delete a restore point

Click **Delete** on a card. A confirmation dialog explains that this permanently deletes every archive in that restore point and cannot be undone; type **DELETE** in the confirmation field to proceed.

### Retention policy

The **Retention policy** section controls how long backups are kept. Two independent caps are available:

| Setting | Meaning |
|---------|---------|
| **Keep most recent** | Keep at most this many restore points |
| **Maximum age** | Remove restore points older than this many days |

Leave a field blank to use the default. Older or excess restore points are pruned automatically by the backup service — you never need to clean up by hand. Click **Save retention policy** to apply changes.

---

## One-click restore

You can roll the whole instance back to any listed restore point without leaving the browser.

<!-- screenshot: typed-RESTORE confirmation dialog over the Backups panel -->

1. **Pick a restore point** and click **Restore to this point**.
2. **Confirm.** A dialog explains that this replaces the current databases, search index, and provider keys with the contents of that backup. Type **RESTORE** to proceed.
3. **Watch the guided progress view.** The panel shows each step live and keeps updating even through the brief window when the app itself is unreachable mid-restore. Other users see a maintenance message until it completes.

### What happens behind the scenes

- **A safety backup is taken first.** Before anything is touched, the current state is captured as a new restore point — so even a restore you regret is recoverable.
- **The restore point is checked for tampering.** Backup points taken by this version or newer carry a signature that only your backup encryption key can produce. Whenever a restore point has one, it is re-checked before the restore starts — every time, on this server and on a fresh one alike. If it does not match, the restore point has been altered on disk, and the restore is refused outright with nothing touched: a signature that fails to verify is never overridable. A point carrying **no** signature is a separate case with its own rules — see *If your only surviving backup is an older, unsigned one* below. Deployments set up without a backup encryption key have nothing to sign with, so their restore points are neither signed nor checked.
- **The restore is staged, not in-place.** Your chosen backup is loaded into a *separate staging database* and only **swapped in atomically** once it verifies. If anything fails before the swap, the original database was never touched and is served again automatically — the restore **self-heals** rather than leaving you with a half-restored instance. The previous database is dropped only after the swap succeeds.
- **Older backups are fine.** A backup taken by an older version of the app is accepted and **migrated forward automatically** after the swap — no manual steps.
- **Newer backups are refused.** If a backup was made by a newer app version than is currently running, its Restore button is disabled with a note to update first (run `./update.sh`, then retry).
- **The search index recovers best-effort.** If the search-index step fails, the rest of the restore still completes — your papers and data are intact, and the index rebuilds itself from the restored database (a few minutes on a large library, no data loss).
- **Everyone is signed out.** The restore replaces the session store along with the rest of the data, so all users — including you — sign in again afterwards. This is expected.

A clean restore lifts the maintenance window by itself. If a restore fails, the guided view says exactly what happened and what to do next — including the safety backup taken beforehand, which appears in the panel and can be restored like any other point.

---

## Recovering a fresh server (disaster recovery, in the browser)

If the original server is gone entirely, you can bring a **brand-new host** back to life from your off-site copies — end to end in the browser, with **no terminal steps after installation**.

**You need:** your off-site **archive set** for one backup point (all the files from that restore point's Details table) and your off-site copy of the **backup encryption key**. A wrong key fails safe — it is checked against the archives before anything destructive happens, so a typo cannot destroy the fresh install.

> Recovering a fresh server requires a backup point taken by this version or newer, because it is verified by signature and older points carry none. Download a fresh off-site archive set after updating so your disaster-recovery copy is a verifiable one. Restore points on the original server are unaffected — older ones keep restoring normally there.

1. **Install JARVIS on the new host** with `./setup.sh` and complete the first-admin sign-in, exactly as for a fresh install ([Getting Started](getting-started.md)).
2. **Generate a one-time upload grant** from the Backups panel. The grant is shown once and is valid for **30 minutes** — it authorizes the browser upload and nothing else.
3. **Upload the archive set and the key** in the browser. Uploads go to a dedicated, locked-down upload service — the key and the archive contents never pass through the app itself, and the one-time key is destroyed automatically once the restore finishes.
4. **Trigger the restore.** The uploaded set appears in the **Restore from another JARVIS** section with **Complete**, **Secrets**, and **Key ready** badges. If a badge shows a problem (say, a missing archive or key), an inline hint says what to add. When everything is ready, click **Restore to this point** and type **RESTORE** to confirm.
5. **Watch the guided view.** The restore runs the same staged, self-healing process as a one-click restore, then the stack **reconciles itself** — restored credentials are put in place, the database account is re-bound, and the app services restart on their own. When it completes, sign in with your restored account. Done.

<!-- screenshot: Restore from another JARVIS section showing a staged backup set with Complete, Secrets, and Key ready badges -->

For a headless server with no browser access, a command-line fallback exists — see [Deployment Guide → Off-host recovery](../DEPLOYMENT.md#off-host-total-host-loss-recovery).

---

## If your only surviving backup is an older, unsigned one

Backup points taken before signature support carry no signature. On the original server they keep restoring normally — with a warning that they cannot be checked — right up until this version takes its **first** backup there. From that first signed backup onwards the server requires a signature on every restore, so those older unsigned points stop restoring too unless you take the deliberate override below. The switch is the arrival of signing on that server, not the age of the point you pick, so take a fresh backup after updating and keep it as your working restore point. Recovering a **fresh server** from an unsigned off-site set is refused outright, because on a new host there is nothing else to check the archives against.

For the genuine disaster where an unsigned set is all that is left, an operator with terminal access on the server can accept it deliberately. It cannot be done from the browser and cannot be done by setting a flag alone: the restore must be run interactively, with `JARVIS_RESTORE_ALLOW_LEGACY=1` set **and** the phrase `I-ACCEPT-UNVERIFIED-BACKUP` typed at the prompt. The restore then logs a permanent warning that its archives were never verified.

This override applies **only** when a signature is absent. It never applies to a restore point whose signature fails to verify — that is evidence of tampering, not of loss, and is always refused.

---

## Related pages

- [Admin Pages](admin.md) — the other admin surfaces: user management, audit log, system health, logs.
- [Getting Started](getting-started.md) — installation and first-admin sign-in (step 1 of disaster recovery).
- [Deployment Guide](../DEPLOYMENT.md#backup-restore) — operator-level detail: backup schedule and encryption settings, the manual host-level restore runbook, and the command-line disaster-recovery fallback.
