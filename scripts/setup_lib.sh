#!/usr/bin/env bash
# Shared setup.sh helpers, factored out so they can be unit-tested directly
# (setup.sh itself is not cleanly sourceable). Three small concerns:
#   - compute_compose_file   : which compose overlays to persist into .env
#   - compute_ollama_models  : which Ollama tags the bootstrap must pull
#   - upsert_env_var          : idempotent in-place .env key write
# Sourced by setup.sh (which cd's to the repo root first, so the relative `.env`
# in upsert_env_var resolves correctly).

# compute_compose_file NVIDIA_PRESENT OVERRIDE_PRESENT -> echoes colon-joined COMPOSE_FILE.
# gpu.yml is added only when the Docker nvidia runtime is present; override.yml is
# appended LAST (an explicit COMPOSE_FILE suppresses Compose's implicit override
# auto-load, and gpu-before-override lets a dev override's `deploy: !reset null` win).
compute_compose_file() {
  local nvidia="$1" override="$2" files="docker-compose.yml"
  [ "$nvidia" = "1" ] && files="${files}:docker-compose.gpu.yml"
  [ "$override" = "1" ] && files="${files}:docker-compose.override.yml"
  printf '%s' "$files"
}

# compute_ollama_models SMART_MODEL -> echoes the comma-set ollama-bootstrap must
# pull: the chosen smart model + the fast + embed defaults, de-duplicated. Keeps
# the pulled set ⊇ the routed models so no LiteLLM alias 404s.
# _FAST/_EMBED mirror the fast/embed tags in .env.example:142 (OLLAMA_MODELS) and
# litellm/config.yaml — keep them in sync if those defaults ever change.
compute_ollama_models() {
  local smart="${1:-qwen3:8b}"
  local _FAST="qwen3:4b" _EMBED="qwen3-embedding:4b"
  case "$smart" in
    "$_FAST"|"$_EMBED") printf '%s,%s' "$_FAST" "$_EMBED" ;;
    *)                  printf '%s,%s,%s' "$smart" "$_FAST" "$_EMBED" ;;
  esac
}

# upsert_env_var KEY VALUE — idempotent in-place .env upsert (no duplicate lines).
# Mirrors scripts/init-secrets.sh::upsert_env_var (bash 3.2-portable awk).
upsert_env_var() {
  local k="$1" v="$2" tmp
  tmp="$(mktemp)" || { printf 'upsert_env_var: mktemp failed\n' >&2; return 1; }
  awk -v k="$k" -v v="$v" '
    index($0, k "=") == 1 { if (!seen) { print k "=" v; seen = 1 } ; next }
    { print }
    END { if (!seen) print k "=" v }
  ' .env > "$tmp" || { rm -f "$tmp"; printf 'upsert_env_var: awk rewrite of .env failed\n' >&2; return 1; }
  mv "$tmp" .env || { rm -f "$tmp"; printf 'upsert_env_var: mv to .env failed\n' >&2; return 1; }
}
