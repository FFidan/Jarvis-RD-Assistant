# n8n Workflow Reference

**n8n is OPTIONAL.** All core scheduling is handled by APScheduler in the Python services (`paper_ingestion` and `learning_engine`). The workflows below are reference templates only, not the production critical path.

If you prefer n8n's visual workflow editor over Python APScheduler jobs, you can recreate the workflows manually in the n8n UI (`docker compose --profile n8n up`).

## Workflow 1: Daily Briefing

**Trigger:** Cron — `30 8 * * *`

1. **Postgres** node: `SELECT COUNT(*) FROM papers WHERE created_at >= NOW() - INTERVAL '24 hours'`
2. **HTTP Request** node: `GET http://learning_engine:8001/api/stats`
3. **Postgres** node: `SELECT t.title, p.name FROM tasks t LEFT JOIN projects p ON t.project_id = p.id WHERE t.status = 'in_progress'`
4. **Postgres** node: `SELECT m.name, m.deadline, p.name FROM milestones m JOIN projects p ON m.project_id = p.id WHERE m.completed = FALSE AND m.deadline <= NOW() + INTERVAL '7 days'`
5. **Function** node: Compose morning briefing message from results
6. **Telegram** node: Send message to configured chat_id

## Workflow 2: Paper Digest (Weekly)

**Trigger:** Cron — `0 9 * * 1` (Monday 9 AM)

1. **Postgres** node: `SELECT * FROM topics WHERE enabled = TRUE`
2. **Loop** over topics:
   - **HTTP Request** node: `POST http://paper_ingestion:8000/api/search` with `{"query": term, "source": "arxiv", "max_results": 10}`
3. **Function** node: Group papers by topic, format digest
4. **Telegram** node: Send digest

## Workflow 3: Review Reminder

**Trigger:** Cron — `0 14 * * *`

1. **HTTP Request** node: `GET http://learning_engine:8001/api/stats`
2. **IF** node: Check `due_now > 0`
3. **Telegram** node: Send reminder with due count

## Workflow 4: Deadline Warning

**Trigger:** Cron — `0 12 * * *`

1. **Postgres** node: `SELECT m.name, m.deadline, p.name FROM milestones m JOIN projects p ON m.project_id = p.id WHERE m.completed = FALSE AND m.deadline <= NOW() + INTERVAL '3 days' AND m.deadline > NOW()`
2. **IF** node: Check if results exist
3. **Function** node: Format warning message
4. **Telegram** node: Send warning

## Workflow 5: Research Pulse (Full Pipeline)

**STATUS: SUPERSEDED** — Use APScheduler `pulse_overnight` job instead.

**Trigger (n8n):** Cron — `0 9 * * *`

The production equivalent runs in `services/paper_ingestion/paper_ingestion/scheduler.py` as `pulse_overnight_job()`, triggered by the configurable `pulse.cron` setting (read from `user_config` table). This job:

1. Fetches topics from `topics` table
2. Runs discovery via source plugins (arXiv, PubMed, OpenAlex, S2)
3. Ranks candidates via Pulse recommender
4. Generates a daily deck stored in `pulse_decks`
5. Emits results to Telegram via `/api/pulse_now` endpoint (if configured)

If you want n8n coverage anyway, the workflow would be:

1. **Postgres** node: `SELECT * FROM topics WHERE enabled = TRUE`
2. **Loop** over topics and their query_terms:
   - **HTTP Request**: `POST http://paper_ingestion:8000/api/search`
   - For each new paper:
     - **HTTP Request**: `POST http://paper_ingestion:8000/api/download-pdf/{id}`
     - **HTTP Request**: `POST http://paper_ingestion:8000/api/process-pdf/{id}`
     - **HTTP Request**: `POST http://paper_ingestion:8000/api/summarize/{id}`
3. **Postgres** node: Mark papers as notified
4. **Function** node: Format briefing
5. **Telegram** node: Send briefing

## APScheduler Equivalents (Production)

The following workflows are handled by APScheduler in Python services and do **not** require n8n:

| Workflow | APScheduler Job | Location | Config |
|----------|-----------------|----------|--------|
| Research Pulse | `pulse_overnight_job` | `paper_ingestion/scheduler.py` | `pulse.cron` (user_config) |
| Weekly Digest | `weekly_digest_job` | `paper_ingestion/scheduler.py` | `_DEFAULT_WEEKLY_DIGEST_CRON` = `0 8 * * 1` |
| Auto-Fetch Papers | `auto_fetch_job` | `paper_ingestion/scheduler.py` | `AUTO_FETCH_INTERVAL_HOURS` env var |
| Zotero Library Sync | `zotero_library_sync_job` | `paper_ingestion/scheduler.py` | `zotero.poll_enabled` + `zotero.poll_cron` |
| Pulse Classifier Training | `pulse_classifier_training_job` | `paper_ingestion/scheduler.py` | `pulse.enabled` + `_DEFAULT_PULSE_CLASSIFIER_CRON` |

All APScheduler jobs use `@job_handler` registry from `jarvis_common.jobs` for async background execution.

## Notes

- All HTTP Request nodes should include header `X-API-Key: {{$env.JARVIS_API_KEY}}`
- Postgres nodes connect to the shared `jarvis` database
- Error handling: add Error Trigger nodes to send failure alerts via Telegram
- For scheduled nudges (SMS, pushes), see `telegram_bot/scheduler.py` which reads `scheduled_nudges` table and uses APScheduler's `CronTrigger`
