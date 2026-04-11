# Changelog

All notable changes to JARVIS RD Assistant will be documented in this file.

## [Unreleased]

### Added
- **My Day page**: daily productivity command center with Pomodoro timer, quick-add tasks, project badges, Project Pulse widget
- **Pomodoro timer**: wall-clock based timing with pause/resume, auto-logging of completed sessions, browser notifications, configurable durations
- **Timer settings**: configurable work/break durations and cycle count in Settings page
- **Quick-add tasks**: create tasks from My Day with optional project assignment
- **Project badges**: clickable project labels on tasks linking to Projects page
- **Collapsed empty cards**: Learning/Recommended section collapses to compact row when both empty
- **Recommendation engine** (Phase 1): score-based paper recommendations with liked-paper, project-relevance, and recency signals

### Fixed
- Migration 015 idempotency guard (conditional NOT NULL constraint)
- Migration runner advisory locking (pg_advisory_lock)
- QuickAddTask/TaskList error handling (onError callbacks)
- Vite dev proxy for /api/executive routes
- Backend focus session duration validation (gt=0, le=24)
- Pomodoro auto-logging: completed work sessions now logged automatically
- Timer state persistence: survives page refresh
- Timer accuracy: wall-clock based, no drift in background tabs
- Rate limits on get_my_day (60/min) and log_focus_session (10/min)
- HTTPException inside transaction causing silent rollback in log_focus_session
- QuickAddTaskRequest validation (title 1-500, priority 1-4)
- Recommendations endpoint unbounded limit (now ge=1, le=200)
- Pipeline HTTP timeouts (120-180s for PDF operations)
- Focus command duplicate timer jobs
- Cross-encoder reranker CUDA fallback to CPU
- Reranker lru_cache permanent None caching
- init.sql sync with migrations (paper_recommendations, search_vector)
- Docker learning_engine dependency ordering

## [1.1.0] - 2026-03-08

### Added
- React 19 dashboard replacing Streamlit (Vite + Shadcn/ui + TanStack Query + Zustand)
- Research Feed with filtering, sorting, and paper detail pages
- Citation graph visualization (Cytoscape.js)
- Knowledge graph visualization (Cytoscape.js)
- Structured extraction table with templates
- FSRS spaced repetition for learning cards
- Analytics page with activity, retention, and review charts
- Project management with tasks, milestones, and papers
- Settings page for topics, sources, authors, ingestion, automation, extraction, recommendations
- nginx reverse proxy for frontend routing
- CORS middleware for both FastAPI services
- SSE streaming for paper analysis and RAG chat

## [1.0.0] - 2026-02-15

### Added
- Paper ingestion from arXiv, Semantic Scholar, and manual PDF upload
- Hybrid search (BM25 + semantic) with cross-encoder reranking
- RAG-powered Q&A with citation verification
- Telegram bot with push notifications and daily briefings
- PostgreSQL database with 13 migrations
- Qdrant vector database for semantic search
- LiteLLM for model-agnostic LLM access
- Docker Compose deployment (9 services)
