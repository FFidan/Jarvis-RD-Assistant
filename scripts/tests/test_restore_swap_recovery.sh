#!/usr/bin/env bash
# test_restore_swap_recovery.sh — REAL postgres:16.8 fault-injection matrix for the
# restore.sh rename-swap + deterministic recovery.
#
# The swap's correctness (rename semantics, ALLOW_CONNECTIONS inheritance, the
# post-swap SQL gate, and `restore.sh --recover`) is UNTESTABLE by source grep, so
# this exercises it against a real engine. It:
#   * drives the REAL restore_one_db_swap (extracted from restore.sh) forward for
#     the happy path, a reload failure, a tmp-verify failure, and a post-swap-verify
#     failure (-> revert), asserting production is untouched or fully restored;
#   * stages EVERY mid-swap crash boundary (for BOTH DBs) as a stranded DB catalog +
#     private restore-swap state, runs the REAL `restore.sh --recover` inside the
#     container, and asserts each end state is untouched-original OR completed-
#     restore — never a reachable half-swap;
#   * stages every durable PDF swap phase and proves recovery finishes forward
#     without replacing the stable PDF storage root;
#   * checks the disk preflight blocks a tight disk and passes a roomy one.
#
# SAFETY: it ONLY ever touches a throwaway `--rm` postgres container (unique name,
# torn down on exit). It refuses to point at anything else — every psql call goes
# through `docker exec <the throwaway>`; it never reads PGHOST/a real stack.
#
# Run: bash scripts/tests/test_restore_swap_recovery.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESTORE_SH="${SCRIPT_DIR}/../restore.sh"
IMAGE="${SWAP_TEST_IMAGE:-postgres:16.8@sha256:301bcb60b8a3ee4ab7e147932723e3abd1cef53516ce5210b39fd9fe5e3602ae}"
CNAME="jarvis-restore-swap-test-${BASHPID}-${RANDOM}"
WORK="$(mktemp -d)"

pass=0; fail=0
ok()  { printf '  PASS: %s\n' "$1"; pass=$((pass + 1)); }
no()  { printf '  FAIL: %s\n' "$1"; fail=$((fail + 1)); }
sec() { printf '\n=== %s ===\n' "$1"; }

cleanup() { docker rm -f "$CNAME" >/dev/null 2>&1 || true; rm -rf "$WORK" 2>/dev/null || true; }
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
  printf 'SKIP: docker unavailable; cannot run the real-pg swap/recovery matrix\n' >&2
  exit 0
fi
# An installed client with an unreachable daemon must skip too, or this suite
# fails the whole gate on a developer machine that simply has Docker stopped.
if ! docker info >/dev/null 2>&1; then
  printf 'SKIP: docker daemon unreachable; cannot run the real-pg swap/recovery matrix\n' >&2
  exit 0
fi
if [ ! -r "$RESTORE_SH" ]; then printf 'FAIL: cannot read %s\n' "$RESTORE_SH" >&2; exit 1; fi

# --- throwaway engine (isolated, --rm, never a real stack) -------------------
docker rm -f "$CNAME" >/dev/null 2>&1 || true
docker run -d --name "$CNAME" --rm -e POSTGRES_PASSWORD=swaptest "$IMAGE" >/dev/null \
  || { printf 'FAIL: could not start %s\n' "$IMAGE" >&2; exit 1; }
# The official image briefly starts a temporary bootstrap server, then stops it
# before execing the final postgres process as PID 1. A bare pg_isready can catch
# that transient server and race the shutdown below. Require both final PID 1
# and readiness so the harness cannot report an application failure for an
# entrypoint transition.
_final_postgres_ready() {
  docker exec "$CNAME" sh -c \
    '[ "$(cat /proc/1/comm 2>/dev/null)" = "postgres" ] && pg_isready -U postgres' \
    >/dev/null 2>&1
}
for _ in $(seq 1 30); do _final_postgres_ready && break; sleep 1; done
_final_postgres_ready \
  || { printf 'FAIL: throwaway container never became ready\n' >&2; exit 1; }
printf 'engine: %s (%s)\n' "$IMAGE" "$(docker exec "$CNAME" psql -U postgres -tAc 'show server_version' 2>/dev/null)"

# --- container psql helpers (local socket, trust auth — no password needed) ---
adm() { docker exec -i "$CNAME" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -tAc "$1"; }
qd()  { docker exec -i "$CNAME" psql -U postgres -d "$1" -v ON_ERROR_STOP=1 -tAc "$2" 2>/dev/null; }
dbex() { [ "$(adm "SELECT count(*) FROM pg_database WHERE datname='$1'" 2>/dev/null)" = "1" ]; }
tag_of() { qd "$1" "SELECT tag FROM marker LIMIT 1" 2>/dev/null || true; }
connectable() { qd "$1" "SELECT 1" >/dev/null 2>&1; }

reset_catalog() {
  local d
  for d in jarvis jarvis_restore_tmp jarvis_pre_restore litellm litellm_restore_tmp litellm_pre_restore; do
    adm "DROP DATABASE IF EXISTS \"$d\";" >/dev/null 2>&1 || true
  done
}

# Structural DDL mirrors PRODUCTION per db type: jarvis has our schema_migrations +
# auth tables; litellm is a third-party (Prisma) schema with LiteLLM_* tables and NO
# schema_migrations. $1=target db  $2=is_jarvis.
struct_ddl() {
  if [ "$2" = "1" ]; then
    qd "$1" "CREATE TABLE schema_migrations(version int primary key); INSERT INTO schema_migrations VALUES (1); CREATE TABLE users(id int); CREATE TABLE sessions(id int);" >/dev/null
  else
    qd "$1" "CREATE TABLE \"LiteLLM_Config\"(param_name text);" >/dev/null
  fi
}

seed_original() { # $1=db  $2=is_jarvis
  adm "CREATE DATABASE \"$1\";" >/dev/null
  qd "$1" "CREATE TABLE marker(tag text); INSERT INTO marker VALUES ('ORIGINAL');" >/dev/null
  struct_ddl "$1" "$2"
  return 0
}

seed_tmp() { # $1=db  $2=is_jarvis  $3=good(1)/no-schema(0)  -> a fully-reloaded tmp
  adm "CREATE DATABASE \"${1}_restore_tmp\";" >/dev/null
  qd "${1}_restore_tmp" "CREATE TABLE marker(tag text); INSERT INTO marker VALUES ('RESTORED');" >/dev/null
  [ "$3" = "1" ] && struct_ddl "${1}_restore_tmp" "$2"
  return 0
}

rename_out() { # $1=db  -> disallow + terminate + rename db -> db_pre_restore (the swap's destructive prefix)
  adm "ALTER DATABASE \"$1\" WITH ALLOW_CONNECTIONS false;" >/dev/null
  adm "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$1' AND pid<>pg_backend_pid();" >/dev/null
  adm "ALTER DATABASE \"$1\" RENAME TO \"${1}_pre_restore\";" >/dev/null
}

make_archive() { # $1=outfile  $2=is_jarvis  $3=good(1)/no-schema(0)  $4=corrupt(1)/valid(0)
  if [ "$4" = "1" ]; then printf 'this is not gzip' > "$1"; return 0; fi
  {
    printf "CREATE TABLE marker(tag text); INSERT INTO marker VALUES ('RESTORED');\n"
    # Structural tables mirror production: jarvis schema_migrations + auth tables;
    # litellm a LiteLLM_* table (no schema_migrations). $3=0 omits them so the
    # tmp-verify gate fails (a reload that produced no real schema).
    if [ "$3" = "1" ]; then
      if [ "$2" = "1" ]; then
        printf 'CREATE TABLE schema_migrations(version int primary key); INSERT INTO schema_migrations VALUES (1);\n'
        printf 'CREATE TABLE users(id int); CREATE TABLE sessions(id int);\n'
      else
        printf 'CREATE TABLE "LiteLLM_Config"(param_name text);\n'
      fi
    fi
  } | gzip > "$1"
}

# =============================================================================
# PART 1 — the REAL restore_one_db_swap forward path (functions sourced from
#          restore.sh; psql routed into the throwaway container).
# =============================================================================
extract_fn() { sed -n "/^$1()/,/^}/p" "$RESTORE_SH"; }
FN_SRC="$(for f in _json_escape write_swap_state clear_swap_state read_swap_db \
                   db_exists verify_db_structural psql_admin decrypt_or_passthrough \
                   purge_restored_auth_state revert_swap reconcile_leftover \
                   restore_one_db_swap; do
  extract_fn "$f"; printf '\n'
done)"
eval "$FN_SRC"

# psql wrapper: strip the "-h <host>" pair restore.sh passes and exec into the
# throwaway container (local socket). This is the ONLY route to a database, so the
# forward path can NEVER reach a real stack.
psql() {
  local a args=() skip=0
  for a in "$@"; do
    if [ "$skip" = "1" ]; then skip=0; continue; fi
    if [ "$a" = "-h" ]; then skip=1; continue; fi
    args+=("$a")
  done
  docker exec -i "$CNAME" psql "${args[@]}"
}
write_status() { :; }               # stub: the FE status file is irrelevant here
# Read by the eval'd restore.sh functions; export so shellcheck sees them as used
# (harmless — docker exec does not forward host env into the container).
export PGHOST="ignored-by-wrapper" PGUSER="postgres"
export JARVIS_DB="jarvis" LITELLM_DB="litellm" ENC_KEYFILE=""
SWAP_STATE_FILE="${WORK}/.swapstate"; MAINTENANCE_DESTRUCTIVE="${WORK}/.destructive"

sec "PART 1: restore_one_db_swap forward path"

# 1a. Happy path for BOTH DBs: reload -> verify -> swap -> drop pre_restore.
reset_catalog; rm -f "$MAINTENANCE_DESTRUCTIVE" "$SWAP_STATE_FILE"
seed_original jarvis 1; seed_original litellm 0
make_archive "${WORK}/jarvis.sql.gz" 1 1 0
make_archive "${WORK}/litellm.sql.gz" 0 1 0
export DROP_STARTED=0   # inherited + mutated by the eval'd restore_one_db_swap
( set -e; restore_one_db_swap "jarvis" "${WORK}/jarvis.sql.gz" 1 ) && jrc=0 || jrc=$?
( set -e; restore_one_db_swap "litellm" "${WORK}/litellm.sql.gz" 0 ) && lrc=0 || lrc=$?
if [ "$jrc" = "0" ] && [ "$lrc" = "0" ] \
   && [ "$(tag_of jarvis)" = "RESTORED" ] && [ "$(tag_of litellm)" = "RESTORED" ] \
   && ! dbex jarvis_restore_tmp && ! dbex jarvis_pre_restore \
   && ! dbex litellm_restore_tmp && ! dbex litellm_pre_restore \
   && [ ! -f "$SWAP_STATE_FILE" ] && [ -f "$MAINTENANCE_DESTRUCTIVE" ]; then
  ok "happy path: both DBs serve RESTORED, no tmp/pre_restore left, state cleared, .destructive marked"
else
  no "happy path wrong (jrc=$jrc lrc=$lrc jarvis=$(tag_of jarvis) litellm=$(tag_of litellm))"
fi

# 1b. Reload failure (corrupt archive = ENOSPC/mid-reload analogue): production
#     untouched, DROP_STARTED never set, no .destructive, no leftover tmp/pre.
reset_catalog; rm -f "$MAINTENANCE_DESTRUCTIVE" "$SWAP_STATE_FILE"
seed_original jarvis 1
make_archive "${WORK}/bad.sql.gz" 1 1 1   # corrupt (not gzip) -> gunzip fails mid-reload
DROP_STARTED=0
( set -e; restore_one_db_swap "jarvis" "${WORK}/bad.sql.gz" 1 ); rc=$?
if [ "$rc" != "0" ] && [ "$(tag_of jarvis)" = "ORIGINAL" ] && connectable jarvis \
   && ! dbex jarvis_pre_restore && [ ! -f "$MAINTENANCE_DESTRUCTIVE" ]; then
  ok "reload failure (ENOSPC/corrupt): jarvis untouched+ORIGINAL, no destructive window entered"
else
  no "reload-failure not fail-safe (rc=$rc jarvis=$(tag_of jarvis) destructive=$([ -f "$MAINTENANCE_DESTRUCTIVE" ] && echo present || echo absent))"
fi

# 1c. tmp-verify failure (archive reloads but lacks schema_migrations): fails BEFORE
#     the destructive window; production untouched.
reset_catalog; rm -f "$MAINTENANCE_DESTRUCTIVE" "$SWAP_STATE_FILE"
seed_original jarvis 1
make_archive "${WORK}/noschema.sql.gz" 1 0 0   # good gzip, no schema_migrations
DROP_STARTED=0
( set -e; restore_one_db_swap "jarvis" "${WORK}/noschema.sql.gz" 1 ); rc=$?
if [ "$rc" != "0" ] && [ "$(tag_of jarvis)" = "ORIGINAL" ] && connectable jarvis \
   && ! dbex jarvis_pre_restore && [ ! -f "$MAINTENANCE_DESTRUCTIVE" ]; then
  ok "tmp-verify failure: jarvis untouched+ORIGINAL, no destructive window entered"
else
  no "tmp-verify failure not fail-safe (rc=$rc jarvis=$(tag_of jarvis))"
fi

# 1d. post-swap verify failure -> REVERT. Inject a verify that passes the tmp but
#     fails the swapped-in DB, so the real revert path runs. Asserts production is
#     back to the untouched ORIGINAL AND connectable (proves the 4.0 finding: the
#     renamed-out pre_restore inherited ALLOW_CONNECTIONS=false and was re-enabled).
reset_catalog; rm -f "$MAINTENANCE_DESTRUCTIVE" "$SWAP_STATE_FILE"
seed_original jarvis 1
make_archive "${WORK}/jarvis.sql.gz" 1 1 0
DROP_STARTED=0
(
  set -e
  verify_db_structural() { case "$1" in *_restore_tmp) return 0 ;; *) return 1 ;; esac; }
  restore_one_db_swap "jarvis" "${WORK}/jarvis.sql.gz" 1
); rc=$?
if [ "$rc" != "0" ] && [ "$(tag_of jarvis)" = "ORIGINAL" ] && connectable jarvis \
   && ! dbex jarvis_restore_tmp && ! dbex jarvis_pre_restore \
   && [ -f "$MAINTENANCE_DESTRUCTIVE" ]; then
  ok "post-swap verify failure: REVERT restored jarvis to ORIGINAL, connectable, no leftovers, maintenance held"
else
  no "post-swap revert wrong (rc=$rc jarvis=$(tag_of jarvis) connectable=$(connectable jarvis && echo y || echo n) tmp=$(dbex jarvis_restore_tmp && echo y || echo n) pre=$(dbex jarvis_pre_restore && echo y || echo n))"
fi
unset -f psql write_status   # end of the sourced-function part

# =============================================================================
# PART 2 — the REAL `restore.sh --recover` against every stranded mid-swap state,
#          for BOTH DBs. restore.sh runs INSIDE the throwaway container.
# =============================================================================
docker cp "$RESTORE_SH" "${CNAME}:/usr/local/bin/restore.sh" >/dev/null
docker exec "$CNAME" chmod +x /usr/local/bin/restore.sh >/dev/null 2>&1 || true

stage_trigger() { docker exec "$CNAME" sh -c 'rm -rf /tmp/trig /tmp/backups /tmp/pdf-storage; mkdir -p /tmp/trig /tmp/backups/.lifecycle /tmp/pdf-storage /run/secrets; printf dummy > /run/secrets/postgres_password'; }
write_state()   { docker exec "$CNAME" sh -c "printf '%s' '$1' > /tmp/backups/.lifecycle/restore-swap-state.json"; }
touch_trig()    { docker exec "$CNAME" sh -c "touch /tmp/trig/$1"; }
has_trig()      { docker exec "$CNAME" sh -c "[ -f /tmp/trig/$1 ]"; }
has_state()     { docker exec "$CNAME" sh -c '[ -f /tmp/backups/.lifecycle/restore-swap-state.json ]'; }
run_recover()   {
  docker exec \
    -e BACKUP_TRIGGER_DIR=/tmp/trig -e BACKUP_DIR=/tmp/backups \
    -e PGHOST=/var/run/postgresql -e PGUSER=postgres \
    -e PGDATABASE=jarvis -e LITELLM_DATABASE=litellm \
    -e HOST_SECRETS_DIR=/tmp/trig -e PDF_STORAGE_DIR=/tmp/pdf-storage \
    "$CNAME" /usr/local/bin/restore.sh --recover >/dev/null 2>&1
}
# maint_state -> "lifted" | "held" (held = .destructive still present after recover)
maint_state() { if has_trig .destructive; then printf held; else printf lifted; fi; }

sec "PART 2: restore.sh --recover across all mid-swap boundaries (both DBs)"

# A (jarvis) — crash during the FIRST reload, pre-window: tmp present, pre absent,
# NO .destructive. Recover drops the stale tmp; production untouched; maintenance LIFTS.
reset_catalog; stage_trigger
seed_original jarvis 1; seed_tmp jarvis 1 1
write_state '{"db":"jarvis","phase":"reload_tmp"}'; touch_trig .maintenance
run_recover
if [ "$(tag_of jarvis)" = "ORIGINAL" ] && connectable jarvis \
   && ! dbex jarvis_restore_tmp && ! dbex jarvis_pre_restore \
   && ! has_state && [ "$(maint_state)" = "lifted" ]; then
  ok "A/jarvis reload_tmp crash (no window): stale tmp dropped, ORIGINAL untouched, maintenance LIFTED"
else
  no "A/jarvis wrong (jarvis=$(tag_of jarvis) tmp=$(dbex jarvis_restore_tmp && echo y||echo n) maint=$(maint_state))"
fi

# A' (litellm) — post-tmp-verify crash (tmp fully reloaded), pre-window: same shape.
reset_catalog; stage_trigger
seed_original litellm 0; seed_tmp litellm 0 1
write_state '{"db":"litellm","phase":"reload_tmp"}'; touch_trig .maintenance
run_recover
if [ "$(tag_of litellm)" = "ORIGINAL" ] && connectable litellm \
   && ! dbex litellm_restore_tmp && [ "$(maint_state)" = "lifted" ]; then
  ok "A'/litellm post-tmp-verify crash (no window): verified tmp dropped, ORIGINAL untouched, LIFTED"
else
  no "A'/litellm wrong (litellm=$(tag_of litellm) tmp=$(dbex litellm_restore_tmp && echo y||echo n) maint=$(maint_state))"
fi

# B-forward (litellm) — crash BETWEEN the two renames: db absent, tmp present (good),
# pre present, .destructive set. Recover completes forward -> RESTORED; maintenance HELD.
reset_catalog; stage_trigger
seed_original litellm 0; seed_tmp litellm 0 1; rename_out litellm
write_state '{"db":"litellm","phase":"swapping_in"}'; touch_trig .maintenance; touch_trig .destructive
run_recover
if [ "$(tag_of litellm)" = "RESTORED" ] && connectable litellm \
   && ! dbex litellm_restore_tmp && ! dbex litellm_pre_restore \
   && ! has_state && [ "$(maint_state)" = "held" ]; then
  ok "B-forward/litellm between-renames: completed forward to RESTORED, pre dropped, maintenance HELD"
else
  no "B-forward/litellm wrong (litellm=$(tag_of litellm) tmp=$(dbex litellm_restore_tmp && echo y||echo n) pre=$(dbex litellm_pre_restore && echo y||echo n) maint=$(maint_state))"
fi

# B-revert (jarvis) — crash BETWEEN the two renames but the tmp is BAD (no
# schema_migrations): recover completes forward, the post-swap verify FAILS, so it
# REVERTS to the untouched ORIGINAL (re-enabling ALLOW_CONNECTIONS). Maintenance HELD.
reset_catalog; stage_trigger
seed_original jarvis 1; seed_tmp jarvis 1 0; rename_out jarvis   # tmp lacks schema_migrations
write_state '{"db":"jarvis","phase":"swapping_in"}'; touch_trig .maintenance; touch_trig .destructive
run_recover
if [ "$(tag_of jarvis)" = "ORIGINAL" ] && connectable jarvis \
   && ! dbex jarvis_restore_tmp && ! dbex jarvis_pre_restore \
   && ! has_state && [ "$(maint_state)" = "held" ]; then
  ok "B-revert/jarvis between-renames+bad-tmp: verify failed -> REVERT to ORIGINAL, connectable, maintenance HELD"
else
  no "B-revert/jarvis wrong (jarvis=$(tag_of jarvis) connectable=$(connectable jarvis && echo y||echo n) tmp=$(dbex jarvis_restore_tmp && echo y||echo n) pre=$(dbex jarvis_pre_restore && echo y||echo n) maint=$(maint_state))"
fi

# C (jarvis) — crash AFTER rename-in, BEFORE dropping pre_restore: db present
# (RESTORED), pre present, tmp absent. Recover verifies + drops pre. Maintenance HELD.
reset_catalog; stage_trigger
seed_original jarvis 1; seed_tmp jarvis 1 1
rename_out jarvis
adm "ALTER DATABASE \"jarvis_restore_tmp\" RENAME TO \"jarvis\";" >/dev/null   # rename-in already done
write_state '{"db":"jarvis","phase":"verified"}'; touch_trig .maintenance; touch_trig .destructive
run_recover
if [ "$(tag_of jarvis)" = "RESTORED" ] && connectable jarvis \
   && ! dbex jarvis_pre_restore && ! dbex jarvis_restore_tmp \
   && ! has_state && [ "$(maint_state)" = "held" ]; then
  ok "C/jarvis post-rename-in pre-drop: verified RESTORED, pre_restore dropped, maintenance HELD"
else
  no "C/jarvis wrong (jarvis=$(tag_of jarvis) pre=$(dbex jarvis_pre_restore && echo y||echo n) maint=$(maint_state))"
fi

# D (litellm) — crash AFTER disallow, BEFORE rename-out: db present but disallowed,
# tmp present, pre absent, .destructive set. Recover re-enables + drops tmp;
# production ORIGINAL + connectable again; maintenance HELD (a window was entered).
reset_catalog; stage_trigger
seed_original litellm 0; seed_tmp litellm 0 1
adm "ALTER DATABASE \"litellm\" WITH ALLOW_CONNECTIONS false;" >/dev/null
write_state '{"db":"litellm","phase":"swapping_out"}'; touch_trig .maintenance; touch_trig .destructive
run_recover
if [ "$(tag_of litellm)" = "ORIGINAL" ] && connectable litellm \
   && ! dbex litellm_restore_tmp && ! dbex litellm_pre_restore \
   && [ "$(maint_state)" = "held" ]; then
  ok "D/litellm disallow-before-rename: re-enabled ORIGINAL, tmp dropped, maintenance HELD"
else
  no "D/litellm wrong (litellm=$(tag_of litellm) connectable=$(connectable litellm && echo y||echo n) tmp=$(dbex litellm_restore_tmp && echo y||echo n) maint=$(maint_state))"
fi

# E — crash BETWEEN the jarvis and litellm swaps: jarvis fully RESTORED (no
# tmp/pre), litellm mid-reload (tmp present, pre absent), state names litellm,
# .destructive set (from jarvis's swap). Recover(litellm) drops the litellm tmp;
# jarvis stays RESTORED, litellm stays ORIGINAL, maintenance HELD for a re-trigger.
reset_catalog; stage_trigger
seed_original jarvis 1; seed_tmp jarvis 1 1
rename_out jarvis
adm "ALTER DATABASE \"jarvis_restore_tmp\" RENAME TO \"jarvis\";" >/dev/null
adm "DROP DATABASE \"jarvis_pre_restore\";" >/dev/null            # jarvis swap complete
seed_original litellm 0; seed_tmp litellm 0 1                     # litellm mid-reload
write_state '{"db":"litellm","phase":"reload_tmp"}'; touch_trig .maintenance; touch_trig .destructive
run_recover
if [ "$(tag_of jarvis)" = "RESTORED" ] && [ "$(tag_of litellm)" = "ORIGINAL" ] \
   && connectable jarvis && connectable litellm \
   && ! dbex litellm_restore_tmp && [ "$(maint_state)" = "held" ]; then
  ok "E/between-DBs: jarvis RESTORED + litellm ORIGINAL (split), litellm tmp dropped, maintenance HELD"
else
  no "E wrong (jarvis=$(tag_of jarvis) litellm=$(tag_of litellm) tmp=$(dbex litellm_restore_tmp && echo y||echo n) maint=$(maint_state))"
fi

# =============================================================================
# PART 3 — PDF transaction recovery finishes forward from every journal phase
#          without replacing the stable bind-root directory.
# =============================================================================
sec "PART 3: PDF swap recovery across every durable phase"

stage_pdf_phase() { # <move_old|move_new|verify|cleanup> <32-hex run id>
  local phase="$1" run_id="$2"
  stage_trigger
  docker exec "$CNAME" sh -ec "
    stage=/tmp/pdf-storage/.restore-stage-${run_id}
    old=/tmp/pdf-storage/.restore-old-${run_id}
    mkdir -m 700 \"\$stage\" \"\$old\"
    printf '%s' OLD-LIVE > /tmp/pdf-storage/1.pdf
    printf '%s' NEW-ONE > \"\$stage/1.pdf\"
    printf '%s' NEW-TWO > \"\$stage/2.pdf\"
    : > \"\$stage/.inventory.tsv\"
    for name in 1.pdf 2.pdf; do
      size=\$(stat -c%s \"\$stage/\$name\")
      sha=\$(sha256sum \"\$stage/\$name\" | cut -d' ' -f1)
      printf '%s\\t%s\\t%s\\n' \"\$name\" \"\$size\" \"\$sha\" >> \"\$stage/.inventory.tsv\"
    done
    case '${phase}' in
      move_old) ;;
      move_new) mv /tmp/pdf-storage/1.pdf \"\$old/1.pdf\" ;;
      verify|cleanup)
        mv /tmp/pdf-storage/1.pdf \"\$old/1.pdf\"
        mv \"\$stage/1.pdf\" \"\$stage/2.pdf\" /tmp/pdf-storage/
        ;;
      *) exit 2 ;;
    esac
    printf '%s' '{\"version\":2,\"resource\":\"pdfs\",\"run_id\":\"${run_id}\",\"phase\":\"${phase}\"}' \
      > /tmp/backups/.lifecycle/restore-swap-state.json
    touch /tmp/trig/.maintenance
  "
}

pdf_fingerprint() {
  docker exec "$CNAME" sh -c 'find /tmp/pdf-storage -regextype posix-extended -mindepth 1 -maxdepth 1 -type f -regex '\''.*/[0-9]+\.pdf'\'' -printf '\''%f\n'\'' | sort | while IFS= read -r name; do printf "%s=%s\n" "$name" "$(cat "/tmp/pdf-storage/$name")"; done'
}

run_pdf_recovery_case() { # <phase> <32-hex run id>
  local phase="$1" run_id="$2" inode_before inode_after expected
  expected="$(printf '1.pdf=NEW-ONE\n2.pdf=NEW-TWO')"
  stage_pdf_phase "$phase" "$run_id"
  inode_before="$(docker exec "$CNAME" stat -c '%d:%i' /tmp/pdf-storage)"
  run_recover
  inode_after="$(docker exec "$CNAME" stat -c '%d:%i' /tmp/pdf-storage)"
  if [ "$(pdf_fingerprint)" = "$expected" ] \
     && [ "$inode_after" = "$inode_before" ] \
     && ! has_state \
     && ! docker exec "$CNAME" test -e "/tmp/pdf-storage/.restore-stage-${run_id}" \
     && ! docker exec "$CNAME" test -e "/tmp/pdf-storage/.restore-old-${run_id}" \
     && [ "$(maint_state)" = "lifted" ]; then
    ok "PDF ${phase}: recovery finished forward, verified exact files, and preserved the stable root inode"
  else
    no "PDF ${phase}: recovery did not finish cleanly (files='$(pdf_fingerprint)' inode=${inode_before}->${inode_after} state=$(has_state && echo present || echo clear) maint=$(maint_state))"
  fi
}

run_pdf_recovery_case move_old 11111111111111111111111111111111
run_pdf_recovery_case move_new 22222222222222222222222222222222
run_pdf_recovery_case verify   33333333333333333333333333333333
run_pdf_recovery_case cleanup  44444444444444444444444444444444

# =============================================================================
# PART 4 — disk preflight (blocks a tight disk, passes a roomy one). The df target
#          is stubbed so the real arithmetic + threshold are exercised deterministic.
# =============================================================================
sec "PART 4: preflight_disk_or_fail threshold"
PRE_SRC="$(extract_fn preflight_disk_or_fail; printf '\n'; extract_fn fail_before_destruction)"
make_archive "${WORK}/pf_jarvis.sql.gz" 1 1 0
make_archive "${WORK}/pf_litellm.sql.gz" 0 1 0
run_preflight() { # $1 = avail_kb the stubbed df reports
  bash -c '
    set -uo pipefail
    STATE=""; ERROR=""; FINISHED_AT=""
    PGHOST=x PGUSER=postgres JARVIS_DB=jarvis LITELLM_DB=litellm
    JARVIS_ARCHIVE="'"${WORK}/pf_jarvis.sql.gz"'"; LITELLM_ARCHIVE="'"${WORK}/pf_litellm.sql.gz"'"
    POSTGRES_DATA_DIR="/postgres-data"
    psql() { printf "0\n"; }                 # existing DB size = 0 KB (isolate the tmp estimate)
    df()   { printf "Filesystem 1024-blocks Used Available Capacity Mounted\n/x 1 1 '"$1"' 1%% /\n"; }
    '"$PRE_SRC"'
    preflight_disk_or_fail && echo "PASSED-THROUGH"
  ' 2>/dev/null
}
# Tight disk (1 GB free) must block: tmp estimate alone is ~200 MB + 2 GB headroom.
tight="$(run_preflight $((1 * 1024 * 1024)))"
# Roomy disk (50 GB free) must pass.
roomy="$(run_preflight $((50 * 1024 * 1024)))"
if [ -z "$tight" ]; then
  ok "preflight BLOCKS a tight disk (fail_before_destruction exits before returning)"
else
  no "preflight did not block a tight disk (out='$tight')"
fi
if [ "$roomy" = "PASSED-THROUGH" ]; then
  ok "preflight PASSES a roomy disk (returns without failing)"
else
  no "preflight wrongly blocked a roomy disk (out='$roomy')"
fi

# =============================================================================
printf '\n================================================================\n'
printf 'SWAP/RECOVERY MATRIX: PASS=%s  FAIL=%s\n' "$pass" "$fail"
printf '================================================================\n'
[ "$fail" -eq 0 ] || exit 1
