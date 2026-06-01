<!-- verified-against-UI: 2026-05-18 | routes: /admin/users, /admin/audit-log, /admin/system-health, /logs -->

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

New users are added exclusively via the admin invite flow (`/admin/users` → Invite modal) or by the first-run wizard. There is no public self-registration. This keeps the user list under admin control.

---

## Related pages

- [Settings](settings.md) — per-section RBAC breakdown; admin-only sections are §II Sources, §III Models, §IV System, and §V Bot Token.
- [Getting Started](getting-started.md) — first-run bootstrap and admin account creation.
