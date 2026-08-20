# Deployment Guide

JARVIS is a self-hosted research assistant. This document covers installation,
remote access, TLS, service configuration, and troubleshooting.

For first-time setup, start with [README.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/README.md) Quickstart. If the two conflict, this file is canonical.

---

## Solo deployment (recommended for single-user)

Use the installer for first-time setup. It generates secrets, asks how you will
access JARVIS (localhost, LAN diagnostics, Tailscale, Cloudflare Tunnel, or
Let's Encrypt), selects
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
| `JARVIS_CONFIG_KEY` | Fernet key; encrypts database-backed integration settings at rest |
| `LITELLM_MASTER_KEY` | 32-byte hex; gates LiteLLM admin endpoints |
| `JARVIS_MODEL_HMAC_KEY` | 64-hex; HMAC-signs Pulse classifier pickle blobs; **auto-generated** — do not hand-edit |

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

OpenAlex requires the free `OPENALEX_API_KEY`; create one at [openalex.org/settings/api](https://openalex.org/settings/api). PubMed works without a key but an NCBI key raises the rate limit.

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

### AMD and Intel GPUs (experimental)

AMD ROCm is selected only when `/dev/kfd` is present. Other AMD and Intel hosts stay on the supported CPU path unless Vulkan is chosen explicitly with `./setup.sh --gpu vulkan`. Both ROCm and Vulkan are experimental, and PDF parsing and reranking stay on CPU regardless of the acceleration path. See the [hardware support matrix](manual/hardware-support-matrix.md) for vendor status and how to report your hardware.

### vLLM overlay (optional, manual)

vLLM is a **separate, optional overlay** — it is not auto-started by `setup.sh`. Use it to serve large models locally at higher throughput than Ollama.

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.vllm.yml --profile vllm up -d
```

After the vLLM container is healthy, expose it through the local LiteLLM route and choose the served model in **Settings → Models → AI models**. The advanced backend and hardware panel shows local runtime guidance, while the Main and Quick model cards remain the assignment surface.

---

## Observability (optional, off by default)

LLM-call tracing, optional OTLP export, and aggregate application logs are
opt-in. `OBSERVABILITY_ENABLED` defaults to `false`; the SDK and log forwarder
are never constructed in the default profile.

To enable (provisions a loopback-only Langfuse instance, no signup needed):

```bash
# Set LANGFUSE_INIT_USER_PASSWORD in .env first
make observability-up
```

Open `http://localhost:3002` and sign in with `LANGFUSE_INIT_USER_EMAIL` (default `operator@jarvis.local`). Langfuse is a single operator tool, loopback-bound, decoupled from JARVIS user accounts. The command also starts Vector and the core application services with a bounded UDP log forwarder to `vector:9000`; view the optional aggregate with `docker compose logs vector`. The forwarder exports only safe metadata, never log messages or user content. Vector does not mount the Docker socket or write infrastructure logs to JARVIS tables. Telegram remains separately profile-gated.

Full contract and rotation procedure: [docs/contracts/04-observability.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/docs/contracts/04-observability.md) §9.

---

## Remote access (optional)

By default the dashboard is reachable only on the machine it runs on
(`http://localhost:3001`): it binds to loopback (`DASHBOARD_BIND_HOST`) and nginx
answers only to a `Host` allowlist (`DASHBOARD_SERVER_NAME`, below). **If you only
use JARVIS on that one machine, skip this section** — nothing needs to be exposed.

To reach it from another device, re-run `./setup.sh`, accept the **Overwrite**
prompt, and select the access route you want. An unattended reconfiguration must
include `--overwrite-env`; without either confirmation, setup starts the existing
configuration unchanged. Setup configures the JARVIS settings, service profiles,
and health checks for the new choice:

- **Guided private HTTPS** uses Tailscale Serve on the same host and keeps the
  dashboard bound to loopback. Setup uses an existing client or offers an
  explicit-consent package install on supported Linux hosts, can ask before
  `sudo tailscale up`, configures Serve, and verifies the final endpoint.
- **A reverse proxy with TLS** on your own public domain — the Caddy `letsencrypt` profile (see *Deployment Modes* below). Note `local-https` (mkcert) is **not** a remote-access option — it is loopback-only, for a locally-trusted `https://localhost:3443` on the JARVIS machine itself.
- **A Cloudflare Tunnel** — outbound-only, no open ports.
- **A mesh VPN with your own proxy** is a manual adapter; provide a named HTTPS
  origin and satisfy the trust contract below.

### Changing access routes safely

Setup treats an access change as one operation and accepts the replacement only
after its exact endpoint passes the health and production-readiness checks. If
a later step fails, setup restores and verifies the last working dashboard and
owned access route. Running setup again resumes that recovery after an
interruption. If recovery cannot be verified automatically, setup stops and
prints exact commands rather than reporting success; follow those commands from
the same checkout, then rerun setup to complete verification. Do not delete the
retained recovery data or edit generated configuration while recovery is
pending.

A proxy supplied with `--public-origin` remains operator-owned. Setup can
restore the dashboard settings and verify that address, but it does not
reconfigure the proxy itself.

### Example: Tailscale

Install Tailscale on family devices, then choose the guided private-HTTPS option
in `./setup.sh`. If the host client is missing on Debian, Ubuntu, Linux Mint,
Pop!_OS, or Fedora with systemd, setup prints the official package-repository
commands and asks before running them. Non-interactive installation requires
`--install-prereqs`. macOS, unsupported distributions, and WSL without systemd
receive manual installation guidance instead.

If the client is signed out, interactive setup asks before `sudo tailscale up`;
Tailscale handles the browser sign-in. JARVIS never handles those credentials.

After authentication, setup sends Tailscale Serve to the host-only listener at
`127.0.0.1:3003` (or the persisted `DASHBOARD_TRUSTED_HOST_PORT`) and probes the
named HTTPS address for JARVIS's exact health marker. A missing client,
cancelled sign-in, or failed Serve command leaves localhost working but exits
nonzero so an unfinished private route is not reported as complete.

Installations configured before v1.2.0 may still have HTTPS 443 pointing at
the old raw listener on port 3001. `jarvis-research doctor` and the update
summary inspect Serve state without changing it. When the complete node-wide
configuration is a single 443 route to that legacy port, they print a targeted
retarget command. Run it only after confirming that the existing route belongs
to this JARVIS installation; shared or custom Serve configurations require
manual inspection and are never reset automatically.

`--public-origin https://<host>` remains the non-interactive/manual adapter for
an HTTPS proxy that the operator already configured. Setup does not install or
configure that proxy, but it verifies the exact JARVIS health marker and exits
nonzero until the address reaches this installation.

---

## Deployment Modes

| Mode | Additional route work | Inbound ports | Transport |
|---|---|---|---|
| **Localhost** | None after the base install | none | Plain HTTP on loopback (`:3001`) |
| **LAN diagnostics** | Firewall or VM-network check when needed | `3001/tcp` on LAN iface | Plain HTTP; remote clients can request only `/health/jarvis` |
| **Guided private HTTPS** (Tailscale Serve) | Depends on account sign-in | none on your host | Real HTTPS at the tailnet hostname; Tailscale owns the certificate |
| **Local HTTPS** (`--profile=local-https`) | Setup creates and trusts a local certificate | `3443/tcp` on loopback | mkcert-issued HTTPS, trusted only by browsers in the same OS trust stores |
| **Cloudflare Tunnel** | Depends on account, hostname, and policy setup | *none* (outbound-only) | HTTPS; Cloudflare edge owns the cert |
| **Let's Encrypt / Caddy** | Starts after DNS and inbound ports are ready | `80/tcp`, `443/tcp` | HTTPS; Caddy ACME edge owns the cert |

The first base install normally spends 20–60 minutes downloading container
images and AI models. Route setup is additional; later starts reuse those files.

`./setup.sh` defaults to localhost. The interactive chooser also offers guided
private HTTPS, LAN diagnostics, Cloudflare Tunnel, and Let's Encrypt. The
selected route sets `APP_BASE_URL`, the host allowlist, and
`JARVIS_ACCESS_MODE`; no hand-edited environment file is required. See [Access
from other devices](manual/access-modes.md).

### Ingress and adapter contract

The dashboard container has two listeners. Container port `3000`, normally
published as host port `3001`, serves the full app only to localhost; remote LAN
clients get only `/health/jarvis`. Trusted HTTPS edges use container port `3002`.
Tailscale reaches that listener through host loopback port `3003`.

Each remote adapter must also agree with JARVIS on the public hostname,
certificate owner, `Host` handling, setup-token handoff, and passkey origin.
Setup configures the listed adapters; a custom proxy must meet the same rules.

| Adapter | Origin / hostname | Cert owner | `Host` rewrite | `APP_BASE_URL` / CORS / `DASHBOARD_SERVER_NAME` | Trusted-proxy | Cookie | Setup-token handoff | Passkey origin |
|---|---|---|---|---|---|---|---|---|
| Cloudflare Tunnel | `TUNNEL_HOSTNAME` configured in Zero Trust | Cloudflare | No — original Host forwarded | Set automatically by `setup.sh` option 4 | Pinned `cloudflared` container reaches the trusted ingress | Secure | URL fragment only after active-tunnel and exact-app checks pass | `TUNNEL_HOSTNAME` |
| Tailscale Serve | Tailnet hostname detected after `tailscale up` | Tailscale | No — original Host forwarded | Set automatically by `setup.sh` option 3 or `--tailscale` | Host loopback reaches the trusted ingress on `${DASHBOARD_TRUSTED_HOST_PORT:-3003}` | Secure | URL fragment only after the exact-app check passes | Exact tailnet hostname |
| Let's Encrypt (Caddy) | `LETSENCRYPT_DOMAIN` | Caddy ACME | Yes — rewritten to `localhost` | Set automatically by `setup.sh` option 5 | Pinned Caddy container reaches the trusted ingress | Secure | URL fragment after the certificate and exact-app gates pass | `LETSENCRYPT_DOMAIN` |
| Local HTTPS (`caddy_local`) | `https://localhost:3443` | mkcert (local trust store only) | Preserved as `localhost:3443` | `CORS_ORIGINS` gains `https://localhost:3443` automatically with `--profile=local-https` | Pinned local Caddy container reaches the trusted ingress | Secure | URL fragment | Exact `https://localhost:3443` origin |
| Custom reverse proxy | Whatever you configure | You | Your choice — must match what you allowlist | Pass its existing HTTPS origin with `--public-origin`; you own the proxy configuration | Forward only through a trusted ingress you have explicitly constrained | Secure (requires HTTPS) | Manual; setup does not install or authenticate the proxy | Exact origin in `APP_BASE_URL`; never a raw IP |

Raw-IP LAN browsing is absent because it is not an app route. It exposes only
`/health/jarvis`; all other remote requests return HTTP 403. See [LAN
diagnostics](manual/access-modes.md#choice-2-lan-diagnostics).

---

## Mode 1 — Localhost

The default mode — see [Solo deployment](#solo-deployment-recommended-for-single-user) above for the commands, or run `./setup.sh` and pick option 1 at the access-mode prompt.

The dashboard is at `http://localhost:3001` (default
`DASHBOARD_HOST_PORT=3001`). Loopback traffic never leaves the machine, and
browsers treat localhost as a secure context for passkeys. Choose
`--profile=local-https` only when you also want a locally trusted
`https://localhost:3443`; setup installs the supported tools with your consent
and prepares the certificate before starting the edge.

### Bootstrapping the first admin (the setup token)

`scripts/init-secrets.sh` generates `JARVIS_SETUP_TOKEN` (a Docker Secret, `secrets/jarvis_setup_token.txt`) on first run. It gates the unauthenticated first-admin bootstrap endpoints via an `X-Setup-Token` header — a second factor on top of "no admin exists yet", closing the window where anyone who reaches the instance before you could create the first account. `setup.sh` prints the click-to-finish link with the token embedded in a URL **fragment** (`.../setup#setup_token=…`), never a query string — a fragment is never sent to the server, so it never lands in access logs, the `Referer` header, or a reverse proxy's request line. Links printed before this fragment migration used a `?setup_token=` query string; the wizard still accepts those for compatibility, but new links are always fragments.

If the browser never received the printed link, the admin-creation step can
accept the token value from the end of the line
`.../setup#setup_token=<this part>`. Use that field only on localhost or a
verified named HTTPS route. Setup and authentication writes are refused over
non-loopback plain HTTP.

In production, an unset `JARVIS_SETUP_TOKEN` fails closed: the bootstrap endpoint refuses every write until the secret exists. Outside production it warns and allows the request, for local/dev convenience.

---

## Mode 2 — LAN

```bash
./setup.sh   # pick option 2
```

`setup.sh` sets `DASHBOARD_BIND_HOST=0.0.0.0`, detects the LAN IPv4
(`--address <ipv4>` overrides it), and adds it to the dashboard allowlist. This
is plain HTTP with no certificate. From another device, only `/health/jarvis`
is available; the dashboard and every setup, sign-in, cookie, and passkey path
return HTTP 403. Use guided private HTTPS for family access.

### When your LAN IP changes

DHCP can assign a different IP after a reboot. The localhost app may still work
while the old LAN health address stops responding.

```bash
# Re-run setup.sh, accept Overwrite, and choose LAN diagnostics again.
./setup.sh
```

### Network precautions

Use LAN diagnostics only on a trusted network. This mode intentionally keeps
`ENVIRONMENT=development` because authenticated routes remain blocked; it writes
the generated API key, encryption key, CORS origins, and host allowlist needed
for the diagnostic endpoint. Setup does not change the host firewall; allow port
3001 only on the intended private interface if a firewall blocks it.

### Rate-limit client-IP trust (automatic)

JARVIS pins the internal Docker network to `10.137.241.0/24` and derives the
trusted dashboard and edge peers from that subnet. Browser-supplied forwarding
headers are discarded at the direct ingress; only the separate trusted edge may
assert HTTPS.

**Subnet collision:** if the default conflicts with another local network,
`setup.sh --check` warns. Set `JARVIS_NET_SUBNET` to a free IPv4 `/27` or larger
network and re-run setup. Setup and update derive the gateway and the highest
five usable addresses; do not edit Compose or nginx.

**`TRUSTED_PROXY_CIDRS`** — Python-layer variable for the XFF walk. The standard
stack trusts loopback and the derived dashboard `/32`. Add another CIDR only
when an external proxy with that exact source address reaches the services.

### Encrypted config key rotation

Database-backed integration settings stored through Settings are encrypted with
`JARVIS_CONFIG_KEY`. Provider, SMTP, and Telegram-bot settings are
deployment-wide; Zotero credentials are per-user. Host Docker secrets are a
separate configuration layer.
Rotation is a maintenance task, not part of a routine update. Before starting,
open **Admin → Backups**, run a backup, and wait until it is marked **Complete**
and **Encrypted**. Keep the matching backup encryption key somewhere off the
server.

From the repository root, run:

```bash
bash scripts/rotate-config-key.sh
```

The script validates every encrypted Settings row before it asks for
confirmation. It then opens a short maintenance window, rotates all rows in one
database transaction, updates both `.env` and the Docker secret file, restarts
the affected services, and waits for them to become healthy. Key values are read
from files rather than placed in command arguments.

If the command is interrupted or reports an error, do not delete its recovery
files and do not edit either key store by hand. Resolve the reported Docker or
service problem, then run the same command again; it resumes from the recorded
phase. Use `bash scripts/rotate-config-key.sh --yes` only in automation where a
current verified backup already exists, because it skips the confirmation
prompt.

On success, the script removes its temporary recovery files. On failure, it
keeps them and prints the safe resume command.

### Docker Secrets

JARVIS supports Docker Secrets for sensitive credentials. Each is read from a file at runtime via a `_FILE`-suffixed env var, keeping plaintext out of the compose environment and shell history. Secrets live in `secrets/` at the repo root (gitignored); `setup.sh` creates and populates them on first run. Each file is mode `644` inside a mode `700` `secrets/` directory: the owner-only directory keeps the files private on the host, while the world-readable file bit lets the non-root service containers read them through the compose bind mount.

Every credential below is created by `scripts/init-secrets.sh`, which `setup.sh`
and `update.sh` both run before any container is started or replaced; the two
Langfuse SDK keys are the exception and are written by
`scripts/gen-langfuse-keys.sh`. Neither script ever overwrites a file that
already exists, so re-running them is safe. Create a file by hand only if you
are not using either script.

The host SMTP fallback is `secrets/smtp_pass.txt`. It is separate from the
deployment-wide encrypted SMTP row saved through Settings; the Settings value
takes effect without a service restart, while changing the host fallback
requires restarting its service consumers.

#### Database roles

Each service connects to PostgreSQL as its own least-privilege role with its own
password, and owns its own schema. There is no shared application superuser: the
`jarvis` role that earlier releases used for everything is `NOLOGIN` after
upgrade, migrations run as a dedicated migrator role, and backup, restore and
erasure each have a role of their own. That is why the inventory below lists
eleven separate database passwords rather than one — losing any single file
affects only the service that uses it, and no single credential can read
another service's schema.

`postgres_legacy_source_password` is the exception: its file is the
`secrets/postgres_password.txt` from before the split. It is read during an
upgrade so the pre-split database can be adopted, and is not a login credential
for any running service.

#### Secret inventory

This table lists every secret `docker-compose.yml` declares. A parity check in
`tests/test_docker_compose_invariants.py` fails if compose and this table
disagree, so a new secret cannot ship undocumented.

| Secret | Purpose |
|---|---|
| `postgres_platform_runtime_password` | Database login for `platform_api` — sign-in, sessions, accounts, configuration |
| `postgres_research_runtime_password` | Database login for `paper_ingestion` |
| `postgres_learning_runtime_password` | Database login for `learning_engine` |
| `postgres_migrator_password` | Database login for the migration job that runs before the services start |
| `postgres_cluster_bootstrap_password` | PostgreSQL superuser password, used to create the roles and schemas and then to finalize migration authority |
| `postgres_backup_reader_password` | Read-only database login for the scheduled backup sidecar |
| `postgres_restore_operator_password` | Database login for the restore sidecar, which needs to drop and recreate databases |
| `postgres_erasure_executor_password` | Database login for the account-erasure worker |
| `litellm_runtime_password` | Database login for the LiteLLM gateway |
| `litellm_migrator_password` | Database login for the LiteLLM gateway's own migrations |
| `postgres_legacy_source_password` | The pre-split `secrets/postgres_password.txt`, read only while upgrading an older installation |
| `platform_identity_private_key` | Ed25519 private key with which `platform_api` signs the identity assertion for each authorized request. It is mounted into that service alone |
| `platform_identity_public_key` | The matching public key, mounted into `paper_ingestion` and `learning_engine` so they can verify an assertion instead of trusting a header |
| `telegram_service_token` | Credential the Telegram service presents to `platform_api` to prove which service it is |
| `research_service_token` | The same, for `paper_ingestion` |
| `learning_service_token` | The same, for `learning_engine` |
| `jarvis_setup_token` | One-time token that authorizes first-admin creation during the setup wizard |
| `jarvis_api_key` | REST API key for operations routes and external clients (minimum 32 characters) |
| `jarvis_model_hmac_key` | HMAC key that signs the Pulse classifier blobs, so a tampered model file is refused |
| `jarvis_config_key` | Fernet key that encrypts integration settings at rest in the database |
| `qdrant_api_key` | API key for the vector database, presented by every service that reads or writes it |
| `litellm_master_key` | Admin credential for the LiteLLM gateway |
| `litellm_salt_key` | Encrypts provider and model keys stored in the LiteLLM admin database — **never rotate**: rotating orphans every stored model key and requires re-entering them |
| `telegram_bot_token` | Bot token from BotFather; created as an empty placeholder when Telegram is not configured |
| `smtp_pass` | Host SMTP fallback password; created as an empty placeholder when SMTP is not configured |
| `backup_encrypt_key` | Encrypts backup archives at rest — required in every environment; backups refuse to run without it, though sets written by older releases without a key stay restorable |
| `cloudflare_tunnel_token` | Tunnel credential; present only when setup was completed with the Cloudflare Tunnel access mode |
| `langfuse_pg_password` | Database password for the optional Langfuse observability service |
| `langfuse_nextauth_secret` | Session signing secret for the optional Langfuse web interface |
| `langfuse_salt` | Encryption salt for the optional Langfuse service |
| `langfuse_init_pk` | Langfuse SDK public key injected into the application services |
| `langfuse_init_sk` | Langfuse SDK secret key injected into the application services |

The Langfuse entries are only used when the `observability` profile is active,
which it is not by default. File permissions, rotation behavior and the
`_FILE` fallback rules are in [`secrets/README.md`](../secrets/README.md).

To rotate the JARVIS API key without exposing it in shell history, generate a
replacement file and then restart every running consumer:

```bash
install -m 644 /dev/null secrets/jarvis_api_key.txt.next
openssl rand -hex 32 > secrets/jarvis_api_key.txt.next
mv secrets/jarvis_api_key.txt.next secrets/jarvis_api_key.txt
docker compose restart paper_ingestion learning_engine
if docker compose ps --status running --services | grep -qx telegram_bot; then
  docker compose restart telegram_bot
fi
```

The old API key stops working after those services restart. Update any external
client that uses it before ending your current local session.

### How the database logins are separated

Each service holds its own PostgreSQL login instead of one shared superuser
account, so a service can only reach the data it owns. This matters to an
operator in three places: which password files exist, what to expect in the
logs at first start, and what to check when a permission error appears.

**Schemas and their owners.** Four schemas — `platform`, `research`, `learning`
and `ops` — are each owned by a role that no service logs in as
(`jarvis_platform_owner` and its three counterparts). Services log in as a
matching runtime role (`jarvis_platform_runtime`, `jarvis_research_runtime`,
`jarvis_learning_runtime`), which can read and write the data but cannot alter
the tables. Each runtime role's `search_path` names its own schema first, so an
unqualified table name resolves there. What a service may reach is decided by
the grants, not by that ordering — the `search_path` is a convenience, not the
boundary.

**The remaining logins are task-scoped** and idle the rest of the time:
`jarvis_migrator` applies schema changes, `jarvis_backup_reader` reads for
backups, `jarvis_restore_operator` writes during a restore,
`jarvis_erasure_executor` completes account erasure, and the two
`jarvis_litellm_*` roles serve the model proxy. `jarvis_cluster_bootstrap`
creates all of them at first start and is not used afterwards.

**One authority has no login at all.** `jarvis_legacy_rollback` owns the
database and holds the privileges an upgrade rollback needs, but nothing
connects as it: the migration that needs it switches into it, and the restore
login reaches it by membership. It has no password and no secret file, so there
is no standing credential to protect.

**At first start** `scripts/postgres-role-bootstrap.sh` creates every role from
the `postgres_*_password.txt` files listed in the secret inventory above. It
refuses to create a role whose password file is empty or missing rather than
creating one without a password, so a failure here means a missing secret file,
not a broken database.

**When a permission error appears,** the role in the message is the one to look
at: `permission denied for schema research` from the Learning service is a
service reaching for data it does not own, which is the boundary working. A
genuine misconfiguration usually shows up as a login failure instead. Note that
the pre-1.2.6 shared `jarvis` login is now `NOLOGIN` and is no longer used by
any service.

### Web UI configuration

All ongoing configuration goes through the web wizard and Settings — no `.env` editing needed beyond what `setup.sh` writes:

| Setting | Where | Restart? |
|---|---|---|
| SMTP relay | Settings → System → Email / SMTP / onboarding wizard | No |
| Cloud LLM providers and routing | Settings → Models → Providers & Routing | No |
| Telegram bot token | Settings → Integrations → Telegram (admin block) | Yes — `docker compose restart telegram_bot` |
| Sign-in method (single ↔ multi-user) | Settings → System → Sign-in Method | No |
| Auto-fetch interval | Settings → Automation | No |

For multi-user (team) deployment and the full trust boundary, see [docs/SECURITY.md](SECURITY.md).

---

## Choice 4 — Cloudflare Tunnel

Exposes JARVIS over an outbound tunnel — no inbound port needed. Edge TLS is terminated by Cloudflare.

**Prerequisites:** a Cloudflare account, a tunnel and public hostname in the
[Zero Trust dashboard](https://one.dash.cloudflare.com/), and these two Access
applications:

1. Protect the main hostname (for example `jarvis.example.com/*`) with your
   normal **Allow** policy.
2. Add a more-specific application for exactly
   `jarvis.example.com/health/jarvis` with **Bypass / Everyone**. Cloudflare
   evaluates the more-specific path first. Bypass disables Access enforcement
   and Access logging for that path, so use it only here: this endpoint returns
   the fixed text `jarvis-rd-assistant` and no account, setup-token, or health
   details. Never bypass `/*` or any API path.

See Cloudflare's documentation for
[path-specific applications](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/)
and the [Bypass policy](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/).
Setup cannot create or authenticate the Cloudflare account or policies.

Run `./setup.sh`, accept **Overwrite** for an existing installation, and choose
**Cloudflare Tunnel**. Setup reads the tunnel token without echoing it, stores it
as a Docker secret, starts the profile, and verifies both an active tunnel
connection and the public hostname.

For unattended setup, put the token in an owner-readable file and pass only its
path:

```bash
./setup.sh --non-interactive --profile=tunnel --tunnel-ack \
  --tunnel-hostname=jarvis.example.com \
  --tunnel-token-file=/secure/path/cloudflare-token
```

```bash
docker compose ps cloudflared               # should show Up
docker compose exec -T dashboard \
  curl -fsS http://cloudflared:2000/ready    # proves an active edge connection
docker compose logs --tail=20 cloudflared   # look for "Connection established"
```

Tunnel routing is configured in the Cloudflare Zero Trust dashboard: set the
public hostname to route to `http://dashboard:3002`. Tunnel setup enables client
IP handling automatically. Do not hand-set `JARVIS_TRUST_CF_CONNECTING_IP` for a
different proxy: nginx accepts Cloudflare's client header only on the trusted
listener and only from the pinned `cloudflared` container.

The external probe expects JARVIS's exact health marker. DNS or TLS failure,
the wrong application, and a Cloudflare Access or WAF challenge are reported as
different states. An Access page means the exact health-path application or its
Bypass policy is missing; a WAF challenge means that exact path still needs a
narrow challenge exception. Neither response proves that the tunnel reaches
JARVIS, so setup keeps the token-bearing finish link on localhost and exits
nonzero until the marker itself is returned.

---

## Other private-network adapters

**Tailscale Funnel** is an internet-facing adapter distinct from the guided
private Serve route. It is not configured by JARVIS. See the [Tailscale Funnel
documentation](https://tailscale.com/kb/1223/funnel/) and satisfy the custom
proxy contract below.

**Another VPN or reverse proxy** can work if it provides a named HTTPS origin
and forwards only to the trusted loopback ingress. A VPN that merely exposes
plain `http://<ip>:3001` provides diagnostics, not JARVIS authentication.

---

## TLS / Certificates

The `dashboard` container does not terminate TLS. It serves plain HTTP on two
internal listeners: `:3000` for localhost and the health-only LAN route, and
`:3002` for trusted HTTPS edges. Host port `3001` maps to `:3000`; Tailscale
Serve reaches `:3002` through host loopback port `3003`. Caddy, Cloudflare, or
Tailscale owns the certificate before forwarding to the trusted listener.

`JARVIS_SKIP_SELFSIGNED_GEN` and `JARVIS_CERT_SAN` belong to an older design.
Current dashboard containers skip self-signed generation and mount `./certs/`
read-only, so those variables do not enable HTTPS.

### Local HTTPS (mkcert)

```bash
./setup.sh --profile=local-https
```

Setup shows the package plan and asks before installing mkcert and its browser
trust dependency. For a non-interactive cold install, add
`--non-interactive --install-prereqs`. It then runs `scripts/init-mkcert.sh`,
which idempotently installs the local CA trust and issues a certificate for `jarvis.localhost`,
`localhost`, `127.0.0.1`, and `::1` only. This is a loopback-only certificate,
not usable for LAN or public access. Setup advertises `https://localhost:3443`
only after the exact JARVIS marker validates both against that mkcert CA and the
host's normal trust store; otherwise it exits nonzero with the localhost HTTP
fallback. The script auto-regenerates when the existing certificate expires
within 30 days.

Trust stays inside the operating system where setup ran. A CA created in WSL
does not trust a Windows browser, and a CA created inside a VM does not trust a
browser outside the VM; mkcert trust does not cross that boundary. Use the
default `http://localhost:3001` inside the same OS, or guided private HTTPS for
family devices.

For setup through SSH or on a headless VM, the installer prints an HTTP
localhost finish link for the outside browser and the exact SSH command that
forwards it to the dashboard's loopback HTTP port. HTTP stays inside the
encrypted SSH connection; it is not exposed on the LAN. Keep the address as
printed instead of importing the VM's local CA into another computer.

The local HTTPS edge sends `Strict-Transport-Security: max-age=0`, which clears
any earlier policy instead of enabling HSTS. Browsers apply HSTS to a hostname
across ports, so a positive policy received from `https://localhost:3443` would
also rewrite the supported `http://localhost:3001` fallback and send it to the
wrong TLS port.

`make certs` remains the advanced repair command. To force early regeneration:

```bash
rm ./certs/cert.pem ./certs/key.pem
make certs
docker compose up -d caddy_local
```

### Let's Encrypt (Caddy profile)

```bash
# .env
LETSENCRYPT_DOMAIN=jarvis.example.com
LETSENCRYPT_EMAIL=you@example.com
CORS_ORIGINS=https://jarvis.example.com,http://localhost:3001

docker compose --profile letsencrypt up -d caddy
```

Point DNS at the host and ensure ports 80/443 are reachable. Caddy terminates public TLS and reverse-proxies to the dashboard on the internal Docker network, rewriting `Host` to `localhost`. The ACME certificate lives in the `caddy_data`/`caddy_config` named volumes, managed entirely by Caddy — there is nothing under `./certs/` to hand-edit or import for this path. `setup.sh` waits for the certificate to be issued (up to 120s) before reporting success or printing the setup link.

The public Caddy edge sends a one-year HSTS policy for the configured hostname
only. JARVIS does not enable `includeSubDomains` or browser preload: either choice
belongs to the domain owner and is safe only after every affected hostname
already supports HTTPS. Preload submission is a separate browser-vendor process,
not a Caddy setting.

### Importing your own certificate

There is no supported "drop in a certificate" path for the dashboard container. Two options:

- **Local HTTPS edge (loopback only):** overwrite `./certs/cert.pem` and `./certs/key.pem` with your own cert/key pair, then restart the edge that reads them: `docker compose up -d caddy_local`. This only affects `https://localhost:3443`.
- **A public certificate:** use the Let's Encrypt profile above (fully automatic), or front JARVIS with your own reverse proxy under the [custom-proxy trust contract](#ingress-and-adapter-contract) — the certificate then belongs to that proxy, not to JARVIS.

---

## Non-interactive / Automated Installer

`setup.sh` supports `--non-interactive` for CI pipelines and cloud-init scripts. Run `./setup.sh --help` for the full flag reference.

Use `./setup.sh --non-interactive` for a complete unattended installation: it
owns access-mode selection, prerequisites, TLS profiles, and route verification.
`scripts/jarvis-setup.sh` is a deprecated compatibility forwarder for older
local-development automation. It delegates to the same `setup.sh`
development-profile path and accepts only `--skip-disk-check`; new automation
should call `setup.sh` directly.

### Pre-flight check

```bash
./setup.sh --check
```

Read-only and idempotent. Verifies Docker Engine, Docker Compose v2.24.4 or
newer, `openssl`, `curl`, Python 3, GPU toolkit (informational), and `.env`
presence. With `--profile=local-https`, it also checks mkcert and browser trust
tooling. Exit 0 = PASS, exit 1 = FAIL. The pre-flight path never installs
packages, writes `.env`, or starts services, even if `--install-prereqs` is also
present.

`setup.sh` verifies the Docker *daemon* is reachable (`docker info`), not just that the `docker` binary is installed — both in `./setup.sh --check` and at the start of a real install, before any prompt. If the daemon is down (Docker Desktop not started, `DOCKER_HOST` misconfigured, or missing group permissions), the installer exits immediately with a fix hint instead of crashing mid-wizard.

### Guided prerequisite installation

If Docker Engine, Docker Compose v2.24.4 or newer, `openssl`, `curl`, Python 3,
or the selected route's host tools are missing, setup prints the package-manager
commands it can run before changing the host. Local HTTPS adds mkcert and the
OS browser-trust package. After showing the plan, setup can install the missing
packages on supported hosts. Interactive setup asks for consent. Non-interactive
setup runs the plan only with `--install-prereqs`.

```bash
# Preflight only; never installs packages
./setup.sh --check

# Non-interactive install; writes configuration, downloads files, and starts JARVIS
./setup.sh --non-interactive --profile=dev

# Interactive setup; prompts before running supported apt/Homebrew commands
./setup.sh

# Unattended setup that is allowed to run the reviewed prerequisite plan
./setup.sh --non-interactive --install-prereqs --profile=dev
```

`--check` is the only read-only setup mode. A non-interactive run without
`--install-prereqs` refuses missing host packages, but otherwise performs the
installation.

General prerequisites support Debian/Ubuntu-style `apt-get`, Fedora `dnf`, and
macOS Homebrew where applicable. For an unsupported host or package, setup
stops with manual installation guidance instead of guessing. Tailscale
installation is limited to supported apt/dnf Linux hosts with systemd. macOS,
unsupported distributions, and WSL without systemd receive manual Tailscale
guidance. A non-interactive install still cannot complete account sign-in: run
`sudo tailscale up`, then rerun setup with `--tailscale`.

Re-runs: running `./setup.sh` with an existing `.env` and declining the overwrite prompt no longer exits with services stopped — it keeps your `.env` (secrets, database, and model selection untouched) and starts the stack with it via `docker compose up -d`, honouring the `COMPOSE_FILE` and `COMPOSE_PROFILES` values persisted in `.env`. New installs write `COMPOSE_PROFILES=<selection>` (for example `telegram`, `tunnel`) into `.env`; for older `.env` files without it, setup derives the telegram profile from a non-empty `TELEGRAM_BOT_TOKEN` and prints a notice.

Key flags include `--mode <single|multi>`, `--tailscale`,
`--install-prereqs`, `--domain <host>`, `--admin-email <email>`, and
`--profile <dev|local-https|tunnel|letsencrypt>`. Run `./setup.sh --help` for the full
reference. `TELEGRAM_BOT_TOKEN` in the environment enables Telegram
automatically.

### Single-user vs multi-user mode

`single` (default) makes the sign-in screen offer API-key login first and does not require SMTP. `multi` makes the sign-in screen offer email magic-link login first; configure and test an SMTP relay to have JARVIS email magic links directly, or leave it unset and the Admin → Invite dialog returns a manual sign-in link to hand to each new user instead. The mode is a login-method preference, not a tenancy switch: user/library scoping and admin invite capability remain governed by sessions, roles, and route-level authorization. In either mode, signed-in users can additionally register **passkeys** (WebAuthn) for future sign-ins; passkeys are bound to the exact origin in `APP_BASE_URL` and require HTTPS for a public origin — see the [User Guide → Passkeys](manual/passkeys.md).

### Copy-paste examples

```bash
# Local development / CI smoke test:
./setup.sh --non-interactive --profile=dev

# Local mkcert HTTPS (loopback only; installs reviewed prerequisites if needed):
./setup.sh --non-interactive --install-prereqs --profile=local-https

# Production with Let's Encrypt:
umask 077
printf '%s' "$MY_SMTP_PASS" > ./smtp-password.txt
./setup.sh --non-interactive \
  --domain=jarvis.example.com \
  --admin-email=ops@example.com \
  --profile=letsencrypt \
  --smtp-host=smtp.resend.com \
  --smtp-user=resend \
  --smtp-pass-file=./smtp-password.txt
rm ./smtp-password.txt
```

After setup, open the exact **Finish setup** link printed by the installer. It
carries the one-time token needed to create the initial admin; opening the bare
dashboard URL does not. Once signed in as an admin, invite additional users at
`https://jarvis.example.com/admin/users`.

---

## Update Workflow

Maintained in-place update support starts at v1.2.0. Installations running
v1.2.2 or later use the lifecycle command:

```bash
jarvis-research update
```

It verifies the target release, protects data-changing migrations with a signed
restore point, and can resume an interrupted run. The [command-line
guide](manual/cli.md#how-update-works) is the canonical update runbook.

A maintained installation running v1.2.0 or v1.2.1 needs the [v1.2.2 update
bootstrap](manual/cli.md#updating-from-a-release-before-v122) once. It loads the
v1.2.2 lifecycle command before the migration check so the required signed
restore point includes both databases, uploaded PDFs and the data-coupled
secrets. The same immutable bootstrap remains available as a separate legacy
bridge from v1.1.3, which is outside the maintained update window. The command
guide describes that bridge, the constrained manual fallback, and rollback
rules; direct v1.1.3-to-current update is not promised.

### Upgrade notes

**Telegram pairing (breaking change).** The Telegram bot now identifies chats
exclusively via the `/pair` code flow. To pair: open the dashboard → Settings
→ Integrations → Telegram, generate a one-time code, and send `/pair <code>`
to the bot. The retired `/start PAIR_<code>` pairing path no longer works.

**Telegram identity boundary.** Telegram is a database-free REST client. It
does not hold PostgreSQL or configuration-encryption credentials and does not
forward a generic impersonation header. Platform supplies only the route-bound
assertion or service capability required by the called API.

**Ownership backfill.** The NULL-owner backfill for pre-existing product rows
is part of the schema-101 baseline in `db/init.sql`, not a startup migration.
The one-shot migrator applies ownership migration 0105, described next.

**Instance-owner migration (0105).** An upgraded instance with exactly one live
administrator assigns that account as the owner automatically. An instance with
two or more live administrators remains deliberately unresolved. After startup,
run `jarvis-research owner status`; if repair is required, choose the account
explicitly with `jarvis-research owner set <admin-email>`. The Admin → Users page
then marks the owner, protects it from demotion or removal, and permits a
database-managed owner to transfer to another live administrator after exact
email confirmation.

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

The default stack takes encrypted restore points daily. Administrators can run,
download, retain, and restore them under **Admin → Backups**. Keep a copy of
`secrets/backup_encrypt_key.txt` off the server and separate from downloaded
archives; the key is not included in the archive it unlocks.

The [backup and restore guide](manual/backup-and-restore.md) is the canonical
runbook for backup contents, same-host restore, fresh-host browser recovery,
headless recovery, and failure handling.

### Restore capability boundaries

The standard stack separates request handling from destructive restore work:

| Service | Restore-related access |
|---|---|
| `paper_ingestion` | Reads backup archives, queues a browser-confirmed restore intent, and exposes the authenticated admin API. It has no restore inbox, host-secret write mount, or Docker socket. |
| `postgres-backup` | Scheduled read-only backup, inventory, and retention work. It has no restore credential or restore authority. |
| `postgres-restore` | A host-started transient no-listener job. It reconstructs roles, owners, grants, defaults, and search paths, migrates, then permits writers to resume. |
| `restore-uploader` | Writes only allowlisted uploads to the restore inbox and reads a hashed, expiring upload grant from the trigger volume. It has no database credentials, application secrets, or Docker socket. |

Learning Engine and Telegram receive the maintenance trigger read-only. None of
the restore roles or optional Vector log collector mounts the Docker socket.

Do not restore with raw database drops, archive extraction, or volume deletion.
Those paths bypass the signed manifest, safety backup, staged swap, and secret
reconciliation. Use the linked runbook even when the web app cannot start.

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
| `NET::ERR_CERT_AUTHORITY_INVALID` on `https://localhost:3443` | The browser is outside the VM/WSL environment, or trust is missing on the JARVIS host | Outside the VM, use setup's SSH command and exact HTTP localhost finish link. On the JARVIS host itself, re-run `./setup.sh --profile=local-https`; for manual repair, run `make certs`, then `docker compose up -d caddy_local`. |
| `SSL_ERROR_RX_RECORD_TOO_LONG` opening a LAN address | The browser is trying HTTPS against the plain-HTTP diagnostic port | Check `http://<lan-ip>:3001/health/jarvis`. Use guided private HTTPS for the app. |
| A LAN health check works but the dashboard returns 403 | This is the intended raw-LAN boundary | Open `http://localhost:3001` on the host or the named HTTPS address printed by setup. |
| Tunnel works but dashboard 404s at `/` | Tunnel routes to wrong port | In Cloudflare Zero Trust, confirm the public hostname routes to `http://dashboard:3002`. |
| Tunnel traffic is rate-limited as one proxy client | The current installation was not completed with the Cloudflare Tunnel choice, or its pinned ingress settings are stale | Re-run `./setup.sh`, accept **Overwrite**, and choose Cloudflare Tunnel again. Do not hand-edit `JARVIS_TRUST_CF_CONNECTING_IP`. |
| Rate limiter buckets by proxy IP instead of client | Upstream proxy not in `TRUSTED_PROXY_CIDRS` | Add the proxy CIDR to `TRUSTED_PROXY_CIDRS`. |
| Pinned subnet `10.137.241.0/24` collides with LAN | `setup.sh --check` warns | Set `JARVIS_NET_SUBNET` to a free IPv4 `/27` or larger network, re-run setup, and accept **Overwrite**; setup derives the addresses. |
| LAN device pings host but `curl http://<IP>:3001/health/jarvis` fails | The dashboard remains loopback-bound or a firewall blocks port 3001 | Re-run setup, accept **Overwrite**, and choose LAN diagnostics; if it still fails, allow port 3001 only on the trusted LAN interface. |
| `jarvis-research doctor` reports a legacy Tailscale target | A pre-v1.2.0 Serve route may still target raw port 3001 instead of trusted port 3003 | Confirm that the node-wide HTTPS 443 route belongs to this installation, then run the exact targeted command printed by doctor. Do not reset shared Serve state. |
| `setup.sh` Cloudflare Tunnel mode stops at the Zero-Trust warning | You have not acknowledged that a tunnel exposes the instance | Configure your Cloudflare Zero-Trust access policy first, then type `I understand`. For unattended setup, use `--profile=tunnel` with `--tunnel-ack`, `--tunnel-hostname`, and `--tunnel-token-file`. The old environment hand-edit is no longer used. |
| Settings → "Models & Preferences" shows "No config entries" | DB initialized before default config rows were seeded | `docker compose restart paper_ingestion`, then verify: `docker compose exec postgres psql -U jarvis -d jarvis -c "SELECT key FROM user_config WHERE key LIKE 'llm.%';"` — expect at least the model rows `llm.smart_model` and `llm.fast_model`. Ollama-backed roles add context-size rows alongside them, both deployment-wide (`llm.smart_num_ctx`) and per-machine (`llm.<machine_id>.smart_num_ctx`), so the exact number of keys varies with the deployment. An empty result means the seed did not run. |
| Selecting a model returns HTTP 400 "LiteLLM config is read-only" | Model not pulled or config read-only | Pull the model from Settings → Models first. Setup selects the smart model for the detected [hardware tier](manual/hardware-and-models.md#hardware-tiers-and-default-models); a manually copied `.env.example` uses `qwen3:8b` only as its template fallback. |
| Pulse generates 0 cards but job shows "done" | All enabled sources returned zero candidates, were rate-limited, or are unconfigured | Open Settings → Pulse → Diagnostics. For OpenAlex set `OPENALEX_API_KEY`; for arXiv wait `Retry-After` (≥30 s). |
| Embedding dimension mismatch on startup | Qdrant collection dimension doesn't match the active embedding model | Stop and follow [Changing the embedding model](manual/changing-embedding-model.md). It covers the required snapshot, exact configuration alignment, deliberate collection recreation, and verification. |
| Re-embedding too slow | `scripts/reembed.py` defaults to the HTTP-bound LiteLLM path | Benchmark before migrating and use the exact recovery guidance in [Changing the embedding model](manual/changing-embedding-model.md). The local backend requires sentence-transformers. |
| `password authentication failed` for a `jarvis_*` database role on startup | The file under `secrets/` no longer matches the password the database holds for that role | Do not delete the database volume. Each service has its own role and its own password file, so the failing role names the file to fix: restore that one file from your backup or revert the accidental change, then run `jarvis-research doctor`. Use the guided restore if the original secret is unavailable. The shared `jarvis` login of earlier releases is `NOLOGIN` after upgrade and is no longer used by any service. |
| `docker compose build`/`up` fails with "no space left on device" during install | Docker's data root ran out of space | Default installs require about **27–54 GB** for images and tier-selected models; custom models may require more. Run `./setup.sh --check`, inspect the data-root path it reports, and free or add space through the host's Docker storage controls. Then re-run setup; already downloaded layers are reused. |
| `docker compose build` prints `pull access denied for jarvis/paper_ingestion` (or a sibling service) before building | Cosmetic — Compose probes the registry for the pinned tag before falling back to a local build | Harmless; ignore it. From v1.1.0 the application images are prebuilt on GHCR and `docker-compose.yml` sets `pull_policy: missing` on them (only the locally-built Langfuse wrapper uses `pull_policy: build`), so this message is only seen on pre-1.1.0 installs or a `--build-local` build. |
| The `telegram_bot` container restarts forever and the bot never answers | The published telegram-bot images for **v1.2.0 through v1.2.3** were missing a dependency the bot imports at startup | Confirm with `docker compose logs telegram_bot` — the affected images exit with `ModuleNotFoundError: No module named 'sentry_sdk'`. Upgrade to v1.2.4 or later with `./update.sh`; no configuration change is needed. |
| `./update.sh` stops partway through replacing containers with a Compose `secret not found` error naming `smtp_pass` | Releases since v1.1.3 mount `secrets/smtp_pass.txt` as a Docker secret, but installations first set up **before v1.1.3** never had the file created, and upgrades before v1.2.4 did not create it either | Confirm with `ls secrets/smtp_pass.txt` (the file is absent). Run `bash scripts/init-secrets.sh` to create the missing placeholder files, then re-run `./update.sh`. From v1.2.4 the updater creates required secret files before pulling images or touching any container. |

---

## API reference

JARVIS exposes REST APIs on `paper_ingestion` (:8010) and `learning_engine`
(:8011). Product routes use a browser session; internal operations routes use
the API key; setup and health endpoints have narrower route-specific gates.
Interactive OpenAPI documentation is available at `/docs` on each service to a
local operator.

---

## Other access options

Both of these are the **custom reverse proxy** row of the [trust contract table](#ingress-and-adapter-contract) — JARVIS does not configure or verify either, so you own the hostname/CORS/allowlist agreement:

- **ngrok** — set `CORS_ORIGINS=https://<subdomain>.ngrok.io`, add the ngrok hostname to `DASHBOARD_SERVER_NAME`, and set `APP_BASE_URL` to the ngrok URL.
- **Traefik** — add a label-based override next to `docker-compose.yml`. Refer to Traefik's documentation.

---

## See also

- [README.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/README.md) — quick start and high-level orientation.
- [docs/SECURITY.md](SECURITY.md) — threat model, auth boundaries, multi-tenant hardening checklist.
- [docs/known-residual-risks.md](known-residual-risks.md) — acknowledged-but-deferred risks and their reopen criteria.
- [docs/contracts/04-observability.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/docs/contracts/04-observability.md) — Langfuse env-var table, trust boundary, rotation procedure.
