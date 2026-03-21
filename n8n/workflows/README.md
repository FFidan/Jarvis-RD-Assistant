# n8n Workflow Reference

JARVIS uses Python-based orchestration inside the `telegram_bot` service by default
(APScheduler + `orchestration/` modules). If you prefer n8n's visual workflow editor,
you can recreate the workflows manually.

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

**Trigger:** Cron — `0 9 * * *`

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

## Notes

- All HTTP Request nodes should include header `X-API-Key: {{$env.JARVIS_API_KEY}}`
- Postgres nodes connect to the shared `jarvis` database
- Error handling: add Error Trigger nodes to send failure alerts via Telegram
- These workflows are equivalent to the Python orchestration in `services/telegram_bot/app/orchestration/`
