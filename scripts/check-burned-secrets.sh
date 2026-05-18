#!/usr/bin/env bash
# scripts/check-burned-secrets.sh — CI tripwire for the publicly-burned
# Langfuse init keypair (OBS-1-RESIDUAL).
#
# The secret files are git-untracked, so in CI they are normally ABSENT —
# that is the GREEN path (nothing to rotate).  If a file IS present and still
# starts with a known-leaked prefix, the build FAILS: run a provision
# (`make up` / `make observability-up` / `./setup.sh`) to self-rotate, since
# scripts/gen-langfuse-keys.sh is burned-aware.
set -euo pipefail
cd "$(dirname "$0")/.."

PK=secrets/langfuse_init_pk.txt
SK=secrets/langfuse_init_sk.txt
BURNED_PK_PREFIX='pk-lf-35d525'
BURNED_SK_PREFIX='sk-lf-031360'

if [ ! -f "$PK" ] && [ ! -f "$SK" ]; then
  echo "[SKIP] No Langfuse init secret files present (expected in CI) — nothing to check."
  exit 0
fi

rc=0
check_one() {
  local file="$1" burned="$2"
  [ -f "$file" ] || return 0
  case "$(tr -d '\n' < "$file")" in
    "$burned"*)
      echo "[FAIL] ${file} still holds the burned/leaked value (prefix ${burned})." >&2
      echo "       Provision once (make up / observability-up / ./setup.sh) to self-rotate." >&2
      rc=1
      ;;
  esac
}

check_one "$PK" "$BURNED_PK_PREFIX"
check_one "$SK" "$BURNED_SK_PREFIX"

if [ "$rc" -eq 0 ]; then
  echo "[OK] No burned Langfuse init secret values present."
fi
exit "$rc"
