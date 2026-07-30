#!/usr/bin/env bash
# Load a release's lifecycle implementation before updating an older checkout.
#
# This script contains no update policy. It verifies the selected Git object,
# extracts the release-owned lifecycle files, and hands control to that
# release's jarvis-research command.
set -euo pipefail

RUNTIME_DIR=""

err() { printf '[ERROR] %s\n' "$*" >&2; }

usage() {
  cat >&2 <<'USAGE'
Usage: update-bootstrap.sh --repo <install-dir> --to <vX.Y.Z|40-hex-commit> [--yes]
USAGE
}

die() {
  err "$1"
  [ -z "${2:-}" ] || printf '        %s\n' "$2" >&2
  exit 1
}

usage_error() {
  err "$1"
  usage
  exit 2
}

cleanup_runtime() {
  [ -z "$RUNTIME_DIR" ] || rm -rf -- "$RUNTIME_DIR"
}

normalize_repository_identity() {
  local value="${1%/}"
  value="${value%.git}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    https://github.com/*) value="${value#https://github.com/}" ;;
    ssh://git@github.com/*) value="${value#ssh://git@github.com/}" ;;
    git@github.com:*) value="${value#git@github.com:}" ;;
  esac
  printf '%s' "$value"
}

resolve_repository() {
  local candidate="$1" resolved
  resolved="$(cd -- "$candidate" 2>/dev/null && pwd -P)" \
    || die "The selected installation directory is not accessible." \
      "Pass --repo with the JARVIS checkout directory."
  [ -f "$resolved/docker-compose.yml" ] && [ -f "$resolved/versions.env" ] \
    || die "${resolved} is not a JARVIS installation." \
      "Pass --repo with the directory containing docker-compose.yml and versions.env."
  git -C "$resolved" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "${resolved} is not a Git checkout." \
      "Restore the managed checkout before updating."
  printf '%s' "$resolved"
}

require_managed_checkout() {
  local repo="$1" branch origin actual expected git_dir op hidden marker_rel marker tracked_marker dirt
  branch="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null || true)"
  [ "$branch" = main ] \
    || die "Updates require a normal main checkout." \
      "Switch this installation to main, then retry."

  # A clean tree is not the same as an updatable tree. An interrupted rebase,
  # merge, cherry-pick or revert leaves the porcelain status empty but makes the
  # release fast-forward fail once the update transaction is already open.
  # Refuse here, while nothing is at stake.
  git_dir="$(git -C "$repo" rev-parse --absolute-git-dir 2>/dev/null)" \
    || die "Could not locate this installation's Git directory." "Check the checkout, then retry."
  for op in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do
    if [ -e "${git_dir}/${op}" ]; then
      die "A Git operation is already in progress in this checkout." \
        "Finish or abort it (for example: git rebase --abort), then retry."
    fi
  done

  # Tracked files flagged skip-worktree (tag 'S') or assume-unchanged (lowercase
  # tag) hide real modifications from every status query, including the one below.
  hidden="$(git -C "$repo" ls-files -v 2>/dev/null | sed -n 's/^[a-zS] //p')" \
    || die "Could not inspect this installation's index flags." "Check the Git installation, then retry."
  if [ -n "$hidden" ]; then
    printf '%s\n' "$hidden" | head -20 >&2
    die "Some tracked files are flagged to hide local changes." \
      "Clear them with: git update-index --no-skip-worktree --no-assume-unchanged <path>"
  fi

  # The exemption is for one product-managed regular file. A directory or symlink
  # at that path is not it: the pathspec below excludes a prefix, so without this
  # fence any content beneath it would be laundered.
  marker_rel="secrets/manifest-hmac-required"
  marker="${repo}/${marker_rel}"
  if { [ -e "$marker" ] || [ -L "$marker" ]; } && { [ ! -f "$marker" ] || [ -L "$marker" ]; }; then
    die "The signed-manifest marker is not a regular file." "Inspect ${marker_rel}, then retry."
  fi
  # It is machine-local and must never be tracked; a tracked copy would let the
  # exclusion hide a real modification to a committed file.
  tracked_marker="$(git -C "$repo" ls-files -- "$marker_rel" 2>/dev/null)" \
    || die "Could not inspect this installation's index." "Check the Git installation, then retry."
  if [ -n "$tracked_marker" ]; then
    die "The signed-manifest marker is tracked in this checkout." \
      "Remove it from version control, then retry."
  fi
  # One repo-wide status. Declared then assigned: `local x="$(...)"` returns
  # local's status and would silently defeat the fail-closed branch.
  dirt="$(git -C "$repo" status --porcelain -- ':(top)' ":(top,exclude)${marker_rel}" 2>/dev/null)" \
    || die "Could not inspect this installation's working tree." \
      "Check the Git installation, then retry."
  if [ -n "$dirt" ]; then
    printf '%s\n' "$dirt" | head -20 >&2
    [ "$(printf '%s\n' "$dirt" | wc -l)" -le 20 ] || printf '        ... and more\n' >&2
    die "The working tree has uncommitted changes." \
      "Commit or stash them before updating. Leave ${marker_rel} in place; it is managed by the backup service."
  fi

  origin="$(git -C "$repo" remote get-url origin 2>/dev/null || true)"
  expected="${JARVIS_RESEARCH_REMOTE:-limitcycle-oss/jarvis-rd-assistant}"
  actual="$(normalize_repository_identity "$origin")"
  expected="$(normalize_repository_identity "$expected")"
  [ -n "$actual" ] && [ "$actual" = "$expected" ] \
    || die "Origin is not the configured JARVIS repository." \
      "Use the managed checkout, or set JARVIS_RESEARCH_REMOTE for an intentional fork."
}

validate_target() {
  local repo="$1" target="$2" resolved remote_commit=""
  if ! [[ "$target" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
     && ! [[ "$target" =~ ^[0-9a-f]{40}$ ]]; then
    usage_error "--to must be a stable vX.Y.Z tag or a lowercase 40-hex commit."
  fi

  git -C "$repo" fetch --tags origin >/dev/null 2>&1 \
    || die "Could not fetch the selected release from origin." \
      "Check network access and the origin remote, then retry."
  resolved="$(git -C "$repo" rev-parse "${target}^{commit}" 2>/dev/null || true)"
  [[ "$resolved" =~ ^[0-9a-f]{40}$ ]] \
    || die "The selected release does not resolve to a commit." \
      "Check the release name and retry."

  if [[ "$target" =~ ^v ]]; then
    remote_commit="$(
      git -C "$repo" ls-remote --tags origin "refs/tags/${target}^{}" 2>/dev/null \
        | awk 'NR == 1 { print $1 }'
    )"
    [ "$remote_commit" = "$resolved" ] \
      || die "The selected release tag does not match origin." \
        "Remove the conflicting local tag and fetch it again."
  else
    [ "$resolved" = "$target" ] \
      || die "The selected commit identity is inconsistent." \
        "Fetch origin and retry with the exact lowercase commit."
  fi

  git -C "$repo" merge-base --is-ancestor "$resolved" origin/main 2>/dev/null \
    || die "The selected release is not on origin/main." \
      "Choose a published main-line release."
  git -C "$repo" merge-base --is-ancestor HEAD "$resolved" 2>/dev/null \
    || die "The installation cannot fast-forward to the selected release." \
      "Reconcile the checkout or reinstall before updating."
  printf '%s' "$resolved"
}

extract_runtime() {
  local repo="$1" commit="$2" destination="$3" path entry mode type object tree_path
  local -a paths=(
    scripts/jarvis-research.sh
    scripts/setup_lib.sh
    scripts/backup-lifecycle.sh
    scripts/backup.sh
  )
  mkdir -p "$destination/scripts"
  for path in "${paths[@]}"; do
    entry="$(git -C "$repo" ls-tree "$commit" -- "$path" 2>/dev/null || true)"
    read -r mode type object tree_path <<< "${entry//$'\t'/ }"
    case "$mode" in 100644|100755) ;; *)
      die "The selected release has an unsafe lifecycle file: ${path}." \
        "Choose an intact published release."
      ;;
    esac
    [ "$type" = blob ] && [[ "$object" =~ ^[0-9a-f]{40,64}$ ]] \
      && [ "$tree_path" = "$path" ] \
      || die "The selected release has an invalid lifecycle file: ${path}." \
        "Choose an intact published release."
    git -C "$repo" cat-file blob "$object" > "$destination/$path" \
      || die "Could not extract ${path} from the selected release." \
        "Check the local Git object database and retry."
    chmod 500 "$destination/$path"
  done
}

main() {
  local repo_arg="" target="" repo commit rc=0 assume_yes=0
  local -a update_args=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --repo)
        [ "$#" -ge 2 ] || usage_error "--repo requires a value."
        repo_arg="$2"; shift 2 ;;
      --repo=*) repo_arg="${1#*=}"; shift ;;
      --to)
        [ "$#" -ge 2 ] || usage_error "--to requires a value."
        target="$2"; shift 2 ;;
      --to=*) target="${1#*=}"; shift ;;
      --yes|-y) assume_yes=1; shift ;;
      -h|--help) usage; return 0 ;;
      *) usage_error "Unknown option: $1" ;;
    esac
  done
  [ -n "$repo_arg" ] || usage_error "--repo is required."
  [ -n "$target" ] || usage_error "--to is required."

  repo="$(resolve_repository "$repo_arg")"
  require_managed_checkout "$repo"
  commit="$(validate_target "$repo" "$target")"
  RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jarvis-update-runtime.XXXXXX")" \
    || die "Could not create a private temporary directory." \
      "Check temporary-directory permissions and retry."
  chmod 700 "$RUNTIME_DIR"
  trap cleanup_runtime EXIT
  extract_runtime "$repo" "$commit" "$RUNTIME_DIR"

  update_args=(--repo "$repo" update --to "$target")
  [ "$assume_yes" -eq 0 ] || update_args+=(--yes)
  bash "$RUNTIME_DIR/scripts/jarvis-research.sh" "${update_args[@]}" || rc=$?
  return "$rc"
}

main "$@"
