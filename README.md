# JARVIS RD Assistant

A self-hosted AI research assistant that delivers citation-backed paper briefings, enforces long-term knowledge retention through spaced repetition, and provides lightweight project management -- all via a React dashboard and optional Telegram push notifications.

## What It Does

JARVIS is designed for researchers who track multiple topics, read (or should read) dozens of papers per week, and want an assistant that doesn't hallucinate. It runs entirely on your own hardware with local LLMs via Ollama, or optionally connects to cloud providers (OpenAI, Anthropic) through LiteLLM.

**Core features:**

- **Research Pulse** -- Discovers papers from arXiv and Semantic Scholar based on your topics, downloads PDFs, chunks and embeds them for semantic search, generates verified summaries with exact quotes, and supports RAG-powered Q&A across your entire paper library.

- **Learning Engine** -- Generates flashcards from paper findings, schedules reviews using the FSRS spaced repetition algorithm, and tracks retention analytics so insights stick long-term.

- **Project Manager** -- Lightweight task and milestone tracking with paper linking, deadline warnings, and progress monitoring.

- **My Day** -- Daily productivity command center surfacing today's tasks, project progress, and due flashcards in one view, with per-task Focus buttons to start a Pomodoro session.

- **Pomodoro Timer** -- Wall-clock based work/break timer with pause/resume, browser notifications, auto-logging of completed sessions to focus history, and configurable durations.

- **Discovery & Pulse** -- Overnight proactive discovery of new papers from arXiv, Semantic Scholar, OpenAlex, and PubMed. Scores candidates against your research interests using embedding similarity plus LLM relevance ranking, then delivers a small curated card deck each morning via the My Day view and optional Telegram. Lightweight 👍/👎/💾 feedback on cards shapes tomorrow's recommendations.

### Key Design Choices

- **Anti-hallucination pipeline**: Every LLM-generated finding must include an exact verbatim quote and page number. A 4-layer verification pipeline checks quotes against the source PDF. Unverifiable claims are discarded, never corrected.
- **Local-first**: Runs on Ollama with no cloud dependency. LiteLLM provides a unified gateway so you can swap between local and cloud models without code changes.
- **Hybrid search**: BM25 full-text search fused with Qdrant vector search via reciprocal rank fusion, then reranked with a cross-encoder model for high-precision retrieval.

## Architecture

```
                         ┌─────────────────────────────────┐
                         │      React Dashboard (:3001)     │
                         │   nginx reverse proxy → backends │
                         └────────────┬────────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                                    ▼
        ┌───────────────────┐              ┌───────────────────┐
        │  Paper Ingestion  │              │  Learning Engine  │
        │   FastAPI (:8000) │              │   FastAPI (:8001) │
        │                   │              │                   │
        │ • Paper discovery │              │ • Card generation │
        │ • PDF processing  │              │ • FSRS scheduling │
        │ • Embedding/RAG   │              │ • Review tracking │
        │ • Summarization   │              │ • Project mgmt    │
        │ • Knowledge graph │              │ • Analytics        │
        └──────┬────────────┘              └──────┬────────────┘
               │                                   │
    ┌──────────┼───────────────────────────────────┼──────────┐
    │          ▼              ▼            ▼       ▼          │
    │   ┌──────────┐   ┌──────────┐  ┌─────────┐             │
    │   │ Postgres │   │  Qdrant  │  │ LiteLLM │             │
    │   │  (:5432) │   │  (:6333) │  │ (:4000) │             │
    │   └──────────┘   └──────────┘  └────┬────┘             │
    │                                      │                  │
    │                                 ┌────▼────┐             │
    │                                 │  Ollama │             │
    │                                 │ (:11434)│             │
    │                                 └─────────┘             │
    │                    Docker network: jarvis                │
    └─────────────────────────────────────────────────────────┘
```

**Additional services:** n8n (optional workflow automation, `--profile n8n`), Telegram bot (optional, `--profile telegram`).

## Quick Start

```bash
git clone https://github.com/FFidan/Jarvis-RD-Assistant.git
cd Jarvis-RD-Assistant
./setup.sh
```

`setup.sh` generates all secrets, asks 2 questions (access mode, optional Telegram token), and starts the Docker stack. Open the dashboard URL it prints and complete setup via the guided 6-step wizard.

- **Upgrading:** `git pull && ./update.sh`
- **Global access:** Re-run `./setup.sh` and pick option 3 (Cloudflare Tunnel — free, no inbound ports).

First boot pulls ~10 GB of Ollama models (`mistral-nemo`, `qwen3:4b`, `nomic-embed-text`); watch progress with `docker compose logs -f ollama`. The dashboard uses HTTPS with a self-signed cert on first boot — click through the browser warning.

### GPU Acceleration (optional)

If you have an NVIDIA GPU, install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) before running `setup.sh`; the Ollama container will pick it up automatically.

## Advanced Configuration

If you would rather not run `setup.sh`, you can bring the stack up by hand. Copy the env template, generate secrets with `openssl rand -hex 32`, optionally `source versions.env` (or rely on the compose fallbacks), then `docker compose up -d`:

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD, N8N_ENCRYPTION_KEY, N8N_JWT_SECRET,
# LITELLM_MASTER_KEY, and JARVIS_API_KEY with: openssl rand -hex 32
bash scripts/init-dirs.sh
source versions.env    # optional — docker-compose.yml has fallbacks
docker compose up -d
```

Then open the dashboard URL (default `https://localhost:3001`) and run the 6-step wizard. Every secret you generate here is equivalent to what `setup.sh` would have produced; the wizard still handles topics, models, and Telegram pairing.

## Upgrading

`versions.env` is the source of truth for pinned image versions; every Docker image in `docker-compose.yml` uses `${VAR:-fallback}`, so committing a new pin is the upgrade. `update.sh` compares the pinned versions against what is currently running, prints a diff, and prompts before pulling. To **rollback** after a bad upgrade:

```bash
git checkout HEAD~1 -- versions.env && ./update.sh
```

Never auto-rollback; always review the diff first.

## Global Access

The supported way to reach JARVIS from outside your LAN is **Cloudflare Tunnel** — free, outbound-only, no router port forwarding, terminates TLS upstream so the self-signed cert warning goes away. Create a tunnel at [dash.cloudflare.com](https://dash.cloudflare.com) → Zero Trust → Networks → Tunnels, copy the token, then either re-run `setup.sh` and pick mode 3, or paste `CLOUDFLARE_TUNNEL_TOKEN=<token>` into `.env` and run:

```bash
docker compose --profile tunnel up -d
```

Alternatives, in rough order of simplicity: **Tailscale** (mesh VPN, zero config on every device), **Caddy + Let's Encrypt** (real public hostname, you manage the certs and DNS), or **SSH tunnel** (`ssh -L 3001:localhost:3001 user@host`, ad-hoc only). None of these are scripted — use Cloudflare Tunnel unless you have a specific reason not to.

## Configuration

### Required Variables

| Variable | Description |
|----------|-------------|
| `POSTGRES_PASSWORD` | Database password |
| `N8N_ENCRYPTION_KEY` | n8n credential encryption key |
| `LITELLM_MASTER_KEY` | LiteLLM API gateway key |
| `JARVIS_API_KEY` | API key for backend auth. Must be at least 32 characters in production. Generate with `openssl rand -hex 32`. |
| `ENVIRONMENT` | Set to `production` for any non-local deployment. In `production`, the service refuses to start if `DEV_MODE=true` or if `JARVIS_API_KEY` is unset / shorter than 32 chars. |

### Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | _(empty)_ | Enable OpenAI models via LiteLLM |
| `ANTHROPIC_API_KEY` | _(empty)_ | Enable Anthropic models via LiteLLM |
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | Telegram bot (requires `--profile telegram`) |
| `TELEGRAM_CHAT_ID` | _(empty)_ | Your Telegram chat ID |
| `OLLAMA_MODELS` | `mistral-nemo,qwen3:4b,nomic-embed-text` | Models to pull on first start |
| `EMBEDDING_MODEL` | `embed` | LiteLLM alias for embedding model |
| `EMBEDDING_DIMENSION` | `768` | Must match the embedding model |
| `DASHBOARD_PASSWORD` | _(empty)_ | Dashboard login password (empty = no auth) |
| `DEV_MODE` | `false` | **⚠️ Bypasses ALL authentication on every endpoint when `true`.** Only for local development. The service refuses to start with `DEV_MODE=true` if `ENVIRONMENT=production`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `DASHBOARD_HOST_PORT` | `3001` | Host port for the dashboard |
| `PAPER_INGESTION_HOST_PORT` | `8010` | Host port for paper ingestion API |
| `LEARNING_ENGINE_HOST_PORT` | `8011` | Host port for learning engine API |
| `OPENALEX_API_KEY` | _(empty)_ | Enable OpenAlex as a paper discovery source. Free key at [openalex.org](https://openalex.org). |
| `PUBMED_API_KEY` | _(empty)_ | Upgrade PubMed rate limit from 3 to 10 requests per second. Free key from [NCBI](https://www.ncbi.nlm.nih.gov/home/develop/api/). |
| `UNPAYWALL_EMAIL` | _(empty)_ | Required by [Unpaywall](https://unpaywall.org) to resolve free legal PDFs for paywalled papers. Any email address. |

See [`.env.example`](.env.example) for the full list with comments.

### Telegram Bot Setup (optional)

The Telegram bot delivers daily paper digests, Pulse cards with 👍/👎/💾 rating buttons, and answers RAG questions over your library from your phone. It uses **long-polling**, so no inbound port needs to be opened on the host.

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts → copy the token it gives you.
2. Start a DM with your new bot and send any message (e.g. `hi`).
3. Get your chat ID:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[0].message.chat.id'
   ```
4. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_CHAT_ID=<your numeric chat ID>
   ```
5. Start the bot:
   ```bash
   docker compose --profile telegram up -d
   ```
6. Send `/start` to the bot — it should reply.

**Security note on `TELEGRAM_CHAT_ID`:** The bot only checks this single chat ID. If you point it at a **group chat**, *any member of that group can send commands and see your papers*. For personal use, keep the bot in a private DM. For a shared setup, add a per-user allowlist in `services/telegram_bot/app/handlers/` (not currently supported out of the box).

## Remote Access (LAN)

By default every port in [docker-compose.yml](docker-compose.yml) binds to `127.0.0.1`, so the dashboard is only reachable from the host machine. To access it from another device on your LAN, drop a `docker-compose.override.yml` next to the tracked compose file (it is already in `.gitignore`):

```yaml
services:
  dashboard:
    ports:
      - "0.0.0.0:3001:3000"
```

Then `docker compose up -d`. From another device, open `http://<host-lan-ip>:3001`. You only need to expose the **dashboard** — its nginx reverse proxies to the backends over the internal Docker network, so `paper_ingestion`, `learning_engine`, `postgres`, `qdrant`, and `ollama` stay localhost-only.

**Before exposing anything beyond your own workstation, set all of these in `.env`:**

```
ENVIRONMENT=production
DEV_MODE=false
JARVIS_API_KEY=<at least 32 random chars, e.g. openssl rand -hex 32>
DASHBOARD_PASSWORD=<strong password>
```

With those set the dashboard requires login and the backend API requires `X-API-Key` with timing-safe comparison. Anyone on your LAN who cannot authenticate cannot do anything. This is safe on a trusted home network. **Do not expose any of these ports to the public internet without a proper TLS reverse proxy.**

For a zero-config alternative, SSH-tunnel from your client device instead of editing compose:

```bash
ssh -L 3001:localhost:3001 user@<host-machine>
```

Then browse `http://localhost:3001` on your client.

## Development

### Prerequisites

- Python 3.12+
- Node.js 20+ (for frontend)
- Docker Engine 24+ with Compose v2

### Local Setup

We strictly use Docker Compose for local development to avoid polluting the host machine with heavy ML dependencies.

### Docker Development

```bash
docker compose up -d                    # Start all services
docker compose logs -f paper_ingestion  # Follow logs
docker compose exec paper_ingestion pytest tests/  # Run tests
docker compose exec paper_ingestion ruff check .   # Run linting
```

### n8n Integration Layer (optional)

n8n is available as an optional workflow automation tool for connecting JARVIS to external services. Enable with `--profile n8n`:

```bash
docker compose --profile n8n up -d
# Access n8n UI at http://localhost:5678
```

Built-in scheduling (daily briefings, review reminders, etc.) is handled by APScheduler in the Telegram bot -- n8n is NOT required for core functionality.

**n8n is useful for power users who want to:**
- Sync papers/tasks to **Notion** databases
- Export paper summaries to **Obsidian** vaults (via shared volume)
- Push briefings to **Slack/Discord** channels
- Send weekly digests via **email**
- Integrate with **Zotero/Mendeley** reference managers

See `n8n/workflows/` for template workflows and the recreation guide.

### Adding a Paper Source

1. Create `services/paper_ingestion/app/sources/new_source.py`
2. Implement the `PaperSource` abstract class from `base.py`
3. Decorate with `@register_source`
4. Add a row to the `paper_sources` table via the Settings UI

### Project Structure

```
├── services/
│   ├── paper_ingestion/        # FastAPI: paper fetch, PDF parse, chunk, embed, RAG
│   ├── learning_engine/        # FastAPI: FSRS cards, review scheduling, projects
│   └── telegram_bot/           # python-telegram-bot (optional profile)
├── frontend/                   # React 19 + TypeScript + Vite + Shadcn/ui
├── libs/jarvis_common/         # Shared Python library (auth, DB helpers, LLM client)
├── db/
│   ├── init.sql                # PostgreSQL schema
│   └── migrations/             # Versioned schema changes (001-019)
├── litellm/config.yaml         # LLM gateway routing (smart/fast/embed aliases)
├── n8n/workflows/              # n8n workflow recreation guide
├── docker-compose.yml          # All services
├── .env.example                # Configuration template
└── Makefile                    # Dev convenience commands
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Frontend** | React 19, TypeScript, Vite, Shadcn/ui, TanStack Query v5, Zustand, React Router v7, Recharts, Cytoscape.js |
| **Backend** | FastAPI, Python 3.12, asyncpg, Pydantic v2 |
| **LLM Gateway** | LiteLLM (routes to Ollama, OpenAI, Anthropic, etc.) |
| **Local LLM** | Ollama (mistral-nemo, qwen3:4b, nomic-embed-text) |
| **Database** | PostgreSQL 16 |
| **Vector DB** | Qdrant |
| **Spaced Repetition** | py-fsrs (FSRS algorithm) |
| **Search** | BM25 + semantic fusion, cross-encoder reranking |
| **Orchestration** | n8n |
| **Notifications** | Telegram Bot API (optional) |
| **Reverse Proxy** | nginx (in dashboard container) |

## Inspiration & Prior Art

JARVIS stands on the shoulders of excellent open-source and public research tools. The Discovery & Pulse subsystem in particular draws on ideas and patterns from:

- [ChatGPT Pulse](https://openai.com/index/introducing-chatgpt-pulse/) — async overnight research, morning card deck, ephemeral delivery, and feedback loop pattern.
- [zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily) — using your existing library as a preference model via weighted centroid cosine similarity.
- [GPT Paper Assistant](https://github.com/tatsu-lab/gpt_paper_assistant) — two-axis LLM scoring (relevance + novelty) and author watchlists via Semantic Scholar IDs.
- [ArxivDigest](https://github.com/AutoLLM/ArxivDigest) — natural-language interest descriptions driving LLM relevance ranking.
- [Scholar Inbox](https://scholar-inbox.com) — per-user logistic regression classifier trained on embedding vectors.
- [Inciteful](https://inciteful.xyz) — citation graph algorithms (PageRank + Adamic/Adar) for paper discovery.
- [BERTopic](https://github.com/MaartenGr/BERTopic) — neural topic modeling with dynamic temporal topics.
- [OpenScholar](https://github.com/AkariAsai/OpenScholar) — iterative self-feedback RAG over scientific literature.
- [PaperQA2](https://github.com/Future-House/paper-qa) — metadata-aware embeddings and agentic retrieval.

These projects are credited for the ideas and patterns that informed JARVIS's design, not for copied code. All are MIT/Apache-licensed.

## Troubleshooting

**Ollama first-boot is slow.** On first start, Ollama pulls `mistral-nemo`, `qwen3:4b`, and `nomic-embed-text`. Expect 5–10 minutes on a decent connection. Watch progress with `docker compose logs -f ollama`.

**`paper_ingestion` exits with "JARVIS_API_KEY not set" in production.** Set `JARVIS_API_KEY` to at least 32 chars in `.env`, or set `ENVIRONMENT=development` for local-only use.

**Migrations fail with "advisory lock held".** A previous startup crashed mid-migration. Restart with `docker compose down && docker compose up -d` — the lock is released on clean shutdown and the runner will retry.

**GPU not detected.** Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host, then `docker compose restart ollama`. Verify with `docker compose exec ollama nvidia-smi`.

**Pulse cards are empty or stage-2 scoring times out.** The scoring pipeline uses the `smart` Ollama model (mistral-nemo). With 50 candidates and `_LLM_CONCURRENCY=5`, a cold model can take several minutes on first run. Subsequent runs are faster. Check logs with `docker compose logs paper_ingestion | grep pulse.stage2`.

**Dashboard shows "Network Error" on every API call.** The frontend calls `/api/*` through the dashboard's nginx, which proxies to `paper_ingestion:8000` and `learning_engine:8001` over the internal Docker network. If the backends are unhealthy, the dashboard still loads but every call fails. Check `docker compose ps` — all services should be `healthy`.

**Tests fail on the host with `ModuleNotFoundError: No module named 'fitz'`.** The backend test suite has Docker-only dependencies (PyMuPDF, marker, qdrant). Run tests inside the container instead: `docker compose exec paper_ingestion pytest tests/`.

**I already had a `.env` and `setup.sh` asks to overwrite.** By design — `setup.sh` is idempotent and will not clobber secrets without confirmation. Pick **no** to keep your existing config; pick **yes** only if you intend to regenerate secrets from scratch and accept being logged out everywhere.

**The setup wizard won't go away.** The wizard is gated on the `setup.completed` flag in `user_config`. You can flip it three ways: click **Done** on the final wizard step, toggle it from **Settings → Integrations**, or do it directly in psql:

```bash
docker compose exec postgres psql -U jarvis -d jarvis -c \
  "UPDATE user_config SET value='true'::jsonb WHERE key='setup.completed';"
```

**Telegram pairing code expired.** Pairing codes are valid for 10 minutes. Generate a new one from **Settings → Integrations → Generate pairing code**, then send `/start <code>` to your bot within the window.

**`cloudflared` container won't start.** Verify `CLOUDFLARE_TUNNEL_TOKEN` is set in `.env` and that you passed the profile flag (`docker compose --profile tunnel up -d`). Then check `docker compose logs cloudflared` — a valid registration prints `Registered tunnel connection`. The most common failure is copying the token with a trailing newline or a space — re-paste it from the Cloudflare dashboard.

## Further Reading

- [docs/PRD.md](docs/PRD.md) — Product requirements and feature-level spec, including the Discovery & Pulse design.
- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — Non-functional requirements and technical constraints.
- [docs/CHANGELOG.md](docs/CHANGELOG.md) — Release notes per version, including the [1.2.1] post-audit hotfix.
- [AGENTS.md](AGENTS.md) — Repository-level guidance for contributors (human or AI).

## License

[MIT](LICENSE)
