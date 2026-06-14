#!/usr/bin/env bash
# scripts/gen-langfuse-keys.sh — Idempotently generate Langfuse init keypair.
#
# Creates secrets/langfuse_init_pk.txt and secrets/langfuse_init_sk.txt when
# absent, empty, or still holding a known-burned (publicly-leaked) value, then
# mirrors the values into .env so that docker-compose interpolation
# (${LANGFUSE_INIT_PROJECT_PUBLIC_KEY:-}) picks them up without leaking secrets
# into the process environment via inline $(cat ...) expansion.
#
# Idempotency contract:
#   - A non-empty file whose value is NOT a burned prefix is NEVER overwritten
#     (no churn for healthy deployments — `make up` stays a no-op).
#   - A known-leaked value is self-rotated on the next provision (make up /
#     observability-up / setup.sh), without any
#     destructive on-disk step.
#   - On (re)generation of EITHER key, BOTH .env lines are rewritten in place so
#     .env can never shadow a freshly rotated file (the old append-if-absent
#     idiom left a stale .env line masking a rotated file — that bug is closed).
#
# POSIX-portable: no `sed -i` (GNU-only); .env is rewritten via grep-filter to a
# temp file then mv, which works identically on BSD/macOS (init-secrets INST-1).
#
# Called automatically by: make up, make observability-up, setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Prefixes of the known-leaked keypair. Any on-disk
# file still starting with these is treated as absent and force-rotated.
BURNED_PK_PREFIX='pk-lf-35d525'
BURNED_SK_PREFIX='sk-lf-031360'

rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# needs_regen FILE BURNED_PREFIX → exit 0 when the file must be (re)generated:
# absent OR empty OR its current content starts with the burned prefix.
needs_regen() {
  local file="$1" burned="$2"
  [ -s "$file" ] || return 0
  case "$(tr -d '\n' < "$file")" in
    "$burned"*) return 0 ;;
    *)          return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# 1. Generate secret files when absent, empty, or burned
# ---------------------------------------------------------------------------
if needs_regen secrets/langfuse_init_pk.txt "$BURNED_PK_PREFIX"; then
  printf 'pk-lf-%s' "$(rand)" > secrets/langfuse_init_pk.txt
  echo "gen pk"
fi
if needs_regen secrets/langfuse_init_sk.txt "$BURNED_SK_PREFIX"; then
  printf 'sk-lf-%s' "$(rand)" > secrets/langfuse_init_sk.txt
  echo "gen sk"
fi
chmod 600 secrets/langfuse_init_pk.txt secrets/langfuse_init_sk.txt

# ---------------------------------------------------------------------------
# 2. Mirror values into .env so compose interpolates them without cat-in-env.
#    Rewrite BOTH lines in place (replace if present, append if absent) so a
#    rotated file can never be shadowed by a stale .env entry.
#    touch .env first so the filter works even on a fresh checkout.
# ---------------------------------------------------------------------------
touch .env
pk="$(tr -d '\n' < secrets/langfuse_init_pk.txt)"
sk="$(tr -d '\n' < secrets/langfuse_init_sk.txt)"

# set_env_line KEY VALUE — replace ^KEY= line in .env (POSIX, no sed -i):
# strip any existing line via grep -v to a temp file, then append the fresh
# one, then atomically mv back.
set_env_line() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -vE "^${key}=" .env > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  mv "$tmp" .env
}

set_env_line LANGFUSE_INIT_PROJECT_PUBLIC_KEY "$pk"
set_env_line LANGFUSE_INIT_PROJECT_SECRET_KEY "$sk"
chmod 600 .env
