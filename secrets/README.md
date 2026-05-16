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
| `qdrant_api_key.txt` | `qdrant`, `paper_ingestion` | Qdrant vector database API key |
| `jarvis_config_key.txt` | `paper_ingestion`, `learning_engine` | Fernet key for at-rest encryption of `user_config.encrypted_value`; generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

## Mode Bits Reminder

**Every secret file must have mode 600** to prevent accidental world-readability. Verify with:

```bash
ls -la secrets/*.txt
```

All files should show `-rw-------` (mode 600). If any are world-readable, fix them:

```bash
chmod 600 secrets/*.txt
```

## How it works

Services read the `*_FILE` environment variable first (Docker Secrets mount path at
`/run/secrets/<name>`), falling back to the plain env var from `.env`. This means you
can run without secrets files in development by setting values in `.env` as usual.

The settings are accessed via `get_secrets_settings()` from `jarvis_common.settings`, which returns a `SecretsSettings` object populated from `*_FILE` env vars (Docker Secrets mount) falling back to plain env vars.

## Rotation & Hot-Reload

Secret file changes require a container restart:

```bash
docker compose down && docker compose up -d
```

There is **no hot-reload**; the `*_FILE` environment variables are read only at container startup.
