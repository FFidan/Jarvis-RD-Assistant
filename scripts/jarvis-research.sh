#!/usr/bin/env bash
# jarvis-research — the lifecycle CLI for a managed JARVIS install.
#
# This repo-tracked script holds ALL of the logic; the `jarvis-research` command
# on PATH is a fixed launcher (installed by scripts/setup_lib.sh::install_cli_shim)
# that resolves the most recently installed repo and execs this file with
# `--repo <dir> "$@"`. Shipping the logic with the repo means an update carries
# the newer CLI too.
#
# Subcommands:
#   update [--to <tag>] [--resume <tag>] [--yes]   transactional, DB-safe upgrade
#   status | start | stop | restart | logs         day-to-day container control
#   doctor                                          read-only health + preflight
#   repair                                          bounded, non-destructive recovery
#   register                                        record this repo as an install
#   uninstall [--dry-run] [--tier N] [--all] ...     tiered, contained teardown
#   version | help
#
# Exit codes: 0 ok · 1 refused/failed · 2 usage · 3 environment (no docker).
# The ONLY operation that advances the branch is a fast-forward merge to an
# approved release tag; the branch is never force-rewritten.
set -euo pipefail

# -----------------------------------------------------------------------------
# Presentation + failure primitives (die pattern mirrored from update.sh).
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi
info() { printf '%s[INFO]%s  %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[ERROR]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

# die MSG NEXT — a refused/failed operation. Two lines: what happened + what to
# run next. Every failure path also points at `jarvis-research doctor`.
die() {
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "$2" "$C_RESET" >&2
  exit 1
}
usage_error() { err "$1"; printf '        %sRun: jarvis-research help%s\n' "$C_YELLOW" "$C_RESET" >&2; exit 2; }
env_die() {
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "$2" "$C_RESET" >&2
  exit 3
}

# -----------------------------------------------------------------------------
# CLI state dir (deliberately NOT ~/.config/jarvis, which is chmod 700 and holds
# the API key — CLI bookkeeping must not live in a secret-bearing directory).
# -----------------------------------------------------------------------------
STATE_DIR="${JARVIS_CLI_CONFIG_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/jarvis-research}"
INSTALLS_FILE="${STATE_DIR}/installs"
PENDING_FILE_PATH="${STATE_DIR}/pending-update.json"

# -----------------------------------------------------------------------------
# Repository resolution.
# -----------------------------------------------------------------------------
_valid_repo() { [ -f "$1/docker-compose.yml" ] && [ -f "$1/versions.env" ]; }

# _find_repo_upwards START — the nearest ancestor of START (inclusive) that is a
# JARVIS checkout, or non-zero when none is.
_find_repo_upwards() {
  local d="$1"
  while [ -n "$d" ] && [ "$d" != "/" ]; do
    _valid_repo "$d" && { printf '%s' "$d"; return 0; }
    d="$(dirname "$d")"
  done
  _valid_repo "/" && { printf '/'; return 0; }
  return 1
}

# resolve_repo — echo the managed repo. Order: --repo / JARVIS_RESEARCH_HOME,
# then cwd-or-ancestor, then the first live line of the installs registry (stale
# lines skipped with a warning). Fails with actionable guidance otherwise.
resolve_repo() {
  local cand line
  if [ -n "${REPO_OVERRIDE:-}" ]; then
    _valid_repo "$REPO_OVERRIDE" && { printf '%s' "$REPO_OVERRIDE"; return 0; }
    die "--repo ${REPO_OVERRIDE} is not a JARVIS install (no docker-compose.yml + versions.env)." \
        "Point --repo at the install directory, or run: jarvis-research register"
  fi
  if [ -n "${JARVIS_RESEARCH_HOME:-}" ]; then
    _valid_repo "$JARVIS_RESEARCH_HOME" && { printf '%s' "$JARVIS_RESEARCH_HOME"; return 0; }
    die "JARVIS_RESEARCH_HOME (${JARVIS_RESEARCH_HOME}) is not a JARVIS install." \
        "Unset it or point it at the install directory; then run: jarvis-research doctor"
  fi
  cand="$(_find_repo_upwards "$PWD" 2>/dev/null || true)"
  [ -n "$cand" ] && { printf '%s' "$cand"; return 0; }
  if [ -f "$INSTALLS_FILE" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      if _valid_repo "$line"; then printf '%s' "$line"; return 0; fi
      warn "Skipping stale install path (no longer a JARVIS checkout): ${line}"
    done < "$INSTALLS_FILE"
  fi
  die "No JARVIS install found (not in a checkout, and none registered)." \
      "cd into your JARVIS directory and run: jarvis-research register"
}

# -----------------------------------------------------------------------------
# Pending-transaction file (written to the state dir before any branch advance).
# -----------------------------------------------------------------------------
_txn_write() {
  local phase="$1" target="$2" version="$3" backup_id="${4:-}" from_sha from_version
  mkdir -p "$STATE_DIR"
  from_sha="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  from_version="$(sed -n 's/^JARVIS_VERSION=//p' .env 2>/dev/null | head -1)"
  printf '{"from_sha":"%s","from_version":"%s","target":"%s","target_version":"%s","phase":"%s","started_at":"%s","backup_id":"%s"}\n' \
    "$from_sha" "${from_version:-unknown}" "$target" "$version" "$phase" "${UPDATE_START_EPOCH:-0}" "$backup_id" \
    > "$PENDING_FILE_PATH"
}
_txn_update_phase() {
  [ -f "$PENDING_FILE_PATH" ] || return 0
  local tmp; tmp="$(mktemp)"
  sed -E "s/\"phase\":\"[^\"]*\"/\"phase\":\"${1}\"/" "$PENDING_FILE_PATH" > "$tmp" && mv "$tmp" "$PENDING_FILE_PATH"
}
_txn_field() {
  [ -f "$PENDING_FILE_PATH" ] || return 0
  grep -oE "\"${1}\":\"[^\"]*\"" "$PENDING_FILE_PATH" 2>/dev/null | head -1 | sed -E "s/\"${1}\":\"([^\"]*)\"/\1/"
}

# -----------------------------------------------------------------------------
# Update-flow guards.
# -----------------------------------------------------------------------------
# (1) Only a registered, managed checkout may be updated by this tool.
_require_managed_install() {
  local registered=0 origin want
  if [ -n "${REPO_OVERRIDE:-}" ]; then
    registered=1
  elif [ -f "$INSTALLS_FILE" ] && grep -qxF "$REPO" "$INSTALLS_FILE"; then
    registered=1
  fi
  if [ "$registered" -ne 1 ]; then
    die "This JARVIS install is not registered with jarvis-research." \
        "Run: jarvis-research register   (or update by hand: git pull && ./update.sh)"
  fi
  want="${JARVIS_RESEARCH_REMOTE:-limitcycle-oss/jarvis-rd-assistant}"
  origin="$(git remote get-url origin 2>/dev/null || true)"
  if ! printf '%s' "$origin" | tr '[:upper:]' '[:lower:]' | grep -qF "$(printf '%s' "$want" | tr '[:upper:]' '[:lower:]')"; then
    die "Origin (${origin:-none}) is not the managed JARVIS repository." \
        "Update by hand: git pull && ./update.sh   (or set JARVIS_RESEARCH_REMOTE for a fork)"
  fi
}

# (2) The Docker daemon, and a clean, on-main, non-detached checkout.
_require_docker_daemon() {
  docker info >/dev/null 2>&1 \
    || env_die "The Docker daemon is not reachable." "Start Docker, then re-run: jarvis-research doctor"
}
_require_clean_main_checkout() {
  local branch; branch="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
  if [ -z "$branch" ]; then
    die "HEAD is detached; jarvis-research updates only a normal 'main' checkout." \
        "Run: git checkout main   (then: jarvis-research update)"
  fi
  if [ "$branch" != "main" ]; then
    die "You are on branch '${branch}', not 'main'; refusing to update a working branch." \
        "Run: git checkout main   (then: jarvis-research update)"
  fi
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    die "Your working tree has uncommitted changes; refusing to update." \
        "Commit or stash them, then re-run: jarvis-research update"
  fi
}

# -----------------------------------------------------------------------------
# Migration classification + the mandatory backup gate.
# -----------------------------------------------------------------------------
_active_profiles() {
  sed -n 's/^COMPOSE_PROFILES=//p' .env 2>/dev/null | head -1 | tr ',' ' '
}

# _new_migrations TARGET_REF — the migration files added between HEAD and the tag.
_new_migrations() {
  git diff --name-only "HEAD..$1" -- db/migrations/ 2>/dev/null || true
}

# _migrations_need_backup TARGET_REF MIGRATIONS — true when any new migration
# carries a data-changing statement. Read each migration's CONTENT from the tag
# (`git show`), NEVER the working tree — those files do not exist locally yet.
# Broad matcher over non-comment lines: fail toward taking a backup.
_migrations_need_backup() {
  local target_ref="$1" migs="$2" path content
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    content="$(git show "${target_ref}:${path}" 2>/dev/null || true)"
    if printf '%s\n' "$content" \
        | grep -vE '^[[:space:]]*(--|$)' \
        | grep -qiE 'DELETE[[:space:]]+FROM|DROP[[:space:]]|TRUNCATE|UPDATE[[:space:]]|ALTER[[:space:]]+TABLE.*(DROP|RENAME|ALTER[[:space:]]+COLUMN|SET[[:space:]]+DATA[[:space:]]+TYPE|TYPE[[:space:]])'; then
      return 0
    fi
  done <<< "$migs"
  return 1
}

_backup_dir() {
  [ -n "${JARVIS_BACKUP_DIR:-}" ] && { printf '%s' "$JARVIS_BACKUP_DIR"; return 0; }
  _container_mount_source postgres-backup /backups
}
_backup_trigger_dir() {
  [ -n "${JARVIS_BACKUP_TRIGGER_DIR:-}" ] && { printf '%s' "$JARVIS_BACKUP_TRIGGER_DIR"; return 0; }
  _container_mount_source postgres-backup /backup-trigger
}
# _container_mount_source SVC DEST — the host source path of SVC's DEST mount.
_container_mount_source() {
  local cid src
  cid="$(docker compose ps -q "$1" 2>/dev/null | head -1 || true)"
  [ -n "$cid" ] || return 1
  src="$(docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{.Source}}{{end}}{{end}}" "$cid" 2>/dev/null || true)"
  [ -n "$src" ] || return 1
  printf '%s' "$src"
}

_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

# _newest_fresh_manifest DIR START_EPOCH — newest manifest_*.json whose mtime is
# at/after START_EPOCH (i.e. produced after this update began).
_newest_fresh_manifest() {
  local dir="$1" start="$2" f mt newest="" newest_mt=0
  for f in "$dir"/manifest_*.json; do
    [ -f "$f" ] || continue
    mt="$(_mtime "$f")" || continue
    [ -n "$mt" ] && [ "$mt" -ge "$start" ] || continue
    if [ "$mt" -ge "$newest_mt" ]; then newest_mt="$mt"; newest="$f"; fi
  done
  [ -n "$newest" ] && printf '%s' "$newest"
}

# _verify_backup_archive_set DIR MANIFEST — recompute and match every archive the
# manifest lists. HARD-required: the jarvis DB archive, plus the secrets archive
# when the set is encrypted. A missing/failed Qdrant archive is reported as the
# optional loss it is; it never passes or fails the gate silently. Returns 0 only
# when every HARD-required archive is present, non-zero, and sha256-matched.
_verify_backup_archive_set() {
  local dir="$1" manifest="$2" entries e fn sha size actual_sha actual_size
  local enc_on=0 jarvis_seen=0 jarvis_ok=0 secrets_seen=0 secrets_ok=0 hard_fail=0 ok reason
  entries="$(grep -oE '\{"filename":"[^"]*","sha256":"[^"]*","size_bytes":[0-9]+\}' "$manifest" 2>/dev/null || true)"
  if [ -z "$entries" ]; then
    err "Backup manifest ${manifest##*/} lists no archives; treating the backup as unverified."
    return 1
  fi
  while IFS= read -r e; do
    case "$(printf '%s' "$e" | sed -E 's/.*"filename":"([^"]*)".*/\1/')" in
      jarvis_*.enc) enc_on=1 ;;
    esac
  done <<< "$entries"
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    fn="$(printf '%s' "$e" | sed -E 's/.*"filename":"([^"]*)".*/\1/')"
    sha="$(printf '%s' "$e" | sed -E 's/.*"sha256":"([^"]*)".*/\1/')"
    size="$(printf '%s' "$e" | sed -E 's/.*"size_bytes":([0-9]+).*/\1/')"
    ok=1; reason="ok"
    if [ ! -s "${dir}/${fn}" ]; then
      ok=0; reason="missing or empty on disk"
    else
      actual_size="$(stat -c%s "${dir}/${fn}" 2>/dev/null || stat -f%z "${dir}/${fn}" 2>/dev/null || echo 0)"
      actual_sha="$(sha256sum "${dir}/${fn}" 2>/dev/null | cut -d' ' -f1)"
      [ "$actual_size" = "$size" ] || { ok=0; reason="size mismatch (${actual_size} vs ${size})"; }
      [ "$actual_sha" = "$sha" ]   || { ok=0; reason="checksum mismatch"; }
    fi
    case "$fn" in
      jarvis_*)
        jarvis_seen=1
        if [ "$ok" -eq 1 ]; then jarvis_ok=1; else err "Required jarvis DB archive ${fn}: ${reason}."; hard_fail=1; fi ;;
      secrets_*)
        secrets_seen=1
        if [ "$ok" -eq 1 ]; then secrets_ok=1
        elif [ "$enc_on" -eq 1 ]; then err "Required secrets archive ${fn}: ${reason} (an encrypted DB backup is useless without it)."; hard_fail=1
        else warn "Secrets archive ${fn}: ${reason}."; fi ;;
      qdrant_*)
        [ "$ok" -eq 1 ] || warn "Optional Qdrant archive ${fn}: ${reason} — the vector store will not be restorable from this backup (DB restore is unaffected)." ;;
      *)
        [ "$ok" -eq 1 ] || warn "Backup archive ${fn}: ${reason}." ;;
    esac
  done <<< "$entries"
  if [ "$jarvis_seen" -ne 1 ] || [ "$jarvis_ok" -ne 1 ]; then
    err "The primary jarvis database archive is absent or unverifiable; the backup cannot be trusted."
    hard_fail=1
  fi
  if [ "$enc_on" -eq 1 ] && { [ "$secrets_seen" -ne 1 ] || [ "$secrets_ok" -ne 1 ]; }; then
    err "Backups are encrypted but the secrets archive is absent or unverifiable."
    hard_fail=1
  fi
  [ "$hard_fail" -eq 0 ]
}

# _require_fresh_backup START_EPOCH — trigger an on-demand backup and poll (fail
# closed) until a manifest produced after START_EPOCH exists AND its archive set
# verifies. Sets VERIFIED_BACKUP_TS on success. Returns non-zero otherwise.
_require_fresh_backup() {
  local start_epoch="$1" trig_dir bk_dir manifest="" timeout interval waited=0
  trig_dir="$(_backup_trigger_dir)" || { err "Cannot locate the backup trigger directory."; return 1; }
  bk_dir="$(_backup_dir)"            || { err "Cannot locate the backup directory.";        return 1; }
  ( umask 022; : > "${trig_dir}/.backup_now" ) 2>/dev/null || true
  info "Requested an on-demand backup; waiting for a fresh, verified restore point..."
  timeout="${JARVIS_BACKUP_POLL_TIMEOUT:-300}"; interval="${JARVIS_BACKUP_POLL_INTERVAL:-3}"
  while :; do
    manifest="$(_newest_fresh_manifest "$bk_dir" "$start_epoch")"
    [ -n "$manifest" ] && break
    [ "$waited" -ge "$timeout" ] && break
    sleep "$interval"; waited=$((waited + interval))
  done
  [ -n "$manifest" ] || { err "No backup manifest appeared within ${timeout}s."; return 1; }
  _verify_backup_archive_set "$bk_dir" "$manifest" || return 1
  VERIFIED_BACKUP_TS="$(basename "$manifest" | sed -E 's/^manifest_(.*)\.json$/\1/')"
  return 0
}

# -----------------------------------------------------------------------------
# Stage-first pull + the fast-forward advance.
# -----------------------------------------------------------------------------
# _stage_target_cohort TARGET_REF TARGET_VERSION — pull the complete registry-
# backed target cohort BEFORE the branch advances, driven by the target ref's
# versions.env pins and the v-less target version. A failed pull aborts with the
# checkout untouched.
_stage_target_cohort() {
  local target_ref="$1" target_version="$2" tmp_versions rc=0
  local -a services=("${PUBLISHED_SERVICES_BASE[@]}") profile_args=()
  if grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' .env 2>/dev/null; then
    services+=("$PUBLISHED_SERVICE_TELEGRAM"); profile_args+=(--profile telegram)
  fi
  tmp_versions="$(mktemp)"
  git show "${target_ref}:versions.env" > "$tmp_versions" 2>/dev/null || true
  info "Staging images for ${target_ref} before advancing..."
  (
    set -a
    # shellcheck disable=SC1090  # the target's pinned third-party image set
    [ -s "$tmp_versions" ] && . "$tmp_versions"
    # shellcheck disable=SC2034  # exported under `set -a` for docker compose to read
    JARVIS_VERSION="$target_version"
    set +a
    docker compose ${profile_args[@]+"${profile_args[@]}"} pull "${services[@]}"
  ) || rc=$?
  rm -f "$tmp_versions"
  [ "$rc" -eq 0 ] || die "Staging images for ${target_ref} failed; nothing was changed." \
    "Check network access to ghcr.io, then re-run: jarvis-research update"
}

# -----------------------------------------------------------------------------
# Update — the transactional entry point.
# -----------------------------------------------------------------------------
cmd_update() {
  local to_ref="" resume_ref="" p_phase p_target
  while [ $# -gt 0 ]; do
    case "$1" in
      --to)       to_ref="${2:-}"; shift 2 ;;
      --to=*)     to_ref="${1#--to=}"; shift ;;
      --resume)   resume_ref="${2:-}"; shift 2 ;;
      --resume=*) resume_ref="${1#--resume=}"; shift ;;
      --yes|-y)   shift ;;   # accepted for symmetry; the flow is non-interactive
      *)          usage_error "update: unknown option '$1'" ;;
    esac
  done

  # Explicit resume (the phase-8 re-exec): run post-merge steps only. A resume ref
  # that disagrees with the pending transaction's recorded target is refused — a
  # mistyped tag would otherwise re-pin .env to the wrong version.
  if [ -n "$resume_ref" ]; then
    if [ -f "$PENDING_FILE_PATH" ]; then
      p_target="$(_txn_field target)"
      if [ -n "$p_target" ] && [ "$p_target" != "$resume_ref" ]; then
        die "--resume ${resume_ref} does not match the pending update target (${p_target})." \
            "Re-run: jarvis-research update --resume ${p_target}   (or remove ${PENDING_FILE_PATH} to abandon it)"
      fi
    fi
    _resume_transaction "$resume_ref"
    return
  fi

  # A pending transaction from a prior interrupted run resumes deterministically
  # at its recorded phase instead of reporting "up to date".
  if [ -f "$PENDING_FILE_PATH" ]; then
    p_phase="$(_txn_field phase)"; p_target="$(_txn_field target)"
    case "$p_phase" in
      committed|"") : ;;
      staging)      info "A previous update stopped before advancing the branch; restarting it." ;;
      *)            info "Resuming an interrupted update at phase '${p_phase}'."
                    _resume_transaction "$p_target"; return ;;
    esac
  fi

  UPDATE_START_EPOCH="$(date +%s)"
  MIGRATIONS_RAN=0

  _require_managed_install                                      # (1)
  _require_docker_daemon                                        # (2)
  _require_clean_main_checkout

  info "Fetching tags from origin..."                          # (3)
  git fetch --tags origin >/dev/null 2>&1 \
    || env_die "Could not fetch from origin." "Check network access, then re-run: jarvis-research update"

  local target_ref target_version
  if [ -n "$to_ref" ]; then
    target_ref="$to_ref"
  else
    target_ref="$(latest_stable_tag origin)"
    [ -n "$target_ref" ] || die "No stable release tag found on origin." "Run: jarvis-research doctor"
  fi
  target_version="${target_ref#v}"

  if ! git merge-base --is-ancestor HEAD "$target_ref" 2>/dev/null; then
    die "Your checkout has diverged from ${target_ref}; a fast-forward update is not possible." \
        "Reconcile by hand (git pull --ff-only) or reinstall; then run: jarvis-research doctor"
  fi
  # Release tags are annotated, so the tag name resolves to the tag object, not
  # the commit it points at; peel it or this never matches.
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse "${target_ref}^{commit}")" ]; then
    ok "Already up to date (${target_ref})."
    return 0
  fi

  local active_profiles; active_profiles="$(_active_profiles)"
  info "Verifying that every published image for ${target_ref} exists..."   # (4)
  # shellcheck disable=SC2086  # active_profiles is an intentional word list
  if ! verify_release_manifests "$target_ref" $active_profiles >/dev/null 2>&1; then
    die "Some images for ${target_ref} are not published yet — a visible tag is not release-readiness." \
        "Wait for the release to finish publishing, then re-run: jarvis-research update"
  fi

  local backup_id=""                                            # (5)
  local migrations; migrations="$(_new_migrations "$target_ref")"
  if [ -n "$migrations" ]; then
    if _migrations_need_backup "$target_ref" "$migrations"; then
      warn "${target_ref} includes a data-changing migration; a fresh verified backup is required."
      if _require_fresh_backup "$UPDATE_START_EPOCH"; then
        backup_id="${VERIFIED_BACKUP_TS:-}"
        MIGRATIONS_RAN=1
        ok "Verified backup ${backup_id} present; continuing."
      else
        die "No fresh, verified backup exists — refusing to apply a data-changing migration." \
            "Take a backup (WebUI Backup panel or the backup sidecar), then re-run: jarvis-research update"
      fi
    else
      info "New additive migration(s) will apply on restart:"
      printf '%s\n' "$migrations" | sed 's/^/    /'
      info "Open the WebUI Backup panel first if you want a restore point."
    fi
  fi

  _txn_write staging "$target_ref" "$target_version" "$backup_id"           # (6)
  _stage_target_cohort "$target_ref" "$target_version"                      # (7)

  info "Advancing the checkout to ${target_ref} (fast-forward only)..."     # (8)
  if ! git merge --ff-only "$target_ref"; then
    err "Fast-forward to ${target_ref} failed; the checkout was not advanced."
    printf '        %s%s%s\n' "$C_YELLOW" "Reconcile by hand, then run: jarvis-research doctor" "$C_RESET" >&2
    exit 1
  fi
  _txn_update_phase merged
  local -a resume_cmd=(bash "${REPO}/scripts/jarvis-research.sh" --repo "$REPO" update --resume "$target_ref" --yes)
  exec "${resume_cmd[@]}"
}

# _resume_transaction TARGET_REF — the post-merge half (phases 9-12). Never
# fetches, guards-mutates, merges, or re-execs.
_resume_transaction() {
  local target_ref="$1" target_version="${1#v}"
  MIGRATIONS_RAN="${MIGRATIONS_RAN:-0}"
  if [ -f "$PENDING_FILE_PATH" ] && [ -n "$(_txn_field backup_id)" ]; then
    MIGRATIONS_RAN=1
  fi

  install_cli_shim "$REPO" >/dev/null 2>&1 || true             # (9)
  upsert_env_var JARVIS_VERSION "$target_version" \
    || die "Could not pin JARVIS_VERSION in .env." "Run: jarvis-research doctor"
  _txn_update_phase pull

  info "Applying ${target_ref} — pulling images and recreating services..."  # (10)
  if ! _run_update_sh; then
    _txn_update_phase health
    _failure_epilogue "$target_ref"
    exit 1
  fi

  _txn_update_phase committed                                  # (11)
  rm -f "$PENDING_FILE_PATH"
  ok "Update to ${target_ref} complete and health-verified."
  _success_epilogue "$target_ref"                              # (12)
}

# _run_update_sh — hand off to update.sh (warm pulls become no-ops) and let it
# own the recreate + health wait; its exit code is the health verdict.
_run_update_sh() {
  ( cd "$REPO" && bash "${REPO}/update.sh" --yes )
}

# _rollback_pin_lines FROM_VERSION — the image-pin rollback commands.
_rollback_pin_lines() {
  local fv="$1" svcs; svcs="$(printf '%s ' "${PUBLISHED_SERVICES_BASE[@]}")"
  printf '    JARVIS_VERSION=%s docker compose pull %s\n' "$fv" "$svcs"
  printf '    JARVIS_VERSION=%s docker compose up -d --no-build %s\n' "$fv" "$svcs"
}
# _schema_not_safe_notice — the honest warning that image rollback is not enough
# once a migration has run, plus the restore-from-backup pointer.
_schema_not_safe_notice() {
  printf '\n%sImage rollback alone is NOT schema-safe:%s a database migration already ran, so the new\n' "$C_YELLOW" "$C_RESET"
  printf '  schema stays in place. To return to the pre-update state, restore the backup taken before this\n'
  printf '  update (WebUI Backup panel -> Restore, or scripts/restore.sh) — it rolls the database back\n'
  printf '  together with the images.\n'
}

_failure_epilogue() {
  local target_ref="$1" fv; fv="$(_txn_field from_version)"; [ -n "$fv" ] || fv="<previous-version>"
  printf '\n'
  err "Update to ${target_ref} did not finish; the transaction remains pending."
  printf '%sRoll the application images back to the previous version:%s\n' "$C_BOLD" "$C_RESET"
  _rollback_pin_lines "$fv"
  [ "${MIGRATIONS_RAN:-0}" -eq 1 ] && _schema_not_safe_notice
  printf '\nDiagnose: jarvis-research doctor\n'
  printf 'Resume once fixed: jarvis-research update --resume %s\n' "$target_ref"
}

_success_epilogue() {
  local target_ref="$1" fv; fv="$(sed -n 's/^JARVIS_VERSION=//p' .env 2>/dev/null | head -1)"
  printf '\n'
  cmd_doctor || true
  printf '\n%sIf you need to roll back to the previous release:%s\n' "$C_BOLD" "$C_RESET"
  _rollback_pin_lines "<previous-version>"
  [ "${MIGRATIONS_RAN:-0}" -eq 1 ] && _schema_not_safe_notice
  printf '\nNow running %s (JARVIS_VERSION=%s).\n' "$target_ref" "${fv:-unknown}"
}

# -----------------------------------------------------------------------------
# Day-to-day container control (start/repair are always --no-build).
# -----------------------------------------------------------------------------
cmd_status()  { _require_docker_daemon; docker compose ps "$@"; }
cmd_start()   { _require_docker_daemon; info "Starting services (no build)..."; docker compose up -d --no-build; }
cmd_stop()    { _require_docker_daemon; info "Stopping services..."; docker compose stop; }
cmd_restart() { _require_docker_daemon; info "Restarting services..."; docker compose restart; }
cmd_logs()    { _require_docker_daemon; docker compose logs "$@"; }

# repair — bounded, never destructive: recreate stopped containers (no build/pull),
# restart any unhealthy mandatory service, wait, then summarise via doctor.
cmd_repair() {
  _require_docker_daemon
  info "Repairing: recreating stopped containers (no build, no pull)..."
  docker compose up -d --no-build || warn "Some services did not come up cleanly."
  local svc cid h
  for svc in $MANDATORY_HEALTH_BASE; do
    cid="$(docker compose ps -q "$svc" 2>/dev/null | head -1 || true)"
    [ -n "$cid" ] || continue
    h="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    if [ "$h" = "unhealthy" ]; then
      info "Restarting unhealthy service: ${svc}"
      docker restart "$cid" >/dev/null 2>&1 || true
    fi
  done
  info "Repair finished; running a doctor summary."
  cmd_doctor || true
}

# -----------------------------------------------------------------------------
# doctor — read-only health, disk, registration, update-availability, and
# host-preflight probes (each a WARN line, never a hard fail). Exit 0/1.
# -----------------------------------------------------------------------------
cmd_doctor() {
  local rc=0
  printf '%s=== jarvis-research doctor ===%s\n' "$C_BOLD" "$C_RESET"
  if [ -x "${REPO}/setup.sh" ]; then
    ( cd "$REPO" && ./setup.sh --check ) || rc=1
  fi
  printf '\n%s-- containers --%s\n' "$C_BOLD" "$C_RESET"
  docker compose ps 2>/dev/null || warn "Could not query container status."
  local disk; disk="$(preflight_disk_lib 1 2>/dev/null || true)"
  printf '\n%s-- disk --%s  free ~%s GB on the Docker data root\n' "$C_BOLD" "$C_RESET" "${disk%% *}"
  if [ -f "$INSTALLS_FILE" ] && grep -qxF "$REPO" "$INSTALLS_FILE"; then
    ok "This install is registered with jarvis-research."
  else
    warn "This install is not registered (run: jarvis-research register)."
  fi
  local latest cur; latest="$(latest_stable_tag origin 2>/dev/null || true)"
  cur="$(sed -n 's/^JARVIS_VERSION=//p' .env 2>/dev/null | head -1)"
  [ -n "$latest" ] && info "Latest published release: ${latest} (installed: ${cur:-unknown})."
  _doctor_host_probes
  return "$rc"
}

_doctor_host_probes() {
  local compose_file torch cv dri
  compose_file="$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | head -1)"
  if printf '%s' "$compose_file" | grep -qE 'docker-compose\.(gpu|rocm|vulkan)\.yml'; then
    dri="${JARVIS_DRI_DIR:-/dev/dri}"
    local -a nodes=("$dri"/renderD*)
    if [ ! -e "${nodes[0]}" ]; then
      warn "A GPU overlay is configured but no ${dri}/renderD* render node is present; the accelerator will be unavailable."
    else
      grep -qE '^JARVIS_RENDER_GID=[0-9]+' .env 2>/dev/null || warn "GPU overlay configured but JARVIS_RENDER_GID is not numeric in .env."
      grep -qE '^JARVIS_VIDEO_GID=[0-9]+'  .env 2>/dev/null || warn "GPU overlay configured but JARVIS_VIDEO_GID is not numeric in .env."
    fi
  fi
  torch="$(sed -n 's/^TORCH_VARIANT=//p' .env 2>/dev/null | head -1)"
  if [ "$torch" = "cuda" ]; then
    docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"' \
      || warn "TORCH_VARIANT=cuda but Docker does not report the nvidia runtime; inference may fall back to CPU."
  fi
  cv="$(docker compose version --short 2>/dev/null || true)"
  if [ -n "$cv" ] && ! compose_meets_floor "$cv" 2.24.4; then
    warn "Docker Compose ${cv} is older than the tested floor 2.24.4; overlay merges may misbehave."
  fi
}

# -----------------------------------------------------------------------------
# register / uninstall / version / help.
# -----------------------------------------------------------------------------
cmd_register() {
  install_cli_shim "$REPO"
  ok "Registered ${REPO} as the default jarvis-research install."
}

cmd_uninstall() {
  [ -x "${REPO}/scripts/uninstall.sh" ] \
    || die "The uninstall helper is missing from this checkout." "Restore it with: git checkout scripts/uninstall.sh"
  exec bash "${REPO}/scripts/uninstall.sh" --repo "$REPO" "$@"
}

cmd_version() {
  local v; v="$(sed -n 's/^JARVIS_VERSION=//p' "${REPO}/.env" 2>/dev/null | head -1)"
  printf 'jarvis-research — JARVIS lifecycle CLI\n'
  printf 'Installed JARVIS_VERSION=%s\n' "${v:-unknown}"
  printf 'Repository: %s\n' "$REPO"
}

cmd_help() {
  cat <<'HELP'
jarvis-research — lifecycle CLI for a managed JARVIS install

Usage: jarvis-research [--repo <dir>] <command> [options]

Commands:
  update [--to <tag>] [--resume <tag>] [--yes]
                     Transactional, DB-safe upgrade to the latest stable release
                     (or --to a specific tag). Refuses on a diverged/dirty/non-main
                     checkout, when target images are not yet published, and applies
                     a data-changing migration only after a fresh verified backup.
  status             Show container status.
  start | stop | restart
                     Start (no build), stop, or restart the stack.
  logs [args]        Tail service logs (passes args through to docker compose logs).
  doctor             Read-only health, disk, registration, and update check.
  repair             Bounded, non-destructive recovery (recreate + restart unhealthy).
  register           Record this checkout as a managed install.
  uninstall [--dry-run] [--tier N] [--keep-data] [--all] [--yes]
                     Tiered, contained teardown: stop (1), remove app images (2),
                     delete data volumes (3, typed confirmation), or full purge (4,
                     third-party images + files + the clone). Lead with --dry-run.
  version | help     Print version / this help.

Exit codes: 0 ok · 1 refused/failed · 2 usage · 3 environment (Docker missing).
HELP
}

# -----------------------------------------------------------------------------
# Argument parsing, resolution, and dispatch.
# -----------------------------------------------------------------------------
REPO_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   REPO_OVERRIDE="${2:-}"; shift 2 ;;
    --repo=*) REPO_OVERRIDE="${1#--repo=}"; shift ;;
    *)        break ;;
  esac
done

SUBCMD="${1:-help}"
[ $# -gt 0 ] && shift || true

REPO="$(resolve_repo)"
cd "$REPO"
# shellcheck source=scripts/setup_lib.sh
# shellcheck disable=SC1091  # resolved at runtime relative to the repo root
. scripts/setup_lib.sh

case "$SUBCMD" in
  update)                cmd_update "$@" ;;
  status)                cmd_status "$@" ;;
  start)                 cmd_start ;;
  stop)                  cmd_stop ;;
  restart)               cmd_restart ;;
  logs)                  cmd_logs "$@" ;;
  doctor)                cmd_doctor ;;
  repair)                cmd_repair ;;
  register)              cmd_register ;;
  uninstall)             cmd_uninstall "$@" ;;
  version|--version|-v)  cmd_version ;;
  help|-h|--help)        cmd_help ;;
  *)                     err "Unknown command: ${SUBCMD}"; cmd_help >&2; exit 2 ;;
esac
