#!/usr/bin/env bash
# uninstall.sh — tiered, contained teardown of a managed JARVIS install.
#
# Reached through `jarvis-research uninstall`, which vouches for the target with
# `--repo <dir>`. Four escalating tiers, each of which enumerates the exact
# resources it will remove and confirms before acting:
#   1 stop  — docker compose down (containers + network; data, images, files kept)
#   2 app   — tier 1 + the JARVIS application images (cleanly reinstallable)
#   3 data  — tier 2 + the named data volumes (irreversible; typed confirmation)
#   4 purge — tier 3 + third-party images (each confirmed), .env, secrets/,
#             shared/, the registry line, the launcher, and the clone itself
#
# Containment is absolute: the target is canonicalised (symlinks resolved) and a
# root / $HOME / ancestor-of-$HOME / non-JARVIS target is refused with zero
# mutation. Every deletion names its exact resource — there is no bulk `prune`.
#
# Flags: --dry-run (enumerate only), --tier N, --yes (skip ordinary [y/N] prompts
# only), --keep-data (cap at tier 2), --all (tier 4). The destructive gates — the
# data project-name entry, the purge phrase, the per-image third-party confirms —
# stay mandatory interactive prompts that --yes/--all can never satisfy.
#
# Exit codes: 0 ok · 1 refused/failed · 2 usage · 3 environment (no docker).
set -euo pipefail

# -----------------------------------------------------------------------------
# Presentation + failure primitives (mirrored from jarvis-research.sh).
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_RESET=""
fi
info() { printf '%s[INFO]%s  %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[ERROR]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }
die()         { err "$1"; printf '        %s%s%s\n' "$C_YELLOW" "${2:-}" "$C_RESET" >&2; exit 1; }
usage_error() { err "$1"; printf '        %sRun: jarvis-research help%s\n' "$C_YELLOW" "$C_RESET" >&2; exit 2; }
env_die()     { err "$1"; printf '        %s%s%s\n' "$C_YELLOW" "${2:-}" "$C_RESET" >&2; exit 3; }

PURGE_PHRASE="I-UNDERSTAND-BACKUPS-BECOME-UNRECOVERABLE"
STATE_DIR="${JARVIS_CLI_CONFIG_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/jarvis-research}"
INSTALLS_FILE="${STATE_DIR}/installs"
BIN_DIR="${JARVIS_CLI_BIN_DIR:-${HOME}/.local/bin}"
TIER_NAME=(unused stop app data purge)

# -----------------------------------------------------------------------------
# Argument parsing.
# -----------------------------------------------------------------------------
DRY_RUN=0; SKIP_ORDINARY=0; KEEP_DATA=0; TIER=0; REPO_OVERRIDE=""
KEY_EXPORT_TARGET=""; TP_CONFIRMED=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)   DRY_RUN=1; shift ;;
    --yes|-y)    SKIP_ORDINARY=1; shift ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --all)       TIER=4; SKIP_ORDINARY=1; shift ;;
    --tier)      TIER="${2:-}"; shift 2 ;;
    --tier=*)    TIER="${1#--tier=}"; shift ;;
    --repo)      REPO_OVERRIDE="${2:-}"; shift 2 ;;
    --repo=*)    REPO_OVERRIDE="${1#--repo=}"; shift ;;
    *)           usage_error "uninstall: unknown option '$1'" ;;
  esac
done
case "$TIER" in 0|1|2|3|4) : ;; *) usage_error "uninstall: --tier must be 1, 2, 3, or 4 (got '$TIER')" ;; esac
# --yes/--all skip only the ordinary prompts; a non-interactive run must still
# name its tier, never fall into the menu.
if [ "$SKIP_ORDINARY" -eq 1 ] && [ "$TIER" -eq 0 ]; then
  usage_error "--yes requires an explicit --tier N (or --all)"
fi

UNINSTALL_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/setup_lib.sh
# shellcheck disable=SC1091
. "$UNINSTALL_SCRIPT_DIR/setup_lib.sh"
command -v python3 >/dev/null 2>&1 \
  || env_die "Python 3 is required for safe path containment." \
    "Install Python 3, then re-run the uninstall."

# -----------------------------------------------------------------------------
# Target resolution + canonicalisation (symlinks resolved before any check).
# -----------------------------------------------------------------------------
if [ -n "$REPO_OVERRIDE" ]; then
  RAW="$REPO_OVERRIDE"; MANAGED_VIA_FLAG=1
else
  RAW="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd -P)" || RAW=""
  MANAGED_VIA_FLAG=0
fi
[ -n "$RAW" ] || die "No uninstall target given and none could be derived." "Pass --repo <install-dir>."
REPO="$(canonical_path_portable "$RAW" 2>/dev/null || true)"
[ -n "$REPO" ] || die "Cannot resolve the target path '${RAW}'." "Pass --repo <install-dir>."

# -----------------------------------------------------------------------------
# Containment: never operate on a dangerous or unmanaged target.
# -----------------------------------------------------------------------------
HOME_CANON="$(canonical_path_portable "$HOME" 2>/dev/null || true)"
[ -n "$HOME_CANON" ] \
  || die "Cannot resolve your home directory safely." "Check HOME, then re-run."
case "$REPO" in
  /) die "Refusing to uninstall: the target resolves to the filesystem root (${REPO})." "This is never a JARVIS install." ;;
esac
if [ "$REPO" = "$HOME_CANON" ]; then
  die "Refusing to uninstall: the target is your home directory (${REPO})." "Point --repo at the JARVIS clone, not \$HOME."
fi
case "$HOME_CANON/" in
  "$REPO"/*) die "Refusing to uninstall: ${REPO} is an ancestor of your home directory." "Point --repo at the JARVIS clone itself." ;;
esac
if [ ! -f "$REPO/docker-compose.yml" ] || [ ! -f "$REPO/versions.env" ]; then
  die "${REPO} is not a JARVIS install (missing docker-compose.yml or versions.env)." "Point --repo at the install directory."
fi
if [ "$MANAGED_VIA_FLAG" -ne 1 ]; then
  if ! { [ -f "$INSTALLS_FILE" ] && grep -qxF "$REPO" "$INSTALLS_FILE"; }; then
    die "${REPO} is not a registered JARVIS install." "Run: jarvis-research register   (or pass --repo)"
  fi
fi

cd "$REPO"

_acquire_lifecycle_lock() {
  local rc=0
  claim_host_lifecycle_lock "$REPO" || rc=$?
  case "$rc" in
    0) ;;
    3) die "Another lifecycle operation is already running for this JARVIS install." \
         "Wait for it to finish, then re-run the uninstall." ;;
    *) die "The per-install lifecycle lock is unavailable or unsafe." \
         "No changes were made. Check ${STATE_DIR}, then run: jarvis-research doctor" ;;
  esac
  rc=0
  claim_lifecycle_operation "$REPO" uninstall || rc=$?
  case "$rc" in
    0) UNINSTALL_LIFECYCLE_CLAIMED=1 ;;
    3|4) die "Another lifecycle operation is active or needs recovery." \
           "No changes were made. Finish that operation, then re-run the uninstall." ;;
    *) die "The private lifecycle volume is unavailable or unsafe." \
         "No changes were made. Check Docker and this install's postgres_backups volume, then run: jarvis-research doctor" ;;
  esac
}

UNINSTALL_LIFECYCLE_CLAIMED=0
UNINSTALL_LIFECYCLE_ID=""
UNINSTALL_MUTATION_STARTED=0
_cleanup_uninstall_lifecycle() {
  local rc=$? action=clear
  trap - EXIT
  if [ "$UNINSTALL_LIFECYCLE_CLAIMED" -eq 1 ]; then
    [ "$UNINSTALL_MUTATION_STARTED" -ne 1 ] || action=retain
    finish_lifecycle_operation "$REPO" uninstall "$action" 2>/dev/null || true
  fi
  exit "$rc"
}

# -----------------------------------------------------------------------------
# Resource enumeration (pure; stub-friendly; single source for preview + action).
# -----------------------------------------------------------------------------
# _jarvis_image_refs — the exact GHCR application refs for this install, derived
# from PUBLISHED_SERVICES_BASE and .env's JARVIS_VERSION / TORCH_VARIANT_SUFFIX
# (paper_ingestion carries the torch suffix; telegram_bot only when configured).
_jarvis_image_refs() {
  local ns="ghcr.io/limitcycle-oss/jarvis-" svc ver suffix
  ver="$(sed -n 's/^JARVIS_VERSION=//p' .env 2>/dev/null | head -1)"; ver="${ver:-unknown}"
  suffix="$(sed -n 's/^TORCH_VARIANT_SUFFIX=//p' .env 2>/dev/null | head -1)"
  for svc in "${PUBLISHED_SERVICES_BASE[@]}"; do
    if [ "$svc" = "paper_ingestion" ]; then
      printf '%s%s:%s%s\n' "$ns" "${svc//_/-}" "$ver" "$suffix"
    else
      printf '%s%s:%s\n' "$ns" "${svc//_/-}" "$ver"
    fi
  done
  if grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' .env 2>/dev/null; then
    printf '%stelegram-bot:%s\n' "$ns" "$ver"
  fi
  return 0
}

# _third_party_image_pins — the pinned upstream image refs from versions.env
# (postgres/ollama/qdrant/caddy/... may be shared with other projects).
_third_party_image_pins() { sed -n 's/^[A-Z0-9_]*_IMAGE=//p' versions.env 2>/dev/null | sort -u; }

# _project_volumes — the named volumes declared in the compose volumes: block.
_project_volumes() {
  local file
  if [ "${#COMPOSE_FILES[@]}" -gt 0 ]; then
    for file in "${COMPOSE_FILES[@]}"; do
      awk '
        /^volumes:/ { inblock=1; next }
        inblock && /^[^[:space:]]/ { inblock=0 }
        inblock && /^[[:space:]]+[A-Za-z0-9_]+:/ { gsub(/[[:space:]]/, ""); sub(/:.*/, ""); print }
      ' "$file" 2>/dev/null
    done | sort -u
  else
    awk '
      /^volumes:/ { inblock=1; next }
      inblock && /^[^[:space:]]/ { inblock=0 }
      inblock && /^[[:space:]]+[A-Za-z0-9_]+:/ { gsub(/[[:space:]]/, ""); sub(/:.*/, ""); print }
    ' docker-compose.yml 2>/dev/null
  fi
}

# _compose_project_name — COMPOSE_PROJECT_NAME from .env, else the compose default
# (the clone's basename lowercased, restricted to [a-z0-9_-]).
_compose_project_name() {
  local name
  name="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env 2>/dev/null | head -1)"
  [ -n "$name" ] || name="$(basename "$REPO" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  printf '%s' "$name"
}

_path_inside() { case "$1/" in "$2"/*) return 0 ;; esac; return 1; }

# Resolve the Compose target exclusively from the managed install. Caller-owned
# COMPOSE_* selectors must never be able to redirect an uninstall to a sibling
# project. Repository-configured hardware overlays remain supported, but every
# file must resolve inside this install and the canonical base file must lead.
COMPOSE_PROJECT=""
COMPOSE_CONFIG_LABEL=""
declare -a COMPOSE_FILES=()
_init_compose_target() {
  local raw item candidate canon joined="" seen=""
  local -a requested_files=()
  COMPOSE_PROJECT="$(_compose_project_name)"
  if ! printf '%s' "$COMPOSE_PROJECT" | grep -Eq '^[a-z0-9][a-z0-9_-]*$'; then
    die "The install has an invalid Compose project name '${COMPOSE_PROJECT}'." "Fix COMPOSE_PROJECT_NAME in ${REPO}/.env, then re-run."
  fi

  raw="$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | head -1)"
  case "$raw" in
    \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
    \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
  esac
  if [ -z "$raw" ]; then
    raw="docker-compose.yml"
    [ ! -f "${REPO}/docker-compose.override.yml" ] || raw="${raw}:docker-compose.override.yml"
  fi
  IFS=':' read -r -a requested_files <<< "$raw"
  for item in "${requested_files[@]}"; do
    [ -n "$item" ] || die "The install's COMPOSE_FILE list contains an empty entry." "Fix COMPOSE_FILE in ${REPO}/.env, then re-run."
    case "$item" in
      /*) candidate="$item" ;;
      *) candidate="${REPO}/${item}" ;;
    esac
    canon="$(canonical_path_portable "$candidate" 2>/dev/null || true)"
    if [ -z "$canon" ] || [ ! -f "$canon" ] || ! _path_inside "$canon" "$REPO"; then
      die "Compose file '${item}' is missing or outside the managed install." "Fix COMPOSE_FILE in ${REPO}/.env, then re-run."
    fi
    if printf '%s\n' "$seen" | grep -qxF "$canon"; then
      die "Compose file '${item}' is listed more than once." "Fix COMPOSE_FILE in ${REPO}/.env, then re-run."
    fi
    COMPOSE_FILES+=("$canon")
    seen="${seen}${canon}"$'\n'
    joined="${joined:+${joined},}${canon}"
  done
  if [ "${COMPOSE_FILES[0]:-}" != "${REPO}/docker-compose.yml" ]; then
    die "The install's Compose target does not start with ${REPO}/docker-compose.yml." "Fix COMPOSE_FILE in ${REPO}/.env, then re-run."
  fi
  COMPOSE_CONFIG_LABEL="$joined"
}

# The one permitted Compose entrypoint. Explicit CLI arguments outrank values
# loaded from .env, while env -u removes ambient selectors inherited from the
# caller. The managed .env still supplies interpolation values and profiles.
_compose() {
  local -a cmd=(docker compose --project-directory "$REPO" --env-file "$REPO/.env" -p "$COMPOSE_PROJECT")
  local file
  for file in "${COMPOSE_FILES[@]}"; do cmd+=(-f "$file"); done
  env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
      -u COMPOSE_PATH_SEPARATOR -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE \
      "${cmd[@]}" "$@"
}

_stack_is_up() { [ -n "$(_compose ps -q 2>/dev/null)" ]; }
VERIFIED_COMPOSE_IDS=""

# Before any `compose down`, prove every container carrying this project label
# belongs to this exact checkout and Compose-file set. Tiers 1 and 2 may remain
# idempotent when both containers and the project network are already absent;
# a leftover network without container metadata cannot be tied to this checkout
# and therefore fails closed.
_verify_compose_down_target() {
  local allow_absent="$1" ids networks cid labels
  local label_project label_workdir label_configs
  VERIFIED_COMPOSE_IDS=""
  if ! ids="$(docker ps -aq --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" 2>/dev/null)"; then
    err "Docker could not enumerate the containers owned by project '${COMPOSE_PROJECT}'."
    printf '        %sNo containers or networks were changed. Check Docker, then re-run.%s\n' "$C_YELLOW" "$C_RESET" >&2
    return 1
  fi
  if [ -z "$ids" ]; then
    if ! networks="$(docker network ls --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" --format '{{.ID}}' 2>/dev/null)"; then
      err "Docker could not enumerate the networks owned by project '${COMPOSE_PROJECT}'."
      printf '        %sNo containers or networks were changed. Check Docker, then re-run.%s\n' "$C_YELLOW" "$C_RESET" >&2
      return 1
    fi
    if [ -n "$networks" ]; then
      err "Cannot verify which installation owns the remaining Compose network: no container metadata remains."
      printf '        %sNo containers or networks were changed. Inspect the project-labelled network, then re-run.%s\n' "$C_YELLOW" "$C_RESET" >&2
      return 1
    fi
    if [ "$allow_absent" -eq 1 ]; then
      return 0
    fi
    err "Cannot verify which installation owns the data volumes: no Compose container metadata remains."
    printf '        %sStart this installation once, then re-run the data uninstall; no volumes were changed.%s\n' "$C_YELLOW" "$C_RESET" >&2
    return 1
  fi

  while IFS= read -r cid; do
    [ -n "$cid" ] || continue
    if ! labels="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}|{{ index .Config.Labels "com.docker.compose.project.working_dir" }}|{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$cid" 2>/dev/null)"; then
      err "Docker could not inspect Compose ownership metadata for container '${cid}'."
      printf '        %sNo containers, networks, or volumes were changed. Check Docker, then re-run.%s\n' "$C_YELLOW" "$C_RESET" >&2
      return 1
    fi
    IFS='|' read -r label_project label_workdir label_configs <<< "$labels"
    if [ "$label_project" != "$COMPOSE_PROJECT" ] \
       || [ "$label_workdir" != "$REPO" ] \
       || [ "$label_configs" != "$COMPOSE_CONFIG_LABEL" ]; then
      err "Compose ownership labels do not match this managed installation; refusing teardown."
      printf '        %sExpected project %s, working directory %s, and this install\x27s Compose files. Nothing was changed.%s\n' \
        "$C_YELLOW" "$COMPOSE_PROJECT" "$REPO" "$C_RESET" >&2
      return 1
    fi
  done <<< "$ids"
  VERIFIED_COMPOSE_IDS="$ids"
  return 0
}

# Before Compose receives --volumes, additionally prove every project-labelled
# volume is an exact named volume declared by this checkout. Data deletion never
# accepts an already-absent container set because it has no trustworthy
# working-directory evidence.
_verify_compose_volume_target() {
  local ids cid declared volumes name volume_labels volume_project logical expected_name
  local mounted mounted_name allowed_names=""
  _verify_compose_down_target 0 || return 1
  ids="$VERIFIED_COMPOSE_IDS"

  declared="$(_project_volumes)"
  if [ -z "$declared" ]; then
    err "The canonical Compose file declares no removable named volumes; refusing volume deletion."
    printf '        %sNo volumes were changed. Validate %s, then re-run.%s\n' "$C_YELLOW" "${REPO}/docker-compose.yml" "$C_RESET" >&2
    return 1
  fi
  if ! volumes="$(docker volume ls --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" --format '{{.Name}}' 2>/dev/null)"; then
    err "Docker could not enumerate the volumes owned by project '${COMPOSE_PROJECT}'."
    printf '        %sNo volumes were changed. Check Docker, then re-run.%s\n' "$C_YELLOW" "$C_RESET" >&2
    return 1
  fi
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    if ! volume_labels="$(docker volume inspect --format '{{ index .Labels "com.docker.compose.project" }}|{{ index .Labels "com.docker.compose.volume" }}' "$name" 2>/dev/null)"; then
      err "Docker could not inspect volume '${name}'; refusing deletion."
      printf '        %sNo volumes were changed. Check Docker, then re-run.%s\n' "$C_YELLOW" "$C_RESET" >&2
      return 1
    fi
    IFS='|' read -r volume_project logical <<< "$volume_labels"
    expected_name="${COMPOSE_PROJECT}_${logical}"
    if [ "$volume_project" != "$COMPOSE_PROJECT" ] \
       || [ -z "$logical" ] \
       || ! printf '%s\n' "$declared" | grep -qxF "$logical" \
       || [ "$name" != "$expected_name" ]; then
      err "Volume '${name}' is not an exact declared volume of this managed installation; refusing deletion."
      printf '        %sNo volumes were changed. Inspect the project with: docker volume inspect %s%s\n' "$C_YELLOW" "$name" "$C_RESET" >&2
      return 1
    fi
    allowed_names="${allowed_names}${name}"$'\n'
  done <<< "$volumes"

  # Compose --volumes also removes anonymous volumes attached to selected
  # containers. Reject any mounted volume that was not proven above.
  while IFS= read -r cid; do
    [ -n "$cid" ] || continue
    if ! mounted="$(docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' "$cid" 2>/dev/null)"; then
      err "Docker could not inspect the mounts for container '${cid}'; refusing volume deletion."
      printf '        %sNo volumes were changed. Check Docker, then re-run.%s\n' "$C_YELLOW" "$C_RESET" >&2
      return 1
    fi
    while IFS= read -r mounted_name; do
      [ -n "$mounted_name" ] || continue
      if ! printf '%s\n' "$allowed_names" | grep -qxF "$mounted_name"; then
        err "Container '${cid}' uses volume '${mounted_name}', which is outside the verified deletion allowlist."
        printf '        %sNo volumes were changed. Remove or reconcile that mount, then re-run.%s\n' "$C_YELLOW" "$C_RESET" >&2
        return 1
      fi
    done <<< "$mounted"
  done <<< "$ids"
  return 0
}

_init_compose_target

# -----------------------------------------------------------------------------
# Docker daemon gate: no daemon -> refuse every tier, print an orphan inventory.
# There is deliberately no file-only cleanup path.
# -----------------------------------------------------------------------------
_print_orphan_inventory() {
  local proj; proj="$(_compose_project_name)"
  printf '\n%sOrphaned resources that remain until Docker is available:%s\n' "$C_BOLD" "$C_RESET" >&2
  printf '  compose project: %s\n' "$proj" >&2
  printf '  network:         %s_jarvis (bridge)\n' "$proj" >&2
  printf '  services:        %s\n' "$MANDATORY_HEALTH_BASE" >&2
  printf '  named volumes:\n' >&2
  _project_volumes | sed 's/^/    /' >&2
  printf '  on-disk files:   %s/.env, %s/secrets, %s/shared\n' "$REPO" "$REPO" "$REPO" >&2
}
if ! docker info >/dev/null 2>&1; then
  err "The Docker daemon is not reachable; uninstall needs it to remove containers, images, and volumes."
  _print_orphan_inventory
  env_die "No changes were made." "Start Docker and re-run, or remove the resources above by hand."
fi
if [ "$DRY_RUN" -ne 1 ]; then
  _acquire_lifecycle_lock
  trap _cleanup_uninstall_lifecycle EXIT
fi

# -----------------------------------------------------------------------------
# Tier selection (interactive menu by default).
# -----------------------------------------------------------------------------
_read_line() { local p="$1" line; printf '%s' "$p" >&2; IFS= read -r line || true; printf '%s' "$line"; }
_confirm() { case "$(_read_line "$1 [y/N]: ")" in y|Y|yes|Yes|YES) return 0 ;; *) return 1 ;; esac; }

if [ "$TIER" -eq 0 ]; then
  printf '%sUninstall %s — choose a tier:%s\n' "$C_BOLD" "$REPO" "$C_RESET" >&2
  printf '  1) stop  — stop containers and remove the network (data, images, files kept)\n' >&2
  printf '  2) app   — also remove the JARVIS application images (cleanly reinstallable)\n' >&2
  printf '  3) data  — also delete the named data volumes (IRREVERSIBLE without a backup)\n' >&2
  printf '  4) purge — also remove third-party images, .env, secrets/, shared/, and the clone\n' >&2
  sel="$(_read_line 'Tier [1-4] (empty to abort): ')"
  case "$sel" in 1|2|3|4) TIER="$sel" ;; *) info "No tier selected; nothing was done."; exit 0 ;; esac
fi

if [ "$KEEP_DATA" -eq 1 ] && [ "$TIER" -gt 2 ]; then
  warn "--keep-data caps this run at tier 2 (app); volumes and files are preserved."
  TIER=2
fi

# -----------------------------------------------------------------------------
# Action primitive: announce every destructive step; execute unless dry-run. The
# stable LABEL is what the dry-run PLAN and the real DONE lines share, so a
# dry-run enumerates exactly the set a real run mutates.
# -----------------------------------------------------------------------------
_step() {
  local label="$1"; shift
  if [ "$DRY_RUN" -eq 1 ]; then printf 'PLAN %s\n' "$label"; return 0; fi
  printf 'DONE %s\n' "$label"
  "$@"
}

# -----------------------------------------------------------------------------
# Preview (human-readable) — shown before the real gates.
# -----------------------------------------------------------------------------
_preview() {
  info "Uninstall plan for ${REPO} (tier ${TIER} — ${TIER_NAME[$TIER]}):"
  printf '  containers + network: docker compose down%s\n' "$([ "$TIER" -ge 3 ] && printf -- ' --volumes')" >&2
  if [ "$TIER" -ge 2 ]; then printf '  application images:\n' >&2; _jarvis_image_refs | sed 's/^/    /' >&2; fi
  if [ "$TIER" -ge 3 ]; then printf '  data volumes (DELETED — irreversible):\n' >&2; _project_volumes | sed 's/^/    /' >&2; fi
  if [ "$TIER" -ge 4 ]; then
    printf '  third-party images (each confirmed individually):\n' >&2; _third_party_image_pins | sed 's/^/    /' >&2
    printf '  files: %s/.env, %s/secrets, %s/shared, and the clone directory\n' "$REPO" "$REPO" "$REPO" >&2
  fi
}

# -----------------------------------------------------------------------------
# Gates: collect every confirmation BEFORE any mutation. A failed gate exits with
# the clone completely untouched.
# -----------------------------------------------------------------------------
_data_backup_offer() {
  local backup_output
  if _stack_is_up; then
    if [ "$SKIP_ORDINARY" -ne 1 ] && _confirm "Capture a backup with scripts/backup.sh before deleting the volumes?"; then
      if ! backup_output="$(_compose run --rm --no-deps \
          --entrypoint /usr/local/bin/backup.sh postgres-backup 2>&1)"; then
        [ -z "$backup_output" ] || printf '%s\n' "$backup_output" >&2
        die "Backup did not complete; refusing to delete the data volumes." \
          "No data was deleted. Fix the backup error above, then re-run the uninstall."
      fi
      [ -z "$backup_output" ] || printf '%s\n' "$backup_output"
      if printf '%s' "$backup_output" | grep -qF 'another backup is already running; skipping'; then
        die "Another backup was still running; refusing to delete the data volumes." \
          "No data was deleted. Wait for that backup to finish, then re-run the uninstall."
      fi
      ok "Backup completed inside the postgres-backup service."
    fi
  else
    info "The stack is not running; to copy volume data cold first, these named volumes live under the Docker data root:"
    _project_volumes | sed 's/^/    /' >&2
  fi
}

_purge_key_gate() {
  local path canon attempts=0
  while :; do
    path="$(_read_line "Path OUTSIDE the clone to export ${REPO}/secrets/backup_encrypt_key.txt (empty to skip): ")"
    if [ -z "$path" ]; then
      local phrase
      phrase="$(_read_line "Deleting secrets/ orphans every encrypted off-host backup forever. Type ${PURGE_PHRASE} to proceed without exporting: ")"
      [ "$phrase" = "$PURGE_PHRASE" ] && { KEY_EXPORT_TARGET=""; return 0; }
      die "Backup key not exported and the confirmation phrase was not given; refusing to purge." "No changes were made."
    fi
    canon="$(canonical_path_portable "$path" 2>/dev/null || true)"
    [ -n "$canon" ] \
      || die "Cannot resolve the backup-key export path safely." \
        "Choose another path and re-run; no changes were made."
    if _path_inside "$canon" "$REPO"; then
      warn "Export path ${canon} is inside the clone; it would be deleted with it. Choose a path elsewhere."
      attempts=$((attempts + 1))
      [ "$attempts" -ge 3 ] && die "Too many in-clone export paths; refusing to purge." "No changes were made."
      continue
    fi
    KEY_EXPORT_TARGET="$canon"; return 0
  done
}

_collect_third_party_confirms() {
  local -a pins=() confirmed=(); local ref skipped=""
  while IFS= read -r ref; do [ -n "$ref" ] && pins+=("$ref"); done < <(_third_party_image_pins)
  for ref in ${pins[@]+"${pins[@]}"}; do
    if _confirm "Remove third-party image ${ref}? It may be used by other projects."; then
      confirmed+=("$ref")
    else
      skipped="${skipped:+$skipped }$ref"
    fi
  done
  TP_CONFIRMED=(${confirmed[@]+"${confirmed[@]}"})
  [ -n "$skipped" ] && info "Keeping shared third-party images (not confirmed): ${skipped}"
  return 0
}

_run_gates() {
  if [ "$TIER" -ge 3 ]; then
    _verify_compose_volume_target || die "The data-volume target could not be verified; refusing the destructive tier." \
      "No volumes were changed. Resolve the ownership warning above, then re-run."
  else
    _verify_compose_down_target 1 || die "The Compose target could not be verified; refusing teardown." \
      "Nothing was changed. Resolve the ownership warning above, then re-run."
  fi
  if [ "$SKIP_ORDINARY" -ne 1 ]; then
    _confirm "Proceed with the '${TIER_NAME[$TIER]}' uninstall of ${REPO}?" || { info "Aborted; nothing was done."; exit 0; }
  fi
  if [ "$TIER" -ge 3 ]; then
    _data_backup_offer
    local proj typed; proj="$(_compose_project_name)"
    typed="$(_read_line "Type the compose project name '${proj}' to confirm deleting the data volumes: ")"
    [ "$typed" = "$proj" ] || die "Project name did not match ('${typed}' != '${proj}'); refusing to delete data volumes." "No changes were made."
  fi
  if [ "$TIER" -ge 4 ]; then
    _purge_key_gate
    _collect_third_party_confirms
  fi
}

# -----------------------------------------------------------------------------
# Destructive helpers (invoked only through _step, never in dry-run).
# -----------------------------------------------------------------------------
# _rmi_ref REF — remove one image, tolerating an already-absent ref. A partial
# prior run (or a declared-but-never-pulled variant) must never abort teardown
# after the containers/volumes are already gone but before the files are removed.
_rmi_ref() { docker rmi -f "$1" 2>/dev/null || warn "Image not present, skipping: $1"; }

_stop_lifecycle_requesters() {
  # Persist recovery ownership if Docker reports a partial/unknown stop. This
  # marker is set before the first mutation attempt, not after its response.
  UNINSTALL_MUTATION_STARTED=1
  _compose stop paper_ingestion postgres-backup
}

_retain_uninstall_lifecycle() {
  UNINSTALL_LIFECYCLE_ID="${JARVIS_SHARED_LIFECYCLE_ID:-}"
  printf '%s' "$UNINSTALL_LIFECYCLE_ID" | grep -Eq '^[0-9a-f]{32}$' \
    || die "The uninstall lifecycle identity is invalid." \
      "Nothing else was removed. Re-run the uninstall for recovery."
  finish_lifecycle_operation "$REPO" uninstall retain \
    || die "Could not safely release the uninstall holder." \
      "Request-producing services are stopped. Re-run the uninstall for recovery."
  UNINSTALL_LIFECYCLE_CLAIMED=0
}

_clear_retained_uninstall_lifecycle() {
  clear_retained_lifecycle_operation "$REPO" uninstall "$UNINSTALL_LIFECYCLE_ID" \
    || die "The stack stopped, but uninstall recovery state could not be cleared." \
      "Re-run the same uninstall command; no service is running."
}

_export_backup_key() {
  local dst="$1" src="$REPO/secrets/backup_encrypt_key.txt" canon parent tmp
  if [ -f "$src" ]; then
    canon="$(canonical_path_portable "$dst" 2>/dev/null || true)"
    if [ -z "$canon" ] || _path_inside "$canon" "$REPO" || [ -d "$canon" ]; then
      die "The backup-key export target is unavailable or inside the clone." \
        "Choose a file path outside ${REPO}, then re-run."
    fi
    parent="$(dirname "$canon")"
    mkdir -p "$parent" \
      || die "Could not create the backup-key export directory ${parent}." \
        "Choose a writable path and re-run."
    parent="$(canonical_path_portable "$parent" 2>/dev/null || true)"
    if [ -z "$parent" ] || _path_inside "$parent" "$REPO"; then
      die "The backup-key export directory resolves inside the clone." \
        "Choose a path elsewhere and re-run."
    fi
    tmp="$(mktemp "${parent}/.jarvis-backup-key.XXXXXX")" \
      || die "Could not stage the backup key in ${parent}." \
        "Choose a writable path and re-run."
    if ! cp -p "$src" "$tmp" || ! chmod 600 "$tmp" || ! mv -f "$tmp" "$canon"; then
      rm -f "$tmp"
      die "Could not copy the backup key to ${canon}." "Choose a writable path and re-run."
    fi
  else
    warn "No backup encryption key at ${src}; nothing to export (backups may be unencrypted)."
  fi
}
_remove_registry_line() {
  [ -f "$INSTALLS_FILE" ] || return 0
  local tmp; tmp="$(mktemp)"
  grep -vxF "$REPO" "$INSTALLS_FILE" 2>/dev/null > "$tmp" || true
  if [ "$RAW" != "$REPO" ]; then
    local tmp2; tmp2="$(mktemp)"
    grep -vxF "$RAW" "$tmp" 2>/dev/null > "$tmp2" || true
    mv "$tmp2" "$tmp"
  fi
  if [ -s "$tmp" ]; then mv "$tmp" "$INSTALLS_FILE"; else rm -f "$tmp" "$INSTALLS_FILE"; fi
}
_remove_shim_if_last() {
  if [ -s "$INSTALLS_FILE" ]; then
    info "Other installs remain registered; leaving the jarvis-research launcher in place."
    return 0
  fi
  local shim="${BIN_DIR}/jarvis-research"
  if [ "$DRY_RUN" -eq 1 ] || [ -e "$shim" ]; then _step "shim ${shim}" rm -f "$shim"; fi
  return 0
}
# _self_delete_clone — a running script cannot rm -rf its own path, so copy a
# minimal remover to a temp file and exec it; it deletes the clone, then itself.
_self_delete_clone() {
  local target="$1" remover
  remover="$(mktemp)"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'cd / 2>/dev/null || true\n'
    printf 'rm -rf -- %q\n' "$target"
    printf 'rm -f -- %q\n' "$remover"
  } > "$remover"
  chmod +x "$remover"
  exec bash "$remover"
}

# -----------------------------------------------------------------------------
# Execution: run the selected tier's steps (highest tier includes the lower).
# -----------------------------------------------------------------------------
_run_tier() {
  local ref
  # Purge: export the backup key BEFORE any file removal (in dry-run, enumerate it
  # even though the operator has not yet chosen a path).
  if [ "$TIER" -ge 4 ] && { [ "$DRY_RUN" -eq 1 ] || [ -n "$KEY_EXPORT_TARGET" ]; }; then
    _step "key-export ${KEY_EXPORT_TARGET:-<operator-chosen path>}" _export_backup_key "$KEY_EXPORT_TARGET"
  fi
  # Keep the private named-volume holder alive while stopping every component
  # that can publish or consume lifecycle requests. The holder is a direct
  # Docker container without Compose labels, so Compose cannot stop it. Only
  # after producers are quiescent do we retain the ID-bound recovery state and
  # release the holder for the actual down/volume removal.
  _step "lifecycle-requesters-stop" _stop_lifecycle_requesters
  _step "lifecycle-holder-retain" _retain_uninstall_lifecycle
  # Stop containers + network (and volumes at tier >= 3).
  if [ "$TIER" -ge 3 ]; then
    if [ "$DRY_RUN" -ne 1 ]; then
      _verify_compose_volume_target || die "Compose ownership changed before data-volume teardown; refusing the destructive tier." \
        "No volumes were changed. Resolve the ownership warning above, then re-run."
    fi
    _step "compose-down-volumes" _compose down --remove-orphans --volumes
  else
    if [ "$DRY_RUN" -ne 1 ]; then
      _verify_compose_down_target 1 || die "Compose ownership changed before teardown; refusing the uninstall." \
        "Nothing was changed. Resolve the ownership warning above, then re-run."
    fi
    _step "compose-down" _compose down --remove-orphans
  fi
  if [ "$TIER" -lt 3 ]; then
    _step "lifecycle-state-clear" _clear_retained_uninstall_lifecycle
  fi
  # Application images by exact ref (an absent image is skipped, never fatal).
  if [ "$TIER" -ge 2 ]; then
    while IFS= read -r ref; do [ -n "$ref" ] && _step "image ${ref}" _rmi_ref "$ref"; done < <(_jarvis_image_refs)
  fi
  # Purge: confirmed third-party images, files, registry line, shim, clone.
  if [ "$TIER" -ge 4 ]; then
    for ref in ${TP_CONFIRMED[@]+"${TP_CONFIRMED[@]}"}; do _step "image ${ref}" _rmi_ref "$ref"; done
    _step "file ${REPO}/.env"    rm -f  "$REPO/.env"
    _step "file ${REPO}/secrets" rm -rf "$REPO/secrets"
    _step "file ${REPO}/shared"  rm -rf "$REPO/shared"
    _step "registry-line ${REPO}" _remove_registry_line
    _remove_shim_if_last
    _step "clone ${REPO}" _self_delete_clone "$REPO"   # execs; nothing runs after
  fi
}

# -----------------------------------------------------------------------------
# Main.
# -----------------------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  info "Dry run for ${REPO} (tier ${TIER} — ${TIER_NAME[$TIER]}); no changes will be made."
  # In dry-run the purge enumerates the full third-party set (no prompts run).
  if [ "$TIER" -ge 4 ]; then
    while IFS= read -r ref; do
      [ -n "$ref" ] && TP_CONFIRMED+=("$ref")
    done < <(_third_party_image_pins)
  fi
  _run_tier
  exit 0
fi

_preview
_run_gates
info "Proceeding with the tier ${TIER} (${TIER_NAME[$TIER]}) uninstall..."
_run_tier
ok "Uninstall (tier ${TIER} — ${TIER_NAME[$TIER]}) complete for ${REPO}."
