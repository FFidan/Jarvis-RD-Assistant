# Docker Secrets

These files contain credentials mounted into containers via Docker Secrets.
They are NOT checked into git.

## Setup

Create one file per secret before running `docker compose up`:

```bash
echo "your-postgres-password" > secrets/postgres_password.txt
echo "your-jarvis-api-key"    > secrets/jarvis_api_key.txt
echo "your-telegram-token"    > secrets/telegram_bot_token.txt
```

## Files

| File | Used by | Description |
|------|---------|-------------|
| `postgres_password.txt` | `postgres` | PostgreSQL superuser password |
| `jarvis_api_key.txt` | `paper_ingestion`, `learning_engine`, `telegram_bot` | JARVIS REST API key (min 32 chars) |
| `telegram_bot_token.txt` | `paper_ingestion`, `telegram_bot` | Telegram Bot API token from @BotFather |

## How it works

Services read the `*_FILE` environment variable first (Docker Secrets mount path at
`/run/secrets/<name>`), falling back to the plain env var from `.env`. This means you
can run without secrets files in development by setting values in `.env` as usual.

The helper is `jarvis_common.secrets.read_secret(name)`.
