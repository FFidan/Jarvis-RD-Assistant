# Contributing to JARVIS RD Assistant

Thank you for your interest in contributing. This document covers the branch
policy, how to run quality gates, migration conventions, and the cross-user
isolation expectation that every contributor must follow.

---

## Branch Policy

- `main` is the default branch. All merged changes land here.
- Work on a feature branch: `git checkout -b feat/<short-description>` or
  `fix/<short-description>`.
- Open a pull request against `main`. This project is currently solo-maintained.
  Pull requests are expected to pass the required CI and security gates; maintainer
  review happens before merge. Branch protection enforces the terminal CI and
  security gates before merging.
- Squash-merge or rebase-merge to keep `main` history linear.
- Delete the branch after merging.

---

## Pull Request Process

1. **Open an issue first** for non-trivial changes to discuss scope and approach.
2. Keep PRs focused — one logical change per PR.
3. Include a brief description of what changed, why, and any migration steps.
4. Ensure all quality gates pass (see below) before requesting review.
5. Update `CHANGELOG.md` if the change is user-visible.
6. Use [Conventional Commits](https://www.conventionalcommits.org/) style for
   commit messages (`feat:`, `fix:`, `chore:`, `docs:`, etc.) — makes the
   changelog easier to generate and review.
7. Write for a public audience. PR titles, descriptions, and commit messages
   must explain the user or engineering value without local hostnames, private
   paths, temporary branch names, or private implementation-process notes. AI co-author trailers are welcome when they accurately record the
   contribution.

---

## License

By contributing to this project you agree that your contributions are licensed
under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0),
the same license that covers the project. No additional contributor license
agreement is required.

---

## AI-Assisted Contributions

This project was built with substantial AI assistance, primarily using Claude
Code, and AI-assisted contributions are welcome. If an AI tool materially
contributed, keep truthful `Co-Authored-By` trailers where appropriate, but do
not include generated tool footers, session URLs, or internal workflow labels in
commit messages or PR descriptions.

Every PR, human- or agent-authored, must pass the full gate (`make check`: ruff
lint, pyright, tach, the Python + frontend test suites, and the build) before
review.

---

## Setting Up a Development Environment

**Prerequisites:** Python 3.12+, Node.js 20+, Docker Engine 24+ with Compose v2, and [`uv`](https://docs.astral.sh/uv/) (Python package manager).

Install `uv` if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Full single-instance install** (Docker, Python, Node, secrets, etc.):

```bash
./setup.sh            # interactive — asks for API keys and config
# or
./scripts/jarvis-setup.sh   # non-interactive — reads env vars / defaults
```

**Python deps only** (after the full install, or for CI-style local work):

```bash
make dev-env          # runs: uv sync --group dev
make check            # runs all quality gates (see below)
```

> `make setup` is a backward-compatible alias for `make dev-env`.
> Neither replaces `./setup.sh` for a first-time install.

**Pre-commit hooks** (run once after cloning):

```bash
pre-commit install
```

This wires the same ruff / secret-check guards that CI runs on every push.

---

## Quality Gates

### One-command local gate

```bash
make check
```

This mirrors the CI `lint-test` + `frontend` jobs end-to-end:

| Step | What it runs |
|---|---|
| Guard: no tracked secrets | `bash scripts/check-no-tracked-secrets.sh` |
| Guard: secret file permissions | `chmod 700 secrets` + `find secrets -maxdepth 1 -type f -name "*.txt" -exec chmod 644 {} \;` |
| Dependency parity | `bash scripts/check-python-deps.sh` |
| Lint | ruff + migrations-no-tx + no-jsonb-double-encode + no-unsafe-resolver |
| Tach | module boundary check (`uv run tach check`) |
| Pyright | type check (`npx --yes pyright@1.1.408`) |
| Test-shape | `uv run python3 scripts/check-test-shape.py` |
| Contract-docs check | `uv run python3 scripts/check_contract_docs.py` |
| Guard: burned secrets | `bash scripts/check-burned-secrets.sh` |
| Fast pytest suite | `uv run pytest` (see below) |
| Frontend | lint + typecheck + unit tests + build |

You can also run each sub-check individually (targets: `lint`, `typecheck`,
`frontend-check`, `test`).

### Fast vs. live-DB test suites

`uv run pytest` (or `make test`) runs the **fast suite only**. The
`pyproject.toml` `addopts` permanently deselects tests marked `live_pg`,
`integration`, or `slow`:

```
addopts = "--import-mode=importlib -m 'not live_pg and not integration and not slow'"
```

For the **contract layer** (DB-backed) and **cross-user isolation** gate you
need a live Postgres. Set `JARVIS_RUN_LIVE_PG=1` and
`JARVIS_TEST_PG_ADMIN_DSN=postgresql://postgres:<password>@localhost:5432/postgres`,
then run pytest with the `contract` or `integration and live_pg` markers (e.g.
`-m contract` / `-m "integration and live_pg"`). CI runs these in dedicated jobs
with a managed Postgres container.

### Frontend (standalone)

```bash
make frontend-check
# or individually:
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
npm --prefix frontend run build
```

### Docs site preview (optional, for docs changes)

CI runs `mkdocs build --strict` on every push. To catch broken links before
pushing:

```bash
pip install -r requirements-docs.txt
mkdocs build --strict
mkdocs serve   # live preview at http://localhost:8000
```

---

## Adding a Database Migration

The baseline schema is **`db/init.sql`**. New migrations go in
**`db/migrations/`** as numbered SQL files; see `db/migrations/README.md` for
the fold-in convention, current next migration number, and idempotency examples.

**Numbering:** name files `NNN_short_description.sql` where `NNN` is the next
sequential three-digit number. Never reuse a number.

**Two hard rules for the SQL:**

1. **No outer transaction control.** Do not include bare `BEGIN` / `COMMIT` /
   `ROLLBACK` at the top level — the migration runner wraps each file in its own
   transaction automatically. PL/pgSQL `DO $$ BEGIN ... END $$` blocks are fine.
2. **Idempotent.** Use `IF NOT EXISTS`, `OR REPLACE`, or equivalent guards so
   the migration is safe to replay.

If the migration seeds `user_config` or `paper_sources` rows, add a test in
`services/paper_ingestion/tests/test_migration_NNN.py` verifying the expected
rows exist after it runs.

---

## Cross-User Isolation Requirement

JARVIS is multi-tenant. **Every new endpoint that touches user-owned data MUST
use `current_user_id_strict` (or `current_user_id_strict_with_owner_override`
for Telegram bot routes) as a `Depends(...)` dependency.** This is a hard
requirement, not a style suggestion.

```python
from jarvis_common.auth import current_user_id_strict

@router.get("/api/my-resource")
async def get_my_resource(
    user_id: int = Depends(current_user_id_strict),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    rows = await pool.fetch(
        "SELECT * FROM my_table WHERE user_id = $1", user_id
    )
    ...
```

- `current_user_id_strict` raises 401 when no session is present, preventing
  API-key-only callers from falling through as anonymous users.
- Never filter by user ID derived from request body or query params — always use
  the injected `user_id` from the session layer.
- PRs that introduce user-data endpoints without `current_user_id_strict` will
  not be merged.

---

## Code Style

- Python: follow `ruff` rules configured in `pyproject.toml`. Type annotations
  are expected on all new public functions.
- TypeScript: follow the ESLint config in `frontend/`. Avoid `any`.
- Commit messages: use Conventional Commits style — imperative mood, present
  tense, with a type prefix (`feat:`, `fix:`, `chore:`, `docs:`, `test:`,
  `refactor:`). Reference issue numbers when applicable.

---

## Questions?

Open a GitHub Discussion or file an issue. For security issues, see
[docs/SECURITY.md](docs/SECURITY.md) for the responsible disclosure process.
