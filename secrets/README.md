# Docker Secrets

These files contain credentials mounted into containers via Docker Secrets.
They are NOT checked into git.

**Secret files use mode 644 inside the mode 700 `secrets/` directory.** The owner-only directory keeps the files private on the host; the file read bit lets the non-root service containers read them through the compose bind mount. Setting files to 600 makes the services crash on startup with a permission error. Use the commands below or run `chmod 700 secrets && chmod 644 secrets/*.txt` after creating them.

## Setup

`scripts/init-secrets.sh` creates every file this directory needs and never
overwrites one that already exists. `setup.sh` and `update.sh` both run it, so a
normal install or upgrade needs no manual step here.

Create a file by hand only when you are not using those scripts:

```bash
printf "%s" "your-jarvis-api-key" > secrets/jarvis_api_key.txt && chmod 644 secrets/jarvis_api_key.txt
```

Alternatively, batch chmod after creation:

```bash
chmod 700 secrets
chmod 644 secrets/*.txt
```

## Files

The complete inventory — every secret `docker-compose.yml` declares, what
creates it and what it is for — is in the deployment guide under
[Docker Secrets](../docs/DEPLOYMENT.md#docker-secrets). It is kept there so one
document describes the whole deployment, and a parity check fails if it drifts
from compose. This file covers the mechanics below.

## Mode Bits Reminder

**Every secret file must have mode 644, and the `secrets/` directory mode 700.** Verify with:

```bash
ls -la secrets/*.txt
```

All files should show `-rw-r--r--` (mode 644) and the directory `drwx------` (mode 700). If they differ, fix them:

```bash
chmod 700 secrets
chmod 644 secrets/*.txt
```

## Langfuse Init Keypair

`langfuse_init_pk.txt` and `langfuse_init_sk.txt` are **git-ignored, machine-local, and
auto-generated** by `scripts/gen-langfuse-keys.sh` on first use. **Never commit them.**

They seed the Langfuse headless-init (project public/secret key). Rotation invalidates
the existing Langfuse project credentials and requires a Langfuse volume wipe per
`docs/observability` §9.2 (administrator-only; `OBSERVABILITY_ENABLED` is off by default so
this is a no-op on standard deployments). The `make up` target ensures they are always
present before `docker compose up`.

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
