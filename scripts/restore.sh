#!/usr/bin/env bash
# Restore one selected backup set from the postgres-backup sidecar. The
# application receives no database tools or container-control privileges.
#
# The admin API writes /backup-trigger/.restore_request.json. The sidecar takes
# a safety backup, verifies and swaps both databases, restores Qdrant and data
# keys, then replaces the PDF set while maintenance remains active.
#
# Safety properties:
#   1. The request is consumed before destructive work, preventing a failed
#      restore from replaying after a sidecar restart. Recorded terminal failures
#      exit successfully so the container does not restart into the same request.
#   2. Database connections are revoked and existing backends terminated before
#      every drop, preventing application pools from reconnecting mid-swap.
#   3. A failure after the first drop keeps maintenance active until recovery
#      completes. Clean restores and pre-destructive failures clear maintenance.

set -euo pipefail

# --- Configuration -----------------------------------------------------------
TRIGGER_DIR="${BACKUP_TRIGGER_DIR:-/backup-trigger}"
REQUEST_FILE="${TRIGGER_DIR}/.restore_request.json"
STATUS_FILE="${TRIGGER_DIR}/.restore_status.json"
MAINTENANCE_SENTINEL="${MAINTENANCE_SENTINEL:-${TRIGGER_DIR}/.maintenance}"
MAINTENANCE_DESTRUCTIVE="${MAINTENANCE_DESTRUCTIVE_SENTINEL:-${TRIGGER_DIR}/.destructive}"
OUTBOUND_QUARANTINE_SENTINEL="${OUTBOUND_QUARANTINE_SENTINEL:-${TRIGGER_DIR}/.outbound-quarantine.json}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
LOCK_DIR="${BACKUP_DIR}/.lifecycle"
# Durable protocol state belongs in the private backup volume, not the
# app-writable request/status volume.
SWAP_STATE_FILE="${LOCK_DIR}/restore-swap-state.json"
RESTORE_TIMEOUT_FILE="${LOCK_DIR}/restore-timeout"
# Inbox restores read the archive set and one-time operator key from this
# writable volume. It is separate from the read-only service-secret mount and
# is unused by same-host guided restores.
INBOX_DIR="${RESTORE_INBOX_DIR:-/restore-inbox}"
OPERATOR_KEYFILE="${INBOX_DIR}/operator_key"
SECRETS_STAGING="${INBOX_DIR}/.secrets-staging"
# The sidecar installs only the three archive data keys in this destination-host
# directory; its other files remain untouched. Database-backed settings restore
# separately and stay quarantined after off-host recovery. A local restore also
# reads the signing marker below from this mount.
HOST_SECRETS_DIR="${HOST_SECRETS_DIR:-/host-secrets}"
LIFECYCLE_OPERATION_LOCK="${LOCK_DIR}/operation.lock"
LIFECYCLE_OPERATION_STATE="${LOCK_DIR}/operation.state"
LIFECYCLE_ADMISSION_LOCK="${LOCK_DIR}/operation-admission.lock"
HOST_RESERVATION="${LOCK_DIR}/host.reservation"
UPDATE_GUARD="${LOCK_DIR}/update.guard"
UPDATE_CONTROL="${LOCK_DIR}/update.control"
UPDATE_RESERVATION="${LOCK_DIR}/update.reservation"
ROTATION_SENTINEL="${LOCK_DIR}/rotation.guard"
ROTATION_RESERVATION="${LOCK_DIR}/rotation.reservation"
# Out-of-band ratchet: backup.sh drops this marker the first time it signs a manifest.
# The decision to REQUIRE a signature must never come from a field inside the
# (unauthenticated) manifest — that would be a strip-the-field downgrade — so it is read
# from the host secrets dir instead, which an attacker who only controls BACKUP_DIR
# cannot touch. Mirrors backup.sh's marker path.
MANIFEST_HMAC_MARKER="${HOST_SECRETS_DIR}/manifest-hmac-required"
# The same ratchet, second copy: a durable host state directory outside the checkout,
# bind-mounted into this sidecar only. Either copy arms the requirement, so a checkout
# that is replaced or re-created between backup and restore cannot disarm it.
BACKUP_STATE_DIR="${BACKUP_STATE_DIR:-/backup-state}"
MANIFEST_HMAC_MARKER_DURABLE="${BACKUP_STATE_DIR}/manifest-hmac-required"
# Mirror of backup.sh's domain label; both sides must agree byte-for-byte.
MANIFEST_HMAC_LABEL="jarvis-manifest-v1"
# The phrase the operator must type to restore a backup set that has no authenticated
# manifest. Deliberately unguessable-by-accident and never satisfiable by a flag.
BREAK_GLASS_PHRASE="I-ACCEPT-UNVERIFIED-BACKUP"

PGHOST="${PGHOST:-postgres}"
PGUSER="${PGUSER:-jarvis}"
JARVIS_DB="${PGDATABASE:-jarvis}"
LITELLM_DB="${LITELLM_DATABASE:-litellm}"
# The postgres data volume is mounted here read-only (compose: postgres_data:ro) so
# the disk preflight can size the free space the reload will consume. Overridable
# only to keep the path in one place; users never set it.
POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-/postgres-data}"
# Keep the destination host's backup key separate from the key that decrypts the
# selected restore point. Inbox restores replace ENC_KEYFILE with operator_key,
# while their just-created safety backup is still signed by this target-host key.
TARGET_BACKUP_KEYFILE="${BACKUP_ENCRYPT_KEYFILE:-}"
ENC_KEYFILE="$TARGET_BACKUP_KEYFILE"
QDRANT_URL="${QDRANT_URL:-http://qdrant:6333}"
QDRANT_API_KEYFILE="${QDRANT_API_KEYFILE:-/run/secrets/qdrant_api_key}"
PDF_STORAGE_DIR="${PDF_STORAGE_DIR:-/pdf-storage}"
# Shared staging dir: the sidecar writes the decrypted snapshot here and Qdrant
# reads it via file:// — both containers mount the restore_staging volume at this
# same path (under Qdrant's default /qdrant/snapshots dir).
QDRANT_STAGING_DIR="/qdrant/snapshots/restore"

# --- Restore state (drives the .restore_status.json the FE polls) ------------
STATE="running"
CURRENT_STEP=""
ERROR=""
SAFETY_BACKUP_TS=""
STARTED_AT="$(date -Iseconds)"
FINISHED_AT=""
STEP_SAFETY="pending"
STEP_DB="pending"
STEP_LITELLM="pending"
STEP_QDRANT="pending"
STEP_FINISH="pending"
DROP_STARTED=0
RESTORE_CLEAN=0
HEARTBEAT_PID=""
MANUAL_STEPS_REQUIRED=0
# Set only by the --inbox-manifest entrypoint branch: a read-only inventory pass that
# short-circuits the EXIT trap entirely (no request consume, no status write, no key
# shred, no maintenance change) — it runs every sidecar loop and must touch nothing.
MANIFEST_MODE=0
# An admission refusal must leave the request, status, keys, and maintenance
# untouched so the single-threaded sidecar can retry it on the next loop.
ADMISSION_REFUSED=0
PHASE=""
# Restore source: "local" (same-host WebUI restore, the default) reads the
# archive set and host key; "inbox" (off-host DR) reads the supplied set and
# one-time key from restore_inbox. Both preserve target-local credentials; both
# install restored-data keys when their archive contains them.
SOURCE="local"
ARCHIVE_DIR="$BACKUP_DIR"
MANIFEST_AUTHENTICATED=0
MANIFEST_LEGACY=0
PRIVATE_INPUT_DIR=""
SAFETY_RUN_ID=""
SAFETY_STAGING_DIR=""
SECRETS_ARCHIVE=""
DATA_KEYS_STAGED=0
PDFS_STAGED=0
PDF_RESTORE_RUN_ID=""
ALLOW_MISSING_PDFS=0
ALLOW_UNKNOWN_SCHEMA=0
RESTORE_ID=""
REQUESTED_AT=""
VECTOR_VISIBILITY_GENERATION=""

# --- JSON status writer (atomic .tmp -> mv; matches the RestoreStatus API
#     shape: state/current_step/steps[].{name,status}/safety_backup_ts/
#     started_at/finished_at/error). Never aborts the script (|| return 0). -----
_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

_json_or_null() {
  if [ -z "$1" ]; then printf 'null'; else printf '"%s"' "$(_json_escape "$1")"; fi
}

write_status() {
  local tmp="${STATUS_FILE}.tmp" drop_json manual_json
  if [ "$DROP_STARTED" = "1" ]; then drop_json="true"; else drop_json="false"; fi
  if [ "$MANUAL_STEPS_REQUIRED" = "1" ]; then manual_json="true"; else manual_json="false"; fi
  {
    printf '{"state":"%s","current_step":%s,"steps":[' "$STATE" "$(_json_or_null "$CURRENT_STEP")"
    printf '{"name":"Safety backup","status":"%s"},' "$STEP_SAFETY"
    printf '{"name":"Restoring database","status":"%s"},' "$STEP_DB"
    printf '{"name":"Restoring API-key store","status":"%s"},' "$STEP_LITELLM"
    printf '{"name":"Restoring search index","status":"%s"},' "$STEP_QDRANT"
    printf '{"name":"Finishing up","status":"%s"}],' "$STEP_FINISH"
    printf '"safety_backup_ts":%s,"started_at":%s,"finished_at":%s,"error":%s,"drop_started":%s,"manual_steps_required":%s,"phase":%s}' \
      "$(_json_or_null "$SAFETY_BACKUP_TS")" "$(_json_or_null "$STARTED_AT")" \
      "$(_json_or_null "$FINISHED_AT")" "$(_json_or_null "$ERROR")" "$drop_json" \
      "$manual_json" "$(_json_or_null "$PHASE")"
  } > "$tmp" 2>/dev/null || return 0
  mv -f "$tmp" "$STATUS_FILE" 2>/dev/null || return 0
}

outbound_quarantine_exists() {
  [ -e "$OUTBOUND_QUARANTINE_SENTINEL" ] || [ -L "$OUTBOUND_QUARANTINE_SENTINEL" ]
}

write_outbound_quarantine() {
  local restore_id="$1" source="$2" requested_at="$3" completed_at="$4"
  local tmp
  printf '%s' "$restore_id" | grep -Eq '^[0-9a-f]{32}$' || return 1
  [ "$source" = "inbox" ] || return 1
  [ -n "$requested_at" ] && [ -n "$completed_at" ] || return 1
  outbound_quarantine_exists && return 1
  tmp="$(mktemp "${OUTBOUND_QUARANTINE_SENTINEL}.tmp.XXXXXX")" || return 1
  {
    printf '{"version":1,"restore_id":"%s","source":"inbox",' \
      "$(_json_escape "$restore_id")"
    printf '"requested_at":"%s","completed_at":"%s",' \
      "$(_json_escape "$requested_at")" "$(_json_escape "$completed_at")"
    printf '"review_state":"awaiting_review"}'
  } > "$tmp" || { rm -f "$tmp"; return 1; }
  chmod 644 "$tmp" || { rm -f "$tmp"; return 1; }
  ln -- "$tmp" "$OUTBOUND_QUARANTINE_SENTINEL" \
    || { rm -f -- "$tmp"; return 1; }
  rm -f -- "$tmp" || return 1
  sync -d "$OUTBOUND_QUARANTINE_SENTINEL" || return 1
  sync -f "$(dirname "$OUTBOUND_QUARANTINE_SENTINEL")" || return 1
}

consume_restore_request() {
  local content
  content="$(cat -- "$REQUEST_FILE" 2>/dev/null)" || return 1
  rm -f -- "$REQUEST_FILE" 2>/dev/null || return 1
  printf '%s' "$content"
}

# --- decrypt_or_passthrough — the INVERSE of backup.sh:encrypt_or_passthrough.
#     With a file arg ending in .enc it openssl-decrypts that file to stdout
#     (same cipher params backup.sh encrypts with); any other file is cat'd; with
#     no arg it passes stdin straight through. -----------------------------------
decrypt_or_passthrough() {
  local f="${1:-}"
  if [ -z "$f" ]; then
    cat
    return
  fi
  case "$f" in
    *.enc) openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -kfile "$ENC_KEYFILE" -in "$f" ;;
    *)     cat -- "$f" ;;
  esac
}

# --- Manifest authentication (the INVERSE of backup.sh's signing helpers) ------
# The manifest carries the sha256 of every archive, so the sha256 gate in STEP 2 is
# only as trustworthy as the manifest itself: anyone who can write the archive
# directory could swap an archive and rewrite its digest. These helpers verify the
# signature backup.sh emitted, BEFORE that gate runs.
#
# The derivation is an exact mirror of backup.sh's, including its deliberate direction:
# the PUBLIC domain label is the HMAC key and the SECRET key-file bytes are the message,
# because openssl cannot be keyed on the secret without exposing it in argv. See
# backup.sh for the full rationale. Duplicated rather than shared for the same reason
# qdrant_http_body is: the two scripts are independently mounted into the sidecar and a
# shared file would need a third mount.
derive_manifest_hmac_key() {
  openssl dgst -sha256 -hmac "$MANIFEST_HMAC_LABEL" -r < "$ENC_KEYFILE" | cut -d' ' -f1
}

# verify_manifest_signature <manifest> — recompute the MAC and compare it with the
# stored <manifest>.hmac. The comparison is a plain byte-compare of two fixed-length
# 64-hex digest files; it is NOT constant-time and does not need to be. A restore is an
# offline one-shot operator action with no online timing oracle, so an attacker gets no
# repeatable measurement to exploit. Returns non-zero on a missing signature, a
# mismatch, or any compute failure.
verify_manifest_signature() {
  local manifest="$1" stored="${1}.hmac" computed rc=1
  [ -s "$stored" ] || return 1
  computed="$(mktemp)" || return 1
  set +e
  openssl dgst -sha256 -mac HMAC -macopt "hexkey:$(derive_manifest_hmac_key)" -r < "$manifest" \
    2>/dev/null | cut -d' ' -f1 > "$computed"
  if [ -s "$computed" ] && cmp -s "$computed" "$stored"; then rc=0; fi
  set -e
  rm -f "$computed"
  return "$rc"
}

verify_manifest_signature_with_key() (
  local manifest="$1" keyfile="$2"
  [ -s "$keyfile" ] && [ ! -L "$keyfile" ] || return 1
  ENC_KEYFILE="$keyfile"
  verify_manifest_signature "$manifest"
)

# manifest_signature_required — whether an ABSENT signature is fatal. It answers only
# that question: a signature that is PRESENT is always verified regardless of this (see
# gate_manifest_signature). Off-host sets require one UNCONDITIONALLY — the operator key
# is present by construction, the archives are the least trusted, and a fresh-host
# restore is the decisive DR path — so off-host recovery needs a backup set taken by a
# version that signs. Same-host sets require one once the out-of-band ratchet marker
# exists — in EITHER of its two locations, since the requirement may only ever be
# added and a copy missing from one location is not evidence that it was never armed.
manifest_signature_required() {
  [ "$SOURCE" = "inbox" ] && return 0
  [ -e "$MANIFEST_HMAC_MARKER_DURABLE" ] || [ -e "$MANIFEST_HMAC_MARKER" ]
}

# break_glass_accepted — the ONLY escape from the signature requirement, for the
# disaster where the sole surviving backup set predates manifest signing. It needs BOTH
# JARVIS_RESTORE_ALLOW_LEGACY=1 AND the operator typing the confirmation phrase at an
# interactive prompt, so no combination of flags alone can take it and the sidecar's
# non-interactive restore can never reach it. It applies ONLY to an ABSENT signature:
# one that fails to verify is evidence of tampering, not of loss.
break_glass_accepted() {
  # Off-host sets are refused unconditionally; break-glass exists only for the disaster where the sole SAME-HOST backup predates signing.
  [ "$SOURCE" != "inbox" ] || return 1
  [ "${JARVIS_RESTORE_ALLOW_LEGACY:-}" = "1" ] || return 1
  [ -t 0 ] || return 1
  local reply=""
  printf 'This backup set has no authenticated manifest and cannot be verified. Type %s to restore it anyway: ' \
    "$BREAK_GLASS_PHRASE" >&2
  IFS= read -r reply || return 1
  [ "$reply" = "$BREAK_GLASS_PHRASE" ]
}

# gate_manifest_signature — refuse an unauthenticated backup set BEFORE any destruction.
# Returns (restore proceeds) or terminates via fail_before_destruction.
#
# Two independent questions, deliberately not conflated:
#   1. Is a signature PRESENT? If so it is ALWAYS verified — every source, every marker
#      state — and a FAILED verification always refuses. Break-glass cannot rescue it: a
#      bad MAC is evidence of tampering, not of loss.
#   2. Is an ABSENT signature fatal? That is manifest_signature_required's question, and
#      the only branch break-glass may rescue.
# A deployment with no backup key has nothing to sign or verify with and skips both,
# symmetrically with backup.sh.
gate_manifest_signature() {
  [ -n "$ENC_KEYFILE" ] && [ -s "$ENC_KEYFILE" ] || return 0
  if [ -e "${MANIFEST}.hmac" ]; then
    if verify_manifest_signature "$MANIFEST"; then
      MANIFEST_AUTHENTICATED=1
      return 0
    fi
    fail_before_destruction "backup ${TIMESTAMP} failed its manifest authentication check — the manifest does not match its signature and may have been tampered with; nothing was changed"
  fi
  if ! manifest_signature_required; then
    echo "[restore] WARNING: backup ${TIMESTAMP} predates manifest signing and cannot be checked for tampering; its archive checksums are self-reported. Take a new backup for a verifiable restore point." >&2
    return 0
  fi
  if break_glass_accepted; then
    echo "[restore] WARNING: restoring backup ${TIMESTAMP} WITHOUT manifest authentication on explicit operator override; its archive checksums are unverified" >&2
    return 0
  fi
  fail_before_destruction "backup ${TIMESTAMP} has no authenticated manifest (manifest_${TIMESTAMP}.json.hmac is missing); restore a backup taken by this version, or re-run interactively with JARVIS_RESTORE_ALLOW_LEGACY=1 to accept an unverified set; nothing was changed"
}

# --- valid_archive_name — mirror of backups.py:_FILENAME_RE. Rejects path
#     separators / '..' and pins the supported archive shapes, so a tampered timestamp
#     in the request can never escape /backups (e.g. into /run/secrets/*). --------
valid_archive_name() {
  local n="$1"
  case "$n" in
    */*|*\\*|*..*) return 1 ;;
  esac
  printf '%s' "$n" | grep -Eq \
    '^(jarvis_[0-9]{8}_[0-9]{6}\.sql\.gz(\.enc)?|litellm_[0-9]{8}_[0-9]{6}\.sql\.gz(\.enc)?|pdfs_[0-9]{8}_[0-9]{6}\.tar\.gz(\.enc)?|secrets_[0-9]{8}_[0-9]{6}\.tar\.gz(\.enc)?|qdrant_[A-Za-z0-9_-]+_[0-9]{8}_[0-9]{6}\.snapshot(\.enc)?)$'
}

# Parse an authenticated manifest into role<TAB>filename<TAB>sha256<TAB>size.
# Current manifests carry a run_id. The narrowly scoped compatibility branch
# accepts the exact manifest shape written before v1.2 only after the caller has
# authenticated it; unsigned historical sets remain on the explicit break-glass
# path in gate_manifest_signature. JSON::PP is part of the Perl runtime already
# used by this sidecar.
parse_authenticated_manifest() {
  local manifest="$1" expected_ts="$2" out="$3" tmp="${3}.tmp" base
  [ -f "$manifest" ] && [ ! -L "$manifest" ] || return 1
  base="$(basename "$manifest")"
  [ "$base" = "manifest_${expected_ts}.json" ] || return 1
  EXPECTED_TS="$expected_ts" perl -MJSON::PP -e '
    use strict; use warnings;
    local $/; my $d = decode_json(<>); my $ts = $ENV{EXPECTED_TS};
    die "root" unless ref($d) eq "HASH";
    die "timestamp" unless ($d->{timestamp} // "") eq $ts;
    my $legacy = !exists($d->{run_id});
    if ($legacy) {
      my @want = sort qw(timestamp app_version schema_version created_at archives);
      my @have = sort keys %$d;
      die "legacy root" unless join("\0", @have) eq join("\0", @want);
      my $v = $d->{app_version};
      die "legacy version" unless defined($v) && !ref($v);
      my $pre_v12 = $v eq "unknown";
      if (!$pre_v12 && $v =~ /^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$/) {
        $pre_v12 = $1 < 1 || ($1 == 1 && $2 < 2);
      }
      die "legacy version" unless $pre_v12;
      die "legacy schema" unless defined($d->{schema_version})
        && !ref($d->{schema_version}) && "$d->{schema_version}" =~ /^(?:0|[1-9][0-9]*)$/;
      die "legacy created_at" unless defined($d->{created_at})
        && !ref($d->{created_at})
        && $d->{created_at} =~ /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$/;
    } else {
      die "run_id" unless ($d->{run_id} // "") =~ /^[0-9a-f]{32}$/;
    }
    die "archives" unless ref($d->{archives}) eq "ARRAY";
    my (%seen, %required); my $archive_mode;
    for my $a (@{$d->{archives}}) {
      die "entry" unless ref($a) eq "HASH";
      if ($legacy) {
        my @want = sort qw(filename sha256 size_bytes);
        my @have = sort keys %$a;
        die "legacy entry" unless join("\0", @have) eq join("\0", @want);
      }
      my ($n,$h,$s) = @{$a}{qw(filename sha256 size_bytes)};
      die "fields" unless defined($n) && defined($h) && defined($s)
        && $h =~ /^[0-9a-f]{64}$/ && "$s" =~ /^\d+$/;
      my $encrypted = $n =~ /\.enc$/ ? 1 : 0;
      if (!$legacy) {
        if (defined($archive_mode)) { die "mixed encryption" unless $archive_mode == $encrypted; }
        else { $archive_mode = $encrypted; }
      }
      my $role;
      if ($n =~ /^jarvis_\Q$ts\E\.sql\.gz(?:\.enc)?$/) { $role="jarvis"; $required{jarvis}=1; }
      elsif ($n =~ /^litellm_\Q$ts\E\.sql\.gz(?:\.enc)?$/) { $role="litellm"; $required{litellm}=1; }
      elsif ($n =~ /^pdfs_\Q$ts\E\.tar\.gz(?:\.enc)?$/) { $role="pdfs"; $required{pdfs}=1; }
      elsif ($n =~ /^secrets_\Q$ts\E\.tar\.gz(?:\.enc)?$/) { $role="secrets"; $required{secrets}=1; }
      elsif ($n =~ /^qdrant_([A-Za-z0-9_-]+)_\Q$ts\E\.snapshot(?:\.enc)?$/) { $role="qdrant:$1"; }
      else { die "filename"; }
      die "duplicate role" if $seen{$role}++;
      print join("\t", $role, $n, $h, $s), "\n";
    }
    die "required" unless $required{jarvis} && $required{litellm}
      && ($legacy || $required{pdfs})
      && ($legacy || !$archive_mode || $required{secrets});
  ' "$manifest" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$out"
}

authenticated_manifest_is_legacy() {
  local manifest="$1"
  [ "$MANIFEST_AUTHENTICATED" = "1" ] \
    && [ -f "$manifest" ] && [ ! -L "$manifest" ] \
    && perl -MJSON::PP -e '
      use strict; use warnings;
      local $/; my $d = decode_json(<>);
      exit((ref($d) eq "HASH" && !exists($d->{run_id})) ? 0 : 1);
    ' "$manifest" 2>/dev/null
}

parse_allow_missing_pdfs_request() {
  local content="$1" count value
  ALLOW_MISSING_PDFS=0
  count="$(printf '%s' "$content" \
    | grep -oE '"allow_missing_pdfs"[[:space:]]*:' \
    | wc -l | tr -d ' ')"
  case "$count" in
    0) return 0 ;;
    1) ;;
    *) return 1 ;;
  esac
  value="$(printf '%s' "$content" \
    | grep -oE '"allow_missing_pdfs"[[:space:]]*:[[:space:]]*(true|false)' \
    | sed -E 's/.*:[[:space:]]*(true|false)/\1/' || true)"
  case "$value" in
    true) ALLOW_MISSING_PDFS=1 ;;
    false) ALLOW_MISSING_PDFS=0 ;;
    *) return 1 ;;
  esac
}

parse_allow_unknown_schema_request() {
  local content="$1" count value
  ALLOW_UNKNOWN_SCHEMA=0
  count="$(printf '%s' "$content" \
    | grep -oE '"allow_unknown_schema"[[:space:]]*:' \
    | wc -l | tr -d ' ')"
  case "$count" in
    0) return 0 ;;
    1) ;;
    *) return 1 ;;
  esac
  value="$(printf '%s' "$content" \
    | grep -oE '"allow_unknown_schema"[[:space:]]*:[[:space:]]*(true|false)' \
    | sed -E 's/.*:[[:space:]]*(true|false)/\1/' || true)"
  case "$value" in
    true) ALLOW_UNKNOWN_SCHEMA=1 ;;
    false) ALLOW_UNKNOWN_SCHEMA=0 ;;
    *) return 1 ;;
  esac
}

parse_restore_identity_request() {
  local content="$1" restore_id_count requested_at_count parsed
  RESTORE_ID=""
  REQUESTED_AT=""
  restore_id_count="$(printf '%s' "$content" \
    | grep -oE '"restore_id"[[:space:]]*:' \
    | wc -l | tr -d ' ')"
  requested_at_count="$(printf '%s' "$content" \
    | grep -oE '"requested_at"[[:space:]]*:' \
    | wc -l | tr -d ' ')"
  [ "$restore_id_count" = "1" ] && [ "$requested_at_count" = "1" ] || return 1
  parsed="$(printf '%s' "$content" | perl -MJSON::PP -e '
    use strict; use warnings;
    local $/; my $d = decode_json(<>);
    die "object" unless ref($d) eq "HASH";
    my ($id, $at) = @{$d}{qw(restore_id requested_at)};
    die "fields" unless defined($id) && defined($at) && !ref($id) && !ref($at);
    print "$id\t$at";
  ' 2>/dev/null)" || return 1
  RESTORE_ID="${parsed%%$'\t'*}"
  REQUESTED_AT="${parsed#*$'\t'}"
  [ "$RESTORE_ID" != "$parsed" ] || return 1
  printf '%s' "$RESTORE_ID" | grep -Eq '^[0-9a-f]{32}$' || return 1
  printf '%s' "$REQUESTED_AT" \
    | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$' \
    || return 1
}

missing_pdf_restore_is_authorized() {
  [ "$ALLOW_MISSING_PDFS" = "1" ] \
    && [ "$MANIFEST_AUTHENTICATED" = "1" ] \
    && [ "$MANIFEST_LEGACY" = "1" ]
}

verify_manifest_inventory() {
  local dir="$1" ts="$2" inventory="$3" role name expected_sha expected_size path actual
  [ -s "$inventory" ] || return 1
  while IFS=$'\t' read -r role name expected_sha expected_size; do
    [ -n "$role" ] && [ -n "$name" ] || return 1
    path="${dir}/${name}"
    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    actual="$(stat -c%s "$path" 2>/dev/null || true)"
    [ "$actual" = "$expected_size" ] || return 1
    actual="$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1 || true)"
    [ "$actual" = "$expected_sha" ] || return 1
  done < "$inventory"
  while IFS= read -r path; do
    name="$(basename "$path")"
    case "$name" in "manifest_${ts}.json"|"manifest_${ts}.json.hmac") continue ;; esac
    awk -F '\t' -v n="$name" '$2 == n { found=1 } END { exit !found }' "$inventory" \
      || return 1
  done < <(find "$dir" -maxdepth 1 \( -type f -o -type l \) -name "*_${ts}.*" -print)
}

stage_manifest_inventory() {
  local source_dir="$1" ts="$2" inventory="$3" dest="$4"
  local role name expected_sha expected_size staged
  verify_manifest_inventory "$source_dir" "$ts" "$inventory" || return 1
  [ -d "$dest" ] && [ ! -L "$dest" ] || return 1
  while IFS=$'\t' read -r role name expected_sha expected_size; do
    staged="${dest}/.${name}.tmp.$$"
    cp --reflink=auto -- "${source_dir}/${name}" "$staged" || return 1
    chmod 400 "$staged" || return 1
    mv -T -- "$staged" "${dest}/${name}" || return 1
  done < "$inventory"
  verify_manifest_inventory "$dest" "$ts" "$inventory"
}

# The safety backup is the only rollback point after the first database swap.
# Keep a private, authenticated copy on the durable backup volume so later
# retention or a path replacement cannot invalidate that rollback point.
stage_safety_backup() {
  local ts="$1" expected_run_id="$2" manifest="$3"
  local stage_tmp stage_final inventory staged_manifest
  [ -n "$TARGET_BACKUP_KEYFILE" ] && [ -s "$TARGET_BACKUP_KEYFILE" ] \
    && [ ! -L "$TARGET_BACKUP_KEYFILE" ] || return 1
  [ -f "$manifest" ] && [ ! -L "$manifest" ] \
    && [ -f "${manifest}.hmac" ] && [ ! -L "${manifest}.hmac" ] || return 1
  verify_manifest_signature_with_key "$manifest" "$TARGET_BACKUP_KEYFILE" || return 1

  stage_final="${BACKUP_DIR}/.restore-safety-${ts}-${expected_run_id}"
  stage_tmp="${stage_final}.tmp.$$"
  [ ! -e "$stage_final" ] && [ ! -L "$stage_final" ] \
    && [ ! -e "$stage_tmp" ] && [ ! -L "$stage_tmp" ] || return 1
  if ! (umask 077 && mkdir -m 700 -- "$stage_tmp"); then return 1; fi
  inventory="${stage_tmp}/.inventory.tsv"

  if ! parse_authenticated_manifest "$manifest" "$ts" "$inventory" \
     || ! EXPECTED_RUN_ID="$expected_run_id" perl -MJSON::PP -e '
          use strict; use warnings;
          local $/; my $d = decode_json(<>);
          die "run_id" unless ref($d) eq "HASH"
            && ($d->{run_id} // "") eq $ENV{EXPECTED_RUN_ID};
        ' "$manifest" >/dev/null 2>&1 \
     || ! awk -F '\t' '
          NF != 4 { bad=1; next }
          $2 !~ /\.enc$/ || $4 !~ /^[1-9][0-9]*$/ { bad=1 }
          $1 == "jarvis" { jarvis++ }
          $1 == "litellm" { litellm++ }
          $1 == "pdfs" { pdfs++ }
          $1 == "secrets" { secrets++ }
          $1 !~ /^(jarvis|litellm|pdfs|secrets|qdrant:[A-Za-z0-9_-]+)$/ { bad=1 }
          END { exit bad || jarvis != 1 || litellm != 1 || pdfs != 1 || secrets != 1 }
        ' "$inventory" \
     || ! verify_manifest_inventory "$BACKUP_DIR" "$ts" "$inventory"; then
    rm -rf -- "$stage_tmp"
    return 1
  fi

  staged_manifest="${stage_tmp}/$(basename "$manifest")"
  if ! cp --reflink=auto -- "$manifest" "$staged_manifest" \
     || ! cp --reflink=auto -- "${manifest}.hmac" "${staged_manifest}.hmac" \
     || ! chmod 400 "$inventory" "$staged_manifest" "${staged_manifest}.hmac" \
     || ! stage_manifest_inventory "$BACKUP_DIR" "$ts" "$inventory" "$stage_tmp" \
     || ! verify_manifest_signature_with_key "$staged_manifest" "$TARGET_BACKUP_KEYFILE" \
     || ! verify_manifest_inventory "$stage_tmp" "$ts" "$inventory" \
     || ! mv -T -- "$stage_tmp" "$stage_final"; then
    rm -rf -- "$stage_tmp"
    return 1
  fi
  SAFETY_STAGING_DIR="$stage_final"
  return 0
}

remove_safety_staging() {
  local base
  [ -n "$SAFETY_STAGING_DIR" ] || return 0
  base="$(basename -- "$SAFETY_STAGING_DIR")"
  [ "$(dirname -- "$SAFETY_STAGING_DIR")" = "$BACKUP_DIR" ] || return 1
  printf '%s' "$base" \
    | grep -Eq '^\.restore-safety-[0-9]{8}_[0-9]{6}-[0-9a-f]{32}$' || return 1
  rm -rf -- "$SAFETY_STAGING_DIR"
  SAFETY_STAGING_DIR=""
}

# --- resolve_secrets_archive — echo the secrets_<ts> archive path in ${ARCHIVE_DIR}
#     (prefer the .enc form), or nothing (return 1) if absent. Single source shared by
#     the STEP-2 secrets preflight (fail BEFORE any destruction when it is missing) and
#     STEP 8 (materialize it), so the two can never diverge on what "has secrets" means.
resolve_secrets_archive() {
  local cand
  for cand in "${ARCHIVE_DIR}/secrets_${TIMESTAMP}.tar.gz.enc" \
              "${ARCHIVE_DIR}/secrets_${TIMESTAMP}.tar.gz"; do
    if [ -f "$cand" ]; then printf '%s' "$cand"; return 0; fi
  done
  return 1
}

resolve_pdfs_archive() {
  local cand
  for cand in "${ARCHIVE_DIR}/pdfs_${TIMESTAMP}.tar.gz.enc" \
              "${ARCHIVE_DIR}/pdfs_${TIMESTAMP}.tar.gz"; do
    if [ -f "$cand" ] && [ ! -L "$cand" ]; then printf '%s' "$cand"; return 0; fi
  done
  return 1
}

# Only keys cryptographically coupled to restored database content cross hosts.
restorable_inbox_secret_basename() {
  local base="$1"
  case "$base" in
    jarvis_config_key.txt|jarvis_model_hmac_key.txt|litellm_salt_key.txt) return 0 ;;
  esac
  return 1
}

validate_restored_data_key() {
  local file="$1" base="$2" value normalized decoded_size size
  [ -f "$file" ] && [ ! -L "$file" ] && [ -s "$file" ] || return 1
  size="$(stat -c%s "$file" 2>/dev/null || echo 4097)"
  [ "$size" -ge 1 ] 2>/dev/null && [ "$size" -le 4096 ] 2>/dev/null || return 1
  value="$(cat "$file")"
  [ -n "$value" ] || return 1
  case "$value" in *$'\n'*|*$'\r'*) return 1 ;; esac
  case "$base" in
    jarvis_config_key.txt)
      printf '%s' "$value" | grep -Eq '^[A-Za-z0-9_+/-]{43}=$' || return 1
      normalized="$(printf '%s' "$value" | tr '_-' '/+')"
      decoded_size="$(printf '%s' "$normalized" \
        | openssl base64 -d -A 2>/dev/null | wc -c | tr -d ' ')"
      [ "$decoded_size" = "32" ] || return 1
      ;;
    jarvis_model_hmac_key.txt)
      [ "${#value}" -ge 32 ] || return 1
      ;;
    litellm_salt_key.txt)
      [ "${#value}" -ge 16 ] || return 1
      ;;
    *) return 1 ;;
  esac
}

# Decrypt and validate the data-key archive before any database mutation. New
# archives must contain exactly the three data keys; authenticated historical
# archives may contain additional host credentials, but those files are ignored
# and never extracted.
stage_restored_data_keys() {
  local archive="$1" exact="$2" incoming listing key count detail size
  case "$exact" in 0|1) ;; *) return 1 ;; esac
  [ -f "$archive" ] && [ ! -L "$archive" ] || return 1
  purge_secrets_staging
  if [ -e "$SECRETS_STAGING" ] || [ -L "$SECRETS_STAGING" ]; then return 1; fi
  mkdir -m 700 -- "$SECRETS_STAGING" || return 1
  incoming="${SECRETS_STAGING}/.incoming.tar.gz"
  if ! decrypt_or_passthrough "$archive" > "$incoming" 2>/dev/null; then
    purge_secrets_staging
    return 1
  fi
  listing="$(tar --quoting-style=escape -tzf "$incoming" 2>/dev/null)" || {
    purge_secrets_staging
    return 1
  }
  if [ "$exact" = "1" ]; then
    while IFS= read -r key; do
      [ -n "$key" ] || continue
      restorable_inbox_secret_basename "$key" || { purge_secrets_staging; return 1; }
    done <<< "$listing"
  fi
  for key in jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt; do
    count="$(printf '%s\n' "$listing" | grep -cxF "$key" || true)"
    [ "$count" = "1" ] || { purge_secrets_staging; return 1; }
    detail="$(tar --numeric-owner --quoting-style=escape -tvzf "$incoming" -- "$key" 2>/dev/null)" \
      || { purge_secrets_staging; return 1; }
    [ "$(printf '%s\n' "$detail" | wc -l | tr -d ' ')" = "1" ] \
      || { purge_secrets_staging; return 1; }
    [ "${detail:0:1}" = "-" ] || { purge_secrets_staging; return 1; }
    size="$(printf '%s\n' "$detail" | awk '{print $3}')"
    printf '%s' "$size" | grep -Eq '^[1-9][0-9]{0,3}$' \
      || { purge_secrets_staging; return 1; }
    [ "$size" -le 4096 ] || { purge_secrets_staging; return 1; }
  done
  if ! tar --no-same-owner --no-same-permissions -xzf "$incoming" \
      -C "$SECRETS_STAGING" -- \
      jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt 2>/dev/null; then
    purge_secrets_staging
    return 1
  fi
  for key in jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt; do
    validate_restored_data_key "${SECRETS_STAGING}/${key}" "$key" \
      || { purge_secrets_staging; return 1; }
    chmod 600 "${SECRETS_STAGING}/${key}" || { purge_secrets_staging; return 1; }
  done
  return 0
}

data_key_transaction_dir() {
  printf '%s/data-key-restore' "$LOCK_DIR"
}

remove_data_key_transaction() {
  local transaction
  transaction="$(data_key_transaction_dir)" || return 1
  case "$transaction" in "${LOCK_DIR}/data-key-restore") ;; *) return 1 ;; esac
  [ ! -L "$transaction" ] || return 1
  rm -rf -- "$transaction"
}

validate_data_key_set() {
  local source_dir="$1" key
  [ -d "$source_dir" ] && [ ! -L "$source_dir" ] || return 1
  for key in jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt; do
    validate_restored_data_key "${source_dir}/${key}" "$key" || return 1
  done
}

replace_data_key_set_from_dir() {
  local source_dir="$1" key source dest tmp
  validate_data_key_set "$source_dir" || return 1
  [ -d "$HOST_SECRETS_DIR" ] && [ ! -L "$HOST_SECRETS_DIR" ] || return 1
  # Validate every destination before replacing the first member.
  for key in jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt; do
    dest="${HOST_SECRETS_DIR}/${key}"
    [ -f "$dest" ] && [ ! -L "$dest" ] || return 1
  done
  for key in jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt; do
    source="${source_dir}/${key}"
    dest="${HOST_SECRETS_DIR}/${key}"
    tmp="${dest}.restore.$$"
    [ ! -e "$tmp" ] && [ ! -L "$tmp" ] || return 1
    if ! cp -- "$source" "$tmp" || ! chmod 644 "$tmp" || ! mv -T -- "$tmp" "$dest"; then
      rm -f -- "$tmp"
      return 1
    fi
  done
}

prepare_data_key_transaction() {
  local transaction old_dir new_dir key
  transaction="$(data_key_transaction_dir)" || return 1
  old_dir="${transaction}/old"
  new_dir="${transaction}/new"
  [ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] || return 1
  [ ! -e "$transaction" ] && [ ! -L "$transaction" ] || return 1
  mkdir -m 700 -- "$transaction" "$old_dir" "$new_dir" || return 1
  for key in jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt; do
    validate_restored_data_key "${SECRETS_STAGING}/${key}" "$key" \
      || { remove_data_key_transaction; return 1; }
    validate_restored_data_key "${HOST_SECRETS_DIR}/${key}" "$key" \
      || { remove_data_key_transaction; return 1; }
    cp -- "${HOST_SECRETS_DIR}/${key}" "${old_dir}/${key}" \
      || { remove_data_key_transaction; return 1; }
    cp -- "${SECRETS_STAGING}/${key}" "${new_dir}/${key}" \
      || { remove_data_key_transaction; return 1; }
    chmod 600 "${old_dir}/${key}" "${new_dir}/${key}" \
      || { remove_data_key_transaction; return 1; }
  done
  validate_data_key_set "$old_dir" && validate_data_key_set "$new_dir" \
    && write_lifecycle_file "${transaction}/state" prepared 600 \
    || { remove_data_key_transaction; return 1; }
}

# Replace exactly the three data-coupled keys as a crash-recoverable set. The
# durable transaction retains complete old and new copies until all members and
# the reload marker are installed. Immediate failures roll the whole set back;
# --recover completes a stranded transaction before maintenance can lift.
install_restored_data_keys() {
  local transaction old_dir new_dir
  transaction="$(data_key_transaction_dir)" || return 1
  old_dir="${transaction}/old"
  new_dir="${transaction}/new"
  prepare_data_key_transaction || return 1
  write_lifecycle_file "${transaction}/state" installing 600 \
    || { remove_data_key_transaction; return 1; }
  if ! replace_data_key_set_from_dir "$new_dir"; then
    if replace_data_key_set_from_dir "$old_dir"; then
      remove_data_key_transaction || true
    fi
    return 1
  fi
  write_lifecycle_file "${transaction}/state" installed 600 || return 1
  write_lifecycle_file "${TRIGGER_DIR}/.secrets_rotated" "$(date +%s)" 644 || return 1
  remove_data_key_transaction
}

recover_restored_data_keys() {
  local transaction state old_dir new_dir
  transaction="$(data_key_transaction_dir)" || return 1
  old_dir="${transaction}/old"
  new_dir="${transaction}/new"
  [ -d "$transaction" ] && [ ! -L "$transaction" ] \
    && [ -f "${transaction}/state" ] && [ ! -L "${transaction}/state" ] || return 1
  state="$(cat "${transaction}/state" 2>/dev/null || true)"
  case "$state" in prepared|installing|installed) ;; *) return 1 ;; esac
  validate_data_key_set "$old_dir" && validate_data_key_set "$new_dir" || return 1
  if ! replace_data_key_set_from_dir "$new_dir"; then
    replace_data_key_set_from_dir "$old_dir" || true
    return 1
  fi
  write_lifecycle_file "${transaction}/state" installed 600 || return 1
  write_lifecycle_file "${TRIGGER_DIR}/.secrets_rotated" "$(date +%s)" 644 || return 1
  remove_data_key_transaction
}

valid_pdf_restore_run_id() {
  printf '%s' "$1" | grep -Eq '^[0-9a-f]{32}$'
}

pdf_stage_dir() {
  valid_pdf_restore_run_id "$1" || return 1
  printf '%s/.restore-stage-%s' "$PDF_STORAGE_DIR" "$1"
}

pdf_old_dir() {
  valid_pdf_restore_run_id "$1" || return 1
  printf '%s/.restore-old-%s' "$PDF_STORAGE_DIR" "$1"
}

verify_pdf_inventory() {
  local dir="$1" inventory="$2" name expected_size expected_sha path actual seen=""
  [ -d "$dir" ] && [ ! -L "$dir" ] || return 1
  [ -f "$inventory" ] && [ ! -L "$inventory" ] || return 1
  while IFS=$'\t' read -r name expected_size expected_sha; do
    [ -n "$name" ] || continue
    printf '%s' "$name" | grep -Eq '^[0-9]+\.pdf$' || return 1
    printf '%s' "$expected_size" | grep -Eq '^(0|[1-9][0-9]*)$' || return 1
    printf '%s' "$expected_sha" | grep -Eq '^[0-9a-f]{64}$' || return 1
    printf '%s\n' "$seen" | grep -qxF "$name" && return 1
    seen="${seen}${name}"$'\n'
    path="${dir}/${name}"
    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    actual="$(stat -c%s "$path" 2>/dev/null || true)"
    [ "$actual" = "$expected_size" ] || return 1
    actual="$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1 || true)"
    [ "$actual" = "$expected_sha" ] || return 1
  done < "$inventory"
  while IFS= read -r -d '' path; do
    name="$(basename -- "$path")"
    printf '%s\n' "$seen" | grep -qxF "$name" || return 1
  done < <(find "$dir" -regextype posix-extended -mindepth 1 -maxdepth 1 \
    -regex '.*/[0-9]+\.pdf' -print0)
}

# Stage and bound the complete PDF set before maintenance or database mutation.
# The archive may be empty, but every member it does contain must be one flat,
# numeric regular file.
stage_restored_pdfs() {
  local archive="$1" run_id="$2" stage old incoming listing name detail size
  local count=0 total=0 archive_size available_kb required_bytes inventory_tmp path sha
  valid_pdf_restore_run_id "$run_id" || return 1
  [ -d "$PDF_STORAGE_DIR" ] && [ ! -L "$PDF_STORAGE_DIR" ] || return 1
  [ -f "$archive" ] && [ ! -L "$archive" ] || return 1
  stage="$(pdf_stage_dir "$run_id")" || return 1
  old="$(pdf_old_dir "$run_id")" || return 1
  [ ! -e "$stage" ] && [ ! -L "$stage" ] \
    && [ ! -e "$old" ] && [ ! -L "$old" ] || return 1
  mkdir -m 700 -- "$stage" || return 1
  incoming="${stage}/.incoming.tar.gz"
  if ! decrypt_or_passthrough "$archive" > "$incoming" 2>/dev/null; then
    rm -rf -- "$stage"
    return 1
  fi
  listing="$(tar --quoting-style=escape -tzf "$incoming" 2>/dev/null)" || {
    rm -rf -- "$stage"
    return 1
  }
  declare -A seen=()
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    printf '%s' "$name" | grep -Eq '^[0-9]+\.pdf$' \
      || { rm -rf -- "$stage"; return 1; }
    [ -z "${seen[$name]+x}" ] || { rm -rf -- "$stage"; return 1; }
    seen[$name]=1
    count=$((count + 1))
    detail="$(tar --numeric-owner --quoting-style=escape -tvzf "$incoming" -- "$name" 2>/dev/null)" \
      || { rm -rf -- "$stage"; return 1; }
    [ "$(printf '%s\n' "$detail" | wc -l | tr -d ' ')" = "1" ] \
      && [ "${detail:0:1}" = "-" ] || { rm -rf -- "$stage"; return 1; }
    size="$(printf '%s\n' "$detail" | awk '{print $3}')"
    printf '%s' "$size" | grep -Eq '^(0|[1-9][0-9]*)$' \
      || { rm -rf -- "$stage"; return 1; }
    [ "$size" -le "${PDF_RESTORE_MAX_FILE_BYTES:-104857600}" ] 2>/dev/null \
      || { rm -rf -- "$stage"; return 1; }
    total=$((total + size))
  done <<< "$listing"
  [ "$count" -le "${PDF_RESTORE_MAX_FILES:-100000}" ] 2>/dev/null \
    && [ "$total" -le "${PDF_RESTORE_MAX_TOTAL_BYTES:-21474836480}" ] 2>/dev/null \
    || { rm -rf -- "$stage"; return 1; }
  archive_size="$(stat -c%s "$incoming" 2>/dev/null || echo 0)"
  required_bytes=$((total + archive_size + ${PDF_RESTORE_HEADROOM_BYTES:-67108864}))
  available_kb="$(df -Pk "$PDF_STORAGE_DIR" 2>/dev/null | awk 'NR == 2 {print $4}')"
  printf '%s' "$available_kb" | grep -Eq '^[0-9]+$' \
    && [ $((available_kb * 1024)) -ge "$required_bytes" ] \
    || { rm -rf -- "$stage"; return 1; }
  if ! tar --no-same-owner --no-same-permissions -xzf "$incoming" -C "$stage" 2>/dev/null; then
    rm -rf -- "$stage"
    return 1
  fi
  rm -f -- "$incoming"
  inventory_tmp="${stage}/.inventory.tsv.tmp"
  : > "$inventory_tmp" || { rm -rf -- "$stage"; return 1; }
  while IFS= read -r -d '' path; do
    name="$(basename -- "$path")"
    [ -f "$path" ] && [ ! -L "$path" ] || { rm -rf -- "$stage"; return 1; }
    size="$(stat -c%s "$path" 2>/dev/null || true)"
    sha="$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1 || true)"
    printf '%s\t%s\t%s\n' "$name" "$size" "$sha" >> "$inventory_tmp" \
      || { rm -rf -- "$stage"; return 1; }
  done < <(find "$stage" -regextype posix-extended -mindepth 1 -maxdepth 1 \
    -type f -regex '.*/[0-9]+\.pdf' -print0 | sort -z)
  mv -T -- "$inventory_tmp" "${stage}/.inventory.tsv" \
    || { rm -rf -- "$stage"; return 1; }
  verify_pdf_inventory "$stage" "${stage}/.inventory.tsv" \
    || { rm -rf -- "$stage"; return 1; }
}

stage_empty_pdf_set() {
  local run_id="$1" stage old
  valid_pdf_restore_run_id "$run_id" || return 1
  [ -d "$PDF_STORAGE_DIR" ] && [ ! -L "$PDF_STORAGE_DIR" ] || return 1
  stage="$(pdf_stage_dir "$run_id")" || return 1
  old="$(pdf_old_dir "$run_id")" || return 1
  [ ! -e "$stage" ] && [ ! -L "$stage" ] \
    && [ ! -e "$old" ] && [ ! -L "$old" ] || return 1
  mkdir -m 700 -- "$stage" || return 1
  : > "${stage}/.inventory.tsv" || { rm -rf -- "$stage"; return 1; }
}

write_pdf_swap_state() {
  local run_id="$1" phase="$2" tmp="${SWAP_STATE_FILE}.tmp"
  valid_pdf_restore_run_id "$run_id" || return 1
  case "$phase" in move_old|move_new|verify|cleanup) ;; *) return 1 ;; esac
  printf '{"version":2,"resource":"pdfs","run_id":"%s","phase":"%s"}' \
    "$run_id" "$phase" > "$tmp" 2>/dev/null || return 1
  mv -T -- "$tmp" "$SWAP_STATE_FILE" 2>/dev/null || return 1
}

read_pdf_swap_state() {
  [ -f "$SWAP_STATE_FILE" ] && [ ! -L "$SWAP_STATE_FILE" ] \
    && [ "$(stat -c%s "$SWAP_STATE_FILE" 2>/dev/null || echo 513)" -le 512 ] || return 1
  perl -MJSON::PP -e '
    use strict; use warnings;
    local $/; my $d = decode_json(<>);
    die "root" unless ref($d) eq "HASH";
    my @want = sort qw(version resource run_id phase);
    my @have = sort keys %$d;
    die "keys" unless join("\0", @want) eq join("\0", @have);
    die "version" unless ($d->{version} // 0) == 2;
    die "resource" unless ($d->{resource} // "") eq "pdfs";
    die "run" unless ($d->{run_id} // "") =~ /^[0-9a-f]{32}$/;
    die "phase" unless ($d->{phase} // "") =~ /^(?:move_old|move_new|verify|cleanup)$/;
    print "$d->{run_id}\t$d->{phase}";
  ' "$SWAP_STATE_FILE" 2>/dev/null
}

_complete_pdf_swap_locked() {
  local run_id="$1" phase="$2" stage old path name inventory
  stage="$(pdf_stage_dir "$run_id")" || return 1
  old="$(pdf_old_dir "$run_id")" || return 1
  inventory="${stage}/.inventory.tsv"
  [ -d "$stage" ] && [ ! -L "$stage" ] && [ -f "$inventory" ] && [ ! -L "$inventory" ] \
    && [ -d "$old" ] && [ ! -L "$old" ] || return 1

  if [ "$phase" = "move_old" ]; then
    while IFS= read -r -d '' path; do
      name="$(basename -- "$path")"
      [ -f "$path" ] && [ ! -L "$path" ] && [ ! -e "${old}/${name}" ] \
        || return 1
      mv -T -- "$path" "${old}/${name}" || return 1
    done < <(find "$PDF_STORAGE_DIR" -regextype posix-extended -mindepth 1 -maxdepth 1 \
      -regex '.*/[0-9]+\.pdf' -print0 | sort -z)
    write_pdf_swap_state "$run_id" move_new || return 1
    phase=move_new
  fi
  if [ "$phase" = "move_new" ]; then
    while IFS= read -r -d '' path; do
      name="$(basename -- "$path")"
      [ -f "$path" ] && [ ! -L "$path" ] && [ ! -e "${PDF_STORAGE_DIR}/${name}" ] \
        || return 1
      mv -T -- "$path" "${PDF_STORAGE_DIR}/${name}" || return 1
    done < <(find "$stage" -regextype posix-extended -mindepth 1 -maxdepth 1 \
      -type f -regex '.*/[0-9]+\.pdf' -print0 | sort -z)
    write_pdf_swap_state "$run_id" verify || return 1
    phase=verify
  fi
  if [ "$phase" = "verify" ]; then
    verify_pdf_inventory "$PDF_STORAGE_DIR" "$inventory" || return 1
    write_pdf_swap_state "$run_id" cleanup || return 1
    phase=cleanup
  fi
  if [ "$phase" = "cleanup" ]; then
    verify_pdf_inventory "$PDF_STORAGE_DIR" "$inventory" || return 1
    # Commit by clearing the durable journal before discarding recovery evidence.
    # If the clear fails, keep both directories so the same phase can retry.
    clear_swap_state || return 1
    rm -rf -- "$old" "$stage" \
      || echo "[restore] WARNING: restored PDFs are committed but transaction staging could not be removed" >&2
  fi
}

open_pdf_publish_lock_exclusive() {
  local lock="${PDF_STORAGE_DIR}/.publish.lock" path_id fd_id
  [ -d "$PDF_STORAGE_DIR" ] && [ ! -L "$PDF_STORAGE_DIR" ] || return 1
  [ ! -L "$lock" ] || return 1
  if [ ! -e "$lock" ]; then
    (umask 022; set -C; : > "$lock") 2>/dev/null || [ -e "$lock" ] || return 1
  fi
  [ -f "$lock" ] && [ ! -L "$lock" ] || return 1
  path_id="$(stat -Lc '%d:%i' -- "$lock" 2>/dev/null || true)"
  [ -n "$path_id" ] || return 1
  exec 8<"$lock" || return 1
  fd_id="$(stat -Lc '%d:%i' -- /proc/self/fd/8 2>/dev/null || true)"
  if [ "$fd_id" != "$path_id" ] || [ -L "$lock" ]; then
    exec 8>&-
    return 1
  fi
  if ! flock 8; then
    exec 8>&-
    return 1
  fi
  path_id="$(stat -Lc '%d:%i' -- "$lock" 2>/dev/null || true)"
  fd_id="$(stat -Lc '%d:%i' -- /proc/self/fd/8 2>/dev/null || true)"
  if [ -L "$lock" ] || [ -z "$path_id" ] || [ "$fd_id" != "$path_id" ]; then
    flock -u 8 2>/dev/null || true
    exec 8>&-
    return 1
  fi
}

swap_restored_pdfs() {
  local run_id="$1" stage old
  valid_pdf_restore_run_id "$run_id" || return 1
  stage="$(pdf_stage_dir "$run_id")" || return 1
  old="$(pdf_old_dir "$run_id")" || return 1
  [ -d "$stage" ] && [ ! -L "$stage" ] && [ ! -e "$old" ] && [ ! -L "$old" ] \
    || return 1
  (
    open_pdf_publish_lock_exclusive || exit 1
    mkdir -m 700 -- "$old" || exit 1
    write_pdf_swap_state "$run_id" move_old || exit 1
    _complete_pdf_swap_locked "$run_id" move_old
  )
}

recover_pdf_swap() {
  local record run_id phase
  record="$(read_pdf_swap_state)" || return 1
  IFS=$'\t' read -r run_id phase <<< "$record"
  (
    open_pdf_publish_lock_exclusive || exit 1
    _complete_pdf_swap_locked "$run_id" "$phase"
  )
}

remove_staged_pdf_restore() {
  local run_id="$1" stage old path
  valid_pdf_restore_run_id "$run_id" || return 1
  stage="$(pdf_stage_dir "$run_id")" || return 1
  old="$(pdf_old_dir "$run_id")" || return 1
  for path in "$stage" "$old"; do
    case "$path" in
      "${PDF_STORAGE_DIR}/.restore-stage-${run_id}"|"${PDF_STORAGE_DIR}/.restore-old-${run_id}") ;;
      *) return 1 ;;
    esac
  done
  rm -rf -- "$stage" "$old"
}

# --- safety_backup_is_fresh <backup_rc> <expected_run_id> — accept only the
# caller-correlated successful run and its matching manifest. Exact correlation
# avoids both stale .last_run reuse and false rejection within the same second.
safety_backup_is_fresh() {
  local rc="$1" expected_run_id="$2" lr ts manifest
  [ "$rc" -eq 0 ] || return 1
  printf '%s' "$expected_run_id" | grep -Eq '^[0-9a-f]{32}$' || return 1
  [ -f "${BACKUP_DIR}/.last_run.json" ] && [ ! -L "${BACKUP_DIR}/.last_run.json" ] || return 1
  lr="$(cat "${BACKUP_DIR}/.last_run.json" 2>/dev/null || true)"
  [ "${#lr}" -le 16384 ] || return 1
  ts="$(printf '%s' "$lr" | EXPECTED_RUN_ID="$expected_run_id" perl -MJSON::PP -e '
    use strict; use warnings;
    local $/; my $d = decode_json(<>);
    die "root" unless ref($d) eq "HASH";
    die "success" unless ref($d->{succeeded}) eq "JSON::PP::Boolean" && $d->{succeeded};
    die "run_id" unless ($d->{run_id} // "") eq $ENV{EXPECTED_RUN_ID};
    my $ts = $d->{timestamp} // "";
    die "timestamp" unless $ts =~ /^[0-9]{8}_[0-9]{6}$/;
    print $ts;
  ' 2>/dev/null || true)"
  printf '%s' "$ts" | grep -Eq '^[0-9]{8}_[0-9]{6}$' || return 1
  manifest="${BACKUP_DIR}/manifest_${ts}.json"
  stage_safety_backup "$ts" "$expected_run_id" "$manifest" || return 1
  SAFETY_BACKUP_TS="$ts"
}

# --- write_inbox_manifest — inventory ${INBOX_DIR} into a SANITIZED
#     ${TRIGGER_DIR}/.inbox_manifest.json (names/booleans ONLY — never a path or a key
#     byte) that the admin app's GET /inbox lists. Groups valid_archive_name-accepted
#     files by their %Y%m%d_%H%M%S timestamp. It distinguishes a current complete
#     set from an authenticated pre-v1.2 set that legitimately has no PDF archive;
#     an arbitrary current set cannot acquire that compatibility label. Atomic
#     tmp->mv; writes [] on an empty inbox. Never touches the DB, consumes the
#     restore request, or emits a path or key content. Called only from the
#     --inbox-manifest branch (MANIFEST_MODE).
write_inbox_manifest() {
  local out="${TRIGGER_DIR}/.inbox_manifest.json"
  local tmp="${out}.tmp"
  local has_key="false"
  [ -s "$OPERATOR_KEYFILE" ] && has_key="true"

  # Distinct, sorted timestamps of every allow-listed archive in the inbox. The ts is
  # extracted immediately before the known extension (mirrors backups.py:_TS_RE) so a
  # collection name that itself contains digits cannot be misread as the timestamp.
  local timestamps="" f base ts
  shopt -s nullglob
  for f in "${INBOX_DIR}"/*; do
    base="$(basename "$f")"
    valid_archive_name "$base" || continue
    ts="$(printf '%s' "$base" \
      | sed -nE 's/.*_([0-9]{8}_[0-9]{6})\.(sql\.gz|tar\.gz|snapshot)(\.enc)?$/\1/p')"
    [ -n "$ts" ] && timestamps="${timestamps}${ts}"$'\n'
  done
  shopt -u nullglob
  timestamps="$(printf '%s' "$timestamps" | sort -u)"

  {
    printf '['
    local first=1 t complete has_pdfs legacy_missing_pdfs has_secrets
    local jarvis_plain jarvis_enc litellm_plain litellm_enc pdfs_plain pdfs_enc
    local secrets_plain secrets_enc manifest signature inventory coherent_current
    while IFS= read -r t; do
      [ -n "$t" ] || continue
      complete="false"
      has_pdfs="false"
      legacy_missing_pdfs="false"
      has_secrets="false"
      coherent_current="false"
      jarvis_plain=0; jarvis_enc=0; litellm_plain=0; litellm_enc=0
      pdfs_plain=0; pdfs_enc=0; secrets_plain=0; secrets_enc=0
      [ -f "${INBOX_DIR}/jarvis_${t}.sql.gz" ] \
        && [ ! -L "${INBOX_DIR}/jarvis_${t}.sql.gz" ] && jarvis_plain=1
      [ -f "${INBOX_DIR}/jarvis_${t}.sql.gz.enc" ] \
        && [ ! -L "${INBOX_DIR}/jarvis_${t}.sql.gz.enc" ] && jarvis_enc=1
      [ -f "${INBOX_DIR}/litellm_${t}.sql.gz" ] \
        && [ ! -L "${INBOX_DIR}/litellm_${t}.sql.gz" ] && litellm_plain=1
      [ -f "${INBOX_DIR}/litellm_${t}.sql.gz.enc" ] \
        && [ ! -L "${INBOX_DIR}/litellm_${t}.sql.gz.enc" ] && litellm_enc=1
      [ -f "${INBOX_DIR}/pdfs_${t}.tar.gz" ] \
        && [ ! -L "${INBOX_DIR}/pdfs_${t}.tar.gz" ] && pdfs_plain=1
      [ -f "${INBOX_DIR}/pdfs_${t}.tar.gz.enc" ] \
        && [ ! -L "${INBOX_DIR}/pdfs_${t}.tar.gz.enc" ] && pdfs_enc=1
      [ -f "${INBOX_DIR}/secrets_${t}.tar.gz" ] \
        && [ ! -L "${INBOX_DIR}/secrets_${t}.tar.gz" ] && secrets_plain=1
      [ -f "${INBOX_DIR}/secrets_${t}.tar.gz.enc" ] \
        && [ ! -L "${INBOX_DIR}/secrets_${t}.tar.gz.enc" ] && secrets_enc=1
      manifest="${INBOX_DIR}/manifest_${t}.json"
      signature="${manifest}.hmac"

      [ "$((pdfs_plain + pdfs_enc))" -eq 1 ] && has_pdfs="true"
      [ "$((secrets_plain + secrets_enc))" -eq 1 ] && has_secrets="true"
      if [ -f "$manifest" ] && [ ! -L "$manifest" ]; then
        if [ "$jarvis_plain" -eq 1 ] && [ "$litellm_plain" -eq 1 ] \
           && [ "$pdfs_plain" -eq 1 ] && [ "$jarvis_enc" -eq 0 ] \
           && [ "$litellm_enc" -eq 0 ] && [ "$pdfs_enc" -eq 0 ] \
           && [ "$secrets_enc" -eq 0 ]; then
          coherent_current="true"
        elif [ "$jarvis_enc" -eq 1 ] && [ "$litellm_enc" -eq 1 ] \
             && [ "$pdfs_enc" -eq 1 ] && [ "$secrets_enc" -eq 1 ] \
             && [ "$jarvis_plain" -eq 0 ] && [ "$litellm_plain" -eq 0 ] \
             && [ "$pdfs_plain" -eq 0 ] && [ "$secrets_plain" -eq 0 ]; then
          coherent_current="true"
        fi
      fi
      [ "$coherent_current" = "true" ] && complete="true"

      # A missing PDF role is eligible only when the supplied key authenticates
      # the strict pre-v1.2 manifest and its exact archive inventory.
      if [ "$has_pdfs" = "false" ] && [ "$has_key" = "true" ] \
         && [ -f "$manifest" ] && [ ! -L "$manifest" ] \
         && [ -f "$signature" ] && [ ! -L "$signature" ]; then
        inventory="$(mktemp "${TRIGGER_DIR}/.inbox-inventory-${t}.XXXXXX" 2>/dev/null || true)"
        if [ -n "$inventory" ] && (
          ENC_KEYFILE="$OPERATOR_KEYFILE"
          verify_manifest_signature "$manifest"
          parse_authenticated_manifest "$manifest" "$t" "$inventory"
          verify_manifest_inventory "$INBOX_DIR" "$t" "$inventory"
          MANIFEST_AUTHENTICATED=1
          authenticated_manifest_is_legacy "$manifest"
        ); then
          legacy_missing_pdfs="true"
          complete="true"
        fi
        [ -z "$inventory" ] || rm -f -- "$inventory" "${inventory}.tmp"
      fi
      [ "$first" = "1" ] || printf ','
      first=0
      printf '{"timestamp":"%s","complete":%s,"has_pdfs":%s,"legacy_missing_pdfs":%s,"has_secrets":%s,"has_key":%s}' \
        "$t" "$complete" "$has_pdfs" "$legacy_missing_pdfs" "$has_secrets" "$has_key"
    done <<< "$timestamps"
    printf ']'
  } > "$tmp" 2>/dev/null || return 0
  mv -f "$tmp" "$out" 2>/dev/null || return 0
}

# --- qdrant_http_body — an EXTENDED copy of backup.sh:qdrant_http (the image has
#     no curl). Unlike the lifted version it sends a JSON request body + a
#     Content-Type header, which the snapshot `recover` PUT requires — a
#     body-less PUT would 4xx and silently fail. ----------------------------------
qdrant_http_body() {
  QDRANT_URL="$QDRANT_URL" QDRANT_API_KEY="$QDRANT_API_KEY" \
  perl -MHTTP::Tiny -e '
    my ($method, $path, $body) = @ARGV;
    my %h;
    $h{"api-key"} = $ENV{QDRANT_API_KEY} if length $ENV{QDRANT_API_KEY};
    my %opts = ( headers => \%h );
    if (defined $body && length $body) {
      $opts{content} = $body;
      $h{"Content-Type"} = "application/json";
    }
    my $res = HTTP::Tiny->new(timeout => 600)->request(
      $method, $ENV{QDRANT_URL} . $path, \%opts);
    if (!$res->{success}) {
      print STDERR "qdrant " . $res->{status} . " " . ($res->{reason} // "") . "\n";
      exit 1;
    }
  ' "$@"
}

litellm_accepts_http() {
  local host="${LITELLM_RESTORE_HOST:-litellm}"
  local port="${LITELLM_RESTORE_PORT:-4000}"
  local timeout="${LITELLM_CONNECT_TIMEOUT_SECONDS:-1}"
  case "$port" in ''|*[!0-9]*|??????*) return 2 ;; esac
  [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || return 2
  case "$timeout" in ''|0*|*[!0-9]*) return 2 ;; esac
  perl -MIO::Socket::INET -e '
    my ($host, $port, $timeout) = @ARGV;
    my $socket = IO::Socket::INET->new(
      PeerHost => $host,
      PeerPort => $port,
      Proto => "tcp",
      Timeout => $timeout,
    ) or exit(1);
    $socket->autoflush(1);
    print {$socket} "GET / HTTP/1.0\r\nHost: $host\r\nConnection: close\r\n\r\n";
    close $socket;
  ' "$host" "$port" "$timeout"
}

wait_for_litellm_quarantine() {
  local timeout="${LITELLM_PAUSE_TIMEOUT_SECONDS:-60}"
  local poll="${LITELLM_PAUSE_POLL_SECONDS:-1}"
  local deadline failures=0 probe_status
  case "$timeout" in ''|0*|*[!0-9]*) return 1 ;; esac
  [[ "$poll" =~ ^(0\.[0-9]+|[1-9][0-9]*(\.[0-9]+)?)$ ]] || return 1
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -le "$deadline" ]; do
    if litellm_accepts_http; then
      failures=0
    else
      probe_status=$?
      if [ "$probe_status" -eq 1 ]; then
        failures=$((failures + 1))
        if [ "$failures" -ge 2 ]; then
          echo "[restore] LiteLLM is no longer accepting HTTP connections." >&2
          return 0
        fi
      else
        echo "[restore] ERROR: LiteLLM reachability check could not run." >&2
        return 1
      fi
    fi
    sleep "$poll"
  done
  echo "[restore] ERROR: LiteLLM remained reachable after ${timeout}s." >&2
  return 1
}

# psql_admin — run a statement against the `postgres` maintenance DB (so the
# DROP/CREATE of a product DB is never blocked by our own connection to it).
psql_admin() {
  psql -h "$PGHOST" -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 -tAc "$1"
}

# --- rename-swap state file (durable, drives --recover) ----------------------
write_swap_state() {
  # $1 = db, $2 = phase (reload_tmp|swapping_out|swapping_in|verified). Written
  # BEFORE each transition so a crash leaves a durable record of the db + step.
  printf '{"db":"%s","phase":"%s"}' "$(_json_escape "$1")" "$(_json_escape "$2")" \
    > "${SWAP_STATE_FILE}.tmp" 2>/dev/null || return 1
  mv -f "${SWAP_STATE_FILE}.tmp" "$SWAP_STATE_FILE" 2>/dev/null || return 1
}

clear_swap_state() {
  [ ! -L "$SWAP_STATE_FILE" ] || return 1
  rm -f "$SWAP_STATE_FILE" 2>/dev/null || return 1
  [ ! -e "$SWAP_STATE_FILE" ] && [ ! -L "$SWAP_STATE_FILE" ]
}

read_swap_db() {
  # Emit the db name recorded in the swap-state file, or nothing if absent.
  # The swap-state file lives on the rw backup_trigger volume, which an app
  # container can write, so its `db` value is attacker-controllable. It flows into
  # single-quoted psql literals downstream (db_exists / revert_swap), so a raw value
  # like x'; DROP DATABASE ... could inject SQL: allowlist it to the two known DB
  # names here, emitting nothing otherwise (the caller's -z guard then no-ops).
  local db
  [ -r "$SWAP_STATE_FILE" ] || return 0
  db="$(grep -oE '"db"[[:space:]]*:[[:space:]]*"[^"]*"' "$SWAP_STATE_FILE" 2>/dev/null \
    | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/' | head -1 || true)"
  case "$db" in
    "$JARVIS_DB"|"$LITELLM_DB") printf '%s' "$db" ;;
  esac
}

# db_exists <name> — true iff a database with that exact name is in pg_database.
# Uses its own non-ON_ERROR_STOP connection so it never aborts the script.
db_exists() {
  local out
  out="$(psql -h "$PGHOST" -U "$PGUSER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${1}'" 2>/dev/null || true)"
  [ "$out" = "1" ]
}

# verify_db_structural <db> <is_jarvis> — the SOLE post-swap gate. Proves the
# reload populated a real schema (not an empty/partial db) using db-appropriate
# tables: jarvis has OUR schema_migrations + auth tables; litellm is a THIRD-PARTY
# (Prisma) schema with LiteLLM_* tables and NO schema_migrations. Returns non-zero
# on any failure, which drives the revert. NEVER polls the app /health: /health
# aggregates every dependency (incl. a mid-restore litellm) and 503s under
# maintenance, so polling it would revert successful restores.
verify_db_structural() {
  local db="$1" is_jarvis="$2" regs
  if [ "$is_jarvis" = "1" ]; then
    psql -h "$PGHOST" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -tAc \
      "SELECT 1 FROM schema_migrations LIMIT 1;" >/dev/null 2>&1 || return 1
    regs="$(psql -h "$PGHOST" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -tAc \
      "SELECT (to_regclass('public.users') IS NOT NULL AND to_regclass('public.sessions') IS NOT NULL);" \
      2>/dev/null || true)"
    [ "$regs" = "t" ] || return 1
  else
    # litellm keyed on schema_migrations would fail EVERY restore (that table does
    # not exist there — verified against the live db). Assert its table set exists
    # instead; version-robust (any LiteLLM_* table, not a name a litellm upgrade
    # might rename).
    regs="$(psql -h "$PGHOST" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -tAc \
      "SELECT (count(*) > 0) FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'LiteLLM%';" \
      2>/dev/null || true)"
    [ "$regs" = "t" ] || return 1
  fi
  return 0
}

# purge_restored_auth_state <temporary-jarvis-db> — invalidate transient login
# state before the restored database can replace the live one. Older verified
# backups may not have every table, so each deletion is conditional. A single
# explicit transaction makes a failed purge leave the temporary database intact
# for cleanup while the live database is still serving.
purge_restored_auth_state() {
  local db="$1"
  [ "$db" = "${JARVIS_DB}_restore_tmp" ] || return 1
  psql -h "$PGHOST" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -q <<'SQL'
BEGIN;
DO $$
BEGIN
  IF to_regclass('public.sessions') IS NOT NULL THEN
    DELETE FROM public.sessions;
  END IF;
  IF to_regclass('public.magic_link_tokens') IS NOT NULL THEN
    DELETE FROM public.magic_link_tokens;
  END IF;
  IF to_regclass('public.webauthn_challenges') IS NOT NULL THEN
    DELETE FROM public.webauthn_challenges;
  END IF;
  IF to_regclass('public.telegram_pairing_tokens') IS NOT NULL THEN
    DELETE FROM public.telegram_pairing_tokens;
  END IF;
END
$$;
COMMIT;
SQL
}

# Replace the restored vector-visibility checkpoint after every database restore.
# The generation is deliberately non-secret. Retrieval requires the current value,
# so every pre-restore, missing, or unrelated Qdrant point remains invisible until
# the application validates and retags it. The global user_config row exists in
# every supported schema and lets older backups fail closed before migrations run.
rotate_vector_visibility_checkpoint() {
  local step_status="$1" qdrant_recovery generation rotated_at
  case "$step_status" in
    done) qdrant_recovery="succeeded" ;;
    degraded) qdrant_recovery="degraded" ;;
    skipped) qdrant_recovery="skipped" ;;
    *) return 1 ;;
  esac
  generation="$(openssl rand -hex 16 2>/dev/null)" || return 1
  printf '%s' "$generation" | grep -Eq '^[0-9a-f]{32}$' || return 1
  rotated_at="$(date -Iseconds)" || return 1

  psql -h "$PGHOST" -U "$PGUSER" -d "$JARVIS_DB" -v ON_ERROR_STOP=1 -q \
    -v "visibility_generation=${generation}" \
    -v "qdrant_recovery=${qdrant_recovery}" \
    -v "rotated_at=${rotated_at}" <<'SQL'
BEGIN;
INSERT INTO public.user_config(user_id, key, value)
VALUES (
  NULL,
  'vector_visibility.checkpoint',
  jsonb_build_object(
    'version', 1,
    'visibility_generation', :'visibility_generation',
    'status', 'pending',
    'last_chunk_id', 0,
    'qdrant_recovery', :'qdrant_recovery',
    'rotated_at', :'rotated_at'
  )
)
ON CONFLICT (user_id, key) DO UPDATE
SET value = EXCLUDED.value,
    updated_at = now();
COMMIT;
SQL
  VECTOR_VISIBILITY_GENERATION="$generation"
}

# preflight_disk_or_fail — refuse a restore that could exhaust the database volume
# BEFORE creating any tmp db. The swap keeps BOTH live dbs in place and reloads a
# transient <db>_restore_tmp per db, then swaps by rename (catalog-only, no new
# space); the <db>_pre_restore snapshot reuses the live db's existing space. So the
# NEW space a restore consumes is just the two tmp dbs + headroom, and the check is
# free space > both tmp dbs + headroom — the live dbs are already resident, not
# free, so counting them again would demand ~2x the live size and false-FAIL a
# legitimate restore on a >50%-full volume. The tmp estimate is ADDITIVE per db
# (FRESH_DB_FLOOR + gz x CONTENT_FACTOR): a single multiplier is unsafe because a
# tiny db is dominated by the fixed base-db overhead (measured on pg16.8: jarvis
# 18.8x, litellm 266x). It is deliberately conservative — it covers reload WAL
# amplification + FSM/VM/temp overhead, and a cluster-wide ENOSPC mid-reload would
# stall WAL for the LIVE db too, so fail-fast is safer than try-and-see.
preflight_disk_or_fail() {
  local fresh_floor_kb=$((100 * 1024)) content_factor=30 headroom_kb=$((2 * 1024 * 1024))
  local tmp_est_kb req_kb avail_kb jarvis_gz litellm_gz
  jarvis_gz="$(stat -c%s "$JARVIS_ARCHIVE" 2>/dev/null || echo 0)"
  litellm_gz="$(stat -c%s "$LITELLM_ARCHIVE" 2>/dev/null || echo 0)"
  tmp_est_kb=$(( fresh_floor_kb + jarvis_gz * content_factor / 1024 \
              + fresh_floor_kb + litellm_gz * content_factor / 1024 ))
  req_kb=$(( tmp_est_kb + headroom_kb ))
  avail_kb="$(df -Pk "$POSTGRES_DATA_DIR" 2>/dev/null | awk 'NR==2{print $4}')"
  # A non-numeric result means df could not read the volume — almost always the
  # read-only postgres_data mount is missing. Fail with a diagnosable message
  # rather than a misleading "0 GB free" (fail-closed is correct for a destructive
  # op; an ENOSPC mid-reload is non-destructive but stalls the live DB's WAL).
  case "$avail_kb" in
    ''|*[!0-9]*)
      fail_before_destruction "cannot read free space on the database volume ${POSTGRES_DATA_DIR} (is the postgres_data read-only mount present?); refusing the restore" ;;
  esac
  if [ "$avail_kb" -le "$req_kb" ]; then
    fail_before_destruction "insufficient disk for a safe restore: need ~$(( req_kb / 1024 / 1024 )) GB free on the database volume, have ~$(( avail_kb / 1024 / 1024 )) GB"
  fi
}

# revert_swap <db> — roll production back to the untouched <db>_pre_restore after a
# post-swap verify failure. The renamed-out snapshot inherited ALLOW_CONNECTIONS
# false from the disallow step (proven on pg16.8), so it MUST be re-enabled BEFORE
# the rename-back or the restored-to-original db stays non-servable. Best-effort
# throughout (the caller already holds maintenance via DROP_STARTED=1).
revert_swap() {
  local db="$1"
  local tmp="${db}_restore_tmp" pre="${db}_pre_restore"
  if db_exists "$db"; then
    psql_admin "ALTER DATABASE \"${db}\" WITH ALLOW_CONNECTIONS false;" >/dev/null 2>&1 || true
    psql_admin "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${db}' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true
    psql_admin "ALTER DATABASE \"${db}\" RENAME TO \"${tmp}\";" >/dev/null 2>&1 || true
  fi
  psql_admin "ALTER DATABASE \"${pre}\" WITH ALLOW_CONNECTIONS true;" >/dev/null 2>&1 || true
  psql_admin "ALTER DATABASE \"${pre}\" RENAME TO \"${db}\";" >/dev/null 2>&1 || true
  if db_exists "$tmp"; then psql_admin "DROP DATABASE \"${tmp}\";" >/dev/null 2>&1 || true; fi
}

# reconcile_leftover <db> — idempotent crash-recovery for one db, keyed off the
# (<db>, <db>_restore_tmp, <db>_pre_restore) existence triple (the catalog is the
# ground truth; the recorded phase can lag reality). Brings any stranded mid-swap
# state to untouched-original OR completed-restore — never a reachable half-swap.
# Called at the top of restore_one_db_swap (its own db) and as the whole body of
# --recover (the recorded db). It does NOT touch DROP_STARTED/.destructive: the
# maintenance-hold decision is owned by the destructive window (main flow) or the
# durable .destructive sentinel read at --recover startup.
reconcile_leftover() {
  local db="$1"
  # Defensive allowlist: every in-tree caller passes a trusted constant
  # (JARVIS_DB/LITELLM_DB), but this guards the single-quoted SQL sinks below
  # against any future caller — or the --recover path — passing an unvalidated name
  # from the attacker-writable swap-state file into a DROP/RENAME.
  case "$db" in "$JARVIS_DB"|"$LITELLM_DB") ;; *) return 1 ;; esac
  local tmp="${db}_restore_tmp" pre="${db}_pre_restore" is_jarvis=0
  [ "$db" = "$JARVIS_DB" ] && is_jarvis=1
  if ! db_exists "$pre"; then
    # No rollback snapshot -> nothing was renamed for this db; the ORIGINAL <db> is
    # the live data. A stale tmp can exist (aborted reload, or the disallow-before-
    # rename window). Re-enable connections on <db> (no-op if already allowed, but
    # heals the disallow-before-rename crash) and drop the tmp.
    if db_exists "$db"; then psql_admin "ALTER DATABASE \"${db}\" WITH ALLOW_CONNECTIONS true;" >/dev/null 2>&1 || true; fi
    if db_exists "$tmp"; then psql_admin "DROP DATABASE \"${tmp}\";" >/dev/null 2>&1 || true; fi
    clear_swap_state
    return 0
  fi
  # pre_restore EXISTS -> a swap was mid-flight. If <db> is absent but the (already
  # verified) tmp is still there, complete forward by renaming it in.
  if ! db_exists "$db" && db_exists "$tmp"; then
    psql_admin "ALTER DATABASE \"${tmp}\" RENAME TO \"${db}\";" >/dev/null || return 1
  fi
  if db_exists "$db" && verify_db_structural "$db" "$is_jarvis"; then
    psql_admin "DROP DATABASE \"${pre}\";" >/dev/null 2>&1 || true
    if db_exists "$tmp"; then psql_admin "DROP DATABASE \"${tmp}\";" >/dev/null 2>&1 || true; fi
    clear_swap_state
    return 0
  fi
  # <db> missing with no tmp to complete, OR the post-swap verify failed -> REVERT
  # to the untouched pre_restore. The half-swap is resolved either way; clear state.
  revert_swap "$db"
  clear_swap_state
  return 1
}

# restore_one_db_swap <db> <archive> <is_jarvis> — reload the plain-SQL dump into a
# fresh <db>_restore_tmp while the OLD <db> stays LIVE, structurally verify the tmp,
# then atomically swap by rename (disallow -> terminate -> rename-out -> rename-in),
# gate the swapped-in db on a post-swap structural verify, and drop the rollback
# snapshot. The ONLY destructive window is disallow->terminate->rename; a failure
# anywhere before the first rename (bad archive, ENOSPC, timeout, tmp-verify fail)
# leaves production untouched (DROP_STARTED stays 0 -> the EXIT trap lifts the 503).
restore_one_db_swap() {
  local db="$1" archive="$2" is_jarvis="$3"
  local tmp="${db}_restore_tmp" pre="${db}_pre_restore" st
  reconcile_leftover "$db" || return 1
  # (1) reload into a fresh tmp db — writers cannot reach it (unknown name), so the
  #     reload minutes are harmless while OLD <db> keeps serving.
  psql_admin "CREATE DATABASE \"${tmp}\";" >/dev/null || return 1
  write_swap_state "$db" "reload_tmp"
  set +e
  decrypt_or_passthrough "$archive" | gunzip | psql -h "$PGHOST" -U "$PGUSER" -d "$tmp" -v ON_ERROR_STOP=1 -q >/dev/null
  st=("${PIPESTATUS[@]}")
  set -e
  { [ "${st[0]}" -eq 0 ] && [ "${st[1]}" -eq 0 ] && [ "${st[2]}" -eq 0 ]; } || return 1
  # (2) structural verify on the tmp BEFORE any destruction.
  verify_db_structural "$tmp" "$is_jarvis" || return 1
  if [ "$is_jarvis" = "1" ]; then
    purge_restored_auth_state "$tmp" || return 1
  fi
  # ---- destructive window opens HERE (not before): disallow -> terminate -> rename.
  #      Write the DURABLE maintenance marker FIRST and fail-closed: it is the hold
  #      that keeps the stack 503 across a crash mid-swap (.maintenance soft-expires
  #      when the heartbeat dies; .destructive never does). If it cannot be written
  #      we abort before touching anything — the disallow is still below, so nothing
  #      destructive has run and fail_before_destruction lifts maintenance cleanly.
  #      Only after the durable hold is in place do we mark DROP_STARTED (the FE
  #      reads drop_started from the status JSON) and open the window.
  touch "$MAINTENANCE_DESTRUCTIVE" 2>/dev/null \
    || fail_before_destruction "cannot write the durable maintenance marker; refusing to start the destructive swap"
  DROP_STARTED=1
  write_status
  write_swap_state "$db" "swapping_out"
  psql_admin "ALTER DATABASE \"${db}\" WITH ALLOW_CONNECTIONS false;" >/dev/null || return 1
  psql_admin "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${db}' AND pid <> pg_backend_pid();" >/dev/null || return 1
  psql_admin "ALTER DATABASE \"${db}\" RENAME TO \"${pre}\";" >/dev/null || return 1
  write_swap_state "$db" "swapping_in"
  psql_admin "ALTER DATABASE \"${tmp}\" RENAME TO \"${db}\";" >/dev/null || return 1
  write_swap_state "$db" "verified"
  # (3) post-swap structural verify — THE SOLE GATE. On pass, drop the rollback
  #     snapshot; on fail, REVERT to the untouched pre_restore and hold maintenance.
  if verify_db_structural "$db" "$is_jarvis"; then
    psql_admin "DROP DATABASE \"${pre}\";" >/dev/null || return 1
    clear_swap_state
    return 0
  fi
  revert_swap "$db"
  return 1
}

# Terminal failure BEFORE any destruction: record + exit 0 (nothing dropped).
fail_before_destruction() {
  STATE="failed"
  ERROR="$1"
  FINISHED_AT="$(date -Iseconds)"
  exit 0
}

# Terminal failure in STEP 5. If the destructive window was entered
# (DROP_STARTED=1) the DB is inconsistent and the EXIT trap keeps maintenance ON;
# if it failed before that (e.g. the very first ALTER) the DB is untouched and the
# trap clears maintenance — so word the message accordingly.
step5_fail() {
  STATE="failed"
  if [ "$DROP_STARTED" = "1" ]; then
    if [ "$SOURCE" = "inbox" ]; then
      ERROR="off-host restore failed mid-reload on a fresh host — re-run the off-host recovery per the runbook"
    else
      ERROR="database inconsistent — restore from safety backup ${SAFETY_BACKUP_TS:-<unknown>}"
    fi
  else
    ERROR="restore could not start; the database was not modified"
  fi
  FINISHED_AT="$(date -Iseconds)"
  exit 0
}

# Terminal failure after the databases were restored. Record it (exit 0) and
# let the EXIT trap hold maintenance so the stack stays 503 until the operator
# retries from the recorded safety backup.
fail_after_restore() {
  STATE="failed"
  ERROR="$1"
  FINISHED_AT="$(date -Iseconds)"
  exit 0
}

# purge_secrets_staging — shred + remove the cross-host secrets staging (a full
# plaintext secret bundle on the rw inbox). shred the files first: a bare rm
# leaves block-level residue on journaling/overlay filesystems. Idempotent and
# || true so it can never abort the EXIT trap.
purge_secrets_staging() {
  [ -d "$SECRETS_STAGING" ] || return 0
  find "$SECRETS_STAGING" -type f -exec shred -u {} + 2>/dev/null || true
  rm -rf "$SECRETS_STAGING" 2>/dev/null || true
}

write_lifecycle_file() {
  local path="$1" value="$2" mode="${3:-644}" tmp
  tmp="$(mktemp "${path}.XXXXXX")" || return 1
  if ! printf '%s\n' "$value" > "$tmp" || ! chmod "$mode" "$tmp" || ! mv -f "$tmp" "$path"; then
    rm -f "$tmp"
    return 1
  fi
}

read_lifecycle_state() {
  [ -f "$LIFECYCLE_OPERATION_STATE" ] \
    && [ ! -L "$LIFECYCLE_OPERATION_STATE" ] || return 1
  cat "$LIFECYCLE_OPERATION_STATE" 2>/dev/null
}

# A restore request outranks only an update that is still preparing its safety
# backup. Ask that holder to yield, then return without consuming the request.
# A promoted update is already mutating and never yields; the request waits.
request_preparing_update_yield() {
  local owner="$1" id control=""
  case "$owner" in
    update-preparing:*) id="${owner#update-preparing:}" ;;
    *) return 1 ;;
  esac
  printf '%s' "$id" | grep -Eq '^[0-9a-f]{32}$' || return 1
  [ -f "$UPDATE_GUARD" ] && [ ! -L "$UPDATE_GUARD" ] \
    && [ -f "$UPDATE_RESERVATION" ] && [ ! -L "$UPDATE_RESERVATION" ] \
    && [ "$(cat "$UPDATE_GUARD" 2>/dev/null || true)" = "$id" ] \
    && [ "$(cat "$UPDATE_RESERVATION" 2>/dev/null || true)" = "$id" ] \
    || return 1
  [ ! -L "$UPDATE_CONTROL" ] || return 1
  if [ -e "$UPDATE_CONTROL" ]; then
    [ -f "$UPDATE_CONTROL" ] \
      && [ "$(wc -l < "$UPDATE_CONTROL" 2>/dev/null || echo 0)" -eq 1 ] 2>/dev/null \
      || return 1
    control="$(cat "$UPDATE_CONTROL" 2>/dev/null || true)"
    case "$control" in
      "${id}:yield-restore") return 0 ;;
      "${id}:promote"|"${id}:release"|\
      "${id}:release:clear"|"${id}:release:retain") return 1 ;;
      *) return 1 ;;
    esac
  fi
  write_lifecycle_file "$UPDATE_CONTROL" "${id}:yield-restore" 600
}

claim_restore_lifecycle_operation() {
  local owner=""
  [ ! -L "$LOCK_DIR" ] || return 2
  mkdir -p "$LOCK_DIR" 2>/dev/null || return 2
  [ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] || return 2
  chmod 700 "$LOCK_DIR" 2>/dev/null || return 2
  [ ! -L "$LIFECYCLE_OPERATION_LOCK" ] \
    && [ ! -L "$LIFECYCLE_OPERATION_STATE" ] \
    && [ ! -L "$LIFECYCLE_ADMISSION_LOCK" ] || return 2
  exec 4>>"$LIFECYCLE_ADMISSION_LOCK" || return 2
  flock -n 4 || { exec 4>&-; return 3; }
  if [ ! -e "$LIFECYCLE_OPERATION_LOCK" ]; then
    (set -C; umask 077; : > "$LIFECYCLE_OPERATION_LOCK") 2>/dev/null || true
  fi
  [ -f "$LIFECYCLE_OPERATION_LOCK" ] \
    && [ ! -L "$LIFECYCLE_OPERATION_LOCK" ] || return 2
  chmod 600 "$LIFECYCLE_OPERATION_LOCK" 2>/dev/null \
    || { exec 4>&-; return 2; }
  exec 5<>"$LIFECYCLE_OPERATION_LOCK" || { exec 4>&-; return 2; }
  if ! flock -n 5; then
    exec 5>&-
    owner="$(read_lifecycle_state 2>/dev/null || true)"
    if request_preparing_update_yield "$owner"; then
      echo "[restore] restore request retained; asking the preparing update to yield" >&2
    else
      echo "[restore] restore request retained; another lifecycle operation is active" >&2
    fi
    flock -u 4 2>/dev/null || true
    exec 4>&-
    return 3
  fi
  owner="$(read_lifecycle_state 2>/dev/null || true)"
  if [ -n "$owner" ] && [ "$owner" != restore ]; then
    exec 5>&-
    exec 4>&-
    echo "[restore] restore request retained; foreign lifecycle recovery state is present" >&2
    return 4
  fi
  if [ -z "$owner" ]; then
    # Legacy/partial durable state predating the shared marker still blocks a
    # foreign restore. Restore-owned destructive/swap state is adoptable.
    if [ -e "$HOST_RESERVATION" ] || [ -L "$HOST_RESERVATION" ] \
        || [ -e "$ROTATION_SENTINEL" ] || [ -L "$ROTATION_SENTINEL" ] \
        || [ -e "$ROTATION_RESERVATION" ] || [ -L "$ROTATION_RESERVATION" ] \
        || [ -e "$UPDATE_GUARD" ] || [ -L "$UPDATE_GUARD" ] \
        || [ -e "$UPDATE_RESERVATION" ] || [ -L "$UPDATE_RESERVATION" ]; then
      exec 5>&-
      exec 4>&-
      echo "[restore] restore request retained; foreign lifecycle recovery state is present" >&2
      return 4
    fi
    write_lifecycle_file "$LIFECYCLE_OPERATION_STATE" restore 600 \
      || { exec 5>&-; exec 4>&-; return 2; }
  fi
  flock -u 4 2>/dev/null || true
  exec 4>&-
  return 0
}

finish_restore_lifecycle_operation() {
  if [ -f "$LIFECYCLE_OPERATION_STATE" ] \
      && [ ! -L "$LIFECYCLE_OPERATION_STATE" ] \
      && [ "$(cat "$LIFECYCLE_OPERATION_STATE" 2>/dev/null || true)" = restore ]; then
    rm -f "$LIFECYCLE_OPERATION_STATE" 2>/dev/null || return 1
  fi
  return 0
}

# --- EXIT trap: single terminal-status writer + maintenance lift gate ---------
_cleanup() {
  set +e
  # --inbox-manifest is a read-only inventory pass: it must NOT consume the restore
  # request, write .restore_status.json, shred the operator key, or touch maintenance.
  # Short-circuit before any of that (it wrote its own manifest and is exiting 0).
  [ "$MANIFEST_MODE" = "1" ] && exit 0
  [ "$ADMISSION_REFUSED" = "1" ] && exit 0
  [ -n "$HEARTBEAT_PID" ] && kill "$HEARTBEAT_PID" 2>/dev/null
  # Off-host DR hygiene: shred the one-time operator key + the plaintext secrets
  # staging on every clean or recorded-failure exit, so a failed restore never
  # leaves them on the rw restore_inbox volume. A SIGKILL cannot run this trap, so
  # it is not absolute. The guard fires on the inbox source OR whenever the key /
  # staging actually exist — so a malformed `source` field that defaults to "local"
  # still shreds an operator key the operator dropped. Harmless for a true local
  # restore (those paths never exist). Idempotent + || true so it never aborts.
  if [ "$SOURCE" = "inbox" ] || [ -e "$OPERATOR_KEYFILE" ] || [ -d "$SECRETS_STAGING" ]; then
    if command -v shred >/dev/null 2>&1; then
      shred -u "$OPERATOR_KEYFILE" 2>/dev/null || rm -f "$OPERATOR_KEYFILE" 2>/dev/null || true
    else
      rm -f "$OPERATOR_KEYFILE" 2>/dev/null || true
    fi
    purge_secrets_staging
  fi
  case "$PRIVATE_INPUT_DIR" in
    /tmp/jarvis-restore-input.*) rm -rf -- "$PRIVATE_INPUT_DIR" 2>/dev/null || true ;;
  esac
  # A staged PDF set is disposable until its swap journal exists. Preserve it
  # only when crash recovery needs it to finish an in-progress file swap.
  if [ "$PDFS_STAGED" = "1" ] && valid_pdf_restore_run_id "$PDF_RESTORE_RUN_ID"; then
    if ! read_pdf_swap_state >/dev/null 2>&1; then
      remove_staged_pdf_restore "$PDF_RESTORE_RUN_ID" 2>/dev/null || true
    fi
  fi
  if [ "$STATE" = "running" ]; then
    STATE="failed"
    if [ -z "$ERROR" ]; then
      if [ -f "$RESTORE_TIMEOUT_FILE" ]; then
        if [ "$DROP_STARTED" = "1" ]; then
          ERROR="restore exceeded its time limit and was abandoned; the database may be inconsistent — restore from the safety backup ${SAFETY_BACKUP_TS:-<unknown>}"
        else
          ERROR="restore exceeded its time limit and was abandoned; nothing was destroyed"
        fi
      elif [ "$DROP_STARTED" = "1" ]; then
        if [ "$SOURCE" = "inbox" ]; then
          ERROR="off-host restore failed mid-reload on a fresh host — re-run the off-host recovery per the runbook"
        else
          ERROR="database inconsistent — restore from safety backup ${SAFETY_BACKUP_TS:-<unknown>}"
        fi
      else
        ERROR="restore terminated unexpectedly"
      fi
    fi
  fi
  if [ -n "$SAFETY_STAGING_DIR" ]; then
    if [ "$STATE" = "failed" ] && [ "$DROP_STARTED" = "1" ]; then
      ERROR="${ERROR}; Recovery copy: safety backup ${SAFETY_BACKUP_TS:-<unknown>} staged at ${SAFETY_STAGING_DIR}"
      echo "[restore] Recovery copy preserved: ${SAFETY_STAGING_DIR}" >&2
    else
      remove_safety_staging \
        || echo "[restore] WARNING: could not remove completed safety staging directory ${SAFETY_STAGING_DIR}" >&2
    fi
  fi
  # Any terminal FAILURE that entered the destructive window (DROP_STARTED=1) leaves the
  # restored state incomplete, so report manual_steps_required honestly rather than a
  # bare error. Covers every post-DROP path (step5_fail, fail_after_restore,
  # timeout, abnormal death). A clean restore is STATE="done" here, so it stays false.
  if [ "$STATE" = "failed" ] && [ "$DROP_STARTED" = "1" ]; then MANUAL_STEPS_REQUIRED=1; fi
  [ -n "$FINISHED_AT" ] || FINISHED_AT="$(date -Iseconds)"
  write_status
  # Lift maintenance on ANY clean restore (same-host OR off-host) OR any failure
  # BEFORE the first DROP (DROP_STARTED=0 => nothing was destroyed => safe to serve,
  # true on a fresh host too). A clean restore is safe to lift after its data keys
  # and exact PDF set are installed; affected services observe the rotation marker
  # and restart with the restored keys.
  # A failure AFTER the first DROP still holds the durable .destructive sentinel (the
  # DB is inconsistent; it does NOT auto-expire — the MAINTENANCE_MAX_AGE_S soft-expiry
  # covers .maintenance only — so the operator MUST clear it explicitly). This can't
  # race a concurrent restore: the restore-request POST is itself 503'd while the
  # sentinel is up, so a post-DROP hold is never lifted by a later run.
  if [ "$RESTORE_CLEAN" = "1" ] || [ "$DROP_STARTED" = "0" ]; then
    rm -f "$MAINTENANCE_SENTINEL" 2>/dev/null
    rm -f "$MAINTENANCE_DESTRUCTIVE" 2>/dev/null
    finish_restore_lifecycle_operation \
      || echo "[restore] WARNING: could not clear completed lifecycle state" >&2
  fi
  rm -f "$RESTORE_TIMEOUT_FILE" 2>/dev/null || true
  # Never crash-restart the sidecar: a recorded terminal failure exits 0.
  exit 0
}
# The script's tests source it to exercise the helpers above directly. Everything below
# this line is trap installation and the restore flow itself; everything above it is
# configuration assignments and function definitions, so sourcing has no side effects.
if [ "${1:-}" = "--functions-only" ]; then
  # shellcheck disable=SC2317  # `return` succeeds when sourced and fails when
  # executed; the `exit` is the executed-path fallback, not dead code.
  return 0 2>/dev/null || exit 0
fi

trap _cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

if [ "${1:-}" != "--inbox-manifest" ]; then
  if ! claim_restore_lifecycle_operation; then
    ADMISSION_REFUSED=1
    exit 0
  fi
fi

# === RECOVERY MODE (--recover): reconcile a stranded mid-swap state, then exit ==
# The sidecar entrypoint invokes this on startup when private swap state is
# present (a crash mid-swap). It runs ONLY the leftover-handler for the recorded db
# — it never consumes a .restore_request.json or runs the full restore flow. A
# durable .destructive sentinel means the crash happened AFTER the destructive
# window opened, so maintenance is held (DROP_STARTED=1) even when the reconcile
# completes the swap: the rest of the restore (Qdrant, the other db) did not run,
# so the operator must re-trigger or clear the sentinels. When nothing was ever
# destroyed (.destructive absent, e.g. a crash during the first reload) the
# reconcile only drops a stale tmp and the EXIT trap lifts the 503.
if [ "${1:-}" = "--recover" ]; then
  CURRENT_STEP="Recovering an interrupted restore"
  PHASE="recover"
  if [ -f "$MAINTENANCE_DESTRUCTIVE" ]; then DROP_STARTED=1; fi
  if [ -d "$(data_key_transaction_dir)" ]; then
    if recover_restored_data_keys; then
      STATE="failed"
      ERROR="completed an interrupted data-key installation; maintenance remains active — retry the restore to finish the PDF and search-index phases"
    else
      STATE="failed"
      ERROR="could not recover the interrupted data-key installation; maintenance remains active and both complete key sets were preserved"
    fi
    FINISHED_AT="$(date -Iseconds)"
    exit 0
  fi
  if PDF_RECOVERY_STATE="$(read_pdf_swap_state 2>/dev/null)"; then
    PDF_RESTORE_RUN_ID="${PDF_RECOVERY_STATE%%$'\t'*}"
    PDFS_STAGED=1
    if recover_pdf_swap; then
      RESTORE_CLEAN=1
      STATE="done"
    else
      STATE="failed"
      ERROR="could not finish the interrupted PDF restore; maintenance remains active and the recovery state was preserved"
    fi
    FINISHED_AT="$(date -Iseconds)"
    exit 0
  fi
  RECOVER_DB="$(read_swap_db)"
  if [ -z "$RECOVER_DB" ]; then
    STATE="failed"
    ERROR="restore recovery state is malformed or names an unsupported resource; it was preserved for inspection"
    FINISHED_AT="$(date -Iseconds)"
    exit 0
  fi
  if [ ! -r /run/secrets/postgres_password ]; then
    STATE="failed"
    ERROR="recovery: cannot read the postgres password secret"
    FINISHED_AT="$(date -Iseconds)"
    exit 0
  fi
  PGPASSWORD="$(cat /run/secrets/postgres_password)"
  export PGPASSWORD
  if reconcile_leftover "$RECOVER_DB"; then
    STATE="done"
  else
    STATE="failed"
    ERROR="could not finish the interrupted restore of ${RECOVER_DB}; the database is consistent (restored or original) but the stack stays in maintenance — re-run the restore or clear the maintenance sentinels per the runbook"
  fi
  FINISHED_AT="$(date -Iseconds)"
  exit 0
fi

# === INVENTORY MODE (--inbox-manifest): refresh the sanitized inbox listing, exit ==
# The sidecar loop invokes this every iteration so the admin app's GET /inbox reflects
# whatever the operator has dropped into the rw restore_inbox. It is READ-ONLY: it
# writes only ${TRIGGER_DIR}/.inbox_manifest.json (names/booleans), never consumes the
# restore request, never touches the DB, and MANIFEST_MODE short-circuits the EXIT trap
# so it cannot shred the operator key or write a restore status. It must never abort the
# loop, so it exits 0 unconditionally.
if [ "${1:-}" = "--inbox-manifest" ]; then
  MANIFEST_MODE=1
  write_inbox_manifest
  exit 0
fi

# === STEP 1: consume the request FIRST (at-most-once) + validate =============
if ! REQ_CONTENT="$(consume_restore_request)"; then
  fail_before_destruction "restore request could not be consumed; nothing was changed"
fi
rm -f "$RESTORE_TIMEOUT_FILE" 2>/dev/null || true

TIMESTAMP="$(printf '%s' "$REQ_CONTENT" \
  | grep -oE '"timestamp"[[:space:]]*:[[:space:]]*"[0-9]{8}_[0-9]{6}"' \
  | grep -oE '[0-9]{8}_[0-9]{6}' | head -1 || true)"

# source defaults to "local" when the field is absent (same-host WebUI restore);
# a present-but-unsupported value must fail safe, so distinguish "absent" (-> local)
# from "present and wrong" (-> fail) rather than silently defaulting the latter.
SOURCE_RAW="$(printf '%s' "$REQ_CONTENT" \
  | grep -oE '"source"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | sed -E 's/.*"source"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' | head -1 || true)"
SOURCE="${SOURCE_RAW:-local}"

parse_restore_identity_request "$REQ_CONTENT" \
  || fail_before_destruction "restore request is missing a valid restore_id or requested_at value"

# Historical backups predate PDF archiving. Parse the acknowledgement strictly:
# absent and false are safe defaults; malformed or duplicate fields are refused.
parse_allow_missing_pdfs_request "$REQ_CONTENT" \
  || fail_before_destruction "restore request has an invalid allow_missing_pdfs value"

# Backups written while the database was unreachable could record no usable schema
# version. Restoring one skips the compatibility check entirely, so it needs the same
# strict, explicit acknowledgement as the missing-PDF path.
parse_allow_unknown_schema_request "$REQ_CONTENT" \
  || fail_before_destruction "restore request has an invalid allow_unknown_schema value"

if outbound_quarantine_exists; then
  fail_before_destruction "restore is blocked until the current outbound credential review is acknowledged"
fi

write_status

if ! printf '%s' "$TIMESTAMP" | grep -Eq '^[0-9]{8}_[0-9]{6}$'; then
  fail_before_destruction "restore request did not name a valid backup timestamp"
fi

# Resolve the source BEFORE any archive lookup or destruction. local reads the
# /backups archive set with the same-host backup_encrypt_key (unchanged). inbox
# reads the operator-supplied archive set + one-time key from the rw restore_inbox
# volume; the operator key is validated here (pre-destruction) so a missing key
# fails safe, and the STEP-2 decrypt probe then proves it actually decrypts the DB
# archives before any DROP.
case "$SOURCE" in
  local) ;;
  inbox)
    ARCHIVE_DIR="$INBOX_DIR"
    ENC_KEYFILE="$OPERATOR_KEYFILE"
    if [ ! -s "$OPERATOR_KEYFILE" ]; then
      fail_before_destruction "off-host restore: operator key ${OPERATOR_KEYFILE} is missing or empty; drop the one-time backup key into the restore_inbox before requesting an inbox restore"
    fi
    ;;
  *)
    fail_before_destruction "restore request named an unsupported source '${SOURCE}' (expected local or inbox); nothing was changed"
    ;;
esac

JARVIS_ARCHIVE=""
LITELLM_ARCHIVE=""
PDFS_ARCHIVE=""
QDRANT_SNAPS=()
shopt -s nullglob
for f in "${ARCHIVE_DIR}"/*_"${TIMESTAMP}".*; do
  base="$(basename "$f")"
  valid_archive_name "$base" || continue
  case "$base" in
    jarvis_*) JARVIS_ARCHIVE="$f" ;;
    litellm_*) LITELLM_ARCHIVE="$f" ;;
    pdfs_*) PDFS_ARCHIVE="$f" ;;
    qdrant_*) QDRANT_SNAPS+=("$f") ;;
  esac
done
shopt -u nullglob

if [ -z "$JARVIS_ARCHIVE" ] || [ -z "$LITELLM_ARCHIVE" ]; then
  fail_before_destruction "backup ${TIMESTAMP} is incomplete (missing a required database archive)"
fi

# --- PGPASSWORD (read AFTER consuming the request so a missing secret records a
#     terminal failure instead of crash-looping on the un-consumed sentinel). ----
if [ ! -r /run/secrets/postgres_password ]; then
  fail_before_destruction "cannot read the postgres password secret; restore aborted"
fi
PGPASSWORD="$(cat /run/secrets/postgres_password)"
export PGPASSWORD

# === STEP 2: compat gate (defense-in-depth, BEFORE any destruction) ==========
MANIFEST_SOURCE="${ARCHIVE_DIR}/manifest_${TIMESTAMP}.json"
MANIFEST="$MANIFEST_SOURCE"
# Copy manifest metadata into sidecar-private storage before authentication. A
# writer may rename files in BACKUP_DIR/inbox, but cannot change the bytes we
# verify and subsequently parse.
if [ -e "$MANIFEST_SOURCE" ]; then
  [ -f "$MANIFEST_SOURCE" ] && [ ! -L "$MANIFEST_SOURCE" ] \
    || fail_before_destruction "manifest_${TIMESTAMP}.json is not a safe regular file"
  PRIVATE_INPUT_DIR="$(mktemp -d /tmp/jarvis-restore-input.XXXXXX)" \
    || fail_before_destruction "could not create private restore-input staging; nothing was changed"
  chmod 700 "$PRIVATE_INPUT_DIR" \
    || fail_before_destruction "could not secure private restore-input staging; nothing was changed"
  cp --reflink=auto -- "$MANIFEST_SOURCE" "${PRIVATE_INPUT_DIR}/manifest_${TIMESTAMP}.json" \
    || fail_before_destruction "could not stage the restore manifest; nothing was changed"
  if [ -e "${MANIFEST_SOURCE}.hmac" ]; then
    [ -f "${MANIFEST_SOURCE}.hmac" ] && [ ! -L "${MANIFEST_SOURCE}.hmac" ] \
      || fail_before_destruction "manifest signature is not a safe regular file"
    cp --reflink=auto -- "${MANIFEST_SOURCE}.hmac" \
      "${PRIVATE_INPUT_DIR}/manifest_${TIMESTAMP}.json.hmac" \
      || fail_before_destruction "could not stage the manifest signature; nothing was changed"
  fi
  MANIFEST="${PRIVATE_INPUT_DIR}/manifest_${TIMESTAMP}.json"
fi
# Authenticate the manifest FIRST. The sha256 gate below reads its expected digests
# from this file, so verifying the archives against an unverified manifest would prove
# nothing about a set whose archives and digests were rewritten together.
gate_manifest_signature
if [ -r "$MANIFEST" ]; then
  MANIFEST_CONTENT="$(cat "$MANIFEST" 2>/dev/null || true)"
  if [ "$MANIFEST_AUTHENTICATED" = "1" ]; then
    INVENTORY_FILE="${PRIVATE_INPUT_DIR}/.inventory.tsv"
    parse_authenticated_manifest "$MANIFEST" "$TIMESTAMP" "$INVENTORY_FILE" \
      || fail_before_destruction "authenticated manifest identity or archive roles are invalid; nothing was changed"
    if authenticated_manifest_is_legacy "$MANIFEST"; then
      MANIFEST_LEGACY=1
    fi
  fi
  CHECKFILE="$(mktemp)"
  printf '%s' "$MANIFEST_CONTENT" \
    | grep -oE '"filename":"[^"]+","sha256":"[0-9a-f]{64}"' \
    | sed -E 's/"filename":"([^"]+)","sha256":"([0-9a-f]{64})"/\2  \1/' > "$CHECKFILE"
  if [ ! -s "$CHECKFILE" ]; then
    rm -f "$CHECKFILE"
    fail_before_destruction "manifest_${TIMESTAMP}.json is present but corrupt or incomplete (no archive checksums); nothing was changed"
  fi
  set +e
  ( cd "$ARCHIVE_DIR" && sha256sum -c --strict "$CHECKFILE" >/dev/null 2>&1 )
  SHA_RC=$?
  set -e
  if [ "$SHA_RC" -ne 0 ]; then
    rm -f "$CHECKFILE"
    fail_before_destruction "backup integrity check failed (sha256 mismatch); nothing was changed"
  fi
  rm -f "$CHECKFILE"

  MANIFEST_SCHEMA="$(printf '%s' "$MANIFEST_CONTENT" \
    | grep -oE '"schema_version"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
  # A manifest recording no usable schema version (absent, or the 0 older backups
  # wrote when their schema query failed) leaves the comparison below with nothing
  # to check, so restoring it takes an explicit acknowledgement in the request.
  if [ -z "$MANIFEST_SCHEMA" ] || [ "$MANIFEST_SCHEMA" = "0" ]; then
    if [ "$ALLOW_UNKNOWN_SCHEMA" != "1" ]; then
      fail_before_destruction "backup ${TIMESTAMP} does not record a usable database schema version, so compatibility with this deployment cannot be checked; choose a restore point written by a healthy backup, or accept the risk: a command-line restore adds --allow-unknown-schema (jarvis-research restore legacy ${TIMESTAMP} --allow-unknown-schema), a web-app or off-host restore sets \"allow_unknown_schema\": true in the restore request; nothing was changed"
    fi
    echo "[restore] WARNING: restoring backup ${TIMESTAMP} without a schema compatibility check on explicit operator acknowledgement" >&2
  fi
  # Compare the backup schema with the maximum schema supported by the installed
  # code. A partial restore can leave the live database unavailable, while the
  # installed schema support remains readable. If there are no incremental
  # migration files, db/SCHEMA_VERSION supplies the baseline schema number.
  if [ -n "$MANIFEST_SCHEMA" ] && [ "$MANIFEST_SCHEMA" != "0" ]; then
    MIG_DIR="${MIGRATIONS_DIR:-/app/db/migrations}"
    # Read the highest NNN_*.sql migration with a glob rather than parsing `ls`.
    # The 10# prefix treats a leading-zero migration number as base 10. Backups at
    # or below the supported schema may proceed; newer backups are refused.
    CODE_MAX=""
    for _mig in "$MIG_DIR"/*.sql; do
      [ -e "$_mig" ] || continue
      _num="${_mig##*/}"
      _num="${_num%%_*}"
      case "$_num" in ''|*[!0-9]*) continue ;; esac
      _num=$((10#$_num))
      if [ -z "$CODE_MAX" ] || [ "$_num" -gt "$CODE_MAX" ]; then CODE_MAX="$_num"; fi
    done
    if [ -z "$CODE_MAX" ]; then
      CODE_MAX="$(tr -dc '0-9' < "${SCHEMA_VERSION_FILE:-${MIG_DIR%/migrations}/SCHEMA_VERSION}" 2>/dev/null || true)"
      [ -n "$CODE_MAX" ] || CODE_MAX="$(tr -dc '0-9' < /app/db/SCHEMA_VERSION 2>/dev/null || true)"
      [ -n "$CODE_MAX" ] || CODE_MAX=111
    fi
    if [ -n "$CODE_MAX" ] && [ "$MANIFEST_SCHEMA" -gt "$CODE_MAX" ]; then
      fail_before_destruction "backup is newer than this deployment (schema ${MANIFEST_SCHEMA} > code ${CODE_MAX}); upgrade JARVIS before restoring"
    fi
  fi

  if [ "$MANIFEST_AUTHENTICATED" = "1" ]; then
    verify_manifest_inventory "$ARCHIVE_DIR" "$TIMESTAMP" "$INVENTORY_FILE" \
      || fail_before_destruction "authenticated restore inventory has missing, changed, duplicate, or extra archive content; nothing was changed"
    stage_manifest_inventory "$ARCHIVE_DIR" "$TIMESTAMP" "$INVENTORY_FILE" "$PRIVATE_INPUT_DIR" \
      || fail_before_destruction "could not stage the exact authenticated restore inventory; nothing was changed"
    JARVIS_ARCHIVE=""; LITELLM_ARCHIVE=""; PDFS_ARCHIVE=""; QDRANT_SNAPS=()
    while IFS=$'\t' read -r _role _name _sha _size; do
      case "$_role" in
        jarvis) JARVIS_ARCHIVE="${PRIVATE_INPUT_DIR}/${_name}" ;;
        litellm) LITELLM_ARCHIVE="${PRIVATE_INPUT_DIR}/${_name}" ;;
        pdfs) PDFS_ARCHIVE="${PRIVATE_INPUT_DIR}/${_name}" ;;
        qdrant:*) QDRANT_SNAPS+=("${PRIVATE_INPUT_DIR}/${_name}") ;;
      esac
    done < "$INVENTORY_FILE"
    ARCHIVE_DIR="$PRIVATE_INPUT_DIR"
  fi
elif [ "$SOURCE" = "inbox" ]; then
  # Off-host archives are operator-supplied and less trusted, and the admin endpoint
  # canNOT pre-check their compatibility (the inbox listing carries no schema version).
  # So the manifest is REQUIRED for an inbox restore: without it neither the sha256
  # integrity check above nor the newer-than-code gate can arm. Refuse before any
  # destruction rather than reload an unverified / newer archive set.
  fail_before_destruction "off-host restore requires manifest_${TIMESTAMP}.json (copy the full backup set, including its manifest, into the restore_inbox); nothing was changed"
else
  echo "[restore] WARN: manifest ${MANIFEST} absent; proceeding (local restore — the admin endpoint pre-checks compatibility)" >&2
fi

# Pre-destruction decrypt probe: verify each DB archive decrypts to a valid gzip
# stream (catches a wrong/rotated BACKUP_ENCRYPT_KEYFILE or a corrupt archive
# BEFORE any DROP — a bad key found mid-reload would leave the DB dropped+empty).
# Only the first bytes are read (head closes the pipe early; guard pipefail).
for arch in "$JARVIS_ARCHIVE" "$LITELLM_ARCHIVE"; do
  set +e
  magic="$(decrypt_or_passthrough "$arch" 2>/dev/null | head -c 2 | od -An -tx1 | tr -d ' \n')"
  set -e
  if [ "$magic" != "1f8b" ]; then
    # An encrypted archive with no key on this host is a missing-key failure, not a
    # wrong-key/corrupt one — name the cause so a keyless install restoring an older
    # encrypted set is not sent chasing a key rotation or corruption it does not have.
    case "$arch" in
      *.enc)
        if [ -z "$ENC_KEYFILE" ] || [ ! -s "$ENC_KEYFILE" ]; then
          fail_before_destruction "backup archive $(basename "$arch") is encrypted, but this host has no usable backup encryption key (BACKUP_ENCRYPT_KEYFILE) to read it; the set was written with a key that is absent here, so restore that key file or re-run setup to provision the backup key; nothing was changed"
        fi
        ;;
    esac
    fail_before_destruction "backup archive $(basename "$arch") is unreadable (wrong encryption key or corrupt); nothing was changed"
  fi
done

# === STEP 2.5: restored-data-key preflight (BEFORE any destruction) ===========
# Decrypt and fully validate the three keys coupled to database content now. A
# wrong key, malformed archive, unsafe member, or missing data key must be found
# while the live databases and target-host credentials are still untouched.
SECRETS_ARCHIVE="$(resolve_secrets_archive || true)"
if [ -n "$SECRETS_ARCHIVE" ]; then
  DATA_KEYS_EXACT=0
  if [ "$MANIFEST_AUTHENTICATED" = "1" ] \
     && grep -qE '"run_id"[[:space:]]*:[[:space:]]*"[0-9a-f]{32}"' "$MANIFEST"; then
    DATA_KEYS_EXACT=1
  fi
  if ! stage_restored_data_keys "$SECRETS_ARCHIVE" "$DATA_KEYS_EXACT"; then
    fail_before_destruction "the restored-data-key archive is incomplete, malformed, or unsafe; nothing was changed"
  fi
  DATA_KEYS_STAGED=1
elif [ "$SOURCE" = "inbox" ]; then
  fail_before_destruction "off-host restore requires secrets_${TIMESTAMP}.tar.gz[.enc] with the restored data keys; nothing was changed"
fi

# Stage and validate the PDF object set before taking maintenance. Signed
# pre-v1.2 backups may omit it, but only an explicit caller acknowledgement may
# replace the live numeric PDF set with empty in that compatibility case.
PDF_RESTORE_RUN_ID="$(openssl rand -hex 16 2>/dev/null || true)"
valid_pdf_restore_run_id "$PDF_RESTORE_RUN_ID" \
  || fail_before_destruction "could not allocate a PDF restore transaction ID; nothing was changed"
PDFS_ARCHIVE="$(resolve_pdfs_archive || true)"
if [ -n "$PDFS_ARCHIVE" ]; then
  stage_restored_pdfs "$PDFS_ARCHIVE" "$PDF_RESTORE_RUN_ID" \
    || fail_before_destruction "the PDF archive is malformed, unsafe, too large, or cannot be staged; nothing was changed"
elif missing_pdf_restore_is_authorized; then
  stage_empty_pdf_set "$PDF_RESTORE_RUN_ID" \
    || fail_before_destruction "could not stage the acknowledged empty PDF set; nothing was changed"
elif [ "$MANIFEST_LEGACY" = "1" ]; then
  fail_before_destruction "this historical backup has no PDF archive. Confirm the legacy data-loss warning before restoring; nothing was changed"
else
  fail_before_destruction "backup ${TIMESTAMP} is incomplete (missing the required PDF archive); nothing was changed"
fi
PDFS_STAGED=1

# === STEP 3: maintenance ON + heartbeat ======================================
# Turn the stack to 503 for the whole restore, and re-touch the sentinel every
# 60s so a >30-min restore does not auto-expire (MAINTENANCE_MAX_AGE_S) mid-flight.
touch "$MAINTENANCE_SENTINEL"
MAIN_PID=$$
RESTORE_DEADLINE=$(( $(date +%s) + ${RESTORE_MAX_SECONDS:-3600} ))
(
  while true; do
    sleep 60
    touch "$MAINTENANCE_SENTINEL" 2>/dev/null || true
    if [ "$(date +%s)" -gt "$RESTORE_DEADLINE" ]; then
      # Deadline exceeded — abandon. If a DROP began (.destructive present) the
      # DB may be inconsistent: HOLD maintenance. If not, nothing was destroyed:
      # LIFT .maintenance. Then signal the main process so its EXIT trap writes
      # the single terminal status. The private timeout marker tells _cleanup to
      # word the error as a timeout.
      : > "$RESTORE_TIMEOUT_FILE" 2>/dev/null || true
      if [ ! -f "$MAINTENANCE_DESTRUCTIVE" ]; then
        rm -f "$MAINTENANCE_SENTINEL" 2>/dev/null || true
      fi
      kill "$MAIN_PID" 2>/dev/null || true
      exit 0
    fi
  done
) &
HEARTBEAT_PID=$!

echo "[restore] Waiting for LiteLLM to stop before the safety backup." >&2
wait_for_litellm_quarantine \
  || fail_before_destruction "LiteLLM did not stop after maintenance began; nothing was changed"

# === STEP 4: safety pre-backup before destructive replacement ================
CURRENT_STEP="Safety backup"
PHASE="safety"
STEP_SAFETY="running"
write_status
# Skip the retention prune in the safety pre-backup: it would otherwise delete the
# very archive being restored when that archive is a `local` target older than
# RETENTION_DAYS, and the STEP-5 reload reads that archive.
export BACKUP_SKIP_PRUNE=1
# Force the safety backup to run even though .maintenance is already up: the backup
# script's own maintenance skip-guard would otherwise treat the restore's own
# maintenance sentinel as "someone else is mid-restore" and abort the very safety
# snapshot the restore depends on.
export BACKUP_FORCE=1
SAFETY_RUN_ID="$(openssl rand -hex 16 2>/dev/null || true)"
if ! printf '%s' "$SAFETY_RUN_ID" | grep -Eq '^[0-9a-f]{32}$'; then
  fail_before_destruction "could not allocate the safety-backup correlation ID; nothing was changed"
fi
export BACKUP_RUN_ID="$SAFETY_RUN_ID"
if /usr/local/bin/backup.sh; then SAFETY_RC=0; else SAFETY_RC=$?; echo "[restore] WARN: safety backup.sh exited non-zero (${SAFETY_RC})" >&2; fi
LAST_RUN="$(cat "${BACKUP_DIR}/.last_run.json" 2>/dev/null || true)"
SAFETY_BACKUP_TS="$(printf '%s' "$LAST_RUN" \
  | grep -oE '"timestamp"[[:space:]]*:[[:space:]]*"[0-9]{8}_[0-9]{6}"' | grep -oE '[0-9]{8}_[0-9]{6}' | head -1 || true)"
# The safety pre-backup is the ONLY rollback point for a mid-swap failure. Accept
# only exit zero plus the caller-assigned run ID in both .last_run and its manifest;
# a stale record or concurrent producer can therefore never masquerade as this run.
if ! safety_backup_is_fresh "$SAFETY_RC" "$SAFETY_RUN_ID"; then
  STEP_SAFETY="failed"
  fail_before_destruction "safety backup failed or is stale (no fresh rollback point); nothing was changed"
fi
STEP_SAFETY="done"
write_status

# Re-verify the resolved DB archives still exist AFTER the safety pre-backup and
# BEFORE the first DROP. The safety backup runs with BACKUP_SKIP_PRUNE=1 so it
# cannot prune them, but re-checking here is a cheap belt-and-braces guard: if a
# target archive vanished between STEP 1 resolution and now, fail before any
# destruction (fail_before_destruction lifts maintenance + records the error,
# no DROP) rather than DROP the DB and then fail to reload it.
for _arch in "$JARVIS_ARCHIVE" "$LITELLM_ARCHIVE"; do
  if [ ! -f "$_arch" ]; then
    fail_before_destruction "archive $(basename "$_arch") disappeared before the restore began (possibly pruned); nothing was changed"
  fi
done
if [ "$MANIFEST_AUTHENTICATED" = "1" ]; then
  verify_manifest_inventory "$PRIVATE_INPUT_DIR" "$TIMESTAMP" "$INVENTORY_FILE" \
    || fail_before_destruction "private restore input changed before use; nothing was changed"
fi

# === STEP 4.5: disk preflight (before the first tmp CREATE) ==================
# The rename-swap keeps both live DBs while it reloads a transient tmp DB per DB,
# so refuse fast if the volume cannot hold live + tmp + headroom (fail_before_-
# destruction lifts maintenance; nothing was touched).
preflight_disk_or_fail

# === STEP 5: restore the DBs — the rename-swap holds the ONLY destructive window
# Each DB is reloaded into <db>_restore_tmp (non-destructive; OLD <db> stays live),
# then swapped in by rename; the destructive window is only disallow->terminate->
# rename. A failure before the first rename leaves production untouched.
CURRENT_STEP="Restoring database"
PHASE="reload-db"
STEP_DB="running"
write_status
if restore_one_db_swap "$JARVIS_DB" "$JARVIS_ARCHIVE" 1; then
  STEP_DB="done"
else
  STEP_DB="failed"
  step5_fail
fi
write_status

CURRENT_STEP="Restoring API-key store"
PHASE="reload-litellm"
STEP_LITELLM="running"
write_status
if restore_one_db_swap "$LITELLM_DB" "$LITELLM_ARCHIVE" 0; then
  STEP_LITELLM="done"
else
  STEP_LITELLM="failed"
  step5_fail
fi
write_status

# === STEP 7: Qdrant recover (best-effort, non-fatal, loud) ===================
# Vectors are rebuildable from Postgres by re-embedding, so a Qdrant failure is
# recorded as degraded and never fails the restore.
CURRENT_STEP="Restoring search index"
PHASE="qdrant"
if [ "${#QDRANT_SNAPS[@]}" -eq 0 ]; then
  STEP_QDRANT="skipped"
  write_status
else
  STEP_QDRANT="running"
  write_status
  QDRANT_API_KEY=""
  [ -r "$QDRANT_API_KEYFILE" ] && QDRANT_API_KEY="$(cat "$QDRANT_API_KEYFILE")"
  mkdir -p "$QDRANT_STAGING_DIR" 2>/dev/null || true
  QDRANT_OK=1
  for snap in "${QDRANT_SNAPS[@]}"; do
    base="$(basename "$snap")"
    col="$(printf '%s' "$base" | sed -E "s/^qdrant_(.+)_${TIMESTAMP}\.snapshot(\.enc)?$/\1/")"
    staged="${QDRANT_STAGING_DIR}/${col}.snapshot"
    if ! decrypt_or_passthrough "$snap" > "$staged" 2>/dev/null; then
      echo "[restore] WARN: could not stage Qdrant snapshot for '${col}'; continuing" >&2
      QDRANT_OK=0
      rm -f "$staged" 2>/dev/null || true
      continue
    fi
    if qdrant_http_body PUT "/collections/${col}/snapshots/recover" \
        "{\"location\":\"file://${QDRANT_STAGING_DIR}/${col}.snapshot\",\"priority\":\"snapshot\"}"; then
      echo "[restore] Qdrant collection '${col}' recovered" >&2
    else
      echo "[restore] WARN: Qdrant recover failed for '${col}'; vectors can be rebuilt by re-embedding" >&2
      QDRANT_OK=0
    fi
    rm -f "$staged" 2>/dev/null || true
  done
  if [ "$QDRANT_OK" -eq 1 ]; then STEP_QDRANT="done"; else STEP_QDRANT="degraded"; fi
  write_status
fi

rotate_vector_visibility_checkpoint "$STEP_QDRANT" \
  || fail_after_restore "the databases were restored, but vector visibility could not be reset; maintenance remains active — retry recovery from the safety backup"
echo "[restore] vector visibility reset after Qdrant recovery: ${STEP_QDRANT}" >&2

# === STEP 8: install restored data keys ======================================
# Database dumps do not contain PostgreSQL roles, so destination-host role
# passwords and secret files remain unchanged. Database-backed SMTP, Telegram,
# provider, Zotero, and source settings were restored with JARVIS and remain
# quarantined after off-host recovery. Only the three data keys staged and
# validated before STEP 3 are installed from the archive.
if [ "$DATA_KEYS_STAGED" = "1" ]; then
  CURRENT_STEP="Restoring data keys"
  PHASE="data-keys"
  write_status
  if ! install_restored_data_keys; then
    fail_after_restore "the databases were restored, but their data keys could not be installed; maintenance remains active — retry recovery from the safety backup"
  fi
  write_status
fi

# The PDF swap is last. Its durable journal can therefore finish forward after
# a crash and safely lift maintenance once the exact staged inventory verifies.
CURRENT_STEP="Restoring PDF library"
PHASE="pdfs"
write_status
if ! swap_restored_pdfs "$PDF_RESTORE_RUN_ID"; then
  fail_after_restore "the databases were restored, but the PDF library swap did not finish; automatic recovery will retry while maintenance remains active"
fi
PDFS_STAGED=0

# === STEP 9: finishing up ====================================================
# A clean restore — same-host OR off-host — lifts maintenance via the EXIT trap.
# When data keys were present, STEP 8 installed them and wrote the rotation
# marker so dependent services reload the restored keys. The stack returns with no
# terminal steps. MANUAL_STEPS_REQUIRED stays 0 (reserved for genuinely
# unrecoverable states) so write_status reports honestly.
CURRENT_STEP="Finishing up"
PHASE="finalize"
STEP_FINISH="running"
write_status
FINISHED_AT="$(date -Iseconds)"
if [ "$SOURCE" = "inbox" ]; then
  write_outbound_quarantine "$RESTORE_ID" "$SOURCE" "$REQUESTED_AT" "$FINISHED_AT" \
    || fail_after_restore "the off-host restore completed, but outbound quarantine could not be recorded; maintenance remains active — use the host recovery command after preserving the restore ID"
fi
RESTORE_CLEAN=1
STATE="done"
STEP_FINISH="done"
write_status
if [ "$SOURCE" = "inbox" ]; then
  echo "[restore] off-host restore complete: databases, PDFs, vectors, and data keys restored; target-host credentials were preserved." >&2
fi
# The EXIT trap clears both .maintenance and .destructive on any clean restore
# + kills the heartbeat.
