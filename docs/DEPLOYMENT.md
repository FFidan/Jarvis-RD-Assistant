# Deployment Guide

JARVIS is a self-hosted research assistant. This document covers running it — from the localhost happy path to LAN exposure, Cloudflare Tunnel, TLS, backups, and common failure modes.

For first-time setup, start with [README.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/README.md) Quickstart. If the two conflict, this file is canonical.

---

## Solo deployment (recommended for single-user)

Use the installer for first-time setup. It generates secrets, asks how you will
access JARVIS (localhost / LAN / Cloudflare Tunnel / Let's Encrypt), selects
single-user or multi-user mode, detects GPU support, pulls the prebuilt
application images, starts the stack, and opens the first-run wizard.

```bash
./setup.sh --check   # read-only preflight
./setup.sh
```

Manual `docker compose up -d` is an advanced recovery path after `.env` and
secrets already exist. GPU users should prefer `./setup.sh`; it persists the GPU
overlay so later compose starts keep using it. See [GPU acceleration](#gpu-acceleration-optional).

Required `.env` vars (`setup.sh` or `init-secrets.sh` generates any that are blank):

| Var | Purpose |
|---|---|
| `JARVIS_API_KEY` | 32-byte hex; gates the REST API + dashboard login |
| `JARVIS_CONFIG_KEY` | Fernet key; encrypts user_config secrets at rest |
| `LITELLM_MASTER_KEY` | 32-byte hex; gates LiteLLM admin endpoints |
| `JARVIS_MODEL_HMAC_KEY` | 64-hex; HMAC-signs Pulse classifier pickle blobs; **auto-generated** — do not hand-edit |

Optional: `JARVIS_CONFIG_KEY_OLD` — enables zero-downtime crypto rotation via MultiFernet.

Health checks:

```bash
curl http://127.0.0.1:8010/health   # paper_ingestion
curl http://127.0.0.1:8011/health   # learning_engine
```

---

## Fresh install on a second machine

```bash
git clone https://github.com/limitcycle-oss/jarvis-rd-assistant.git
cd jarvis-rd-assistant
./setup.sh --check
./setup.sh
```

Manual `.env` editing is optional and mostly for advanced operators. Telegram,
SMTP, source keys, model choices, and cloud providers can be configured through
the first-run wizard or Settings after the stack starts.

**Telegram is optional.** If you enable it, two machines must **never** share a bot token — Telegram routes updates to whichever client polled last. Create a separate bot via @BotFather per machine.

After the stack is healthy, open the dashboard. The onboarding wizard runs on first visit and guides you through admin-account creation, SMTP, Pulse schedule, and Telegram pairing in one continuous flow.

### Source HTTP cache

JARVIS caches successful `GET` responses from external metadata sources (Semantic Scholar, arXiv, OpenAlex, Crossref, PubMed) to reduce duplicate outbound requests and 429 failures. Cache is on by default; only `GET`+`200` responses for those hosts are stored.

| Env var | Default | Description |
|---|---|---|
| `SOURCE_HTTP_CACHE_ENABLED` | `true` | Set `false` to disable entirely. |
| `SOURCE_HTTP_CACHE_TTL_SECONDS` | `900` | Seconds a cached response is considered fresh. |

A service restart is required when changing these.

### Pulse source caveats

arXiv rate-limits to ~1 req/3 s; repeated manual Pulse runs can return HTTP 429. A zero-card run with `degraded_reason` means the job completed but every source was empty, rate-limited, or misconfigured. If arXiv returns 429, wait the `Retry-After` window (≥30 s) before retrying. Use Settings → Pulse → Diagnostics for production troubleshooting; `/api/pulse/debug` is dev-mode only (`DEV_MODE=true`).

OpenAlex requires `OPENALEX_EMAIL` or `OPENALEX_API_KEY`. PubMed works without a key but an NCBI key raises the rate limit.

---

## GPU acceleration (optional)

The default stack is CPU-safe. Ollama runs on CPU out of the box. On GPU, the first paper analysis takes a few minutes; on CPU-only it can take 30 minutes or more — fully supported, just slower. The first run also downloads the Ollama model set for your hardware tier — roughly 5–22 GB depending on tier, after the application images are pulled; see [Disk budget](REQUIREMENTS.md#disk-budget). On macOS, Docker containers cannot use the Apple GPU — expect CPU-speed analysis; allocate ≥8 GB to Docker Desktop.

### Standard GPU overlay (auto-engaged by `setup.sh`)

`setup.sh` automatically enables GPU support when it detects the Docker NVIDIA runtime (`docker info` probe). On a CUDA-capable host with the NVIDIA Container Toolkit installed, run `setup.sh` once — it merges `docker-compose.gpu.yml` into the active compose file list and **persists `COMPOSE_FILE` to `.env`**, so every subsequent `docker compose up -d` call continues to use the GPU overlay without any extra flags.

To start with the GPU overlay manually (without running `setup.sh`):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

Note: this one-off command does **not** persist `COMPOSE_FILE`. To make it permanent, add to `.env`:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
```

**Makefile targets (`make up`, `make profile-stack-up`) always run CPU-only.** They do not include the GPU overlay, regardless of hardware. Use the explicit `-f` form or the `.env` `COMPOSE_FILE` setting for GPU acceleration outside of `setup.sh`.

The `./setup.sh --check` command reports GPU toolkit availability as an informational item.

### vLLM overlay (optional, manual)

vLLM is a **separate, optional overlay** — it is not auto-started by `setup.sh`. Use it to serve large models locally at higher throughput than Ollama.

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.vllm.yml --profile vllm up -d
```

After the vLLM container is healthy, expose it through the local LiteLLM route and choose the served model in **Settings → Models → AI models**. The advanced backend and hardware panel shows local runtime guidance, while the Main and Quick model cards remain the assignment surface.

---

## Observability (optional, off by default)

LLM-call tracing via Langfuse is opt-in. `OBSERVABILITY_ENABLED` defaults to `false` and the Langfuse SDK is never constructed — zero overhead.

To enable (provisions a loopback-only Langfuse instance, no signup needed):

```bash
# Set LANGFUSE_INIT_USER_PASSWORD in .env first
make observability-up
```

Open `http://localhost:3002` and sign in with `LANGFUSE_INIT_USER_EMAIL` (default `operator@jarvis.local`). Langfuse is a single operator tool, loopback-bound, decoupled from JARVIS user accounts.

Full contract and rotation procedure: [docs/contracts/04-observability.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/docs/contracts/04-observability.md) §9.

---

## Remote access (optional)

By default the dashboard is reachable only on the machine it runs on
(`http://localhost:3001`): it binds to loopback (`DASHBOARD_BIND_HOST`) and nginx
answers only to a `Host` allowlist (`DASHBOARD_SERVER_NAME`, below). **If you only
use JARVIS on that one machine, skip this section** — nothing needs to be exposed.

To reach it from another device — your phone, or a laptop away from home —
without putting it on the open internet, common options are:

- **A reverse proxy with TLS** on your own domain — the Caddy `local-https` /
  `letsencrypt` profiles (see *Deployment Modes* below).
- **A Cloudflare Tunnel** — outbound-only, no open ports.
- **A mesh VPN** — your devices share one private encrypted network.

These are alternatives; pick whichever suits you. The walkthrough below is **one
example** (Tailscale, a mesh VPN), not a requirement.

### Example: Tailscale

Tailscale is a zero-config mesh VPN (WireGuard) that is free for personal use. Install it on the JARVIS host and on your phone, sign both into the same account, and the dashboard becomes reachable over the private tailnet with **zero open inbound ports** — reached by a stable MagicDNS name (e.g. `my-host.my-tailnet.ts.net`). See <https://tailscale.com>.

> **Scope of the steps below:** they assume the **default single-host setup** —
> dashboard on plain-HTTP loopback `:3001`, Tailscale running on the **same host**
> (on WSL2: inside the same distro as Docker, not the Windows host). If you front
> the app with Caddy (`local-https`/`letsencrypt`), use Caddy's domain for remote
> access instead. Adjust the port if you changed `DASHBOARD_HOST_PORT`.

**1. Install Tailscale and join the tailnet** on both the host and the client:

```bash
curl -fsSL https://tailscale.com/install.sh | sh   # on the host (Linux/WSL2)
sudo tailscale up
```

Install the Tailscale app on the phone and sign in to the same account.

**2. Allow the host's tailnet name.** The dashboard nginx rejects (`444`) any
`Host` it doesn't recognise (`localhost`/`127.0.0.1` by default; the Caddy
profiles rewrite `Host`→`localhost`). Add your MagicDNS name so access isn't
refused — a `444` surfaces as a `502` through a proxy:

```bash
echo 'DASHBOARD_SERVER_NAME=<host>.<tailnet>.ts.net' >> .env
docker compose up -d dashboard
```

**3. Expose the dashboard on the tailnet** — two ways:

- **`tailscale serve`** (clean HTTPS URL, keeps the loopback bind):

  ```bash
  sudo tailscale set --operator=$(whoami)   # one-time: run `serve` without sudo
  tailscale serve --bg --https=443 http://127.0.0.1:3001
  ```

  Then open `https://<host>.<tailnet>.ts.net` (no port). Requires HTTPS enabled
  in the Tailscale admin console (Settings → Features → HTTPS Certificates).

- **Direct port** (simpler): also set `DASHBOARD_BIND_HOST=0.0.0.0`, run
  `docker compose up -d dashboard`, and open
  `http://<host>.<tailnet>.ts.net:3001`. Binds the port on all interfaces —
  fine on a private tailnet, looser than `serve`.

Telegram needs no change for any of this — the bot only makes outbound calls.

---

## Deployment Modes

| Mode | Setup time | Inbound ports | TLS story |
|---|---|---|---|
| **Localhost** | ~5 min | none | Self-signed (localhost SAN) |
| **LAN** | ~10 min | `3001/tcp` on LAN iface | Self-signed with LAN IP SAN |
| **Cloudflare Tunnel** | ~30 min | *none* (outbound-only) | Self-signed origin + Cloudflare edge TLS |
| **Let's Encrypt / Caddy** | ~15 min after DNS | `80/tcp`, `443/tcp` | Caddy ACME edge TLS |
| **Tailscale Funnel** | ~20 min | *none* on your host | Tailscale-provisioned TLS |
| **VPN (Tailscale/WireGuard)** | ~15 min | VPN ports only | Self-signed |

`./setup.sh` runs localhost mode by default. LAN, Cloudflare Tunnel, and Let's Encrypt are prompts in the same script (access-mode options 2–4); the chosen mode also sets `APP_BASE_URL` and `JARVIS_ACCESS_MODE` in `.env`. See [Choosing how you access JARVIS](manual/access-modes.md).

---

## Mode 1 — Localhost

The default mode — see [Solo deployment](#solo-deployment-recommended-for-single-user) above for the commands, or run `./setup.sh` and pick option 1 at the access-mode prompt.

The dashboard is at `http://localhost:3001` (default `DASHBOARD_HOST_PORT=3001`). Enable the `caddy-local` profile only when you want local HTTPS.

### Self-signed cert — browser acceptance

On first start JARVIS generates a self-signed certificate. Accept the browser warning once: Chrome/Edge — type `thisisunsafe` (no input field); Firefox — *Advanced → Accept the Risk*; Safari — *Show Details → visit this website* (macOS prompts for your admin password).

---

## Mode 2 — LAN

```bash
./setup.sh   # pick option 2
```

`setup.sh` sets `DASHBOARD_BIND_HOST=0.0.0.0`, detects your LAN IP, adds it to `CORS_ORIGINS` and `JARVIS_CERT_SAN`, and removes stale override files.

### When your LAN IP changes

DHCP can re-lease a different IP after a reboot (symptom: dashboard works on `localhost` but other devices get a cert or CORS error).

```bash
# Option A — re-run setup.sh (re-detects IP, prompts to regenerate cert):
./setup.sh

# Option B — update .env manually, then regenerate:
# JARVIS_CERT_SAN=DNS:localhost,IP:127.0.0.1,IP:<new-LAN-IP>
# CORS_ORIGINS=https://localhost:3001,https://<new-LAN-IP>:3001
rm ./certs/cert.pem ./certs/key.pem && docker compose up -d dashboard
```

### Hardening checklist before LAN exposure

Set in `.env` (`setup.sh` enforces the first two for option 2; verify the rest):

```
ENVIRONMENT=production
DEV_MODE=false
JARVIS_API_KEY=<at least 32 chars: openssl rand -hex 32>
JARVIS_CONFIG_KEY=<Fernet key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
```

`DEV_MODE=true` is a meta-flag that promotes granular dev flags (`DEV_AUTH_BYPASS`, `DEV_ERROR_DETAIL`, `DEV_CORS_OPEN`, `DEV_SMTP_LOG_ONLY`, `DEV_CRYPTO_RELAXED`) to `true` unless explicitly overridden. None are permitted when `ENVIRONMENT=production` — startup will crash if any is `true`.

### Rate-limit client-IP trust (automatic)

JARVIS pins the internal Docker network to `10.137.241.0/24` and assigns Caddy fixed IPs within it. nginx trusts only those IPs plus `127.0.0.1` — no operator step required.

**Subnet collision:** if `10.137.241.0/24` conflicts with your LAN, `setup.sh --check` warns. Set `JARVIS_NET_SUBNET=<a free /24>` and update `frontend/nginx.conf`'s two `set_real_ip_from` literals.

**`TRUSTED_PROXY_CIDRS`** — Python-layer variable for the XFF walk. Add load-balancer CIDRs only if you place an additional external proxy in front of the stack. RFC-1918 and `127.0.0.0/8` are always trusted.

### Encrypted config key rotation

Provider keys stored through Settings are encrypted with `JARVIS_CONFIG_KEY`. Dry-run first:

```bash
DATABASE_URL=postgresql://jarvis:<password>@localhost:5432/jarvis \
OLD_JARVIS_CONFIG_KEY=<old-fernet-key> \
NEW_JARVIS_CONFIG_KEY=<new-fernet-key> \
python scripts/rotate_config_key.py
```

Add `--apply` after the dry run validates every row. The script runs in a transaction and never logs plaintext or ciphertext.

### Docker Secrets

JARVIS supports Docker Secrets for sensitive credentials. Each is read from a file at runtime via a `_FILE`-suffixed env var, keeping plaintext out of the compose environment and shell history. Secrets live in `secrets/` at the repo root (gitignored); `setup.sh` creates and populates them on first run. Each file is mode `644` inside a mode `700` `secrets/` directory: the owner-only directory keeps the files private on the host, while the world-readable file bit lets the non-root service containers read them through the compose bind mount.

**Operator-provisioned secrets** (create manually if not using `setup.sh`):

| Secret name | `_FILE` env var | Purpose |
|---|---|---|
| `postgres_password` | `POSTGRES_PASSWORD_FILE` | PostgreSQL password for the `jarvis` user |
| `litellm_master_key` | `LITELLM_MASTER_KEY_FILE` | LiteLLM master key (gateway auth) |
| `jarvis_api_key` | `JARVIS_API_KEY_FILE` | JARVIS REST API key (frontend + Telegram) |
| `jarvis_model_hmac_key` | `JARVIS_MODEL_HMAC_KEY_FILE` | HMAC-signs Pulse classifier pickle blobs (auto-generated; mandatory in production) |
| `jarvis_config_key` | `JARVIS_CONFIG_KEY_FILE` | Fernet key for encrypted config values |
| `litellm_salt_key` | `LITELLM_SALT_KEY` | Encrypts provider/model keys stored in LiteLLM's admin DB — **never rotate**: rotating orphans all stored model keys and requires re-entering them |
| `telegram_bot_token` | `TELEGRAM_BOT_TOKEN_FILE` | Telegram bot token (`telegram` profile only) |
| `qdrant_api_key` | `QDRANT_API_KEY_FILE` | Qdrant service API key |
| `infra_ingest_key` | `INFRA_INGEST_KEY_FILE` | Shared key for the infrastructure ingestion endpoint |
| `backup_encrypt_key` | `BACKUP_ENCRYPT_KEYFILE` | Encrypts backup archives at rest |

**Observability profile secrets** (auto-provisioned by `make observability-up`; only present when the `observability` profile is active):
`langfuse_init_pk`, `langfuse_init_sk` — Langfuse SDK keys injected into app services. `langfuse_pg_password`, `langfuse_nextauth_secret`, `langfuse_salt` — internal Langfuse service credentials.

```bash
# To rotate a secret manually:
echo -n '<new-value>' > secrets/jarvis_api_key.txt
chmod 644 secrets/jarvis_api_key.txt
docker compose up -d paper_ingestion learning_engine
```

### Web UI configuration

All ongoing configuration goes through the web wizard and Settings — no `.env` editing needed beyond what `setup.sh` writes:

| Setting | Where | Restart? |
|---|---|---|
| SMTP relay | Settings → System → Email / SMTP / onboarding wizard | No |
| Cloud LLM providers and routing | Settings → Models → Providers & Routing | No |
| Telegram bot token | Settings → Integrations → Bot Token | Yes — `docker compose restart telegram_bot` |
| Sign-in method (single ↔ multi-user) | Settings → System → Sign-in Method | No |
| Auto-fetch interval | Settings → Automation | No |

For multi-user (team) deployment and the full trust boundary, see [docs/SECURITY.md](SECURITY.md).

---

## Mode 3 — Cloudflare Tunnel

Exposes JARVIS over an outbound tunnel — no inbound port needed. Edge TLS is terminated by Cloudflare.

**Prerequisites:** A Cloudflare account with a configured tunnel ([Zero Trust dashboard](https://one.dash.cloudflare.com/)).

```bash
echo -n '<your-tunnel-token>' > secrets/cloudflare_tunnel_token.txt
chmod 644 secrets/cloudflare_tunnel_token.txt
docker compose --profile tunnel up -d
```

`setup.sh` option 3 writes the token and brings the tunnel profile up automatically. The `cloudflared` container reads the token from `/run/secrets/cloudflare_tunnel_token`.

```bash
docker compose ps cloudflared          # should be "healthy" or "running"
docker compose logs --tail=20 cloudflared   # look for "Connection established"
```

Tunnel routing is configured in the Cloudflare Zero Trust dashboard: set the public hostname to route to `http://dashboard:3000`. Set `JARVIS_TRUST_CF_CONNECTING_IP=true` in `.env` only if you lock access to Cloudflare IPs.

---

## Mode 4 — Tailscale Funnel / VPN

**Tailscale Funnel** — outbound-only, edge TLS terminated by Tailscale. Not wired into `setup.sh`. See [Tailscale Funnel docs](https://tailscale.com/kb/1223/funnel/).

**VPN (Tailscale / WireGuard)** — mesh your devices onto one network and keep JARVIS in localhost mode. Simplest and most secure option for a small set of trusted devices.

---

## TLS / Certificates

### Self-signed (default)

- Generated inside the `dashboard` container on first start.
- SAN is controlled by `JARVIS_CERT_SAN` in `.env`; `setup.sh` sets this based on access mode.
- Key + cert live in `./certs/` (bind-mount at the repo root).
- `JARVIS_SKIP_SELFSIGNED_GEN` defaults to `true` — generation is skipped if cert files already exist.

To force regeneration:

```bash
rm ./certs/cert.pem ./certs/key.pem
docker compose up -d dashboard
```

### Let's Encrypt (Caddy profile)

```bash
# .env
LETSENCRYPT_DOMAIN=jarvis.example.com
LETSENCRYPT_EMAIL=you@example.com
CORS_ORIGINS=https://jarvis.example.com,https://localhost:3001

docker compose --profile letsencrypt up -d caddy
```

Point DNS at the host and ensure ports 80/443 are reachable. Caddy terminates public TLS and reverse-proxies to the dashboard on the internal Docker network. **Option B — host nginx + certbot:** run certbot on the host, copy the cert into `./certs/`, and set `JARVIS_SKIP_SELFSIGNED_GEN=true`.

### Importing an existing certificate

```bash
docker compose stop dashboard
cp fullchain.pem ./certs/cert.pem
cp privkey.pem ./certs/key.pem
chmod 600 ./certs/key.pem
chmod 644 ./certs/cert.pem
docker compose up -d dashboard
```

---

## Non-interactive / Automated Installer

`setup.sh` supports `--non-interactive` for CI pipelines and cloud-init scripts. Run `./setup.sh --help` for the full flag reference.

### Pre-flight check

```bash
./setup.sh --check
```

Read-only and idempotent. Verifies Docker Engine, Docker Compose v2, `openssl`, GPU toolkit (informational), and `.env` presence. Exit 0 = PASS, exit 1 = FAIL. The pre-flight path never installs packages, writes `.env`, or starts services, even if `--install-prereqs` is also present.

`setup.sh` verifies the Docker *daemon* is reachable (`docker info`), not just that the `docker` binary is installed — both in `./setup.sh --check` and at the start of a real install, before any prompt. If the daemon is down (Docker Desktop not started, `DOCKER_HOST` misconfigured, or missing group permissions), the installer exits immediately with a fix hint instead of crashing mid-wizard.

### Guided prerequisite installation

If Docker, the Docker Compose v2 plugin, or `openssl` are missing, normal interactive setup prints the exact package-manager commands it can run and asks for consent before touching the host. Automated installs never prompt: use `--install-prereqs` only after reviewing the printed commands, or install the prerequisites manually and re-run `./setup.sh --check`.

```bash
# Preflight only; never installs packages
./setup.sh --check

# Review the guided install plan without running it
./setup.sh --non-interactive --profile=dev

# Interactive setup; prompts before running supported apt/Homebrew commands
./setup.sh

# Unattended setup that is allowed to run the reviewed prerequisite plan
./setup.sh --non-interactive --install-prereqs --profile=dev
```

The guided installer supports common Debian/Ubuntu-style `apt-get` hosts and macOS Homebrew. Unsupported distributions, hosts without the needed package manager, and Docker Desktop/daemon startup issues receive manual guidance instead of automatic package changes. Docker may still need to be started manually, and Linux users may need to log out and back in after Docker group changes.

Re-runs: running `./setup.sh` with an existing `.env` and declining the overwrite prompt no longer exits with services stopped — it keeps your `.env` (secrets, database, and model selection untouched) and starts the stack with it via `docker compose up -d`, honouring the `COMPOSE_FILE` and `COMPOSE_PROFILES` values persisted in `.env`. New installs write `COMPOSE_PROFILES=<selection>` (for example `telegram`, `tunnel`) into `.env`; for older `.env` files without it, setup derives the telegram profile from a non-empty `TELEGRAM_BOT_TOKEN` and prints a notice.

Key flags: `--mode <single|multi>`, `--domain <host>`, `--admin-email <email>`, `--profile <dev|local-https|letsencrypt>` (default `dev`; `letsencrypt` requires `--domain` + `--admin-email`), `--smtp-host/--smtp-user/--smtp-pass-file`. Run `./setup.sh --help` for the full reference. `TELEGRAM_BOT_TOKEN` in the environment enables Telegram automatically.

### Single-user vs multi-user mode

`single` (default) makes the sign-in screen offer API-key login first and does not require SMTP. `multi` makes the sign-in screen offer email magic-link login first; configure and test an SMTP relay before inviting other users. The mode is a login-method preference, not a tenancy switch: user/library scoping and admin invite capability remain governed by sessions, roles, and route-level authorization. In either mode, signed-in users can additionally register **passkeys** (WebAuthn) for future sign-ins; passkeys are bound to the exact origin in `APP_BASE_URL` and require HTTPS for a public origin — see the [User Guide → Passkeys](manual/passkeys.md).

### Copy-paste examples

```bash
# Local development / CI smoke test:
./setup.sh --non-interactive --profile=dev

# Self-hosted with self-signed HTTPS (home lab):
./setup.sh --non-interactive --domain=jarvis.local --admin-email=admin@example.com --profile=local-https

# Production with Let's Encrypt:
printf '%s' "$MY_SMTP_PASS" > /run/secrets/smtp_pass && chmod 600 /run/secrets/smtp_pass
./setup.sh --non-interactive \
  --domain=jarvis.example.com \
  --admin-email=ops@example.com \
  --profile=letsencrypt \
  --smtp-host=smtp.resend.com \
  --smtp-user=resend \
  --smtp-pass-file=/run/secrets/smtp_pass
```

After setup, open the dashboard and create the initial admin in the onboarding wizard. Once signed in as an admin, invite additional users at `https://jarvis.example.com/admin/users`.

---

## Update Workflow

```bash
git pull
./update.sh
```

`update.sh` loads pinned versions from `versions.env`, diffs running vs pinned images, and refreshes the services (application images are pulled prebuilt from GHCR; pass `./update.sh --build-local` to rebuild them from source), waiting up to 180 s per service. On failure it prints recovery commands for both image sets: `git checkout HEAD~1 -- versions.env && ./update.sh` rolls back the third-party pins, while the application images roll back by pinning `JARVIS_VERSION` to a previously published tag and pulling it.

From v1.1.0, `update.sh` pulls the prebuilt application images from GitHub Container Registry by default — smaller than a local rebuild (see [Disk budget](REQUIREMENTS.md#disk-budget)). Contributors, forks, and air-gapped hosts rebuild from source with `./update.sh --build-local` (the same flag exists on `./setup.sh`).

### Upgrade notes

**Telegram pairing (breaking change).** The Telegram bot now identifies chats
exclusively via the `/pair` token flow. To pair: open the dashboard → Settings
→ Integrations → Telegram, copy the one-time token, and send `/pair <token>`
to the bot. The legacy `TELEGRAM_CHAT_ID` environment variable and the
dashboard-code pairing path (`/start PAIR_<code>`) no longer work — any
existing `TELEGRAM_CHAT_ID` value can be removed from `.env` after pairing.

**Telegram owner-override network.** The bot calls service endpoints with
`X-Owner-User-Id` to make per-user requests, trusted only from
`OWNER_OVERRIDE_ALLOWED_CIDRS`. The bundled compose stack sets this
automatically to cover the jarvis bridge subnet (it tracks `JARVIS_NET_SUBNET`,
default `10.137.241.0/24`), so no change is needed for the default stack. **If
you override `JARVIS_NET_SUBNET`, the allowlist follows it** — only set
`OWNER_OVERRIDE_ALLOWED_CIDRS` explicitly if the bot reaches the services from
some other network. (The bare code default `127.0.0.0/8` is loopback-only and
does *not* cover the jarvis bridge — the compose stack overrides it.)

**Ownership migration (0092).** On first startup after upgrade, migration 0092
re-assigns any pre-existing product rows with a NULL owner to the single admin
account. This is automatic and non-destructive; it only runs when exactly one
admin user exists (the normal single-tenant state).

Makefile shortcuts for development:

```bash
make rebuild-dashboard   # frontend-only changes
make rebuild-backend     # paper_ingestion + learning_engine
make rebuild-local       # core app containers
make rebuild-telegram    # optional bot
make up-build            # full docker compose up -d --build
```

---

## Backup + Restore

### Backup

`scripts/backup.sh` runs by default in the `postgres-backup` sidecar (a daily run plus on-demand triggers). It is on out of the box and archives are encrypted with the auto-generated `backup_encrypt_key`; if you back up externally, opt out with `docker compose stop postgres-backup`.

Admins can list, download, trigger an on-demand backup, and restore from any listed backup point in the WebUI at **Admin → Backups**. The on-demand backup button signals the sidecar to run immediately. Restore can be performed either from the admin UI (one-click) or from the host command line (manual, for situations where the app itself cannot run).

JARVIS state lives in more than one place, so each run captures **all four** of the durable stores (per-store files are timestamped together so a single run is internally consistent):

| Store | Output (in `/backups`) | Notes |
|---|---|---|
| `jarvis` Postgres DB | `jarvis_<ts>.sql.gz[.enc]` | Papers, users, config, jobs |
| `litellm` Postgres DB | `litellm_<ts>.sql.gz[.enc]` | API keys, virtual keys, spend ledger |
| Qdrant vectors | `qdrant_<collection>_<ts>.snapshot[.enc]` | One per collection via Qdrant's snapshot REST API; **best-effort** — if Qdrant is unreachable this is skipped and the Postgres/secrets backups still succeed |
| `secrets/` directory | `secrets_<ts>.tar.gz[.enc]` | The Docker-secret source files — without these an encrypted DB backup cannot be decrypted |

| Env var | Default | Purpose |
|---|---|---|
| `BACKUP_DIR` | `/backups` | Where archives are written inside the container |
| `BACKUP_RETENTION_DAYS` | `7` | Prune interval (applies to every store above) |
| `BACKUP_S3_BUCKET` | empty | Optional S3 destination (skipped if unset; uploads every archive) |
| `BACKUP_INTERVAL_SECONDS` | `86400` | Sleep between backup runs |
| `BACKUP_ENCRYPT_KEYFILE` | `/run/secrets/backup_encrypt_key` | When the file is non-empty, DB dumps, Qdrant snapshots, and the secrets archive are encrypted at rest (`.enc` suffix) |
| `LITELLM_DATABASE` | `litellm` | Name of the LiteLLM DB to dump |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant base URL for the snapshot API |
| `QDRANT_API_KEYFILE` | `/run/secrets/qdrant_api_key` | Qdrant API key (sent as the `api-key` header) |
| `SECRETS_DIR` | `/secrets` | Read-only mount of the host `./secrets` dir to archive |

The `litellm` DB is owned by the same superuser (`POSTGRES_USER`) that created it, so the same credentials dump both DBs. A failed `pg_dump` aborts the run (non-zero exit) rather than leaving a tiny but well-formed empty archive. The encryption key, Qdrant API key, and `./secrets` source are already wired into the `postgres-backup` sidecar in `docker-compose.yml`. S3 credentials: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (or IAM instance role) scoped to `s3:PutObject`.

> **In production (`ENVIRONMENT=production`) the backup hard-fails if `BACKUP_ENCRYPT_KEYFILE` is unset** — the script exits immediately and no secrets archive is written. In non-production the secrets archive is skipped (not written in the clear) and the script logs a warning. Set `BACKUP_ENCRYPT_KEYFILE` (the default already points at the `backup_encrypt_key` Docker Secret) before running in production.

> **Store the backup encryption key OFF-SITE, separately from the `.enc`
> archives.** The secrets archive is encrypted with `backup_encrypt_key` and
> that key is **excluded** from the archive (it is not packed inside the file
> it unlocks). After total host loss you can only decrypt the off-site `.enc`
> backups if you also kept a copy of `backup_encrypt_key` somewhere else
> (a password manager, a sealed envelope, a separate cloud secret). Losing the
> key makes every encrypted backup permanently unrecoverable.

### Restore

#### One-click restore (admin UI)

From **Admin → Backups**, choose a backup set from the archive table and click **Restore**. Type **RESTORE** in the confirmation dialog to proceed. The sidecar:

1. Takes a **safety pre-backup** of the current live data before making any destructive change.
2. Checks the backup against the running app version. A **newer** backup is refused (update the app first with `./update.sh`); an **older** backup is accepted and migrated forward automatically after it is swapped in.
3. Loads each database into a **staging database behind a maintenance gate** (the app returns "restore in progress" to all users while this runs) and **swaps it in atomically** once it verifies. If anything fails before the swap, the original database is left untouched and served again automatically — the restore self-heals rather than leaving a half-restored database. The previous database is dropped only after the swap succeeds.
4. Recovers the Qdrant search index best-effort — if the Qdrant step fails, the database restore still completes. The search index re-embeds automatically from the restored data on next use; no paper data is lost.

After a clean restore the maintenance gate lifts automatically. All users are signed out (the session store was replaced) and will need to sign in again.

**Security posture:** the app container holds no new privilege — it only writes a small JSON sentinel file to the `backup_trigger` volume. All database work (the staged reload, the atomic swap, and Qdrant recovery) runs exclusively inside the already-privileged `postgres-backup` sidecar. The `/backups` volume remains read-only to the app container. The restore can only be triggered by an active admin browser session (not the ops API key), and requires typing the word RESTORE to confirm.

**Residual risk:** a compromised admin browser session can trigger a restore, replacing live data with the chosen backup point (a safety pre-backup is always taken first, so the prior state remains recoverable). See [SECURITY.md](SECURITY.md) and `docs/known-residual-risks.md` for the full residual-risk register.

---

#### Manual restore (last resort)

> **Prefer the sidecar-driven restore.** The `postgres-backup` sidecar runs independently of the app, so even when the app cannot start you can trigger a restore by writing `/backup-trigger/.restore_request.json` (a `local` source for same-host archives already in `/backups`, or an `inbox` source for an off-site set — see [Off-host recovery](#off-host-total-host-loss-recovery)). That path keeps the safety pre-backup, the staged atomic swap, and the automatic forward-migration of older backups. The raw steps below **bypass all of those protections** — use them only if the sidecar itself is unavailable.

Use the steps below as a last resort — for example, after a total host failure where you are starting fresh on a new machine and cannot run the sidecar. This procedure does the database work by hand from the host.

**Step 0 — decrypt** (if the archive ends in `.enc`):

```bash
openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -d \
  -kfile ./secrets/backup_encrypt_key.txt \
  -in <archive>.enc -out <archive>   # then use the plain file below
```

The decryption flags match what `scripts/backup.sh` uses (`-aes-256-cbc -pbkdf2 -iter 600000`). Repeat for each `.enc` archive before using it.

> **Key dependency warning:** the `jarvis` DB stores provider/model credentials encrypted with `JARVIS_CONFIG_KEY`; the LiteLLM DB stores its model keys encrypted with `LITELLM_SALT_KEY`. Restoring a DB dump without the **same key that encrypted it** leaves those secrets undecryptable. Always restore the matching `secrets/` archive (or confirm the keys are already in place) before starting the stack.

**Step 1 — `secrets/`** (restore before starting the stack, so the right keys are in place):

```bash
# Review the contents first — this overwrites live key files.
tar -tzf secrets_YYYYMMDD_HHMMSS.tar.gz
tar -xzf secrets_YYYYMMDD_HHMMSS.tar.gz -C ./secrets
```

**Step 2 — Postgres (`jarvis` and `litellm`)**:

```bash
docker compose stop paper_ingestion learning_engine
docker compose exec postgres psql -U jarvis -d postgres \
  -c 'DROP DATABASE jarvis;' -c 'CREATE DATABASE jarvis OWNER jarvis;'
gunzip -c /path/to/jarvis_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose exec -T postgres psql -U jarvis -d jarvis
docker compose exec postgres psql -U jarvis -d postgres \
  -c 'DROP DATABASE litellm;' -c 'CREATE DATABASE litellm OWNER jarvis;'
gunzip -c /path/to/litellm_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose exec -T postgres psql -U jarvis -d litellm
docker compose up -d paper_ingestion learning_engine
```

**Step 3 — Qdrant** — stop the container first so the snapshot restore doesn't race an active index write:

```bash
# JARVIS uses TWO Qdrant collections — restore both.
docker compose stop qdrant
docker compose cp qdrant_paper_chunks_YYYYMMDD_HHMMSS.snapshot qdrant:/qdrant/snapshots/paper_chunks.snapshot
docker compose cp qdrant_kg_entities_YYYYMMDD_HHMMSS.snapshot qdrant:/qdrant/snapshots/kg_entities.snapshot
docker compose start qdrant

# Recover paper_chunks (paper embeddings):
docker compose exec qdrant sh -c 'curl -s -X PUT \
  -H "api-key: $(cat /run/secrets/qdrant_api_key)" \
  "http://localhost:6333/collections/paper_chunks/snapshots/recover" \
  -H "Content-Type: application/json" \
  -d "{\"location\":\"file:///qdrant/snapshots/paper_chunks.snapshot\"}"'

# Recover kg_entities (knowledge-graph entities):
docker compose exec qdrant sh -c 'curl -s -X PUT \
  -H "api-key: $(cat /run/secrets/qdrant_api_key)" \
  "http://localhost:6333/collections/kg_entities/snapshots/recover" \
  -H "Content-Type: application/json" \
  -d "{\"location\":\"file:///qdrant/snapshots/kg_entities.snapshot\"}"'
```

---

#### Off-host / total-host-loss recovery

Use this when you are recovering on a **fresh host** from an off-site copy of the backups — the original machine (and its `./secrets`) is gone. Both paths below drive the same hardened `restore.sh` in the `postgres-backup` sidecar, which — for an off-host restore — takes a safety pre-backup, runs the compat + decrypt-probe gates, restores each database via the staged swap, recovers Qdrant, **rebinds the `jarvis` role password** to the restored secret (`ALTER ROLE … WITH PASSWORD`, single-quote-safe), **materializes the restored `./secrets` on the host** (except the keys the new host is authoritative for — `qdrant_api_key`, `langfuse_pg_password`, `backup_encrypt_key`), writes a `.secrets_rotated` marker so the app services **self-restart** onto the rotated secrets, and **shreds the one-time operator key and the plaintext staging on exit**. On a clean restore the maintenance gate then lifts automatically — **there are no terminal steps after you start the restore**.

**Prerequisites (either path):** the off-site **archive set** for one timestamp (`jarvis_<ts>.sql.gz.enc`, `litellm_<ts>.sql.gz.enc`, `secrets_<ts>.tar.gz.enc`, any `qdrant_*_<ts>.snapshot.enc`, and `manifest_<ts>.json`), plus the **backup encryption key** you kept out-of-band (the key is excluded from its own archive, so you must hold a separate copy — see the off-site-key warning above). A wrong or corrupt key fails safe: `restore.sh` proves the key decrypts the database archives before any `DROP`, so nothing is destroyed.

##### Web-based recovery (recommended)

1. **Bring up a fresh stack** on the new host (`./setup.sh`) and complete first-admin sign-in.
2. From the **Backups** panel, generate a **one-time upload grant** and, in the browser, upload the archive set and the backup encryption key, then trigger the inbox restore from the UI. Uploads go to the dedicated `restore-uploader` ingress (grant-gated, inbox-only); the operator key and the archive bytes never transit the application. See [Backup & restore](manual/backup-and-restore.md) for the click-through.
3. Watch progress in the guided restore view. When it completes, the stack has reconciled its secrets, database role, and services and returned to service — no shell access required.

##### Command-line fallback (constrained environments)

For a headless host with no browser access, write the request yourself. The drop zone is the **`restore_inbox`** Docker volume, mounted **read-write** into the sidecar at `/restore-inbox` (never under the read-only `/secrets` mount).

1. **Bring up a fresh stack** on the new host (`./setup.sh` / `docker compose up -d`).
2. **Place the archive set into the inbox** — copy it from your off-site store, or pull one timestamp's set from S3 with the built-in helper (aws-guarded; a no-op with a notice if `aws`/`BACKUP_S3_BUCKET` are absent):

   ```bash
   # Option A — copy a local off-site set into the volume:
   docker compose cp ./offsite/. postgres-backup:/restore-inbox/

   # Option B — pull one timestamp's set from S3 into the inbox:
   docker compose exec -e BACKUP_PULL_TS=<ts> -e BACKUP_PULL_DEST=/restore-inbox \
     postgres-backup /usr/local/bin/backup.sh
   ```

3. **Place the one-time backup key** into the inbox as the file **`operator_key`** (fixed name), then write the restore trigger:

   ```bash
   docker compose cp /path/to/backup_encrypt_key.txt postgres-backup:/restore-inbox/operator_key
   docker compose exec postgres-backup sh -c \
     'printf "{\"source\":\"inbox\",\"timestamp\":\"<ts>\"}" > /backup-trigger/.restore_request.json'
   ```

   The sidecar's poll loop runs `restore.sh` within a few seconds. Watch progress in `/backup-trigger/.restore_status.json` (or the sidecar logs). On a clean restore it rebinds the role, materializes `./secrets`, self-restarts the app services, and lifts the maintenance gate on its own — nothing further to run. Do **not** wipe the Postgres data volume afterward, or the role rebind is lost.

4. **Only if the restore fails after the destructive step** does the stack stay at HTTP 503, holding the durable `.destructive` sentinel (which — unlike the soft `.maintenance` sentinel — never auto-expires). Recover from the safety backup the sidecar took first, then clear both sentinels explicitly:

   ```bash
   docker compose exec postgres-backup rm -f /backup-trigger/.maintenance /backup-trigger/.destructive
   ```

Once recovery is confirmed, remove the archive set from the inbox. The operator key is one-time — `restore.sh` shreds `/restore-inbox/operator_key` (and the plaintext staging) on every clean or recorded-failure exit.

---

#### Sentinel cleanup reference

The app returns HTTP 503 during and after a restore via two sentinel files in the `backup_trigger` volume:

| Sentinel | Env var | Default path | Expiry |
|---|---|---|---|
| Soft | `MAINTENANCE_SENTINEL` | `/backup-trigger/.maintenance` | Auto-expires after `MAINTENANCE_MAX_AGE_S` (default 1800 s) |
| Durable | `MAINTENANCE_DESTRUCTIVE_SENTINEL` | `/backup-trigger/.destructive` | Never auto-expires; written at the DROP boundary |

**Any clean restore clears both sentinels automatically** — same-host one-click, off-host inbox, and older-than-code restores alike. An off-host restore reconciles its own secrets and role and self-restarts the app; an older-than-code restore is migrated forward by the app-factory watcher when the maintenance gate lifts. No operator action is needed in the success case.

**Only a restore that fails after the destructive step** leaves the stack at HTTP 503, holding the durable `.destructive` sentinel:

| Situation | Required step before clearing |
|---|---|
| Crash/failure after the DROP boundary | Restore from the safety backup the sidecar took before the destructive step, then clear both sentinels |

Once the required step is done, clear both sentinels:

```bash
docker compose exec postgres-backup rm -f /backup-trigger/.maintenance /backup-trigger/.destructive
```

---

## Production Readiness Check

```bash
bash scripts/production-readiness-check.sh
```

`setup.sh` runs this at install end. It checks `DEV_*` flags, `JARVIS_API_KEY` length, SMTP presence, and HTTPS config. Exit non-zero on any HIGH finding; `--profile=letsencrypt` treats that as fatal.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `NET::ERR_CERT_AUTHORITY_INVALID` even after accepting once | Cert SAN doesn't match hostname/IP | Regenerate: update `JARVIS_CERT_SAN`, `rm ./certs/cert.pem ./certs/key.pem`, restart dashboard. |
| API calls work from host but CORS-blocked from other devices | `CORS_ORIGINS` missing the calling origin | Add origin to `CORS_ORIGINS` in `.env`, then `docker compose up -d paper_ingestion learning_engine`. |
| Tunnel works but dashboard 404s at `/` | Tunnel routes to wrong port | In Cloudflare Zero Trust, confirm the public hostname routes to `http://dashboard:3000`. |
| Rate limiter 429s every request as "Cloudflare" | Behind CF but `JARVIS_TRUST_CF_CONNECTING_IP` not enabled | Set `JARVIS_TRUST_CF_CONNECTING_IP=true` and restart `paper_ingestion`/`learning_engine`. |
| Rate limiter buckets by proxy IP instead of client | Upstream proxy not in `TRUSTED_PROXY_CIDRS` | Add the proxy CIDR to `TRUSTED_PROXY_CIDRS`. |
| Pinned subnet `10.137.241.0/24` collides with LAN | `setup.sh --check` warns | Set `JARVIS_NET_SUBNET=<free /24>` and update `frontend/nginx.conf` `set_real_ip_from` literals. |
| LAN device pings host but `curl -k https://<IP>:3001` hangs | `DASHBOARD_BIND_HOST=127.0.0.1` | Run `setup.sh` mode 2, or set `DASHBOARD_BIND_HOST=0.0.0.0` and restart dashboard. |
| `setup.sh` Cloudflare Tunnel mode stops at the Zero-Trust warning | You have not acknowledged that a tunnel exposes the instance | Configure your Cloudflare Zero-Trust access policy first, then type `I understand` at the prompt (or pass `--tunnel-ack` for a non-interactive install). The old `JARVIS_TUNNEL_ACK_ZT_CONFIGURED` env hand-edit has been removed. |
| Settings → "Models & Preferences" shows "No config entries" | DB initialized before default config rows were seeded | `docker compose restart paper_ingestion`, then verify: `docker compose exec postgres psql -U jarvis -d jarvis -c "SELECT key FROM user_config WHERE key LIKE 'llm.%';"` — expect 3 rows. |
| Selecting a model returns HTTP 400 "LiteLLM config is read-only" | Model not pulled or config read-only | Pull the model from Settings → Models first. The default smart model is `qwen3:8b` (16 GB VRAM tier). |
| Pulse generates 0 cards but job shows "done" | All enabled sources returned zero candidates, were rate-limited, or are unconfigured | Open Settings → Pulse → Diagnostics. For OpenAlex set `OPENALEX_EMAIL`; for arXiv wait `Retry-After` (≥30 s). |
| Embedding dimension mismatch on startup | Qdrant collection dimension doesn't match the active embedding model | Set `EMBEDDING_MODEL_NAME=qwen3-embedding:4b` and `EMBEDDING_DIMENSION=2560`, pull the model, then run `REEMBED_RECREATE_COLLECTION=true REEMBED_SNAPSHOT_CONFIRMED=true python -m scripts.reembed` if the collection dimension is still wrong. |
| Re-embedding too slow | `scripts/reembed.py` defaults to the HTTP-bound LiteLLM path | Switch to local backend: `REEMBED_BACKEND=local python -m scripts.reembed` (requires sentence-transformers). Benchmark first with `REEMBED_BENCHMARK=true`. |
| `password authentication failed for user "jarvis"` after changing `POSTGRES_PASSWORD` | Postgres bakes the password into its data volume on first init; a later `.env` change is not applied to an existing volume | Reset the volume: `docker compose down && docker volume rm <project>_postgres_data && docker compose up -d` — this destroys local DB data. The `<project>` prefix is your compose project name (the repo directory name by default; visible in `docker compose ps`). Run `setup.sh` afterwards if you need the GPU overlay re-engaged. |
| `docker compose build`/`up` fails with "no space left on device" during install | Docker's data root ran out of space — the cold-install peak can reach several tens of GB depending on hardware tier; see [Disk budget](REQUIREMENTS.md#disk-budget) | Check free space on the data root (not `/`): `df -h $(docker info -f '{{ .DockerRootDir }}')`. Reclaim the local build cache: `docker builder prune -af`. If the data root lives on an LVM volume with spare space in the volume group, grow it: `sudo lvextend -r -L +20G <lv-path>`. Re-run `./setup.sh` — cached images are kept, so a re-run only needs the remaining gap. |
| `docker compose build` prints `pull access denied for jarvis/paper_ingestion` (or a sibling service) before building | Cosmetic — Compose probes the registry for the pinned tag before falling back to a local build | Harmless; ignore it. From v1.1.0 the application images are prebuilt on GHCR and `docker-compose.yml` sets `pull_policy: missing` on them (only the locally-built Langfuse wrapper uses `pull_policy: build`), so this message is only seen on pre-1.1.0 installs or a `--build-local` build. |

---

## API reference

JARVIS exposes a REST API on `paper_ingestion` (:8010) and `learning_engine` (:8011). All routes (except `/health` and `/health/live`) require an `X-API-Key` header. Full interactive documentation is at `/docs` on each service.

---

## Other access options

- **ngrok** — set `CORS_ORIGINS=https://<subdomain>.ngrok.io` and `JARVIS_CERT_SAN` accordingly.
- **Traefik** — add a label-based override next to `docker-compose.yml`. Refer to Traefik's documentation.

---

## See also

- [README.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/README.md) — quick start and high-level orientation.
- [docs/SECURITY.md](SECURITY.md) — threat model, auth boundaries, multi-tenant hardening checklist.
- [docs/known-residual-risks.md](known-residual-risks.md) — acknowledged-but-deferred risks and their reopen criteria.
- [docs/contracts/04-observability.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/docs/contracts/04-observability.md) — Langfuse env-var table, trust boundary, rotation procedure.
