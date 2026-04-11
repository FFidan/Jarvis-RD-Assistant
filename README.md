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

- **Discovery & Pulse** *(coming soon)* -- Overnight proactive discovery of new papers from arXiv, Semantic Scholar, OpenAlex, and PubMed. Scores candidates against your research interests using embedding similarity plus LLM relevance ranking, then delivers a small curated card deck each morning via the My Day view and optional Telegram. Lightweight 👍/👎/💾 feedback on cards shapes tomorrow's recommendations. Complements the existing weekly reflection tools without overlapping them.

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
# 1. Clone and configure
git clone https://github.com/FFidan/Jarvis-RD-Assistant.git
cd Jarvis-RD-Assistant
cp .env.example .env

# 2. Edit .env -- at minimum, set these:
#    POSTGRES_PASSWORD=<strong-password>
#    N8N_ENCRYPTION_KEY=<random-string>
#    LITELLM_MASTER_KEY=<random-string>

# 3. Start everything
docker compose up -d

# 4. Wait for Ollama to pull models (first run only, ~5-10 min)
docker compose logs -f ollama

# 5. Open the dashboard
open http://localhost:3001
```

### GPU Acceleration (optional)

If you have an NVIDIA GPU, install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), then the Ollama container will automatically use it. Speeds up inference significantly.

## Configuration

### Required Variables

| Variable | Description |
|----------|-------------|
| `POSTGRES_PASSWORD` | Database password |
| `N8N_ENCRYPTION_KEY` | n8n credential encryption key |
| `LITELLM_MASTER_KEY` | LiteLLM API gateway key |

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
| `DEV_MODE` | `false` | Skip API key checks when `true` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `DASHBOARD_HOST_PORT` | `3001` | Host port for the dashboard |
| `PAPER_INGESTION_HOST_PORT` | `8010` | Host port for paper ingestion API |
| `LEARNING_ENGINE_HOST_PORT` | `8011` | Host port for learning engine API |
| `OPENALEX_API_KEY` | _(empty)_ | Enable OpenAlex as a paper discovery source. Free key at [openalex.org](https://openalex.org). *(Used by the upcoming Discovery & Pulse feature.)* |
| `PUBMED_API_KEY` | _(empty)_ | Upgrade PubMed rate limit from 3 to 10 requests per second. Free key from [NCBI](https://www.ncbi.nlm.nih.gov/home/develop/api/). *(Used by the upcoming Discovery & Pulse feature.)* |
| `UNPAYWALL_EMAIL` | _(empty)_ | Required by [Unpaywall](https://unpaywall.org) to resolve free legal PDFs for paywalled papers. Any email address. *(Used by the upcoming Discovery & Pulse feature.)* |

See [`.env.example`](.env.example) for the full list with comments.

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
│   └── migrations/             # Versioned schema changes (001-017)
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

## License

[MIT](LICENSE)
