# infra/vector.toml

Vector ships container stdout/stderr from non-app services into `system_events` with `category='infra'`.

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
