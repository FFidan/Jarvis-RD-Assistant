#!/usr/bin/env sh
# scripts/gen-langfuse-keys.sh — Idempotently generate Langfuse init keypair.
#
# Creates secrets/langfuse_init_pk.txt and secrets/langfuse_init_sk.txt when
# absent (or empty), then mirrors the values into .env so that docker-compose
# interpolation (${LANGFUSE_INIT_PROJECT_PUBLIC_KEY:-}) picks them up without
# leaking secrets into the process environment via inline $(cat ...) expansion.
#
# Safe to run multiple times — existing non-empty files are NEVER overwritten.
# The .env entries are appended only if absent (mirrors init-secrets.sh idiom).
#
# Called automatically by: make up, make observability-up
set -eu
cd "$(dirname "$0")/.."

rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# ---------------------------------------------------------------------------
# 1. Generate secret files when absent or empty
# ---------------------------------------------------------------------------
[ -s secrets/langfuse_init_pk.txt ] || { printf 'pk-lf-%s' "$(rand)" > secrets/langfuse_init_pk.txt; echo gen pk; }
[ -s secrets/langfuse_init_sk.txt ] || { printf 'sk-lf-%s' "$(rand)" > secrets/langfuse_init_sk.txt; echo gen sk; }
chmod 600 secrets/langfuse_init_pk.txt secrets/langfuse_init_sk.txt

# ---------------------------------------------------------------------------
# 2. Mirror values into .env so compose interpolates them without cat-in-env
#    Only appends when the key is absent from .env — never duplicates.
# ---------------------------------------------------------------------------
if [ -f .env ]; then
  pk="$(tr -d '\n' < secrets/langfuse_init_pk.txt)"
  sk="$(tr -d '\n' < secrets/langfuse_init_sk.txt)"
  grep -qE '^LANGFUSE_INIT_PROJECT_PUBLIC_KEY=' .env 2>/dev/null \
    || printf 'LANGFUSE_INIT_PROJECT_PUBLIC_KEY=%s\n' "$pk" >> .env
  grep -qE '^LANGFUSE_INIT_PROJECT_SECRET_KEY=' .env 2>/dev/null \
    || printf 'LANGFUSE_INIT_PROJECT_SECRET_KEY=%s\n' "$sk" >> .env
fi
