# Security Notes

Operational security guidance for JARVIS RD Assistant deployments. This is a
living document; new sections are added per audit closeout.

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

This section states the committed data-sharing model for a JARVIS instance
(decision D4, 2026-05-15).  The enforcement point is
`assert_paper_ownership` / `assert_papers_ownership` in
`libs/jarvis_common/jarvis_common/db_helpers.py`.

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

### Design rationale

This is the same posture as a shared scholarly library with private
workspaces (comparable to Zotero group libraries or institutional repositories):
the corpus is a shared resource; the intellectual work on top of it is private.

The prior ``multitenant_enabled`` boolean on the ownership helpers was removed
in this decision to make the corpus-sharing explicit and untoggleable.
Regression coverage lives in
``libs/jarvis_common/tests/test_ownership_canonical_invariant.py``.

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
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | SMTP relay credentials for magic-link delivery. Without these, links fall back to container stdout. | Strongly recommended |
| `OWNER_OVERRIDE_ALLOWED_CIDRS` | Comma-separated CIDR allowlist for `X-Owner-User-Id` header (Telegram bot per-user orchestration). Defaults to loopback + Docker bridge (`127.0.0.0/8,172.16.0.0/12`). | No (default is safe) |

### X-Owner-User-Id Mechanism

The Telegram bot sends `X-Owner-User-Id: <user_id>` to route per-user API
calls. This header is only honored when all three guards pass:

1. A valid `JARVIS_API_KEY` is present on the request.
2. The source IP falls within `OWNER_OVERRIDE_ALLOWED_CIDRS`.
3. The supplied `user_id` exists in the `users` table and is not deleted.

Any guard failure returns 403. The mechanism is implemented in
`current_user_id_with_owner_override` in `libs/jarvis_common/jarvis_common/auth.py`.

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
  defense-in-depth to the HMAC challenge alone.

**Deployment requirement:** keep `trusted_proxy_hosts` scoped to the
actual reverse-proxy host(s) — in this stack that is the Caddy container's
bridge IP only. Do NOT set `trusted_proxy_hosts="*"` in any production
deployment. The setting is exposed via `TRUSTED_PROXY_HOSTS` in
`CoreSettings`; the historical `learning_engine` default of `"*"` is
acceptable for single-host loopback but NOT for any deployment exposed
beyond loopback.

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

---

## Leaked-secret remediation (one-time, before public)

*(2026-05-17; corrected 2026-05-18)*

**OBS-1 status: RESOLVED (verified 2026-05-25, during the 2026-05 pre-release hardening).**
`git log origin/master --all --full-history --diff-filter=ACMRT -- secrets/langfuse_init_pk.txt secrets/langfuse_init_sk.txt` returns ZERO commits — the
two secret-files are not present on any reachable ref of `origin/master`
(commit `4587e9ab` at verification time). Prefix grep
(`pk-lf-35d525` / `sk-lf-031360`) on `origin/master` returns 8 file hits,
ALL in intentional CI-guard / documentation / audit references (this
runbook, `scripts/check-burned-secrets.sh:15-16`,
`scripts/gen-langfuse-keys.sh:29-30`, and 5 dated audit artifacts).
No further `git filter-repo` or force-push is required for the
public-launch transition.

`secrets/langfuse_init_pk.txt` and `secrets/langfuse_init_sk.txt` were
accidentally git-tracked in early development despite the `secrets/*.txt`
`.gitignore` rule.

**Done:** they have been untracked (`git rm --cached`) and the introducing
commits purged from published GitHub history (operator-run history filter).
The files are no longer tracked and no longer reachable from any remote ref.

**Pending until next provision:** the leaked key *values* on disk are NOT yet
rotated.  `scripts/gen-langfuse-keys.sh` is now **burned-aware**: it detects a
file whose content still starts with a known-leaked prefix (`pk-lf-35d525` /
`sk-lf-031360`) and treats it as absent, regenerating a fresh keypair and
rewriting the matching `.env` lines in place.  Rotation therefore happens
automatically the next time the keypair is provisioned via `make up`,
`make observability-up`, or `./setup.sh` — no manual or destructive step is
required.  Until then the leaked values remain live; provision once to retire
them.  CI enforces this via `scripts/check-burned-secrets.sh`, which fails the
build if a burned value is ever present in the working tree.

The commits that introduced them still appear in git history.  Because the
repository is currently **private** and the keys are **rotated dead**, the
history rewrite below is **optional** — it is only required before making the
repository public or sharing its history.

**When to run:** only with explicit operator confirmation, only when the
repository is still private, and only once (a force-push rewrites all
published history).

```bash
# Rewrite history to remove the two files from every commit
git filter-repo \
  --invert-paths \
  --path secrets/langfuse_init_pk.txt \
  --path secrets/langfuse_init_sk.txt

# Force-push the rewritten history to origin
git push --force origin master
```

**Consequences of the force-push:**
- Every existing clone and CI ref becomes stale and must `git clone` fresh.
- GitHub Actions caches that reference old SHAs will miss and rebuild.
- Any open pull requests targeting `master` must be rebased.

This step is **NOT** performed automatically by any Makefile target or CI job.
It requires deliberate operator action and is gate-deferred to the finishing
step of this workstream.

---

## Vulnerability Disclosure

If you discover a security vulnerability in JARVIS RD Assistant, please report
it responsibly:

**Contact:** Use [GitHub Security Advisories](https://github.com/FFidan/Jarvis-RD-Assistant/security/advisories/new) (private vulnerability reporting).

**Process:**
1. Open a private security advisory on GitHub with a description of the
   vulnerability, steps to reproduce, and any proof-of-concept.
2. Allow up to **90 days** for a fix to be developed and released before public
   disclosure (coordinated disclosure).
3. Acknowledgement will be sent within 5 business days of receipt.

Please do not open public GitHub issues for security vulnerabilities.

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

If neither is set, `_hmac_key()` raises `RuntimeError`. The previous
public-literal fallback (`"jarvis-dev-unsafe-hmac-key"`) was removed by audit
H14 (2026-05-14).

In production (`ENVIRONMENT=production`), `validate_production_config()` —
called at lifespan startup — refuses to start unless at least one of the two
paths above is configured.

### Key Rotation

There is no in-place rotation framework. To rotate:

1. Update `JARVIS_MODEL_HMAC_KEY` (or `JARVIS_API_KEY` if you are relying on
   derivation).
2. Restart the affected services.
3. Existing rows in `pulse_models` will fail HMAC verification on load. The
   service handles this gracefully: `load_active_classifier` returns
   `(None, {"available": False, "degradation_reason": "active model could
   not be loaded"})`, and the scoring path falls back to zeros until a fresh
   model is trained.
4. The nightly `pulse.train_classifier` job (cron `30 3 * * *`) re-trains and
   persists a new model signed with the new key. No manual migration is
   required.

If you need an immediate re-train rather than waiting for the cron tick,
enqueue `pulse.train_classifier` via the jobs API (one job per user with
ratings).
