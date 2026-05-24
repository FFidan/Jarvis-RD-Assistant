# Contributing to JARVIS RD Assistant

Thank you for your interest in contributing. This document covers the branch
policy, how to run quality gates, migration conventions, and the cross-user
isolation expectation that every contributor must follow.

---

## Branch Policy

- `master` is the main branch. All merged changes land here.
- Work on a feature branch: `git checkout -b feat/<short-description>` or
  `fix/<short-description>`.
- Open a Pull Request against `master`. One approving review is required before
  merging.
- Squash-merge or rebase-merge to keep `master` history linear.
- Delete the branch after merging.

---

## Pull Request Process

1. **Open an issue first** for non-trivial changes to discuss scope and approach.
2. Keep PRs focused — one logical change per PR.
3. Include a brief description of what changed, why, and any migration steps.
4. Ensure all quality gates pass (see below) before requesting review.
5. Update `CHANGELOG.md` if the change is user-visible.

---

## Quality Gates

Run these before opening a PR. All must pass.

### Python (backend + shared libs)

```bash
uv run ruff check services/ libs/ scripts/
uv run pytest
```

Ruff covers linting and import ordering. Pytest runs the full unit + integration
suite. Tests that require a live database connection use `pytest-postgresql` or
are marked `live_pg` — the full suite runs inside the Docker containers.

### Frontend

```bash
npm --prefix frontend run lint
npm --prefix frontend run test -- --run
npm --prefix frontend run build
```

`lint` runs ESLint. `test --run` runs Vitest in non-watch mode. `build` confirms
the TypeScript compile and bundle succeed.

### Security resolver check

```bash
python3 scripts/check-no-unsafe-resolver.py
```

Ensures that no user-data endpoint uses an unsafe auth resolver. Must exit 0.

### Dependency parity (optional, for backend changes)

```bash
bash scripts/export-service-requirements.sh
bash scripts/check-python-deps.sh
```

### Doc alignment check

```bash
python3 scripts/check_agent_docs.py
```

### Docs site preview (optional but recommended before pushing docs changes)

CI runs `mkdocs build --strict` on every push (`.github/workflows/docs.yml`). To catch broken links / unresolved refs / nav-config errors before pushing:

```bash
pip install -r requirements-docs.txt
mkdocs build --strict
mkdocs serve  # live preview at http://localhost:8000
```

The local dev container does NOT include mkdocs by default; install on demand.

---

## Adding a Database Migration

Migrations live in `db/migrations/` and are applied automatically by the
migration runner at service startup.

### Numbering convention

Name files `NNN_short_description.sql` where `NNN` is the next sequential
three-digit number. Check `db/migrations/` for the highest existing number and
increment by one. Never reuse a number.

### Writing the SQL

- **Idempotent**: use `IF NOT EXISTS`, `OR REPLACE`, `DO $$ BEGIN IF NOT EXISTS ...
  END IF; END $$;` guards so the migration is safe to replay.
- **No outer transaction control**: do not include bare `BEGIN` / `COMMIT` /
  `ROLLBACK` at the top level. The migration runner wraps each file in its own
  transaction and strips standalone transaction-control statements automatically
  (see `_strip_outer_transaction_control` in `jarvis_common/migrations.py`).
  PL/pgSQL `DO $$ BEGIN ... END $$` blocks are fine — those are not stripped.
- **Seed data**: if the migration seeds `user_config` or `paper_sources` rows,
  add a test in `services/paper_ingestion/tests/test_migration_NNN.py` verifying
  the expected rows exist after the migration runs.
- Test your migration with `docker compose exec paper_ingestion pytest
  tests/test_migration_NNN.py`.

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
- Commit messages: imperative mood, present tense (e.g. "add X", "fix Y",
  "remove Z"). Reference issue numbers when applicable.

---

## Questions?

Open a GitHub Discussion or file an issue. For security issues, see
[docs/SECURITY.md](docs/SECURITY.md) for the responsible disclosure process.
