# B.4 — procrastinate Job Broker Migration

**Status:** SPEC  
**Date:** 2026-05-03  
**Branch:** `chore/b4-job-broker-spec`  
**Implementation sprint:** Post B.1+B.2 merge

---

## 1. Decision — procrastinate (PG-native)

Replace the custom `libs/jarvis_common/jarvis_common/jobs.py` (~726 lines, 19 job kinds, PG LISTEN/NOTIFY) with [procrastinate](https://procrastinate.readthedocs.io/) — a PostgreSQL-native task queue with built-in retry, scheduling, periodic tasks, and an admin CLI.

| Option | Trade-off |
|---|---|
| **procrastinate** ← adopted | PG-native LISTEN/NOTIFY; proper retry/scheduling/periodic-task support; admin CLI; no new infrastructure; 3.5k GitHub stars; maps 1:1 to current jobs.py pattern |
| Taskiq + Redis | Feature-rich, well-documented; adds Redis container; jobs no longer browseable via psql |
| arq (Redis) | Simpler; no built-in periodic tasks or admin CLI; custom reaper still needed |

**Rationale:**
- procrastinate uses the same PG LISTEN/NOTIFY mechanism as the current `worker_loop` (jobs.py:631) — the migration is structural, not conceptual.
- No new infrastructure container required. Same `postgres` service.
- `@app.periodic(cron="*/1 * * * *")` replaces `_reap_stale_jobs` (jobs.py:582) as a built-in primitive.
- Langfuse (B.2) will be in place for trace-level debugging by the time B.4 executes.
- Migration 023 `jobs` table dropped in migration 052 only after full cutover.

---

## 2. 19 Job Kinds → procrastinate task mapping

| Old kind | Service owner | procrastinate task name | queue | retry_count |
|---|---|---|---|---|
| `paper.process` | paper_ingestion | `paper_process` | `paper_ingestion` | 2 |
| `paper.analyze` | paper_ingestion | `paper_analyze` | `paper_ingestion` | 2 |
| `papers.batch_process` | paper_ingestion | `papers_batch_process` | `paper_ingestion` | 1 |
| `papers.batch_summarize` | paper_ingestion | `papers_batch_summarize` | `paper_ingestion` | 1 |
| `papers.scan_local` | paper_ingestion | `papers_scan_local` | `paper_ingestion` | 1 |
| `paper.summarize` | paper_ingestion | `paper_summarize` | `paper_ingestion` | 2 |
| `citations.batch_fetch` | paper_ingestion | `citations_batch_fetch` | `paper_ingestion` | 3 |
| `digest.weekly` | paper_ingestion | `digest_weekly` | `paper_ingestion` | 2 |
| `extraction.single` | paper_ingestion | `extraction_single` | `paper_ingestion` | 2 |
| `extraction.batch` | paper_ingestion | `extraction_batch` | `paper_ingestion` | 1 |
| `contradictions.scan` | paper_ingestion | `contradictions_scan` | `paper_ingestion` | 1 |
| `pulse.generate` | paper_ingestion | `pulse_generate` | `paper_ingestion` | 2 |
| `pulse.train_classifier` | paper_ingestion | `pulse_train_classifier` | `paper_ingestion` | 1 |
| `zotero.push` | paper_ingestion | `zotero_push` | `paper_ingestion` | 3 |
| `zotero.resync` | paper_ingestion | `zotero_resync` | `paper_ingestion` | 2 |
| `zotero.sync_from_zotero` | paper_ingestion | `zotero_sync_from_zotero` | `paper_ingestion` | 2 |
| `zotero.sync_annotations` | paper_ingestion | `zotero_sync_annotations` | `paper_ingestion` | 2 |
| `card.generate` | learning_engine | `card_generate` | `learning_engine` | 2 |
| `card.generate_batch` | learning_engine | `card_generate_batch` | `learning_engine` | 1 |

**Queue convention:** each service (`paper_ingestion`, `learning_engine`) has one queue. Workers are started per-service in the service lifespan.

---

## 3. SSE replacement — `stream_job_events` retained

The current HTTP SSE layer (`stream_job_events` at `libs/jarvis_common/jarvis_common/jobs.py:234`) is **retained without change** — it is the public API consumed by frontend job-progress polling. Only the underlying PG LISTEN/NOTIFY listener is replaced.

```
Current: asyncpg LISTEN "jobs_channel" → SSE yield
After:   procrastinate App.listen() event bus → same SSE yield
```

The `stream_job_events` function subscribes to the `jobs` PG channel via asyncpg. In the procrastinate world, procrastinate emits task status events on its own `procrastinate_*` notification channels. The SSE bridge will subscribe to `procrastinate_job_updated` events instead and map them to the existing SSE event shape (`{type, job_id, status, progress, ...}`).

**Router layer unchanged.** `GET /api/jobs/{id}/stream` and `GET /api/jobs/` remain as-is from the service's perspective.

---

## 4. Reaper replacement — periodic task

```python
# Current (jobs.py:582 — called from worker_loop every 60s)
async def _reap_stale_jobs(pool: asyncpg.Pool, kinds: list[str]) -> int: ...

# After — procrastinate periodic task
@app.periodic(cron="*/1 * * * *")
@app.task(queue="paper_ingestion")
async def reap_stale_jobs(timestamp: datetime) -> None:
    # Same logic: mark running jobs with no heartbeat > 5min as failed
    ...
```

The `worker_loop` (jobs.py:631) is replaced by `await app.run_worker_async(queues=["paper_ingestion"])` in the service lifespan.

---

## 5. Migration plan — 5-step phased cutover

### Step 1: Add dependency
- Add `procrastinate[aiopg]>=0.49` to root `pyproject.toml` `[dependency-groups].jarvis-common`
- Write migration `052_procrastinate_schema.sql` — runs `procrastinate schema apply` SQL (auto-generated by procrastinate CLI: `procrastinate schema --app=myapp print-schema`)
- Keep migration 023 `jobs` table alive

### Step 2: Dual-write infrastructure
- Create `libs/jarvis_common/jarvis_common/task_registry.py` — `procrastinate.App` instance + all 19 task definitions as `@app.task` decorated async functions
- Each task function receives `job_id: int` (old system ID for backward-compat SSE during transition) + `payload: dict`
- Services create one procrastinate worker in their lifespan alongside the existing `worker_loop`

### Step 3: Migrate one kind at a time (canary: `digest.weekly`)
- Start with `digest.weekly` — low-frequency, easy to observe in Langfuse
- Route `digest.weekly` enqueues to `app.task_function.defer_async(payload=...)` instead of `create_job(kind="digest.weekly", ...)`
- Keep old `create_job` for all other kinds
- Verify SSE events fire correctly via `stream_job_events`
- Roll out remaining kinds one by one per sprint

### Step 4: Full cutover
- All 19 kinds enqueue through procrastinate
- Remove `worker_loop` call from service lifespans
- Remove old `create_job`, `update_job_status`, `_dequeue_job` functions from `jobs.py`
- SSE bridge updated to subscribe to `procrastinate_job_updated` channel

### Step 5: Drop legacy jobs table
- Add migration `053_drop_jobs_table.sql`:
  ```sql
  DROP TABLE IF EXISTS jobs;
  DROP FUNCTION IF EXISTS notify_job_update() CASCADE;
  -- migration 023 JOB_NOTIFY_CHANNEL trigger also dropped here
  ```
- Remove residual `jobs.py` skeleton (keep only `stream_job_events` as thin SSE bridge)

---

## 6. Risk table

| Risk | Impact | Mitigation |
|---|---|---|
| SSE contract break | Frontend job-progress polling stops working | Adapter layer maps procrastinate events to existing SSE shape before removing `jobs` table; test with existing `GET /api/jobs/{id}/stream` E2E |
| asyncpg LISTEN port conflict | Both old and new LISTEN on same channel during Step 2 | Use separate channel name for procrastinate; dual-LISTEN during cutover window is fine |
| Migration 023 `jobs` table lifetime | `jobs` table dropped only in Step 5 migration 053 | Reserve migration 053 in this spec; 052 adds procrastinate schema |
| procrastinate schema collides with JARVIS tables | Table name conflicts | procrastinate uses `procrastinate_*` prefix; no collision risk |
| Periodic task double-fire | `reap_stale_jobs` fires from both old `worker_loop` and new `@app.periodic` during dual-write | Add `IF EXISTS` guards to reaper; idempotent by design |
| `JobContext.update_progress` API surface | Many callers pass `ctx` with `.update_progress` and `.is_cancelled` | Wrap procrastinate task context in a `JobContext`-compatible shim during cutover |

---

## 7. Reserved migration numbers

| Migration | Purpose |
|---|---|
| 052 | Add procrastinate schema (`procrastinate_*` tables + functions) |
| 053 | Drop legacy `jobs` table and `notify_job_update` trigger (Step 5 only) |

Migration 052 must apply `procrastinate schema apply` SQL (100% additive, no existing tables touched).

---

## 8. Open questions (for implementation sprint)

1. **`JobContext` shim**: procrastinate tasks receive a `context: JobContext` (procrastinate's own type) — decide whether to adapt procrastinate's context to the existing JARVIS `JobContext` protocol (`.update_progress`, `.is_cancelled`) or refactor all call sites.
2. **Frontend job list `GET /api/jobs/`**: currently queries the `jobs` PG table directly. During Step 2–4, this must union both tables or migrate to procrastinate's own `procrastinate_jobs` view.
3. **Job result storage**: current system stores `result` in `jobs.result` JSONB. procrastinate stores results differently. Decide: keep JARVIS `jobs` as result store until Step 5, or add a `procrastinate_results` side table.
