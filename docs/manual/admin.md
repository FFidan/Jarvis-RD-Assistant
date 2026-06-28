<!-- verified-against-UI: 2026-06-26 | routes: /admin/users, /admin/audit-log, /admin/system-health, /admin/backups, /logs -->

# Admin & Multi-tenant

JARVIS RD Assistant supports multiple user accounts with two roles: **Admin** and **User**. Admin-gated surfaces are only accessible to accounts with the Admin role; attempting to access them without that role redirects immediately to the Home page.

---

## Role-based access overview

The sidebar navigation includes a **Group V** section that is hidden entirely for non-admin users. All routes in this section are wrapped in an **AdminOnlyRoute** guard that performs a hard redirect to `/` for any session that lacks the Admin role.

For Settings RBAC, see the [Settings](settings.md) page which documents per-section access levels.

---

## Admin pages

### User Management — `/admin/users`

The **AdminUsersPage** provides a table of all registered users on the instance.

<!-- screenshot: /admin/users — table showing user rows with email, role dropdown, and action buttons -->

**Per-user actions:**

| Action | Description |
|--------|-------------|
| **Role dropdown** | Set a user's role to **User** or **Admin**. Takes effect immediately. |
| **Send sign-in link** | Send a magic-link email directly to the user's registered address. Useful for account recovery if the user cannot receive email through the normal flow. |
| **Remove** | Soft-delete the user account. This button is **disabled for the currently signed-in admin** (you cannot delete yourself). |

**Invite modal:**

Click **Invite user** to open the invite modal. Enter an email address and select the initial role (User or Admin), then click **Send invite**. The invited user receives a magic-link email; clicking the link creates their account and signs them in directly.

A **soft-delete confirmation** dialog appears before any remove action to prevent accidental deletion. Soft-deleted users cannot sign in but their data is retained and can be restored by contacting a database administrator.

---

### Audit Log — `/admin/audit-log`

The audit log is a chronological record of security-relevant events on the instance: sign-ins, sign-in failures, role changes, user invites, user removals, admin-configuration changes, and similar actions.

**Action-prefix filter:** A filter input lets you narrow the log by action prefix (e.g. type `user.` to see only user-management events, or `auth.` to see authentication events).

**Cursor-based pagination:** The log loads **50 events per page**. Click **Load more** at the bottom of the list to fetch the next 50 events. Newer events appear at the top.

---

### System Health — `/admin/system-health`

A live operational dashboard showing the health of all backend services.

**Services table:** A table of services (Postgres, Qdrant, Ollama, LiteLLM, Langfuse if configured) with their current status. The table **auto-refreshes every 30 seconds**.

**Readiness checklist:** A checklist of deployment prerequisites (database migrations applied, required environment variables set, source API keys present, etc.). Each item shows a status indicator and a **remediation note** describing how to resolve it if it is failing.

**InfoTooltips:** Each checklist item has an info tooltip explaining what the check verifies and why it matters.

---

### Backups — `/admin/backups`

The **Admin Backups panel** surfaces the disaster-recovery archives produced by the `postgres-backup` sidecar, which runs by default (a scheduled run plus on-demand triggers).

**Archive table:** Each archive is listed newest-first with its store (main database, model-router database, secrets, or Qdrant vectors), size, age, and an **Encrypted / Plaintext** badge. A per-row **Download** streams the archive to your browser for off-site storage. Archives are encrypted at rest whenever a backup key is configured (the default).

**Run backup now:** Requests an immediate on-demand backup (a confirm step guards it); the sidecar runs one right away in addition to its scheduled runs.

**One-click restore:**

Admins can restore the instance to any listed backup point without leaving the browser.

*How it works:*

1. **Choose a restore point.** The archive table shows each backup set with its timestamp, stores covered, and size. Click **Restore** on the set you want to roll back to.
2. **Confirm.** A dialog explains that this will overwrite all current data. Type **RESTORE** in the confirmation field to proceed — there is no undo once you confirm.
3. **Watch progress.** The panel shows live status. The app enters a **maintenance window** while the restore runs: other users see a "restore in progress" message and cannot use the app. You do not need to stay on the Backups page. While the restore runs the whole app is in the maintenance window, so a page reload shows a brief "restore in progress" message rather than the live progress until it finishes. After the restore completes, you may need to sign in again, because the restore replaces the session store along with the rest of the data.

*What happens automatically:*

- **Safety pre-backup.** Before touching any live data, the sidecar captures a snapshot of the current state. If something goes wrong after the destructive step, the safety backup appears in the panel and can be used to recover.
- **NEWER-version block.** If the chosen backup was made by a newer version of the app than is currently running, the restore is refused. Update the app first (`./update.sh`), then retry.
- **Maintenance gate.** User-facing requests are blocked during the restore and resume automatically when it is complete.
- **Qdrant recovery.** The search index is restored best-effort. If the Qdrant step fails, the rest of the restore still completes — your papers and data are intact. The search index rebuilds itself by re-embedding from the restored database; this may take a few minutes on a large library but involves no data loss.

**Manual restore (advanced):**

If the one-click path is unavailable — for example, after a catastrophic host failure where the app itself cannot start — use the host-level procedure documented in the [Deployment Guide](../DEPLOYMENT.md#backup--restore). The steps: decrypt `.enc` archives with `openssl`, restore the `secrets/` archive first, restore the `jarvis` and `litellm` databases (drop/create, then `gunzip | psql`), and recover the Qdrant snapshots.

---

### System Logs — `/logs`

The logs page provides a real-time view of application log output, organised into tabs:

| Tab | Content |
|-----|---------|
| **Live** | Streaming tail of the most recent application log lines across all services |
| **Jobs** | Background job history: job type, status, start/end time, and any error messages |
| **Sources** | Source-fetch logs: per-source ingestion runs, paper counts, and errors |
| **Events** | Application-level event log (distinct from the security audit log): webhooks, scheduled-job triggers, and system events |

---

## Multi-tenant model

JARVIS RD Assistant is designed for small teams where multiple researchers share a single self-hosted instance.

**Per-user isolation:**

- Each user has their own **library**: papers they save, reading states, notes, tags, and priorities are private to their account.
- Learning cards, projects, research topics, and Pulse decks are per-user.
- Users do not see each other's private data.

**Shared corpus:**

- The underlying paper corpus (PDF text, chunks, embeddings) is shared across the instance. If two users save the same paper, the PDF and its processed data are stored once.
- The [Research Feed](research-feed.md) Library surface has a scope toggle: **My library** (private state) and **All discovered** (all papers any user has ingested).

**Roles:**

- **User** — full access to their own data and all research surfaces.
- **Admin** — same as User, plus access to the admin pages described above, admin-gated Settings sections, and the ability to manage other users.

**Magic-link invites:**

New users are added exclusively via the admin invite flow (`/admin/users` → Invite modal) or by the onboarding wizard (which creates the initial admin account). There is no public self-registration. This keeps the user list under admin control.

---

## Related pages

- [Settings](settings.md) — per-section RBAC breakdown; admin-only sections are §II Sources, §III Models, §IV System, and §V Bot Token.
- [Getting Started](getting-started.md) — onboarding wizard and admin account creation.
