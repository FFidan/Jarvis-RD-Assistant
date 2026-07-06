# Security Notes

Operational security guidance for JARVIS RD Assistant deployments.

---

## Threat Model

### Three Identities

JARVIS uses three distinct credential types with strictly bounded authority:

| Identity | Credential | Scope | Accesses user research data? |
|---|---|---|---|
| **Ops** | `JARVIS_API_KEY` (X-API-Key header) | Service-to-service calls (Telegram bot, cron jobs, health checks) | No — ops callers get no `user_id` from the session layer; user-data routes reject them with 401 |
| **User** | Magic-link session cookie (`jarvis_session`) | Own data only — the session layer injects `user_id` into every request; `current_user_id_strict` enforces it | Yes — own rows only |
| **Admin** | Session cookie with `role = admin` | Manages users via `/admin/users`, views the audit log at `/api/admin/audit-log`; **no access to other users' research data** | No cross-user data access — admin role controls ops, not data |

The `JARVIS_API_KEY` is an ops secret, not a user password. Anyone who holds it can call service endpoints but cannot read or write another user's papers, cards, or settings — the user-data layer requires a valid session identity.

The session layer includes a deliberate 24-hour `SESSION_GRACE` window (in `session_middleware.py`): a session expired by no more than 24 hours still resolves the user's identity (without renewing `expires_at`) so that reviews captured offline can reconcile after a realistic offline gap. This is an intentional offline-tolerance design choice, not a misconfiguration. `revoked_at` and `deleted_at` still hard-fail immediately regardless of the grace window — explicit revocation is never relaxed.

### In-Scope Attackers

- Unauthenticated network-level attacker (open dashboard port, no session or key).
- Authenticated user attempting to access another user's data (IDOR, horizontal escalation).
- Admin attempting to read or mutate another user's research data.
- Attacker with write access to the `pulse_models` table attempting RCE via pickle deserialization.

### Out of Scope (acknowledged residual risks)

- Attacker with host-level OS access (Docker socket, filesystem, or env).
- Attacker with direct PostgreSQL superuser access.
- Physical access to the host.
- Supply-chain compromise of upstream images or Python packages.

See `docs/known-residual-risks.md` for the full residual-risk register.

---

## Data Sharing Boundary

This section states the committed data-sharing model for a JARVIS instance.
The enforcement point is `assert_paper_ownership` / `assert_papers_ownership`
in `libs/jarvis_common/jarvis_common/db_helpers.py`.

### What is shared (corpus layer)

The following data is **shared across all authenticated users on an instance**:

- Paper metadata (title, authors, abstract, publication date, DOI, source).
- Full-text chunks and embeddings stored in Qdrant.
- Citation graph edges and knowledge-graph entities derived from those papers.
- Papers discovered by instance-level feeds: Pulse recommender, background
  scheduler, and Zotero group syncs.  These have ``papers.discovered_by IS NULL``
  (audit column only; no functional role).

A paper with ``discovered_by IS NULL`` is a system/instance paper and is
accessible to every authenticated user without requiring a ``user_library``
membership row.  This reflects instance configuration (feed settings, Pulse
model), not any individual user's behavior.

Citation metadata surfaced in RAG answers (e.g. the title/authors of a cited
paper) is drawn from this shared corpus layer by design — citations name public
scholarly works, so a cited paper's bibliographic detail may appear in any
user's answer even if that paper is not in their own ``user_library``. No
per-user activity (library membership, notes, ratings) crosses this boundary.

### What is strictly per-user (activity/output layer)

The following is **never cross-visible** — every query is scoped to
``user_id`` and no cross-user join is permitted:

- Library membership (``user_library`` rows).
- Paper read-state and ratings.
- Notes and annotations.
- Flashcards and card decks.
- Projects, tasks, and project–paper associations.
- Daily intent and Pulse preference signals.
- Structured extractions-of-record.
- Magic-link identity, session cookies, and user config values.

Every product row in the database carries a non-NULL `user_id` — there is no
NULL-owned product data. Single-tenant deployments are treated as multi-tenant
with one user; ownership is enforced by the same server-side scoping that
applies in the multi-user case.

The corpus is a shared resource; the intellectual work on top of it is private
(comparable to a shared scholarly library with per-user workspaces).
Regression coverage lives in
`libs/jarvis_common/tests/test_ownership_canonical_invariant.py`.

---

## Dev Flags and Production Refusal

JARVIS has five granular development flags. When `DEV_MODE=true`, all five are
promoted to `true` unless overridden individually (see `settings.py`
`_promote_dev_flags`). **None are permitted in `ENVIRONMENT=production`** — the
service refuses to start if any is `true` at boot (`validate_production_config`
in `auth.py`).

| Flag | Effect when `true` (the protection it removes) |
|---|---|
| `DEV_AUTH_BYPASS` | All authentication is bypassed when no `JARVIS_API_KEY` is configured (no key and no session required). |
| `DEV_ERROR_DETAIL` | Raw exception detail is included in API error responses (information leakage). |
| `DEV_CORS_OPEN` | `Access-Control-Allow-Origin` is opened to `*` instead of the `CORS_ORIGINS` allowlist. |
| `DEV_SMTP_LOG_ONLY` | Magic-link emails are written to stdout/logs instead of being delivered via SMTP. |
| `DEV_CRYPTO_RELAXED` | Fernet key validation and HMAC key entropy requirements are relaxed. |

`DEV_MODE=true` is a meta-flag: it promotes any of the five that were not
explicitly set in the environment. An explicit env var always wins.

---

## Secret Environment Variables

| Variable | Purpose | Required in production |
|---|---|---|
| `JARVIS_API_KEY` | Ops API key — gates all non-auth, non-health backend endpoints. Min 32 chars; enforced at startup. | Yes (startup refuses if absent or < 32 chars) |
| `JARVIS_CONFIG_KEY` | Fernet key for `user_config.encrypted_value` at-rest encryption. | Yes (startup refuses if absent) |
| `JARVIS_CONFIG_KEY_OLD` | Previous Fernet key — enables zero-downtime rotation via MultiFernet. Set during key rotation; remove after. | No (rotation only) |
| `JARVIS_MODEL_HMAC_KEY` | Dedicated HMAC-SHA256 key for Pulse classifier pickle signing (see below). Min 32 chars. | Yes (startup refuses if absent; derivation from `JARVIS_API_KEY` is refused in production) |
| `LITELLM_SALT_KEY` | Salt key LiteLLM uses to encrypt model credentials (cloud provider API keys) stored in its admin database. Auto-generated into `secrets/litellm_salt_key.txt` by `scripts/init-secrets.sh`; delivered as a Docker secret. **Never rotate** — LiteLLM decrypts stored credentials with this exact key, and without a dedicated salt LiteLLM falls back to the master key, so a master-key rotation would brick every encrypted row. | Yes (the litellm container refuses to start without it) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | SMTP relay credentials for magic-link delivery. Without these, single-user installs can still use API-key login. Multi-user magic-link email is not deliverable until SMTP is configured and tested. Explicit empty-string SMTP secret values are rejected at settings load; leave fields unset when SMTP is intentionally disabled. | Strongly recommended for multi-user mode |
| `SMTP_REPLY_TO` | Optional Reply-To address for sign-in emails. When set, email clients route replies here instead of the From address. Not a secret — this value appears in outgoing email headers. Configurable via the wizard or Settings → System → Email / SMTP. | No |
| `SMTP_FROM_NAME` | Optional sender display name shown in the From header (e.g. `JARVIS RD`). Not a secret — this value appears in outgoing email headers. Configurable via the wizard or Settings → System → Email / SMTP. | No |
| `OWNER_OVERRIDE_ALLOWED_CIDRS` | Comma-separated CIDR allowlist for the `X-Owner-User-Id` header (Telegram bot per-user orchestration). The compose stack sets this to loopback + the jarvis bridge subnet (tracks `JARVIS_NET_SUBNET`, default `10.137.241.0/24`) so the bot is trusted. The bare code default (`127.0.0.0/8`) is loopback-only (deny-by-default); non-loopback callers must opt in explicitly. | No (compose default is correct) |
| `ALLOW_PRIVATE_SMTP_HOST` | Default `false`. When `false`, the SMTP host is validated at config-save AND at magic-link send time and rejected if it resolves to a private/loopback/link-local/reserved address (SSRF guard). Set `true` ONLY if you run a legitimate **internal SMTP relay** on a private address/hostname — otherwise magic-link delivery to that relay is refused. | No (set only for an internal relay) |

### Cloud LLM Provider Settings at Rest

Cloud provider settings entered in Settings are deployment-wide admin settings.
Existing providers keep their backward-compatible flat keys (`llm.anthropic.api_key`,
`llm.openai.api_key`, `llm.google.api_key`); newer providers use namespaced keys
such as `llm.providers.openrouter.api_key`, `llm.providers.deepseek.api_key`,
`llm.providers.mistral.api_key`, `llm.providers.moonshot.api_key`,
`llm.providers.zai.api_key`, and `llm.providers.custom_openai_compatible.api_key`.
The custom OpenAI-compatible endpoint also stores
`llm.providers.custom_openai_compatible.base_url`.

Provider keys live in TWO encrypted stores when assigned to a role:

1. **`user_config.encrypted_value`** — Fernet-encrypted under
   `JARVIS_CONFIG_KEY`. This is the source of truth; it survives LiteLLM
   resets and is what the UI masks/reads back.
2. **LiteLLM's admin database** (`litellm` DB in the bundled Postgres) — when
   a cloud model is assigned to a role, the key is decrypted in memory and
   carried in the `POST /model/new` payload; LiteLLM persists it in
   `LiteLLM_ProxyModelTable` encrypted under `LITELLM_SALT_KEY`.

Keys are never written to `.env`, `litellm/config.yaml`, container
environment blocks (visible via `docker inspect`), or any other file.
Rotating `JARVIS_CONFIG_KEY` (supported, see below) does not affect store 2;
`LITELLM_SALT_KEY` itself must never rotate (see the table above).

Custom endpoint base URLs are validated before save. The validator rejects
credentials in the URL, fragments, invalid schemes, and unsafe IP ranges such
as metadata-service, link-local, multicast, and unspecified addresses. HTTPS is
allowed for normal remote endpoints; HTTP is limited to loopback development or
self-hosted gateway use. Administrators should only configure endpoints they
trust, because prompts and relevant source excerpts are sent there when a custom
model is assigned.

### X-Owner-User-Id Mechanism

The Telegram bot sends `X-Owner-User-Id: <user_id>` to route per-user API
calls. This header is only honored when all three guards pass:

1. A valid `JARVIS_API_KEY` is present on the request.
2. The source IP falls within `OWNER_OVERRIDE_ALLOWED_CIDRS`.
3. The supplied `user_id` exists in the `users` table and is not deleted.

Any guard failure returns 403. The mechanism is implemented in
`current_user_id_strict_with_owner_override` in `libs/jarvis_common/jarvis_common/auth.py`.

The bot resolves the `user_id` it injects from `telegram_user_pairings` — the
durable record written when a user runs `/pair <token>` in the bot. This is
the bot's sole identity mechanism; it does not rely on a fixed chat-id
environment variable. The `TELEGRAM_CHAT_ID` environment variable is an inert
tombstone (not read at runtime) and may be removed in a future cleanup. The
`telegram.owner_chat_id` config key is active: the scheduler reads it to
resolve the deployment owner's Telegram chat ID for timezone-based nudge
scheduling and job failure-alert delivery (see
`services/telegram_bot/telegram_bot/scheduler.py`).

The bot's product-data tenant isolation (projects, tasks, milestones, papers,
author alerts) is enforced entirely server-side by the service endpoints it
calls — the same routes the web app uses. The bot is a REST caller, not a
second writer implementing its own scoping.

### Config Key Rotation

To rotate `JARVIS_CONFIG_KEY` without downtime:

1. Set `JARVIS_CONFIG_KEY_OLD=<old key>` and `JARVIS_CONFIG_KEY=<new key>`.
2. Run `scripts/rotate_config_key.py --apply` (dry-run first without `--apply`).
3. Restart services.
4. Remove `JARVIS_CONFIG_KEY_OLD` from the environment.

---

## Proxy-Trust and Source-IP Allowlisting

Two auth surfaces use `request.client.host` to determine source IP for
allowlist checks. The reported IP is rewritten by `ProxyHeadersMiddleware`
when the request originates from a host in `trusted_proxy_hosts` (see
`configure_middleware_and_errors` in
`libs/jarvis_common/jarvis_common/app_factory.py`). If `trusted_proxy_hosts`
is broader than the actual reverse-proxy fleet, an attacker behind any
included host could spoof `X-Forwarded-For` to forge the source IP.

The IP-allowlist call sites are:

- `_ip_in_allowlist` (`libs/jarvis_common/jarvis_common/auth.py`) — backs
  `OWNER_OVERRIDE_ALLOWED_CIDRS` for the `X-Owner-User-Id` header bypass;
  mis-trusted XFF forges the operator's IP guard.
- `_infra_ip_in_allowlist`
  (`services/paper_ingestion/paper_ingestion/routers/infra_events.py`) —
  backs `INFRA_INGEST_ALLOWED_CIDRS` for the Vector sidecar ingest
  endpoint; mis-trusted XFF forges the sidecar's IP and reduces the
  defense-in-depth to the static shared key (`INFRA_INGEST_KEY`) alone.
  Note: `/infra-events` authenticates with a static shared key (constant-time
  `hmac.compare_digest`) plus the CIDR allowlist — it is NOT a challenge-response
  scheme and has no replay/nonce protection; the internal-network + default-deny
  CIDR posture is the boundary.

**Deployment requirement:** keep `trusted_proxy_hosts` scoped to the
actual reverse-proxy host(s) — in this stack that is the Caddy container's
bridge IP only. Do NOT set `trusted_proxy_hosts="*"` in any production
deployment. The setting is exposed via `TRUSTED_PROXY_HOSTS` in
`CoreSettings`; the default value is `dashboard` (the Caddy reverse-proxy
service), which is correct for the standard single-host stack. Override only
when deploying behind a different proxy fleet.

---

## Audit Log

Security-relevant events are written to the `audit_log` table by
`libs/jarvis_common/jarvis_common/audit.py`. The audit log is best-effort
(failures are logged but do not abort the request) and caps metadata payloads at
4 KB to prevent log inflation.

Events currently audited include: invalid API key attempts, missing session on
user-data routes, magic-link request and verify, user creation and deletion,
admin actions.

The audit log is readable by admins at `GET /api/admin/audit-log`
(cursor-paginated, admin session required).

### Append-only invariant and GDPR erasure

`audit_log` is append-only: the `no_update_audit_log` and `no_delete_audit_log`
RULEs rewrite any ordinary `UPDATE`/`DELETE` into a no-op, so the operational
record cannot be tampered with after the fact. Because `audit_log.user_id` is a
free-text column (not a foreign key), hard-deleting a user does not cascade to
it. To honour GDPR erasure, the daily purge (`jobs/data_purge.py`) anonymizes a
hard-deleted user's audit rows — nulling `user_id` and stripping PII metadata
keys (e.g. `ip`) while keeping the non-identifying operational record (action,
resource, timestamp). This is the one sanctioned mutation: it brackets the
`UPDATE` with `ALTER TABLE … DISABLE/ENABLE RULE no_update_audit_log` in a single
transaction (RULEs are query-rewrite and role-independent, so `SECURITY DEFINER`
cannot bypass them); the rule is re-enabled before commit, so ordinary writes
remain no-ops.

---

## Vulnerability Disclosure

Reporting a vulnerability is documented in the repository's top-level
[`SECURITY.md`](../SECURITY.md) policy (private GitHub Security Advisories;
acknowledgement within 5 business days). Do not open public GitHub issues for
security reports.

---

## Pulse Model Signing

The Pulse classifier persists a serialized scikit-learn `LogisticRegression`
model into the `pulse_models` table as an HMAC-signed pickle blob. Verification
happens in `services/paper_ingestion/paper_ingestion/pulse/training.py::_verify_and_unpickle`
before `pickle.loads` is called — without the HMAC gate, anyone with DB write
access could forge a blob and trigger RCE.

### Configuration

The HMAC key is resolved at call time, in this order:

1. **`JARVIS_MODEL_HMAC_KEY`** (preferred) — a dedicated secret used solely for
   signing model blobs. Generate with `openssl rand -hex 32`. Keeping this
   separate from `JARVIS_API_KEY` means a compromise of the HTTP bearer does
   not also let an attacker forge model blobs, and vice versa.
2. **Derived from `JARVIS_API_KEY`** — when `JARVIS_MODEL_HMAC_KEY` is unset,
   the signing key is `sha256(b"model-signing:" + JARVIS_API_KEY)`. The
   `model-signing:` prefix domain-separates this key from any direct use of
   the bearer.

If neither is set, `_hmac_key()` raises `RuntimeError`. In production
(`ENVIRONMENT=production`), `validate_production_config()` — called at lifespan
startup — refuses to start unless at least one of the two paths above is
configured.

### Key Rotation

There is no in-place rotation framework. To rotate the HMAC key:

1. Update `JARVIS_MODEL_HMAC_KEY` and restart the affected services.
2. Existing `pulse_models` rows will fail HMAC verification and the scoring path
   falls back to zeros until a new model is trained.
3. The nightly `pulse.train_classifier` cron job re-trains automatically. To
   force an immediate re-train, enqueue `pulse.train_classifier` via the jobs API.

---

## Ollama Security Posture

The Ollama daemon handles local LLM inference. Key constraints:

- **Image pin:** keep `OLLAMA_IMAGE` in `versions.env` at the tested pin
  (`ollama/ollama:0.23.1`) or a newer validated pin. Downgrading below the
  patched line reintroduces known vulnerabilities (see
  [known-residual-risks.md](known-residual-risks.md) for the current CVE
  posture entry).
- **Host binding:** the default Compose configuration binds the Ollama host
  port to loopback only (`127.0.0.1`), preventing browser and LAN clients from
  calling the daemon directly.
- **Docker network boundary:** every container on the `jarvis` Docker network
  can reach `http://ollama:11434`. Do not attach untrusted sidecars to that
  network.
- **Shared daemon override:** if the operator configures an external shared
  Ollama daemon instead of the bundled one, that daemon must be patched to an
  equivalent or newer pin and bound to loopback or an equivalently trusted
  private network.

Review this posture whenever `OLLAMA_IMAGE` is updated or the Compose network
topology changes.

---

## One-click Restore — Security Posture

The admin Backups panel (v1.0.0+) allows an admin to restore the instance to a previous backup point from the browser.

**Privilege boundary:** the app container gains no new operating-system privilege. On restore request, it writes a small JSON sentinel file to the `backup_trigger` volume (a minimal RW mount shared only with the backup sidecar). All destructive operations — DROP/CREATE DATABASE, the gunzip-and-psql dump reload, Qdrant snapshot recovery — run exclusively inside the `postgres-backup` sidecar, which already runs with the elevated database credentials it needs for scheduled backups. The `/backups` volume remains read-only to the app container; the app cannot read or overwrite backup archives.

**Access controls:**

- Restore can only be triggered by an **active admin browser session**. The ops API key (`JARVIS_API_KEY`) cannot trigger a restore.
- A **typed confirmation** (the word RESTORE) is required before the sentinel is written.
- A **safety pre-backup** is taken before any data is modified. If the restore fails after the destructive step, the safety backup appears in the admin panel and can be used to recover.
- A **NEWER-version block** prevents restoring a backup that was made by a newer app version than is currently running — the operator must update the app first.

**Residual risk:** an attacker who compromises an admin browser session can trigger a restore, causing data loss (current state is overwritten with the backup). This is equivalent to the existing risk of any admin action that modifies instance state. The safety pre-backup and typed confirmation reduce accidental-trigger risk; they do not prevent a deliberate action by a compromised admin account. For the full residual-risk register, see [docs/known-residual-risks.md](known-residual-risks.md).

### Off-host (cross-host) restore — Security Posture

Off-host disaster recovery (recovering on a fresh host from off-site backups) is **operator-driven via the runbook** in [DEPLOYMENT.md](DEPLOYMENT.md), not a WebUI action — a total-host-loss recovery may have no working UI. It adds **no new application surface or privilege**: the app gains nothing; the same already-privileged `postgres-backup` sidecar runs the same `restore.sh`, triggered by an operator-written request. It is the only restore path that touches secrets, so it is hardened specifically:

- **The backup encryption key is held out-of-band and is one-time.** The operator drops it into the writable `restore_inbox` volume as `operator_key` for a single restore. `restore.sh` **shreds** it (`shred -u`, falling back to `rm`) on every clean or recorded-failure exit, so a failed restore never leaves the key on the volume. A `SIGKILL` of the sidecar cannot run the exit trap; the next run shreds any leftover before re-staging.
- **No write is ever attempted against the read-only `/secrets` mount.** On a fresh host the script cannot and does not write `/secrets` (it stays `:ro`). The **rw `restore_inbox`** volume is the only writable cross-host secrets-staging surface, and it is never mounted under `/secrets`.
- **Plaintext secrets are shredded after use.** The script decrypts the secrets archive only into a `chmod 700` staging dir under the inbox, reads the role password to rebind the database role, and then **shreds the staged files (`shred` per file) and removes the staging on exit** (a bare `rm` would leave block-level residue on a journaling/overlay filesystem). A `SIGKILL` mid-restore can leave staged plaintext until the next run shreds it. Materializing the host `./secrets` for the app is a separate, explicit operator step.
- **A wrong key fails safe.** The operator key is validated (present + non-empty) before any destructive step, and the pre-`DROP` decrypt probe proves it actually decrypts the database archives before anything is dropped — so a wrong or corrupt key destroys nothing.
- **Role-password rebind is a parameterized-safe SQL literal.** The restored password is embedded in `ALTER ROLE … WITH PASSWORD` with single quotes doubled, so a password containing a quote cannot break the statement or inject SQL.
- **Same-host rollback is unchanged and never touches secrets.** The one-click (`local`) path leaves `./secrets` read-only and the live config key in place; all of the above applies only to the `inbox` path.
