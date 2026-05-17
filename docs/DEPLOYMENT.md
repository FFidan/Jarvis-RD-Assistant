# Deployment Guide

JARVIS is a self-hosted, multi-tenant research assistant. This document is the single source of truth for how to run it — from the localhost happy path to LAN exposure, Cloudflare Tunnel, TLS, backups, and common failure modes.

Most content here is also summarised in the [README](../README.md); this document expands on edge cases and operator-grade details. If the two conflict, this file is canonical.

---

## Solo deployment (recommended for single-user)

The minimal path to a healthy stack on your own PC:

    bash scripts/init-secrets.sh   # idempotent; generates JARVIS_API_KEY,
                                   # JARVIS_CONFIG_KEY, LITELLM_MASTER_KEY if absent
    docker compose up -d           # waits for postgres → init-migrations →
                                   # paper_ingestion + learning_engine + telegram_bot

Required `.env` vars (init-secrets generates if blank):
- `JARVIS_API_KEY`          — 32-byte hex; gates the REST API + dashboard login
- `JARVIS_CONFIG_KEY`       — Fernet key; encrypts user_config secrets at rest
- `LITELLM_MASTER_KEY`      — 32-byte hex; gates LiteLLM admin endpoints
- `JARVIS_MODEL_HMAC_KEY`   — 64-hex; HMAC-signs Pulse classifier pickle blobs; **auto-generated** by `init-secrets.sh` — leave blank in `.env`, do not hand-edit (updated 2026-05-17, agent: claude-code)

Optional:
- `JARVIS_CONFIG_KEY_OLD`   — enables zero-downtime crypto rotation via MultiFernet

Health checks:

    curl http://127.0.0.1:8010/health   # paper_ingestion
    curl http://127.0.0.1:8011/health   # learning_engine

---

## Fresh install on a second machine

Clone and bootstrap exactly like the primary machine:

    git clone <your-remote>/JARVIS_RD_Assistant.git
    cd JARVIS_RD_Assistant
    cp .env.example .env
    bash scripts/init-secrets.sh         # generates JARVIS_API_KEY, POSTGRES_PASSWORD, etc.
    $EDITOR .env                         # optional: set TELEGRAM_BOT_TOKEN for Telegram
    docker compose up -d

**Telegram is optional.** For dashboard/Pulse/RAG testing you can leave
`TELEGRAM_BOT_TOKEN` blank and run without the Telegram profile. If you do
enable Telegram, two machines must NEVER share a bot token. Telegram routes
long-poll updates to whichever client polled last; sharing causes random message
loss. Create a separate bot via @BotFather for the second machine.

After the stack is healthy, open the dashboard. The setup wizard runs
automatically on first visit (`setup.completed = false` in DB). Complete the
wizard to configure your timezone, Pulse schedule, and Telegram pairing.

### Source HTTP cache

JARVIS caches successful `GET` responses from external metadata sources
(Semantic Scholar, arXiv, OpenAlex, Crossref, PubMed) in memory to reduce
duplicate outbound requests and 429 rate-limit failures during discovery runs.
The cache is on by default; only `GET`+`200` responses for those hosts are
stored — errors are never cached.

| Env var | Default | Description |
|---|---|---|
| `SOURCE_HTTP_CACHE_ENABLED` | `true` | Set `false` to disable the in-memory source GET cache entirely. |
| `SOURCE_HTTP_CACHE_TTL_SECONDS` | `900` | How long (seconds) a cached response is considered fresh. Raise this to reduce API calls on high-frequency Pulse runs; lower it to pick up faster source updates. |

These vars are read by `paper_ingestion` at startup. A service restart is
required when changing them. No action is needed for the defaults.

### Pulse source caveats

Manual Pulse regeneration can temporarily exhaust public-source rate limits.
arXiv asks API clients to keep requests to one connection at a time and no more
than one request every three seconds; repeated manual runs can therefore return
HTTP 429 even when the code is working correctly. A zero-card run with
`degraded_reason` and source diagnostics means the job completed but every
enabled source was empty, rate-limited, unsupported for polling, or missing
required source settings.

The arXiv source uses minute-precision `submittedDate` bounds and serializes
requests inside each `paper_ingestion` process. If arXiv reports 429 or times
out, wait at least the `Retry-After` window or 30 seconds before another manual
regeneration; repeated button clicks only extend the public API cooldown. Use
Settings -> Pulse -> Diagnostics or `GET /api/pulse/debug` to confirm whether
arXiv returned candidates, no matches, a rate limit, or a transport error.
The `/api/pulse/debug` endpoint is dev-mode only (returns 404 unless
`DEV_MODE=true`); use the user-facing `source_diagnostics` payload on
`GET /api/pulse/today` for production troubleshooting.

OpenAlex requires `OPENALEX_EMAIL` or `OPENALEX_API_KEY` for Pulse polling in
this stack. PubMed can run without an API key, but an NCBI key raises the rate
limit and is recommended for frequent testing.

---

## Observability (optional — off by default)

LLM-call tracing via Langfuse is **opt-in** and disabled in every default
deployment: the `observability` compose profile is not started and the
`OBSERVABILITY_ENABLED` boot-gate defaults to `false`, so the Langfuse SDK is
never constructed and there is zero latency, network, or log overhead. Nothing
to do if you don't want it.

To enable it (provisions a loopback-only, operator-only Langfuse instance
headlessly — no signup, no key copy-paste):

```bash
# set LANGFUSE_INIT_USER_PASSWORD in .env first (operator dashboard login)
make observability-up
```

Then open `http://localhost:3002` and sign in with `LANGFUSE_INIT_USER_EMAIL`
(default `operator@jarvis.local`) / `LANGFUSE_INIT_USER_PASSWORD`. Langfuse is
a single deployment-wide *operator* tool, decoupled from JARVIS user accounts
(no SSO/iframe bridge, loopback-bound, signup disabled). Rotating the keypair
requires wiping the `langfuse_postgres_data` volume (write-once provisioning).
Full contract, trust boundary, and rotation procedure:
[docs/contracts/04-observability.md](contracts/04-observability.md) §9.

---

## Remote access via Tailscale

Reach the webapp + Telegram bot from outside your LAN with zero inbound
ports opened. Encrypted at the WireGuard layer, no DNS / cert provisioning.

    # 1. Install Tailscale on the PC running JARVIS:
    curl -fsSL https://tailscale.com/install.sh | sh
    sudo tailscale up

    # 2. Install Tailscale on phone / laptop / etc.

    # 3. Reach the webapp on tailnet:
    #    https://<jarvis-pc-tailnet-name>:3001
    #    or without exposing port 3001 in the URL:
    sudo tailscale serve --https=443 https+insecure://localhost:3001

Telegram remote works without any setup change — the bot polls Telegram
outbound (no inbound exposure required).

Self-signed HTTPS still works inside the tailnet; browser warnings are
tolerable for solo use. For real DNS+cert, `tailscale serve` covers it
without leaving Tailscale's network.

### Browser-trusted HTTPS from a phone (Safari / iOS)

Safari requires a trusted certificate. Tailscale can terminate TLS with a
Let's Encrypt cert it issues for your tailnet, then proxy to the dashboard
backend without validating its self-signed cert:

    # Run once on the host PC — persists across reboots:
    sudo tailscale serve --https=443 https+insecure://localhost:3001 --bg

Access the dashboard at `https://<host-tailnet-name>` from your phone. No
certificate warning, no port 3001 in the URL.

### Friends showcase mode over Tailscale

JARVIS can be shown to trusted friends by adding their devices to your tailnet
and using the Tailscale URL above. This is shared single-user access, not
multi-user mode.

Everyone who can open the dashboard shares the same library, settings, Pulse
feedback, Telegram pairing state, and `JARVIS_API_KEY`. Use this only for a
trusted showcase where shared state is expected.

JARVIS is multi-tenant by default. Every user's data is isolated at the query
layer — each API call that touches user-owned rows passes through
`current_user_id_strict`, and the first-run wizard creates the initial admin
automatically. Admins invite additional users from **Settings → Admin → Users**
(or `/admin/users` directly). The `JARVIS_API_KEY` is an ops credential for
service-to-service calls and is not a user login — users authenticate via
magic-link sessions.

---

## Deployment Modes

| Mode | Setup time | Inbound ports | TLS story | Rate-limit trust | Prerequisites |
|---|---|---|---|---|---|
| **Localhost** | ~5 min | none | Self-signed (localhost SAN) | All traffic is local; no proxy headers trusted | Docker Engine 24+, Docker Compose v2 |
| **LAN** | ~10 min | `3001/tcp` (dashboard) on LAN iface | Self-signed with LAN IP SAN | Rightmost-trusted-hop XFF walk | Same + a stable LAN IP |
| **Cloudflare Tunnel** | ~30 min | *none* (outbound-only) | Self-signed origin + Cloudflare edge TLS | `CF-Connecting-IP` when `JARVIS_TRUST_CF_CONNECTING_IP=true` | Cloudflare account + Zero-Trust access policy |
| **Let's Encrypt / Caddy** | ~15 min after DNS | `80/tcp`, `443/tcp` | Caddy ACME edge TLS + dashboard internal HTTPS | Rightmost-trusted-hop XFF walk | Public DNS A/AAAA record to this host |
| **Tailscale Funnel** | ~20 min | *none* on your host | Tailscale-provisioned TLS | Right-to-left XFF walk (SEC-001) | Tailscale account, approved Funnel feature |
| **VPN (Tailscale/WireGuard)** | ~15 min | VPN ports only | Self-signed | All traffic is intra-tunnel | WG/TS on both ends |

**Default.** `./setup.sh` runs mode 1 (localhost). Mode 2 (LAN) and mode 3 (Cloudflare Tunnel) are prompts in the same script. VPN and Tailscale Funnel are not auto-wired — they work but you set them up yourself.

---

## Mode 1 — Localhost

```bash
cp .env.example .env   # setup.sh does this for you
./setup.sh             # pick option 1 at the access-mode prompt
```

`setup.sh` generates `POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY`, `N8N_JWT_SECRET`, and `JARVIS_API_KEY`, then brings the stack up. The direct dashboard URL is `http://localhost:3001` when `.env` uses the default `DASHBOARD_HOST_PORT=3001`; enable the `caddy-local` profile only when you explicitly want local HTTPS.

### Self-signed cert — browser acceptance

JARVIS generates a self-signed certificate inside the `dashboard` container on first start. Browsers will warn you; accept the risk once per browser:

- **Chrome / Edge** — on the warning page, type `thisisunsafe` (no prompt, no input field — just type it with the page focused).
- **Firefox** — *Advanced → Accept the Risk and Continue*.
- **Safari** — *Show Details → visit this website → Visit Website*. macOS will prompt for your admin password.

If you ever pin HSTS for `localhost:3001` accidentally, use the browser's "Clear HSTS" developer tool before trying again.

---

## Mode 2 — LAN

Reaches the dashboard from other devices on your local network.

```bash
./setup.sh   # pick option 2
```

What `setup.sh` does:
- Sets `DASHBOARD_BIND_HOST=0.0.0.0` in `.env` so the dashboard port binds on all interfaces.
- Detects your LAN IP (`hostname -I` or `ipconfig getifaddr en0` on macOS).
- Adds the LAN IP to `CORS_ORIGINS` and `JARVIS_CERT_SAN`.
- Removes any stale `docker-compose.override.yml` that could cause duplicate port bindings.

### When your LAN IP changes

DHCP can re-lease your host a different IP after a reboot or WiFi reconnect. Symptoms: dashboard works on `localhost` but browsers on other devices get a cert error or CORS failure.

Fix:

```bash
# Option A: re-run setup.sh, which re-detects the LAN IP and prompts
# you to regenerate the cert (setup.sh:278-292).
./setup.sh

# Option B: manually override JARVIS_CERT_SAN and regenerate.
# Edit .env:
#   JARVIS_CERT_SAN=DNS:localhost,IP:127.0.0.1,IP:<new-LAN-IP>
#   CORS_ORIGINS=https://localhost:3001,https://<new-LAN-IP>:3001
docker compose down -v dashboard   # wipes the cert volume so it regenerates
docker compose up -d dashboard
```

The actual cert generator is `frontend/scripts/generate-certs.sh`; it reads `JARVIS_CERT_SAN` at container startup. There is intentionally no standalone `scripts/generate_cert.sh` — regeneration is driven by the volume wipe above.

### Hardening checklist before LAN exposure

Set every one of these in `.env` (`setup.sh` enforces the first two for you when you pick option 2; verify the rest):

```
ENVIRONMENT=production
DEV_MODE=false
JARVIS_API_KEY=<at least 32 random chars from: openssl rand -hex 32>
JARVIS_CONFIG_KEY=<Fernet key from: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
```

- `DEV_MODE=true` does **not** bypass auth when a `JARVIS_API_KEY` is configured; the flag only helps unauthenticated local development.
- `DEV_MODE=true` is a meta-flag: it promotes any granular dev flag (`DEV_AUTH_BYPASS`, `DEV_ERROR_DETAIL`, `DEV_CORS_OPEN`, `DEV_SMTP_LOG_ONLY`, `DEV_CRYPTO_RELAXED`) to `true` unless that flag is explicitly set in the environment. An explicit value always wins. In production, set each flag independently; none are permitted in `ENVIRONMENT=production` (startup will crash if any is `true`).
- `n8n` is not protected by the JARVIS API key — if you expose the n8n port on LAN, set `N8N_BASIC_AUTH_USER`/`N8N_BASIC_AUTH_PASSWORD` or keep it on `127.0.0.1`. See finding S-7.4 in `docs/archive/2026-05/CODE_SECURITY_REVIEW_2026-04-14.md`.

### Rate-limit client-IP trust (automatic, updated 2026-05-17, agent: claude-code)

JARVIS pins the internal Docker network to a static subnet (`10.137.241.0/24` by default) and assigns Caddy a fixed IP within it (`10.137.241.2` for the public Caddy TLS terminator, `10.137.241.3` for `caddy_local`). nginx is configured to trust **only** those two /32 addresses plus `127.0.0.1` as `set_real_ip_from` sources — so client IP extraction is automatic and requires no operator step.

There is **no `NGINX_TRUSTED_PROXY_CIDR` environment variable** — it was removed. Trust is determined solely by the static Docker IPs, not by any configurable CIDR list.

**Rare override — subnet collision:** If `10.137.241.0/24` conflicts with an existing host LAN segment, `setup.sh --check` will warn about the collision. To resolve it, set `JARVIS_NET_SUBNET=<a free /24>` in `.env` (leave unset normally — the default is appropriate for most setups) and update **two hard-coded literals** before recreating the Docker network:

1. **`docker-compose.yml`** — change `gateway: 10.137.241.1` to a gateway IP inside the new subnet (e.g. `10.200.0.1`). If you skip this step `docker compose up` hard-fails because the gateway is outside the declared subnet.
2. **`frontend/nginx.conf`** — update `set_real_ip_from 10.137.241.2/32` and `set_real_ip_from 10.137.241.3/32` to the new static IPs assigned to `caddy` and `caddy_local` in the new subnet. If you skip this step all clients collapse to one rate-limit bucket (RB-4 self-DoS regression).

`setup.sh --check` emits a non-fatal warning when `JARVIS_NET_SUBNET` is set to a non-default value as a reminder to update both files.

```bash
# Edit .env:
#   JARVIS_NET_SUBNET=10.200.0.0/24   # pick a /24 that doesn't conflict
# Also update docker-compose.yml gateway: and frontend/nginx.conf set_real_ip_from literals.
docker compose down && docker compose up -d
```

**Note on `TRUSTED_PROXY_CIDRS`:** this is a separate Python app-layer variable that controls the rightmost-trusted-hop XFF walk for the rate limiter — it is not an nginx concern. Add your load-balancer or upstream proxy CIDRs to it only if you put an additional external proxy in front of the stack. RFC-1918 ranges and `127.0.0.0/8` are always trusted by the Python layer.

### Configuration principle: environment variables are for security boot-gates only

Environment variables in JARVIS are reserved for two purposes: (1) security-critical flags that must bind before the auth layer starts (the `dev_*` flags, `JARVIS_API_KEY`, `JARVIS_CONFIG_KEY`, `CORS_ORIGINS`, HTTPS/TLS settings), and (2) bootstrap secrets needed before the database exists (database password, encryption keys, LLM master key). All other configuration — model assignments, Pulse schedules, Telegram pairing, notification preferences — is managed via the web app and persisted in the database. This keeps the `dev_*` flags as explicit, high-visibility operator decisions rather than defaults buried in `.env` that can be forgotten before sharing an instance.

### Encrypted config key rotation

Provider keys stored through Settings use `user_config.encrypted_value` and are decrypted with `JARVIS_CONFIG_KEY`. On startup, paper_ingestion and learning_engine validate encrypted rows before schedulers or workers start. In non-dev mode, services fail fast when encrypted rows exist and the key is missing, malformed, or wrong.

Dry-run a key rotation first:

```bash
DATABASE_URL=postgresql://jarvis:<password>@localhost:5432/jarvis \
OLD_JARVIS_CONFIG_KEY=<old-fernet-key> \
NEW_JARVIS_CONFIG_KEY=<new-fernet-key> \
python scripts/rotate_config_key.py
```

Apply only after the dry run validates every encrypted row:

```bash
DATABASE_URL=postgresql://jarvis:<password>@localhost:5432/jarvis \
OLD_JARVIS_CONFIG_KEY=<old-fernet-key> \
NEW_JARVIS_CONFIG_KEY=<new-fernet-key> \
python scripts/rotate_config_key.py --apply
```

The script runs in a transaction and prints counts only; it never logs plaintext or ciphertext.

### Docker Secrets

JARVIS supports Docker Secrets for the five most sensitive credentials. Each secret is read from a file at runtime via a `_FILE`-suffixed environment variable, keeping plaintext values out of the compose environment and shell history.

| Secret name | `_FILE` env var | Purpose |
|---|---|---|
| `jarvis_postgres_password` | `POSTGRES_PASSWORD_FILE` | PostgreSQL password for the `jarvis` user |
| `jarvis_litellm_api_key` | `LITELLM_API_KEY_FILE` | LiteLLM master key (gateway auth) |
| `jarvis_api_key` | `JARVIS_API_KEY_FILE` | JARVIS REST API key (frontend + Telegram) |
| `jarvis_qdrant_api_key` | `QDRANT_API_KEY_FILE` | Qdrant service API key |
| `jarvis_telegram_bot_token` | `TELEGRAM_BOT_TOKEN_FILE` | Telegram bot token (telegram profile only) |
| `jarvis_model_hmac_key` | `JARVIS_MODEL_HMAC_KEY_FILE` | Pulse classifier pickle HMAC signing key (auto-generated; mandatory in production) |

Secrets are stored as files in a `secrets/` directory at the repo root (gitignored). On first run, `setup.sh` creates them:

```bash
# setup.sh creates secrets/ and populates files automatically.
# To rotate a secret manually:
echo -n '<new-value>' > secrets/jarvis_api_key
docker compose up -d paper_ingestion learning_engine   # triggers re-read on next start
```

> **Note:** `secrets/` is the canonical secret store — every secret used by the stack lives here as `secrets/<name>.txt` (mode 600, gitignored). `setup.sh` writes all core secrets on first run, including `secrets/litellm_master_key.txt`. Never commit secret files. Use `chmod 600 secrets/*` to restrict read access on multi-user hosts.

### Web UI configuration — zero manual `.env` editing required (updated 2026-05-17, agent: claude-code)

A normal install needs **no manual `.env` editing** beyond what `setup.sh` writes automatically. All ongoing configuration is handled through the web wizard and Settings:

| Setting | Where to configure | Restart required? |
|---|---|---|
| SMTP relay | Settings → Integrations → SMTP / first-run wizard | No — takes effect immediately |
| Cloud LLM keys (OpenAI, Anthropic, Gemini) | Settings → Models → Cloud Providers | No — re-pushed live on save; restart only on push failure |
| Telegram bot token | Settings → Integrations → Bot Token | Yes — an administrator must restart the `telegram_bot` container: `docker compose restart telegram_bot` |
| Access mode (single ↔ multi-user) | Settings → System → Access Mode | Yes — requires an app restart after the setting is saved: `docker compose restart paper_ingestion learning_engine` |
| Auto-fetch interval | Settings → Automation | No — takes effect immediately (live scheduler reschedule) |

**What still lives in `.env`:** security boot-gates (`JARVIS_API_KEY`, `JARVIS_CONFIG_KEY`, `ENVIRONMENT`, TLS/CORS), bootstrap secrets, and infrastructure parameters (ports, bind address, SMTP on multi-user installs without a wizard-configured relay). See the [Configuration principle](#configuration-principle-environment-variables-are-for-security-boot-gates-only) section.

---

> **Alternatives:** Cloudflare Tunnel and DIY port-forward+ACME are possible but out of scope here; setup is on the operator if pursued.

## Mode 4 — Tailscale Funnel (alternative tunnel)

Pattern is analogous to Cloudflare Tunnel: outbound-only WebSocket, edge TLS terminated by Tailscale. Not wired into `setup.sh`. See the [Tailscale Funnel docs](https://tailscale.com/kb/1223/funnel/). The same SEC-001 right-to-left XFF note applies — no special client-IP header to trust; the default XFF handling is correct.

## VPN (Tailscale / WireGuard)

Mesh your devices onto one network and keep JARVIS in localhost mode. No TLS hardening needed beyond the defaults. Simplest, most secure option if you only need remote access from a small set of devices you control.

---

## TLS / Certificates

### Self-signed (default)

- Generated inside the `dashboard` container on first start.
- SAN is controlled by `JARVIS_CERT_SAN` in `.env`; `setup.sh` sets this based on access mode.
- Key + cert live in a named volume; `docker compose down -v dashboard` forces regeneration on next start.
- Generator script: `frontend/scripts/generate-certs.sh` (reads `JARVIS_CERT_SAN`, falls back to localhost-only).

### Let's Encrypt (Caddy profile)

The tracked `letsencrypt` profile starts a Caddy sidecar in front of the dashboard:

```bash
# .env
LETSENCRYPT_DOMAIN=jarvis.example.com
LETSENCRYPT_EMAIL=you@example.com
CORS_ORIGINS=https://jarvis.example.com,https://localhost:3001

docker compose --profile letsencrypt up -d caddy
```

Point DNS at the host and ensure inbound ports 80/443 are reachable. Caddy terminates public TLS and proxies to the dashboard over **internal HTTPS**:

```caddyfile
reverse_proxy https://dashboard:3000 {
    transport http {
        tls_insecure_skip_verify
    }
}
```

Keep `JARVIS_SKIP_SELFSIGNED_GEN=false` for this sidecar path unless you also mount real cert files into the dashboard container. The dashboard nginx process still needs its internal cert/key files even though browsers see the Caddy certificate.

**Option B — host nginx + certbot.** Run certbot on the host, place the cert where the dashboard container can volume-mount it, set `JARVIS_CERT_SAN` to match your real hostname, and set `JARVIS_SKIP_SELFSIGNED_GEN=true` so the container doesn't overwrite your real cert.

### Importing an existing certificate

Drop the cert into the cert volume and prevent regeneration:

```bash
docker compose down dashboard
docker run --rm -v jarvis_dashboard_certs:/certs -v "$PWD/my-cert":/src alpine \
  sh -c 'cp /src/fullchain.pem /certs/cert.pem && cp /src/privkey.pem /certs/key.pem && chmod 600 /certs/key.pem && chmod 644 /certs/cert.pem'
docker compose up -d dashboard
```

Force regeneration at any time: `docker compose down -v dashboard && docker compose up -d dashboard`.

---

## Update Workflow

```bash
git pull
./update.sh
```

What `update.sh` does (see the top of `update.sh` for the full flow):

1. Loads pinned image versions from `versions.env`.
2. Diffs running vs pinned images for `postgres`, `ollama`, `qdrant`, `litellm`, `n8n`, `cloudflared`. Prints a status table.
3. Prompts to `docker compose pull && up -d` the stale services.
4. Separately prompts to rebuild `paper_ingestion`, `learning_engine`, and `dashboard` from local source. It includes `telegram_bot` only when `.env` contains `TELEGRAM_BOT_TOKEN`.
5. Waits up to 180 s per updated service for its HEALTHCHECK to report healthy.
6. If any service fails to become healthy, prints the exact rollback command: `git checkout HEAD~1 -- versions.env && ./update.sh`.

`update.sh` never auto-rollbacks — the operator decides. Logs for the failed service: `docker compose logs --tail=200 <svc>`.

For local development or same-commit smoke tests, the Makefile exposes focused
rebuild targets:

```bash
make rebuild-dashboard   # frontend-only changes
make rebuild-backend     # paper_ingestion + learning_engine
make rebuild-local       # core app containers: backend + dashboard
make rebuild-telegram    # optional bot, requires TELEGRAM_BOT_TOKEN
make up-build            # full docker compose up -d --build
```

Host Python/npm packages do not make Docker images current. If source code
changed, rebuild the affected container before testing the webapp.

### Database migration repair after updates

`paper_ingestion` runs the migration runner on startup. The runner is
idempotent and also repairs the known 2026-05-05 false-applied migration state
caused by old `db/init.sql` snapshots that blanket-seeded `schema_migrations`.
If rows for migrations 033 or 049-058 were marked applied without their schema
objects/data, startup removes only the bad marker and replays the migration.

Do not manually `ALTER TABLE` for missing `user_config.encrypted_value`,
Procrastinate objects, `job_progress`, or the 049-058 follow-up schema unless
the migration runner logs a real SQL failure. The normal repair path is:

```bash
git pull
./update.sh
docker compose logs --tail=200 paper_ingestion
```

---

## Backup + Restore

### Backup script

`scripts/backup.sh` is scheduled by the compose profile `backup` (`docker compose --profile backup up -d`). Reads:

| Env var | Default | Purpose |
|---|---|---|
| `BACKUP_DIR` | `/backups` | Where `.sql.gz` files are written inside the container |
| `BACKUP_RETENTION_DAYS` | `7` | `find ... -mtime +N -delete` prune interval |
| `BACKUP_S3_BUCKET` | empty | Optional S3 destination; skipped if unset or if `aws` CLI is missing |
| `BACKUP_INTERVAL_SECONDS` | `86400` | Sleep between backup iterations |
| `PGHOST` / `PGUSER` / `PGDATABASE` | `postgres` / `jarvis` / `jarvis` | Postgres connection parameters |

Behaviour:
- Runs `pg_dump --no-owner --no-acl | gzip` to `$BACKUP_DIR/jarvis_<timestamp>.sql.gz`.
- If `BACKUP_S3_BUCKET` is set and `aws` is on PATH, uploads with `aws s3 cp`. Otherwise logs a skip message (it does **not** auto-install awscli).
- Prunes files in `$BACKUP_DIR` older than `BACKUP_RETENTION_DAYS`.

### Installing awscli for S3 upload

Two options; you need one of them in the backup container:

```bash
# Option A — rebuild the backup image with awscli baked in (simplest).
# Add to the backup-container Dockerfile:
#   RUN apk add --no-cache aws-cli   (alpine)
#   RUN pip install awscli           (python base)

# Option B — run the amazon/aws-cli image as a sidecar in an override file.
# Pipes `pg_dump` into `aws s3 cp -`. See Amazon docs.
```

S3 env vars: set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (or an IAM instance role if your host has one). Use an IAM policy scoped to `s3:PutObject` on the backup bucket only.

### Restore (manual — no script yet)

A restore script is not shipped. Manual recipe:

```bash
# 1. Stop paper_ingestion and learning_engine so they don't hold connections.
docker compose stop paper_ingestion learning_engine

# 2. Drop and recreate the database.
docker compose exec postgres psql -U jarvis -d postgres \
  -c 'DROP DATABASE jarvis;' -c 'CREATE DATABASE jarvis OWNER jarvis;'

# 3. Restore the dump.
gunzip -c /path/to/jarvis_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose exec -T postgres psql -U jarvis -d jarvis

# 4. Re-import Qdrant snapshots (if you have them — Qdrant is not backed up by
#    scripts/backup.sh; use Qdrant's own snapshot API per collection).
#    See: https://qdrant.tech/documentation/concepts/snapshots/

# 5. Restart services; the migration runner will verify schema on startup.
docker compose up -d paper_ingestion learning_engine
```

Qdrant is **not** backed up by `scripts/backup.sh`. If you want durable vector backups, add a Qdrant snapshot step — Qdrant exposes a snapshot API per collection at `:6333/collections/<name>/snapshots`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser shows `NET::ERR_CERT_AUTHORITY_INVALID` even after accepting once | Cert SAN doesn't match the hostname/IP you used | See [When your LAN IP changes](#when-your-lan-ip-changes). Regenerate with the correct `JARVIS_CERT_SAN`. |
| API calls work from browser on host but fail from other devices with "CORS blocked" | `CORS_ORIGINS` doesn't include the origin you're calling from | Add your origin to `CORS_ORIGINS` in `.env` and `docker compose up -d paper_ingestion learning_engine` |
| Tunnel works on `https://<tunnel-host>` but dashboard 404s at `/` | Cloudflare tunnel is pointed at the wrong service port or public hostname mismatch | In Cloudflare Zero Trust → Networks → Tunnels, confirm the public hostname routes to `http://dashboard:3000`. Confirm `TUNNEL_HOSTNAME` in `.env` matches exactly. |
| Rate limiter 429s every request as "Cloudflare" | Behind Cloudflare but `JARVIS_TRUST_CF_CONNECTING_IP` not enabled | Set `JARVIS_TRUST_CF_CONNECTING_IP=true` in `.env` and restart paper_ingestion / learning_engine. |
| Rate limiter rate-limits by proxy IP instead of client | Upstream proxy missing from `TRUSTED_PROXY_CIDRS` | Add the proxy's public CIDR to `TRUSTED_PROXY_CIDRS`. Note: `NGINX_TRUSTED_PROXY_CIDR` no longer exists — nginx trust is now automatic via pinned Docker IPs (see [Rate-limit client-IP trust](#rate-limit-client-ip-trust-automatic)). |
| Pinned Docker subnet `10.137.241.0/24` collides with my LAN | `setup.sh --check` warns on host-route collision | Set `JARVIS_NET_SUBNET=<free /24>` in `.env` **and** update `docker-compose.yml` `gateway:` and `frontend/nginx.conf` `set_real_ip_from` literals to the new range (see [Rate-limit client-IP trust](#rate-limit-client-ip-trust-automatic)), then run `docker compose down && docker compose up -d`. |
| LAN device can ping host but `curl -k https://<LAN-IP>:3001` hangs | `DASHBOARD_BIND_HOST=127.0.0.1` (localhost mode) | Run `./setup.sh` mode 2, or edit `.env` to `DASHBOARD_BIND_HOST=0.0.0.0` and restart dashboard. |
| `setup.sh` option 3 exits immediately with a ZT warning | `JARVIS_TUNNEL_ACK_ZT_CONFIGURED=1` not set | Configure your Zero-Trust access policy first, then add `JARVIS_TUNNEL_ACK_ZT_CONFIGURED=1` to `.env`. |
| `update.sh` says "not running" for a service you're not using | The service is profile-gated (n8n / telegram / cloudflared / backup) | Expected; ignore the warning for profiles you haven't activated. |
| Pre-existing `docker-compose.override.yml` causes port conflicts after running `setup.sh` mode 2 | `setup.sh` now backs it up automatically, but old installs may still have one | `setup.sh` moves it to `docker-compose.override.yml.bak.<ts>`. Delete the backup once you're sure you don't need it. |
| Settings → "Models & Preferences" shows "No config entries" | DB was initialized before migration 057 seeded default config rows | Restart paper_ingestion to trigger the migration runner: `docker compose restart paper_ingestion`. Verify: `docker compose exec postgres psql -U jarvis -d jarvis -c "SELECT key FROM user_config WHERE key LIKE 'llm.%';"` — should return 3 rows. |
| Selecting a model returns HTTP 400 "LiteLLM config is read-only" | Runtime LiteLLM config is mounted read-only or the selected model is not pulled/assignable | Pull the model first from Settings → Models, or restart LiteLLM with an updated config. The default smart model is `qwen3:14b`. |
| Pulse generates a 0-card deck but Settings says the job is done | Every enabled source returned zero usable candidates, was rate-limited, or is unconfigured | Open Settings → Pulse → Diagnostics and inspect Source diagnostics. For OpenAlex set `OPENALEX_EMAIL` (and optionally `OPENALEX_API_KEY`); for arXiv wait for `Retry-After` or at least 30 seconds and avoid repeated manual regenerations; for Semantic Scholar add an API key if you hit public-rate limits. |
| Pulse diagnostics show `ArxivSource: error` or `rate_limit` | arXiv rejected or timed out the public API request, often after repeated manual runs | Wait out the cooldown, then trigger one Pulse run. If it persists, check `docker compose logs --tail=200 paper_ingestion` for the sanitized arXiv status/transport message and verify the query uses a full `submittedDate:[YYYYMMDDHHMM TO 299912312359]` range. |
| `paper_ingestion` exits with an embedding dimension mismatch, or `/api/system/models` reports `embedding_config` | Existing `.env` or Qdrant state still points at an old 768d/1024d embedding setup while LiteLLM uses the current Qwen3 4B 2560d default | Set `EMBEDDING_MODEL_NAME=qwen3-embedding:4b` and `EMBEDDING_DIMENSION=2560` in `.env`, pull the model, take the explicit Qdrant checkpoint, then run `REEMBED_RECREATE_COLLECTION=true REEMBED_SNAPSHOT_CONFIRMED=true python -m scripts.reembed` only if the collection is still wrong-dimension. Use `qwen3-embedding:0.6b`/`1024` only as an explicit fallback on smaller machines, with a matching Qdrant rebuild. |
| Phase C re-embedding is too slow through LiteLLM/Ollama | `scripts/reembed.py` defaults to the runtime LiteLLM path, which is safe but HTTP-bound and sequential | Benchmark locally first: `REEMBED_BENCHMARK=true REEMBED_BACKEND=local python -m scripts.reembed`. If output count and dimension are correct, resume with `REEMBED_BACKEND=local python -m scripts.reembed`. Use `REEMBED_BACKEND=onnx` only when optional ONNX dependencies are installed and benchmarked faster on that host. |

---

## Other tunnel options (brief)

- **ngrok** — works; set `CORS_ORIGINS=https://<your-ngrok-subdomain>.ngrok.io` and `JARVIS_CERT_SAN` similarly. No dedicated script.
- **Caddy (standalone)** — see the Let's Encrypt recipe above; Caddy can also proxy for any of the remote modes.
- **Traefik** — drop a label-based Traefik override next to `docker-compose.yml`. No documentation target here; Traefik's own docs are thorough.

---

## Key API Endpoints

Key `paper_ingestion` endpoints referenced in operator workflows:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | none | Service health; returns 200 OK or 503 on dependency failure |
| `GET` | `/api/papers` | API key | Paper list with filtering and pagination |
| `GET` | `/api/papers/{id}` | API key | Full paper detail including user state |
| `PUT` | `/api/papers/{id}/save`        | API key (60/min) | Set lifecycle to `to_read` (no body) |
| `PUT` | `/api/papers/{id}/skip`        | API key (60/min) | Set lifecycle to `done` (skipped from inbox) |
| `PUT` | `/api/papers/{id}/trash`       | API key (60/min) | Set lifecycle to `trash`; records `state_before_trash` for restore |
| `PUT` | `/api/papers/{id}/restore`     | API key (60/min) | Restore from trash to prior state |
| `PUT` | `/api/papers/{id}/star` / `/unstar` | API key (60/min) | Toggle the orthogonal `starred` flag |
| `PUT` | `/api/papers/{id}/annotations` | API key (60/min) | Update `rating` / `user_notes` / `flagged` |
| `POST`| `/api/papers/{id}/feedback`    | API key (60/min) | Record recommendation feedback (signal: positive\|negative; source: pulse_thumbs\|feed_thumbs\|paper_detail_thumbs\|dismiss_combined) |
| `POST`| `/api/papers/bulk`             | API key (10/min) | Bulk action over paper IDs (save/skip/reading/done/trash/restore/star/unstar/trash_reject) |
| `DELETE`| `/api/papers/{id}`           | API key (10/min) | Hard delete (requires `state='trash'` precondition) |
| `GET` | `/api/papers/feed/counts`      | API key          | Per-view paper counts (10 buckets: inbox, library, reading_list, reading, done, starred, trash, active, kept, all_non_trash) |
| `GET` | `/api/recommendation_feedback` | API key (30/min) | List user's recommendation feedback rows |
| `DELETE`| `/api/recommendation_feedback?topic_id={id}` | API key (5/min) | Reset all feedback for a topic |
| `POST` | `/api/papers/process` | API key | Trigger PDF parse + embed pipeline |
| `POST` | `/api/ask/stream` | API key | SSE-streamed RAG chat |
| `GET` | `/api/pulse/today` | API key | Today's Pulse deck |
| `POST` | `/api/pulse/generate` | API key | Trigger overnight Pulse run on-demand |
| `GET` | `/api/system/hardware` | API key | Detected local VRAM, source, and hardware tier for model selection |
| `GET` | `/api/system/models` | API key | Curated model catalog with installed/current/status/can-assign metadata |
| `GET` | `/api/system/models/recommendations?role=smart` | API key | Ranked model recommendations for a role |
| `POST` | `/api/system/models/{tag}/pull` | API key | Enqueue a Procrastinate-backed Ollama model pull job |
| `DELETE` | `/api/system/models/{tag}` | API key | Delete an inactive curated Ollama model; rejects active/unknown tags |
| `GET` | `/api/jobs/{id}/stream` | API key | SSE stream for async job progress |

`learning_engine` (:8001) endpoints follow the same auth convention (`X-API-Key` header). See `services/learning_engine/paper_ingestion/routers/` for the full surface.

---

---

## Non-interactive / Automated Installer

`setup.sh` supports a `--non-interactive` mode for CI pipelines, cloud-init
scripts, and automated provisioning where stdin is not a terminal.

### Pre-flight check

Before running `setup.sh` for the first time, run the preflight doctor to catch
missing dependencies and configuration problems early:

```bash
./setup.sh --check
```

`--check` is **read-only and idempotent** — it writes nothing to disk, does not
modify `.env`, and never starts the stack. It verifies:

- Docker Engine is installed and the daemon is reachable.
- Docker Compose v2 is available (`docker compose version`).
- `openssl` is on PATH (required for secret generation).
- GPU/NVIDIA Container Toolkit presence (informational; not fatal).
- `.env` file exists (warns if absent).

Exit codes: **0** — all checks pass (PREFLIGHT: PASS). **1** — one or more
checks failed (PREFLIGHT: FAIL). Run this as a sanity step before every fresh
install or CI pipeline.

### Single-user vs multi-user mode

`setup.sh` accepts a `--mode` flag that controls authentication behaviour:

```bash
./setup.sh --mode single   # default
./setup.sh --mode multi
```

When `--mode` is omitted and `--non-interactive` is **not** set, `setup.sh`
prompts: *"Who will use this instance? [1] Just me  [2] A team"* — pick 1 for
single, 2 for multi. The default when nothing is specified is `single`.

| Mode | Env vars written | How users log in | SMTP required? |
|---|---|---|---|
| `single` | `JARVIS_SETUP_MODE=single`, `API_KEY_LOGIN_ENABLED=true` | Operator uses `JARVIS_API_KEY` via the api-key-session endpoint (`POST /api/auth/api-key-session`) | No — SMTP is optional (shown in the first-run wizard but can be skipped) |
| `multi` | `JARVIS_SETUP_MODE=multi`, `API_KEY_LOGIN_ENABLED=false` | Email magic-link (team/shared instance) | Yes — configure SMTP relay in the first-run wizard or via `SMTP_*` env vars |

**Single-user mode is not an auth downgrade.** It reuses the existing
single-tenant-gated `POST /api/auth/api-key-session` endpoint; `JARVIS_API_KEY`
still gates all REST calls. The only difference is that the dashboard's
magic-link sign-in flow is bypassed in favour of the API key, removing the SMTP
dependency for solo installs.

### Flags

| Flag | Required | Description |
|---|---|---|
| `--non-interactive` | yes | Enable non-interactive mode. Every prompt is driven by flags or defaults; no `read` calls are attempted. |
| `--mode <single\|multi>` | no (default: `single`) | Authentication mode. `single` — API-key login, no SMTP required. `multi` — magic-link login, SMTP relay expected. Interactive prompt when omitted and not `--non-interactive`. |
| `--domain <host>` | letsencrypt only | Public hostname (e.g. `jarvis.example.com`). Populates `LETSENCRYPT_DOMAIN` and `TUNNEL_HOSTNAME`. |
| `--admin-email <email>` | letsencrypt only | Let's Encrypt ACME account email. Populates `LETSENCRYPT_EMAIL`. |
| `--profile <dev\|local-https\|letsencrypt>` | no (default: `dev`) | `dev` — `ENVIRONMENT=development`, localhost binding. `local-https` — self-signed cert, localhost binding. `letsencrypt` — Caddy + ACME; requires `--domain` + `--admin-email`; sets `ENVIRONMENT=production`. |
| `--smtp-host <host>` | no | SMTP relay hostname (populates `SMTP_HOST`). |
| `--smtp-user <user>` | no | SMTP relay username (populates `SMTP_USER`). |
| `--smtp-pass-file <path>` | no | Path to a file whose first line is the SMTP password. Avoids passing credentials on the command line. |

Telegram is handled via the `TELEGRAM_BOT_TOKEN` **environment variable** in
non-interactive mode. If the variable holds a valid token
(`<digits>:<20+ chars>`), the Telegram profile is enabled automatically.

### Copy-paste examples

**Local development / CI smoke test:**

```bash
./setup.sh --non-interactive --profile=dev
```

**Self-hosted with self-signed HTTPS (home lab):**

```bash
./setup.sh --non-interactive \
  --domain=jarvis.local \
  --admin-email=admin@example.com \
  --profile=local-https
```

**Production with Let's Encrypt (public server):**

```bash
# Write SMTP password to a protected temp file; never pass on the command line.
printf '%s' "$MY_SMTP_PASS" > /run/secrets/smtp_pass
chmod 600 /run/secrets/smtp_pass

./setup.sh --non-interactive \
  --domain=jarvis.example.com \
  --admin-email=ops@example.com \
  --profile=letsencrypt \
  --smtp-host=smtp.resend.com \
  --smtp-user=resend \
  --smtp-pass-file=/run/secrets/smtp_pass
```

After setup completes, invite the first admin user via the web UI:

```
https://jarvis.example.com/admin/users
```

---

## Production Readiness Check

`scripts/production-readiness-check.sh` audits the active configuration
against a set of HIGH-severity rules. `setup.sh` runs it automatically at the
end of every install; you can also run it standalone at any time.

```bash
bash scripts/production-readiness-check.sh
```

### Checks performed

| Check | HIGH when | WARN when |
|---|---|---|
| `ENVIRONMENT` | — | Unrecognised value |
| `DEV_AUTH_BYPASS` | `true` + `ENVIRONMENT=production` | `true` in non-production |
| `DEV_ERROR_DETAIL` | `true` + `ENVIRONMENT=production` | `true` in non-production |
| `DEV_CORS_OPEN` | `true` + `ENVIRONMENT=production` | `true` in non-production |
| `DEV_SMTP_LOG_ONLY` | `true` + `ENVIRONMENT=production` | `true` in non-production |
| `DEV_CRYPTO_RELAXED` | `true` + `ENVIRONMENT=production` | `true` in non-production |
| `JARVIS_API_KEY` | Not set or < 32 chars + `ENVIRONMENT=production` | Not set in non-production |
| SMTP | — | `SMTP_HOST` not set (magic links go to stdout) |
| HTTPS | — | No `LETSENCRYPT_DOMAIN` and empty `JARVIS_CERT_SAN` in production |

The script exits **non-zero** when any HIGH finding is present. In
`--profile=letsencrypt` (i.e. `ENVIRONMENT=production`) `setup.sh` treats a
non-zero readiness exit as fatal and aborts with a clear message.

**Simulating a HIGH failure:**

```bash
ENVIRONMENT=production DEV_AUTH_BYPASS=true \
  bash scripts/production-readiness-check.sh; echo "exit=$?"
# → prints FAIL row and exits 1
```

**Clean dev config:**

```bash
bash scripts/production-readiness-check.sh; echo "exit=$?"
# → all OK, exits 0
```

---

## See also

- [README.md](../README.md) — quick start and high-level orientation.
- [AGENTS.md](../AGENTS.md) — repository conventions and architecture.
- [docs/PRD.md](PRD.md) — product requirements; §4.1 security NFRs.
- [docs/archive/2026-05/CODE_SECURITY_REVIEW_2026-04-14.md](archive/2026-05/CODE_SECURITY_REVIEW_2026-04-14.md) — security posture and known residual findings.
- [docs/known-residual-risks.md](known-residual-risks.md) — acknowledged-but-deferred risks and their reopen criteria.
- `PERSONAL-SETUP.md` (gitignored) — your own environment notes; not committed.
