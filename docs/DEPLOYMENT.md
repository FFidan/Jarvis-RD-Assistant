# Deployment Guide

JARVIS is a self-hosted, single-user research assistant. This document is the single source of truth for how to run it — from the localhost happy path to LAN exposure, Cloudflare Tunnel, TLS, backups, and common failure modes.

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
    $EDITOR .env                         # set TELEGRAM_BOT_TOKEN to a FRESH bot token
    docker compose up -d

**⚠ Telegram:** Two machines must NEVER share a bot token. Telegram routes
long-poll updates to whichever client polled last; sharing causes random message
loss. Create a separate bot via @BotFather for the second machine.

After the stack is healthy, open the dashboard. The setup wizard runs
automatically on first visit (`setup.completed = false` in DB). Complete the
wizard to configure your timezone, Pulse schedule, and Telegram pairing.

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

Do not set `MULTITENANT_ENABLED=true` for this mode. That flag currently logs a
critical warning because the runtime still uses a single-user auth resolver; it
does not isolate users or enforce ownership.

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

`setup.sh` generates `POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY`, `N8N_JWT_SECRET`, and `JARVIS_API_KEY`, then brings the stack up. Dashboard lives at `https://localhost:3001`.

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
DASHBOARD_PASSWORD=<strong password>
```

- `DEV_MODE=true` does **not** bypass auth when a `JARVIS_API_KEY` is configured (verified in `libs/jarvis_common/jarvis_common/auth.py`); the flag only helps unauthenticated local development.
- `n8n` is not protected by the JARVIS API key — if you expose the n8n port on LAN, set `N8N_BASIC_AUTH_USER`/`N8N_BASIC_AUTH_PASSWORD` or keep it on `127.0.0.1`. See finding S-7.4 in `docs/CODE_SECURITY_REVIEW_2026-04-14.md`.

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

Secrets are stored as files in a `secrets/` directory at the repo root (gitignored). On first run, `setup.sh` creates them:

```bash
# setup.sh creates secrets/ and populates files automatically.
# To rotate a secret manually:
echo -n '<new-value>' > secrets/jarvis_api_key
docker compose up -d paper_ingestion learning_engine   # triggers re-read on next start
```

> **Note:** `secrets/` is in `.gitignore`. Never commit secret files. Use `chmod 600 secrets/*` to restrict read access on multi-user hosts.

---

> **Alternatives:** Cloudflare Tunnel and DIY port-forward+ACME are possible but out of scope for this single-user stack; setup is on the operator if pursued.

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
4. Separately prompts to rebuild `paper_ingestion`, `learning_engine`, `telegram_bot`, `dashboard` from local source (these are the services you care about after a `git pull`).
5. Waits up to 180 s per updated service for its HEALTHCHECK to report healthy.
6. If any service fails to become healthy, prints the exact rollback command: `git checkout HEAD~1 -- versions.env && ./update.sh`.

`update.sh` never auto-rollbacks — the operator decides. Logs for the failed service: `docker compose logs --tail=200 <svc>`.

### Database migration repair after updates

`paper_ingestion` runs the migration runner on startup. The runner is
idempotent and also repairs the known 2026-05-05 false-applied migration state
caused by old `db/init.sql` snapshots that blanket-seeded `schema_migrations`.
If rows for migrations 033 or 049-057 were marked applied without their schema
objects/data, startup removes only the bad marker and replays the migration.

Do not manually `ALTER TABLE` for missing `user_config.encrypted_value`,
Procrastinate objects, `job_progress`, or the 049-057 follow-up schema unless
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
| Rate limiter rate-limits by proxy IP instead of client | Upstream proxy missing from `TRUSTED_PROXY_CIDRS` | Add the proxy's public CIDR to `TRUSTED_PROXY_CIDRS`. |
| LAN device can ping host but `curl -k https://<LAN-IP>:3001` hangs | `DASHBOARD_BIND_HOST=127.0.0.1` (localhost mode) | Run `./setup.sh` mode 2, or edit `.env` to `DASHBOARD_BIND_HOST=0.0.0.0` and restart dashboard. |
| `setup.sh` option 3 exits immediately with a ZT warning | `JARVIS_TUNNEL_ACK_ZT_CONFIGURED=1` not set | Configure your Zero-Trust access policy first, then add `JARVIS_TUNNEL_ACK_ZT_CONFIGURED=1` to `.env`. |
| `update.sh` says "not running" for a service you're not using | The service is profile-gated (n8n / telegram / cloudflared / backup) | Expected; ignore the warning for profiles you haven't activated. |
| Pre-existing `docker-compose.override.yml` causes port conflicts after running `setup.sh` mode 2 | `setup.sh` now backs it up automatically, but old installs may still have one | `setup.sh` moves it to `docker-compose.override.yml.bak.<ts>`. Delete the backup once you're sure you don't need it. |
| Settings → "Models & Preferences" shows "No config entries" | DB was initialized before migration 057 seeded default config rows | Restart paper_ingestion to trigger the migration runner: `docker compose restart paper_ingestion`. Verify: `docker compose exec postgres psql -U jarvis -d jarvis -c "SELECT key FROM user_config WHERE key LIKE 'llm.%';"` — should return 3 rows. |
| Selecting a model returns HTTP 400 "LiteLLM config is read-only" | Runtime LiteLLM config is mounted read-only or the selected model is not pulled/assignable | Pull the model first from Settings → Models, or restart LiteLLM with an updated config. The default smart model is `qwen3:14b`. |
| `paper_ingestion` exits with an embedding dimension mismatch, or `/api/system/models` reports `embedding_config` | Existing `.env` or Qdrant state still points at the old 768d embedding setup while LiteLLM uses Qwen3 1024d | Set `EMBEDDING_MODEL_NAME=qwen3-embedding:0.6b` and `EMBEDDING_DIMENSION=1024` in `.env`, pull the model, take the explicit Qdrant checkpoint, then run `REEMBED_RECREATE_COLLECTION=true python -m scripts.reembed` only if the collection is still wrong-dimension. |
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

## See also

- [README.md](../README.md) — quick start and high-level orientation.
- [AGENTS.md](../AGENTS.md) — repository conventions and architecture.
- [docs/PRD.md](PRD.md) — product requirements; §4.1 security NFRs.
- [docs/CODE_SECURITY_REVIEW_2026-04-14.md](CODE_SECURITY_REVIEW_2026-04-14.md) — security posture and known residual findings.
- [docs/known-residual-risks.md](known-residual-risks.md) — acknowledged-but-deferred risks and their reopen criteria.
- `PERSONAL-SETUP.md` (gitignored) — your own environment notes; not committed.
