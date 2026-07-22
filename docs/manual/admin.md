<!-- verified-against-UI: 2026-06-26 | routes: /admin/users, /admin/audit-log, /admin/system-health, /admin/backups, /logs -->

# Admin and multi-user operation

JARVIS supports two account roles: **Admin** and **User**. Only administrators
can open user management, audit, health, backup, and log pages.

Open these pages on the JARVIS host at `http://localhost:3001` or through the
named HTTPS address configured during setup. A numeric LAN address exposes only
`/health/jarvis` and returns HTTP 403 for admin and sign-in pages.

---

## Role-based access overview

Admin pages are hidden from a user's sidebar. Opening an admin address without
the Admin role returns the person to Home.

For Settings RBAC, see the [Settings](settings.md) page which documents per-section access levels.

---

## Admin pages

### User Management — `/admin/users`

The page lists registered users on the instance.

<!-- screenshot: /admin/users — table showing user rows with email, role dropdown, and action buttons -->

**Per-user actions:**

| Action | Description |
|--------|-------------|
| **Role dropdown** | Set a user's role to **User** or **Admin**. Takes effect immediately. |
| **Send sign-in link** | Create a fresh 15-minute sign-in link. JARVIS emails it when delivery works; otherwise the dialog shows the link so the administrator can share it privately. |
| **Remove** | Soft-delete the user account. This button is **disabled for the currently signed-in admin** (you cannot delete yourself). |
| **Passkeys** | Shows how many passkeys the user has registered, with a **Revoke all** action that immediately invalidates every passkey on the account (for example after a lost or compromised device). The user can still sign in via magic link and re-register. |

**Invite modal:**

Click **Invite user**, enter an email address, and select the initial role. JARVIS
creates the account and a 24-hour invitation. When email delivery works, it
sends the link. When email is absent or fails, the dialog stays open and shows
the link for the administrator to copy.

Share a displayed link through a private channel. Anyone holding an unused link
can sign in until it expires. The recipient should open it at the same named
HTTPS address the family will keep using, then add a passkey under **Settings →
Account → Passkeys**.

A **soft-delete confirmation** dialog appears before any remove action. Removed
users cannot sign in; the administrator can restore an account during its
retention window from this page.

### Instance ownership and recovery

One live administrator is the **instance owner**. The operations API key can
recover only this account; it is not a shared administrator password. The User
Management table marks the account with an **instance owner** badge and prevents
it from being demoted or removed.

Migration `0105` automatically assigns ownership when an upgraded instance has
exactly one live administrator. If it has two or more live administrators, the
migration cannot choose safely. On the JARVIS host, inspect and repair the
state explicitly:

```bash
jarvis-research owner status
jarvis-research owner set <admin-email>
```

When ownership is stored in the database, the current owner can transfer it in
User Management to another live administrator. The target must already be an
administrator, and the owner must type that administrator's email exactly.
When `OWNER_USER_ID` manages ownership on the host, change that value and
restart JARVIS instead; the web page does not override host policy.

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

Each checklist item explains what it verifies and why it matters.

The same page contains **Disk usage** for the stores JARVIS can measure and
**model runtime diagnostics** for detected hardware, the backend serving recent
requests, and the recommended model. Use **Re-detect** after changing hardware
or a GPU overlay. These application views cannot measure Docker's host-wide
builder cache; see the deployment requirements before running any host cleanup
command.

---

### Backups — `/admin/backups`

The Backups page lists restore points, starts an on-demand backup, downloads an
off-site copy, sets retention, and starts a guided restore. It also accepts an
off-site restore set when a server must be rebuilt.

Keep the backup encryption key somewhere other than the JARVIS host. The key is
deliberately excluded from the encrypted archive it unlocks. The complete
backup, restore, and fresh-host procedure lives in one place: [Backup and
restore](backup-and-restore.md).

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

## Multi-user data model

JARVIS RD Assistant is designed for small teams where multiple researchers share a single self-hosted instance.

**Per-user isolation:**

- Each user has their own **library**: papers they save, reading states, notes, tags, and priorities are private to their account.
- Learning cards, projects, research topics, and Pulse decks are per-user.
- Users do not see each other's private data.

**Paper visibility:**

- Verified papers discovered by the server's arXiv, Semantic Scholar, OpenAlex,
  and PubMed adapters are public to authenticated users on the instance.
- Local uploads, unverified client batches, and personal or group Zotero
  imports remain private unless the signed-in user has added them to their own
  library.
- The [Research Feed](research-feed.md) Library surface distinguishes **My
  library** from **Public + mine**. The latter means verified public papers plus
  the signed-in user's private library, never another user's private imports.

The canonical matrix and vector-search boundary are documented in
[Source-aware paper visibility](../SECURITY.md#source-aware-paper-visibility).

**Roles:**

- **User** — full access to their own data and all research surfaces.
- **Admin** — same as User, plus access to the admin pages described above, admin-gated Settings sections, and the ability to manage other users.

**Invitations and recovery links:**

New users are added through the admin invite flow or by the onboarding wizard,
which creates the first administrator. There is no public self-registration.
SMTP only changes how a link is delivered; it is not required to create the
account or the link. Without SMTP, the invite or **Send sign-in link** dialog
returns a manual one-time link for the administrator to share privately.

---

## Related pages

- [Settings](settings.md) — per-section RBAC breakdown; admin-only sections are §II Sources, §III Models, §IV System, and §V Bot Token.
- [First sign-in and setup](getting-started.md) — first administrator and family onboarding.
