# Deployment Guide

JARVIS is a self-hosted, single-user research assistant. This document is the single source of truth for how to run it — from the localhost happy path to LAN exposure, Cloudflare Tunnel, TLS, backups, and common failure modes.

Most content here is also summarised in the [README](../README.md); this document expands on edge cases and operator-grade details. If the two conflict, this file is canonical.

---

## Deployment Modes

| Mode | Setup time | Inbound ports | TLS story | Rate-limit trust | Prerequisites |
|---|---|---|---|---|---|
| **Localhost** | ~5 min | none | Self-signed (localhost SAN) | All traffic is local; no proxy headers trusted | Docker Engine 24+, Docker Compose v2 |
| **LAN** | ~10 min | `3001/tcp` (dashboard) on LAN iface | Self-signed with LAN IP SAN | Rightmost-trusted-hop XFF walk | Same + a stable LAN IP |
| **Cloudflare Tunnel** | ~30 min | *none* (outbound-only) | Self-signed origin + Cloudflare edge TLS | `CF-Connecting-IP` when `JARVIS_TRUST_CF_CONNECTING_IP=true` | Cloudflare account + Zero-Trust access policy |
| **Tailscale Funnel** | ~20 min | *none* on your host | Tailscale-provisioned TLS | Right-to-left XFF walk (SEC-001) | Tailscale account, approved Funnel feature |
| **VPN (Tailscale/WireGuard)** | ~15 min | VPN ports only | Self-signed | All traffic is intra-tunnel | WG/TS on both ends |

**Default.** `./setup.sh` runs mode 1 (localhost). Mode 2 (LAN) and mode 3 (Cloudflare Tunnel) are prompts in the same script. VPN and Tailscale Funnel are not auto-wired — they work but you set them up yourself.

---

## Mode 1 — Localhost

```bash
cp .env.example .env   # setup.sh does this for you
./setup.sh             # pick option 1 at the access-mode prompt
```

`setup.sh` generates `POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY`, `N8N_JWT_SECRET`, `LITELLM_MASTER_KEY`, and `JARVIS_API_KEY`, then brings the stack up. Dashboard lives at `https://localhost:3001`.

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

---

## Mode 3 — Cloudflare Tunnel

Access JARVIS from anywhere without opening any inbound ports. Traffic enters Cloudflare's edge and exits to your host via an outbound WebSocket.

### Prerequisites

1. A Cloudflare account with a domain on it.
2. A Zero-Trust tenant at <https://one.dash.cloudflare.com/>.
3. A Zero-Trust **Access Application** covering the public hostname you intend to use. Without it, the tunnel publishes your services to the open internet.

### Setup

```bash
# Acknowledge the ZT gate first (setup.sh refuses to proceed without this).
echo 'JARVIS_TUNNEL_ACK_ZT_CONFIGURED=1' >> .env
./setup.sh   # pick option 3; paste the tunnel token when prompted
```

`setup.sh` will:
- Enforce the `JARVIS_TUNNEL_ACK_ZT_CONFIGURED=1` gate at `setup.sh:231-236`.
- Prompt for your tunnel token and public hostname.
- Set `CORS_ORIGINS=https://<tunnel-hostname>,https://localhost:3001`.
- Set `JARVIS_CERT_SAN=DNS:localhost,IP:127.0.0.1,DNS:<tunnel-hostname>`.
- Activate `docker compose --profile tunnel` so the `cloudflared` service starts.

### Cloudflare-specific trust header

Cloudflare strips and replaces `X-Forwarded-For` with `CF-Connecting-IP`. JARVIS's rate limiter needs to know this to avoid rate-limiting every request as "Cloudflare":

```
# Recommended when running behind Cloudflare Tunnel:
JARVIS_TRUST_CF_CONNECTING_IP=true
```

This flag is read by `libs/jarvis_common/jarvis_common/http_rate_limiter.py:50` and already wired as a compose placeholder (`docker-compose.yml:27`). `setup.sh` currently does **not** set it for you — set it manually. Auto-setting in tunnel mode is tracked in [post-R14 roadmap WS-8](plans/2026-04-24-post-r14-roadmap.md).

### How the rate-limiter walks XFF

The IP-extraction walk is **right-to-left** (rightmost-trusted hop, Werkzeug-style — `http_rate_limiter.py:59-61`) — a left-to-right walk would let an attacker prepend a fake IP and bypass rate limits (SEC-001). If you add any other proxy in front of JARVIS, add its public egress CIDRs to `TRUSTED_PROXY_CIDRS` in `.env`.

---

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

### Let's Encrypt (manual recipe)

There is no automated Let's Encrypt wiring yet — that's planned in [post-R14 roadmap WS-8](plans/2026-04-24-post-r14-roadmap.md) along with a `JARVIS_SKIP_SELFSIGNED_GEN=true` flag to suppress self-signed generation when a real cert is mounted.

Until then, two manual options:

**Option A — Caddy sidecar (recommended).** Add a `docker-compose.override.yml` with a Caddy service in front of the dashboard. Caddy handles ACME automatically. Skeleton:

```yaml
services:
  caddy:
    image: caddy:2
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - dashboard
volumes:
  caddy_data:
  caddy_config:
```

Caddyfile:
```
your-host.example.com {
    reverse_proxy dashboard:3000 {
        transport http {
            tls_insecure_skip_verify
        }
    }
}
```

Point your DNS at the host, ensure ports 80/443 are reachable, and `docker compose up -d caddy`.

**Option B — host nginx + certbot.** Run certbot on the host, place the cert where the dashboard container can volume-mount it, and set `JARVIS_CERT_SAN` to match your real hostname. Again, when the `JARVIS_SKIP_SELFSIGNED_GEN` flag ships, enable it so the container doesn't overwrite your real cert.

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

---

## Other tunnel options (brief)

- **ngrok** — works; set `CORS_ORIGINS=https://<your-ngrok-subdomain>.ngrok.io` and `JARVIS_CERT_SAN` similarly. No dedicated script.
- **Caddy (standalone)** — see the Let's Encrypt recipe above; Caddy can also proxy for any of the remote modes.
- **Traefik** — drop a label-based Traefik override next to `docker-compose.yml`. No documentation target here; Traefik's own docs are thorough.

---

## See also

- [README.md](../README.md) — quick start and high-level orientation.
- [AGENTS.md](../AGENTS.md) — repository conventions and architecture.
- [docs/PRD.md](PRD.md) — product requirements; §4.1 security NFRs.
- [docs/CODE_SECURITY_REVIEW_2026-04-14.md](CODE_SECURITY_REVIEW_2026-04-14.md) — security posture and known residual findings.
- `PERSONAL-SETUP.md` (gitignored) — your own environment notes; not committed.
