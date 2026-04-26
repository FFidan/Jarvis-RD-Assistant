# Docker Secrets

These files contain credentials mounted into containers via Docker Secrets.
They are NOT checked into git.

**All secret files MUST be mode 600 to prevent accidental world-readability.** Use the commands below or run `chmod 600 secrets/*.txt` after creating them.

## Setup

Create one file per secret before running `docker compose up`:

```bash
printf "%s" "your-postgres-password" > secrets/postgres_password.txt && chmod 600 secrets/postgres_password.txt
printf "%s" "your-litellm-master-key" > secrets/litellm_master_key.txt && chmod 600 secrets/litellm_master_key.txt
printf "%s" "your-jarvis-api-key"    > secrets/jarvis_api_key.txt && chmod 600 secrets/jarvis_api_key.txt
printf "%s" "your-telegram-token"    > secrets/telegram_bot_token.txt && chmod 600 secrets/telegram_bot_token.txt
```

Alternatively, batch chmod after creation:

```bash
chmod 600 secrets/*.txt
```

## Files

| File | Used by | Description |
|------|---------|-------------|
| `postgres_password.txt` | `postgres`, `paper_ingestion`, `learning_engine`, `telegram_bot`, `n8n`, `postgres-backup` | PostgreSQL superuser password |
| `litellm_master_key.txt` | `litellm` | LiteLLM master key for the gateway API |
| `jarvis_api_key.txt` | `paper_ingestion`, `learning_engine`, `telegram_bot` | JARVIS REST API key (min 32 chars) |
| `telegram_bot_token.txt` | `paper_ingestion`, `telegram_bot` | Telegram Bot API token from @BotFather |

## How it works

Services read the `*_FILE` environment variable first (Docker Secrets mount path at
`/run/secrets/<name>`), falling back to the plain env var from `.env`. This means you
can run without secrets files in development by setting values in `.env` as usual.

The helper is `jarvis_common.secrets.read_secret(name)`.
