# infra/vector.toml

Vector ships container stdout/stderr from non-app services into `system_events` with `category='infra'`.

## Transport

Vector 0.40.0-alpine has no native `postgres` sink (only `postgresql_metrics`, a
metrics source, and `http`, a sink). We use the `http` sink to POST batched
NDJSON to `paper_ingestion`'s `/infra-events` endpoint, which bulk-inserts into
`system_events`. Auth is via the `INFRA_INGEST_KEY` secret
(`secrets/infra_ingest_key.txt`), shared between Vector and paper_ingestion.

The `vector_writer` Postgres role from migration 068 is currently orphaned —
left in place in case we move back to a native postgres sink in a future Vector
release.

## What's filtered out
- nginx access logs with status < 400
- Postgres routine LOG entries (non-error)
- Qdrant INFO chatter
- Ollama per-request lines

## How to add a new exclusion
1. Add the container name to `exclude_containers` in `[sources.docker_logs]`, OR
2. Add a regex/condition in the `drop_noise` filter transform.

## Disk buffer
100MB. On overflow, newest events drop (best-effort logging — app categories use the in-process `SystemEventHandler` which has its own ring buffer).

## Version
Pinned via `VECTOR_IMAGE` in `versions.env`.
