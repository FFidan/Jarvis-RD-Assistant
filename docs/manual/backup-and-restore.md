<!-- verified-against-UI: 2026-07-20 | routes: /admin/backups -->

# Backup and restore

The default installation takes a restore point each day and can take one on
demand. Setup creates an encryption key, so those archives are encrypted unless
the operator deliberately changes the backup configuration. Administrators use
**Admin → Backups** (`/admin/backups`) to review, download, retain, and restore
them.

This page is for **admins**. Regular users do not see the Backups panel; during
a restore they see a brief "restore in progress" message.

---

## Keep the encryption key off the server

Setup creates a local backup encryption key. The key is excluded from the
archive it unlocks.

Keep a copy off-site and separate from the archive set: a password manager,
sealed physical copy, or secret store on another account. If the host and the
only key copy are lost together, encrypted backups cannot be recovered.

If someone else operates the server, ask them to confirm that an off-site copy
exists without sending the key through an untrusted channel.

---

## What a backup contains

Each **restore point** is a consistent snapshot of everything the instance needs, captured together:

| Component | What it holds |
|-----------|---------------|
| **Main database** | Papers, users, notes, projects, settings, jobs |
| **AI model router database** | Model routing configuration and API keys |
| **PDF archive** | The numeric PDF files referenced by the main database; required in every current restore point |
| **Data keys** | Exactly three keys coupled to encrypted database content; host service credentials are not moved between servers |
| **Search index (Qdrant)** | Vector search snapshots (best-effort; a missing snapshot leaves search degraded until you repair it) |

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

Click **Details** on a card to expand a per-file table with a **Download** button for each archive. Download a full set periodically, including the `manifest_<timestamp>.json.hmac` sidecar when present, and keep it **off-site** (together with — but separate from — your off-site key copy): off-site archives plus the off-site key are what make total-server-loss recovery possible.

### Delete a restore point

Click **Delete** on a card. A confirmation dialog explains that this permanently deletes every archive in that restore point and cannot be undone; type **DELETE** in the confirmation field to proceed.

### Retention policy

The **Retention policy** section controls how long backups are kept. Two independent caps are available:

| Setting | Meaning |
|---------|---------|
| **Keep most recent** | Keep at most this many restore points |
| **Maximum age** | Remove restore points older than this many days |

Leave a field blank to use the default. Older or excess restore points are pruned automatically by the backup service — you never need to clean up by hand. Click **Save retention policy** to apply changes.

Automatic pruning has two floors you cannot switch off. Your last restore point is never removed, however old it is; and a single automatic run never removes more than half of your restore points, because a server clock that jumps forward makes every archive look old at once. When that happens the run removes only the oldest half and says so, and normal runs converge on the window you asked for over the following days. Deleting a restore point yourself from the Backups panel is not subject to either floor — that is what the typed **DELETE** confirmation is for — and each delete records how many restore points are left.

If you set the `BACKUP_RETENTION_DAYS` environment variable, `0` means *no age limit*; earlier releases read `0` as "remove anything older than about a day". The **Keep most recent** cap still applies either way.

---

## One-click restore

You can roll the whole instance back to any listed restore point without leaving the browser.

<!-- screenshot: typed-RESTORE confirmation dialog over the Backups panel -->

1. **Pick a restore point** and click **Restore to this point**.
2. **Confirm.** A dialog explains that this replaces the current databases, search index, and provider keys with the contents of that backup. Type **RESTORE** to proceed.
3. **Watch the guided progress view.** The panel shows each step live and keeps updating even through the brief window when the app itself is unreachable mid-restore. Other users see a maintenance message until it completes.

### What happens behind the scenes

- **A safety backup is taken first.** Before anything is touched, the current state is captured as a new restore point — so even a restore you regret is recoverable.
- **The restore point is checked before use.** Current backup points carry a
  signed manifest that names the exact archives for one run. Restore checks the
  signature, timestamp, file names, sizes, and checksums before decrypting or
  replacing data. A duplicate, extra, missing, mismatched, or invalidly signed
  archive is refused. An older point with no signature follows the separate
  legacy procedure below; an invalid signature can never be overridden.
- **The restore is staged, not in-place.** Your chosen backup is loaded into a *separate staging database* and only **swapped in atomically** once it verifies. If anything fails before the swap, the original database was never touched and is served again automatically — the restore **self-heals** rather than leaving you with a half-restored instance. The previous database is dropped only after the swap succeeds.
- **Older signed backups migrate forward.** Unsigned points follow the legacy
  procedure below.
- **Pre-v1.2 backups may not contain PDFs.** A correctly authenticated legacy
  point is offered only with an explicit data-loss warning. Accepting it clears
  the server's current PDF files before the restored database goes live; paper
  records can remain, but their PDFs will not open until they are obtained and
  processed again. A current backup missing its PDF archive is incomplete and
  cannot be restored.
- **Newer backups are refused.** If a backup was made by a newer app version,
  update with `jarvis-research update`, then retry.
- **The search index recovers best-effort.** Every restore rotates the vector
  visibility generation. Readiness stays amber and search can temporarily
  under-fetch while the reconciliation job validates and retags recovered
  vectors. If the snapshot step was skipped or failed, use **Home → Prepare
  library → Process whole library** for each affected library; healthy PDFs do
  not need to be extracted again.
- **Everyone is signed out.** Restored sessions, magic links, WebAuthn
  challenges, and Telegram pairing codes are removed before the database goes
  live. Durable accounts, roles, and passkeys remain, but every user — including
  the initiating administrator — must sign in again.

A clean restore lifts the maintenance window by itself. If a restore fails, the guided view says exactly what happened and what to do next — including the safety backup taken beforehand, which appears in the panel and can be restored like any other point.

---

## Identity and credential boundaries

A restore preserves durable accounts, roles, and passkeys while revoking
transient sessions and one-time authentication challenges, so everyone is
signed out. Existing passkeys continue to work only when the replacement server
keeps the same hostname and RP ID; see [Passkeys](passkeys.md#after-a-restore-or-hostname-change).

The encrypted archive installs exactly these three data-coupled keys:
`jarvis_config_key.txt`, `jarvis_model_hmac_key.txt`, and
`litellm_salt_key.txt`. They are needed to read database-backed encrypted
settings and model-router credentials.

The target host keeps its own PostgreSQL role password, backup key, operations
API key, Qdrant key, SMTP host secret, Telegram host secret, and access-edge
credentials. Database-backed SMTP, Telegram, cloud-provider, Zotero, and source
settings are restored with the main database. After an off-host restore, those
outbound database settings remain quarantined until the operator reviews and
acknowledges them.

---

## Application recovery is separate

A data restore replaces databases, PDFs, the vector snapshot, and the three
data keys; it does not install application images or change the checked-out
JARVIS release. On a replacement server, first install a compatible release. If
the restore reports that its backup came from newer code, recover or update the
application with `jarvis-research update`, then retry the data restore. Do not
use a database restore as an image rollback mechanism.

---

## Recovering a fresh server in the browser

Portable fresh-host restore starts with a complete, signed archive set created
by JARVIS v1.2.0 or later and the matching backup encryption key. The set
includes `manifest_<timestamp>.json` and
`manifest_<timestamp>.json.hmac`. JARVIS checks the key and archive set before
it replaces data.

1. Install JARVIS on the new host with `./setup.sh` and complete first-admin
   setup.
2. In **Admin → Backups**, generate a one-time upload grant. It expires after 30
   minutes.
3. Upload the archive set and encryption key. The dedicated browser upload
   service writes only to the restore inbox; it has no database, application
   secret, or Docker-socket access. The backup sidecar validates and consumes
   that inbox.
4. Under **Restore from another JARVIS**, confirm that **Complete**,
   **Secrets**, and **Key ready** are shown. Select **Restore to this point** and
   type **RESTORE**.
5. Follow the progress view. A successful restore installs the recovered data
   keys, rebinds the database account, restarts affected services, and signs
   everyone out. Sign in with the recovered account.
6. Review every restored outbound integration. In the original progress tab,
   type `I HAVE REVIEWED RESTORED CREDENTIALS` to release quarantine. If that
   restore session is no longer available, sign in as the configured owner or
   run `jarvis-research restore acknowledge <restore-id>` on the host. Until
   acknowledgement, SMTP, Telegram, source, Zotero, and cloud-provider egress
   remains blocked.

<!-- screenshot: Restore from another JARVIS section showing a staged backup set with Complete, Secrets, and Key ready badges -->

An earlier or unsigned off-site set is not accepted as portable fresh-host
recovery. After updating an old installation, create and download a new complete
signed restore point for disaster recovery. Older unsigned points retain only
the deliberate same-host path below.

---

## Headless fresh-host recovery

Use this fallback only when the new server cannot be reached with a browser.
Replace `<timestamp>` with the restore point timestamp in `YYYYMMDD_HHMMSS`
format.

1. Start the fresh stack with `./setup.sh`.
2. Print the procedure for the restore point you want:

   ```bash
   jarvis-research restore request <timestamp>
   ```

   The command generates the restore identifier and request timestamp the
   backup service requires, fills in the real paths, and prints the three steps
   below. It submits nothing and moves nothing.

3. Follow the printed steps **in order**: copy the archive set into the restore
   inbox, then the matching encryption key under its required one-time name,
   and only then submit the printed request. The backup service acts on a
   submitted request within seconds, so submitting it first fails the restore
   against an empty inbox.

   If the set is in the configured S3 bucket, pull only that timestamp instead
   of copying the archives by hand:

   ```bash
   docker compose exec -e BACKUP_PULL_TS=<timestamp> \
     -e BACKUP_PULL_DEST=/restore-inbox \
     postgres-backup /usr/local/bin/backup.sh
   ```

4. Follow the restore:

   ```bash
   jarvis-research restore status
   ```

   It reports the state, the current step, any error, whether manual follow-up
   is required, and the safety backup to restore if it is. The WebUI Backup
   panel shows the same status, and `docker compose logs -f postgres-backup`
   shows the raw progress. The one-time key and decrypted secrets staging are
   removed when the restore exits.

5. Review the restored outbound settings and run
   `jarvis-research restore acknowledge <restore-id>` with the restore
   identifier step 2 printed. The command requires the exact ID and a second
   typed confirmation before it releases quarantine.

A failure before the database swap lifts maintenance because live data was not
replaced. A later failure keeps the service at HTTP 503. Do not remove the
`.destructive` marker merely to make the app reachable. Read the status and fix
the reported cause; for an inbox restore, copy the one-time key again and repeat
the restore request above. A successful retry clears the maintenance markers.
Clear them manually only after the status instructions are complete and the
databases and services have been checked.

Remove the copied archive set from the restore inbox after recovery is confirmed.

---

## If your only surviving backup is an older, unsigned one

Backup points taken before signature support carry no signature. On the original server they keep restoring normally — with a warning that they cannot be checked — right up until this version takes its **first** backup there. From that first signed backup onwards the server requires a signature on every restore, so those older unsigned points stop restoring too unless you take the deliberate override below. The switch is the arrival of signing on that server, not the age of the point you pick, so take a fresh backup after updating and keep it as your working restore point. Recovering a **fresh server** from an unsigned off-site set is refused outright, because on a new host there is nothing else to check the archives against.

For the genuine disaster where an unsigned set is all that is left, an operator with terminal access on the server can accept it deliberately:

```bash
jarvis-research restore legacy <timestamp>
```

A restore replays into a running database, so start the stack first if it is down (`jarvis-research start`); the command says so rather than failing part way. The command explains that the set cannot be verified, stops the backup service so it cannot pick the request up first, and runs the restore interactively so the prompt reaches you. You must type the phrase `I-ACCEPT-UNVERIFIED-BACKUP` before anything is changed; nothing else — no flag, no browser button — can take the override. The restore then logs a permanent warning that its archives were never verified, and the backup service resumes afterwards either way. Follow the outcome with `jarvis-research restore status`.

This is same-host recovery only. An off-site set is refused outright regardless of the override, because on a fresh host there is nothing to check the archives against.

This override applies **only** when a signature is absent. It never applies to a restore point whose signature fails to verify — that is evidence of tampering, not of loss, and is always refused.

---

## Related pages

- [Admin Pages](admin.md) — the other admin surfaces: user management, audit log, system health, logs.
- [Getting Started](getting-started.md) — installation and first-admin sign-in (step 1 of disaster recovery).
- [Deployment Guide](../DEPLOYMENT.md#backup-restore) — access modes, host setup, and troubleshooting.
