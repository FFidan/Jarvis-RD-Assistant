# Deployment Guide

JARVIS is a self-hosted research assistant. This document covers running it — from the localhost happy path to LAN exposure, Cloudflare Tunnel, TLS, backups, and common failure modes.

For first-time setup, start with [README.md](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/README.md) Quickstart. If the two conflict, this file is canonical.

---

## Solo deployment (recommended for single-user)

```bash
bash scripts/init-secrets.sh   # idempotent; generates API key, config key, HMAC key if absent
docker compose up -d           # waits for postgres → init-migrations → all services
```

> **GPU users:** run `bash setup.sh` instead of the bare `docker compose up -d` above — it engages the GPU overlay. See [GPU acceleration](#gpu-acceleration-optional) for details.

Required `.env` vars (`init-secrets.sh` generates any that are blank):

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
git clone <your-remote>/JARVIS_RD_Assistant.git
cd JARVIS_RD_Assistant
cp .env.example .env
bash scripts/init-secrets.sh
$EDITOR .env     # optional: set TELEGRAM_BOT_TOKEN for Telegram
docker compose up -d
```

> **GPU users:** run `bash setup.sh` in place of the bare `docker compose up -d` above — it engages the GPU overlay. See [GPU acceleration](#gpu-acceleration-optional) for details.

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

The default stack is CPU-safe. Ollama runs on CPU out of the box. On GPU, the first paper analysis takes a few minutes; on CPU-only it can take 30 minutes or more — fully supported, just slower. The first run also pulls 7–11 GB of model data; allow 20–60 minutes on a typical connection. On macOS, Docker containers cannot use the Apple GPU — expect CPU-speed analysis; allocate ≥8 GB to Docker Desktop.

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

After the vLLM container is healthy, point the LiteLLM `smart` alias at it: open **Settings → Models** and update the `smart` model alias to your vLLM endpoint (e.g. `openai/meta-llama/Meta-Llama-3-8B-Instruct` with `api_base` set to your vLLM URL).

---

## Observability (optional, off by default)

LLM-call tracing via Langfuse is opt-in. `OBSERVABILITY_ENABLED` defaults to `false` and the Langfuse SDK is never constructed — zero overhead.

To enable (provisions a loopback-only Langfuse instance, no signup needed):

```bash
# Set LANGFUSE_INIT_USER_PASSWORD in .env first
make observability-up
```

Open `http://localhost:3002` and sign in with `LANGFUSE_INIT_USER_EMAIL` (default `operator@jarvis.local`). Langfuse is a single operator tool, loopback-bound, decoupled from JARVIS user accounts.

Full contract and rotation procedure: [docs/contracts/04-observability.md](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/docs/contracts/04-observability.md) §9.

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

`./setup.sh` runs localhost mode by default. LAN and Cloudflare Tunnel are prompts in the same script.

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

JARVIS supports Docker Secrets for sensitive credentials. Each is read from a file at runtime via a `_FILE`-suffixed env var, keeping plaintext out of the compose environment and shell history. Secrets live in `secrets/` at the repo root (gitignored); `setup.sh` creates and populates them on first run.

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
chmod 600 secrets/jarvis_api_key.txt
docker compose up -d paper_ingestion learning_engine
```

### Web UI configuration

All ongoing configuration goes through the web wizard and Settings — no `.env` editing needed beyond what `setup.sh` writes:

| Setting | Where | Restart? |
|---|---|---|
| SMTP relay | Settings → System → Email / SMTP / onboarding wizard | No |
| Cloud LLM keys (OpenAI, Anthropic, Gemini) | Settings → Models → Cloud Providers | No |
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
chmod 600 secrets/cloudflare_tunnel_token.txt
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

Read-only and idempotent. Verifies Docker Engine, Docker Compose v2, `openssl`, GPU toolkit (informational), and `.env` presence. Exit 0 = PASS, exit 1 = FAIL.

`setup.sh` verifies the Docker *daemon* is reachable (`docker info`), not just that the `docker` binary is installed — both in `./setup.sh --check` and at the start of a real install, before any prompt. If the daemon is down (Docker Desktop not started, `DOCKER_HOST` misconfigured, or missing group permissions), the installer exits immediately with a fix hint instead of crashing mid-wizard.

Re-runs: running `./setup.sh` with an existing `.env` and declining the overwrite prompt no longer exits with services stopped — it keeps your `.env` (secrets, database, and model selection untouched) and starts the stack with it via `docker compose up -d`, honouring the `COMPOSE_FILE` and `COMPOSE_PROFILES` values persisted in `.env`. New installs write `COMPOSE_PROFILES=<selection>` (for example `telegram`, `tunnel`) into `.env`; for older `.env` files without it, setup derives the telegram profile from a non-empty `TELEGRAM_BOT_TOKEN` and prints a notice.

Key flags: `--mode <single|multi>`, `--domain <host>`, `--admin-email <email>`, `--profile <dev|local-https|letsencrypt>` (default `dev`; `letsencrypt` requires `--domain` + `--admin-email`), `--smtp-host/--smtp-user/--smtp-pass-file`. Run `./setup.sh --help` for the full reference. `TELEGRAM_BOT_TOKEN` in the environment enables Telegram automatically.

### Single-user vs multi-user mode

`single` (default) — API-key login, no SMTP required. `multi` — email magic-link, SMTP relay required. Single-user is not an auth downgrade — `JARVIS_API_KEY` still gates all REST calls.

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

After setup, invite the first admin: `https://jarvis.example.com/admin/users`

---

## Update Workflow

```bash
git pull
./update.sh
```

`update.sh` loads pinned versions from `versions.env`, diffs running vs pinned images, prompts to pull stale services and rebuild local containers, and waits up to 180 s per service. On failure it prints: `git checkout HEAD~1 -- versions.env && ./update.sh`.

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

`scripts/backup.sh` is scheduled by the `backup` compose profile (`docker compose --profile backup up -d`).

With the `backup` profile running, admins can also list, download, and trigger an on-demand backup from the WebUI at **Admin → Backups** (the on-demand button signals the sidecar to run immediately). Restore stays a manual host procedure — the page surfaces the commands below as a read-only runbook but never executes them.

JARVIS state lives in more than one place, so each run captures **all four** of the durable stores (per-store files are timestamped together so a single run is internally consistent):

| Store | Output (in `/backups`) | Notes |
|---|---|---|
| `jarvis` Postgres DB | `jarvis_<ts>.sql.gz[.enc]` | Papers, users, config, jobs |
| `litellm` Postgres DB | `litellm_<ts>.sql.gz[.enc]` | API keys, virtual keys, spend ledger |
| Qdrant vectors | `qdrant_<collection>_<ts>.snapshot` | One per collection via Qdrant's snapshot REST API; **best-effort** — if Qdrant is unreachable this is skipped and the Postgres/secrets backups still succeed |
| `secrets/` directory | `secrets_<ts>.tar.gz[.enc]` | The Docker-secret source files — without these an encrypted DB backup cannot be decrypted |

| Env var | Default | Purpose |
|---|---|---|
| `BACKUP_DIR` | `/backups` | Where archives are written inside the container |
| `BACKUP_RETENTION_DAYS` | `7` | Prune interval (applies to every store above) |
| `BACKUP_S3_BUCKET` | empty | Optional S3 destination (skipped if unset; uploads every archive) |
| `BACKUP_INTERVAL_SECONDS` | `86400` | Sleep between backup runs |
| `BACKUP_ENCRYPT_KEYFILE` | `/run/secrets/backup_encrypt_key` | When the file is non-empty, DB dumps and the secrets archive are encrypted at rest (`.enc` suffix) |
| `LITELLM_DATABASE` | `litellm` | Name of the LiteLLM DB to dump |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant base URL for the snapshot API |
| `QDRANT_API_KEYFILE` | `/run/secrets/qdrant_api_key` | Qdrant API key (sent as the `api-key` header) |
| `SECRETS_DIR` | `/secrets` | Read-only mount of the host `./secrets` dir to archive |

The `litellm` DB is owned by the same superuser (`POSTGRES_USER`) that created it, so the same credentials dump both DBs. A failed `pg_dump` aborts the run (non-zero exit) rather than leaving a tiny but well-formed empty archive. The encryption key, Qdrant API key, and `./secrets` source are already wired into the `postgres-backup` sidecar in `docker-compose.yml`. S3 credentials: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (or IAM instance role) scoped to `s3:PutObject`.

> **Without an encryption key the secrets archive is written in the clear** and the script logs a warning — set `BACKUP_ENCRYPT_KEYFILE` (default already points at the `backup_encrypt_key` Docker Secret) for production.

> **Store the backup encryption key OFF-SITE, separately from the `.enc`
> archives.** The secrets archive is encrypted with `backup_encrypt_key` and
> that key is **excluded** from the archive (it is not packed inside the file
> it unlocks). After total host loss you can only decrypt the off-site `.enc`
> backups if you also kept a copy of `backup_encrypt_key` somewhere else
> (a password manager, a sealed envelope, a separate cloud secret). Losing the
> key makes every encrypted backup permanently unrecoverable.

### Restore

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
docker compose stop qdrant
docker compose cp qdrant_papers_YYYYMMDD_HHMMSS.snapshot qdrant:/qdrant/snapshots/restore.snapshot
docker compose start qdrant
docker compose exec qdrant sh -c 'curl -s -X PUT \
  -H "api-key: $(cat /run/secrets/qdrant_api_key)" \
  "http://localhost:6333/collections/papers/snapshots/recover" \
  -H "Content-Type: application/json" \
  -d "{\"location\":\"file:///qdrant/snapshots/restore.snapshot\"}"'
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
| `setup.sh` option 3 exits with a ZT warning | `JARVIS_TUNNEL_ACK_ZT_CONFIGURED=1` not set | Configure your Zero-Trust access policy first, then add the var to `.env`. |
| Settings → "Models & Preferences" shows "No config entries" | DB initialized before default config rows were seeded | `docker compose restart paper_ingestion`, then verify: `docker compose exec postgres psql -U jarvis -d jarvis -c "SELECT key FROM user_config WHERE key LIKE 'llm.%';"` — expect 3 rows. |
| Selecting a model returns HTTP 400 "LiteLLM config is read-only" | Model not pulled or config read-only | Pull the model from Settings → Models first. The default smart model is `qwen3:8b` (16 GB VRAM tier). |
| Pulse generates 0 cards but job shows "done" | All enabled sources returned zero candidates, were rate-limited, or are unconfigured | Open Settings → Pulse → Diagnostics. For OpenAlex set `OPENALEX_EMAIL`; for arXiv wait `Retry-After` (≥30 s). |
| Embedding dimension mismatch on startup | Qdrant collection dimension doesn't match the active embedding model | Set `EMBEDDING_MODEL_NAME=qwen3-embedding:4b` and `EMBEDDING_DIMENSION=2560`, pull the model, then run `REEMBED_RECREATE_COLLECTION=true REEMBED_SNAPSHOT_CONFIRMED=true python -m scripts.reembed` if the collection dimension is still wrong. |
| Re-embedding too slow | `scripts/reembed.py` defaults to the HTTP-bound LiteLLM path | Switch to local backend: `REEMBED_BACKEND=local python -m scripts.reembed` (requires sentence-transformers). Benchmark first with `REEMBED_BENCHMARK=true`. |
| `password authentication failed for user "jarvis"` after changing `POSTGRES_PASSWORD` | Postgres bakes the password into its data volume on first init; a later `.env` change is not applied to an existing volume | Reset the volume: `docker compose down && docker volume rm <project>_postgres_data && docker compose up -d` — this destroys local DB data. The `<project>` prefix is your compose project name (the repo directory name by default; visible in `docker compose ps`). Run `setup.sh` afterwards if you need the GPU overlay re-engaged. |

---

## API reference

JARVIS exposes a REST API on `paper_ingestion` (:8010) and `learning_engine` (:8011). All routes (except `/health` and `/health/live`) require an `X-API-Key` header. Full interactive documentation is at `/docs` on each service.

---

## Other access options

- **ngrok** — set `CORS_ORIGINS=https://<subdomain>.ngrok.io` and `JARVIS_CERT_SAN` accordingly.
- **Traefik** — add a label-based override next to `docker-compose.yml`. Refer to Traefik's documentation.

---

## See also

- [README.md](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/README.md) — quick start and high-level orientation.
- [docs/SECURITY.md](SECURITY.md) — threat model, auth boundaries, multi-tenant hardening checklist.
- [docs/known-residual-risks.md](known-residual-risks.md) — acknowledged-but-deferred risks and their reopen criteria.
- [docs/contracts/04-observability.md](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/docs/contracts/04-observability.md) — Langfuse env-var table, trust boundary, rotation procedure.
