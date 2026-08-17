#!/bin/sh
# Provision and finalize isolated PostgreSQL login authorities.

set -eu

mode="${1:-prepare}"
secret_dir="${POSTGRES_SECRET_DIR:-/run/secrets}"
host="${PGHOST:-postgres}"
database="${POSTGRES_DB:-jarvis}"
bootstrap_role="jarvis_cluster_bootstrap"
bootstrap_password_file="postgres_cluster_bootstrap_password"
owner_roles="jarvis_platform_owner jarvis_research_owner jarvis_learning_owner jarvis_ops_owner"

case "$mode" in
  prepare|finalize) ;;
  *)
    echo "[cluster-bootstrap] expected prepare or finalize mode." >&2
    exit 2
    ;;
esac
case "$database" in
  ''|*[!A-Za-z0-9_]*)
    echo "[cluster-bootstrap] POSTGRES_DB must be a simple PostgreSQL identifier." >&2
    exit 2
    ;;
esac

read_secret() {
  secret_path="$secret_dir/$1"
  if [ ! -s "$secret_path" ] || [ ! -f "$secret_path" ] || [ -L "$secret_path" ]; then
    echo "[cluster-bootstrap] missing or unsafe password file for $2" >&2
    exit 1
  fi
  cat "$secret_path"
}

connect_as() {
  connect_role="$1"
  connect_password_file="$2"
  shift 2
  connect_password="$(read_secret "$connect_password_file" "$connect_role")" || exit 1
  PGPASSWORD="$connect_password" \
    psql -X -v ON_ERROR_STOP=1 -h "$host" -U "$connect_role" -d "$database" "$@"
}

escaped_secret() {
  read_secret "$1" "$2" | sed "s/'/''/g"
}

role_exists() {
  connect_as "$bootstrap_role" "$bootstrap_password_file" -At \
    -c "SELECT 1 FROM pg_roles WHERE rolname = '$1'" | grep -qx 1
}

provision_login() {
  login_role="$1"
  login_password_file="$2"
  login_password="$(escaped_secret "$login_password_file" "$login_role")"
  printf 'DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '\''%s'\'') THEN CREATE ROLE %s LOGIN; END IF; END $$;\nALTER ROLE %s WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '\''%s'\'';\n' \
    "$login_role" "$login_role" "$login_role" "$login_password" \
    | connect_as "$bootstrap_role" "$bootstrap_password_file"
}

ensure_owner_roles() {
  for owner_role in $owner_roles; do
    if ! role_exists "$owner_role"; then
      connect_as "$bootstrap_role" "$bootstrap_password_file" -c "CREATE ROLE ${owner_role}"
    fi
    connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
      "ALTER ROLE ${owner_role} WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
  done
}

migration_floor() {
  relation="$(connect_as "$bootstrap_role" "$bootstrap_password_file" -At \
    -c "SELECT to_regclass('ops.schema_migrations')")"
  if [ "$relation" = "ops.schema_migrations" ]; then
    connect_as "$bootstrap_role" "$bootstrap_password_file" -At \
      -c 'SELECT COALESCE(MAX(version), 0) FROM ops.schema_migrations'
    return
  fi
  relation="$(connect_as "$bootstrap_role" "$bootstrap_password_file" -At \
    -c "SELECT to_regclass('public.schema_migrations')")"
  if [ "$relation" != "schema_migrations" ]; then
    echo "[cluster-bootstrap] migration catalog is unavailable." >&2
    return 1
  fi
  connect_as "$bootstrap_role" "$bootstrap_password_file" -At \
    -c 'SELECT COALESCE(MAX(version), 0) FROM public.schema_migrations'
}

normalize_memberships() {
  connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
    'REVOKE jarvis_legacy_rollback FROM jarvis_migrator'
  for owner_role in $owner_roles; do
    connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
      "REVOKE CREATE ON DATABASE ${database} FROM ${owner_role}"
    connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
      "REVOKE ${owner_role} FROM jarvis_legacy_rollback"
    connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
      "REVOKE ${owner_role} FROM jarvis_migrator"
    connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
      "GRANT ${owner_role} TO jarvis_migrator WITH INHERIT FALSE"
  done
}

assert_final_memberships() {
  invalid_count="$(connect_as "$bootstrap_role" "$bootstrap_password_file" -At -c "
    WITH governed(role_name) AS (
      VALUES ('jarvis_platform_owner'), ('jarvis_research_owner'),
             ('jarvis_learning_owner'), ('jarvis_ops_owner')
    )
    SELECT
      (SELECT count(*) FROM pg_auth_members AS m
       JOIN pg_roles AS granted ON granted.oid = m.roleid
       JOIN pg_roles AS member ON member.oid = m.member
       WHERE granted.rolname IN (SELECT role_name FROM governed)
         AND member.rolname = 'jarvis_migrator'
         AND (m.admin_option OR m.inherit_option OR NOT m.set_option))
      +
      (SELECT count(*) FROM pg_auth_members AS m
       JOIN pg_roles AS granted ON granted.oid = m.roleid
       JOIN pg_roles AS member ON member.oid = m.member
       WHERE (granted.rolname IN (SELECT role_name FROM governed)
              AND member.rolname = 'jarvis_legacy_rollback')
          OR (granted.rolname = 'jarvis_legacy_rollback'
              AND member.rolname = 'jarvis_migrator'))
      +
      (4 - (SELECT count(*) FROM pg_auth_members AS m
            JOIN pg_roles AS granted ON granted.oid = m.roleid
            JOIN pg_roles AS member ON member.oid = m.member
            WHERE granted.rolname IN (SELECT role_name FROM governed)
              AND member.rolname = 'jarvis_migrator'));")"
  if [ "$invalid_count" != "0" ]; then
    echo "[cluster-bootstrap] final role membership graph is invalid." >&2
    exit 1
  fi
}

assert_recovery_roles() {
  invalid_count="$(connect_as "$bootstrap_role" "$bootstrap_password_file" -At -c "
    SELECT
      (1 - (SELECT count(*) FROM pg_roles WHERE rolname = 'jarvis_backup_reader'))
      +
      (SELECT count(*) FROM pg_roles
       WHERE rolname = 'jarvis_backup_reader'
         AND NOT (rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                  AND NOT rolcreaterole AND NOT rolbypassrls AND NOT rolinherit))
      +
      (SELECT count(*) FROM pg_auth_members AS membership
       JOIN pg_roles AS member ON member.oid = membership.member
       WHERE member.rolname = 'jarvis_backup_reader')
      +
      (1 - (SELECT count(*) FROM pg_roles WHERE rolname = 'jarvis_restore_operator'))
      +
      (SELECT count(*) FROM pg_roles
       WHERE rolname = 'jarvis_restore_operator'
         AND NOT (rolcanlogin AND NOT rolsuper AND rolcreatedb
                  AND NOT rolcreaterole AND NOT rolbypassrls AND rolinherit))
      +
      (SELECT count(*) FROM pg_auth_members AS membership
       JOIN pg_roles AS member ON member.oid = membership.member
       JOIN pg_roles AS granted ON granted.oid = membership.roleid
       WHERE member.rolname = 'jarvis_restore_operator'
         AND granted.rolname NOT IN (
           'pg_signal_backend', 'jarvis_legacy_rollback', 'jarvis_litellm_migrator'
         ))
      +
      (3 - (SELECT count(*) FROM pg_auth_members AS membership
            JOIN pg_roles AS member ON member.oid = membership.member
            JOIN pg_roles AS granted ON granted.oid = membership.roleid
            WHERE member.rolname = 'jarvis_restore_operator'
              AND granted.rolname IN (
                'pg_signal_backend', 'jarvis_legacy_rollback', 'jarvis_litellm_migrator'
              )));")"
  if [ "$invalid_count" != "0" ]; then
    echo "[cluster-bootstrap] backup or restore role authority is invalid." >&2
    exit 1
  fi
}

transfer_v125_objects() {
  target_database="$1"
  target_role="$2"
  connect_as "$bootstrap_role" "$bootstrap_password_file" -d "$target_database" -c "
    DO \$\$
    DECLARE
      obj record;
      command text;
    BEGIN
      FOR obj IN
        SELECT n.nspname, c.relname, c.relkind
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'jarvis')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_toast'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          AND NOT (
            c.relkind = 'S'
            AND EXISTS (
              SELECT 1 FROM pg_depend AS owned_sequence
              WHERE owned_sequence.classid = 'pg_class'::regclass
                AND owned_sequence.objid = c.oid
                AND owned_sequence.refclassid = 'pg_class'::regclass
                AND owned_sequence.deptype IN ('a', 'i')
            )
          )
          AND NOT EXISTS (
            SELECT 1 FROM pg_depend AS d
            WHERE d.classid = 'pg_class'::regclass
              AND d.objid = c.oid
              AND d.deptype = 'e'
          )
      LOOP
        command := CASE obj.relkind
          WHEN 'S' THEN 'ALTER SEQUENCE '
          WHEN 'v' THEN 'ALTER VIEW '
          WHEN 'm' THEN 'ALTER MATERIALIZED VIEW '
          WHEN 'f' THEN 'ALTER FOREIGN TABLE '
          ELSE 'ALTER TABLE '
        END;
        EXECUTE command || format('%I.%I OWNER TO ${target_role}', obj.nspname, obj.relname);
      END LOOP;

      FOR obj IN
        SELECT n.nspname, p.proname, p.prokind,
               pg_get_function_identity_arguments(p.oid) AS identity_arguments
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE p.proowner = (SELECT oid FROM pg_roles WHERE rolname = 'jarvis')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND NOT EXISTS (
            SELECT 1 FROM pg_depend AS d
            WHERE d.classid = 'pg_proc'::regclass
              AND d.objid = p.oid
              AND d.deptype = 'e'
          )
      LOOP
        command := CASE WHEN obj.prokind = 'p' THEN 'ALTER PROCEDURE ' ELSE 'ALTER FUNCTION ' END;
        EXECUTE command || format('%I.%I(%s) OWNER TO ${target_role}', obj.nspname, obj.proname, obj.identity_arguments);
      END LOOP;

      FOR obj IN
        SELECT n.nspname, t.typname
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        WHERE t.typowner = (SELECT oid FROM pg_roles WHERE rolname = 'jarvis')
          AND (
            (t.typrelid = 0 AND t.typtype IN ('d', 'e', 'm', 'r'))
            OR (
              t.typtype = 'c'
              AND EXISTS (
                SELECT 1 FROM pg_class AS composite_relation
                WHERE composite_relation.oid = t.typrelid
                  AND composite_relation.relkind = 'c'
              )
            )
          )
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND NOT EXISTS (
            SELECT 1 FROM pg_depend AS d
            WHERE d.classid = 'pg_type'::regclass
              AND d.objid = t.oid
              AND d.deptype = 'e'
          )
      LOOP
        EXECUTE format('ALTER TYPE %I.%I OWNER TO ${target_role}', obj.nspname, obj.typname);
      END LOOP;
    END
    \$\$;"
}

if [ "$mode" = "finalize" ]; then
  live_floor="$(migration_floor)" || exit 1
  case "$live_floor" in
    ''|*[!0-9]*|0|1|2|3|4|5|6|7|8|9|[1-9][0-9]|10[0-9]|11[0-3])
      echo "[cluster-bootstrap] schema is below 114; refusing privilege finalization." >&2
      exit 1
      ;;
  esac
  normalize_memberships
  assert_final_memberships
  assert_recovery_roles
  echo "[cluster-bootstrap] migration authority finalized." >&2
  exit 0
fi

# A fresh cluster creates the bootstrap login through the official image. On a
# v1.2.5 volume, use the original bootstrap user once to create the isolated
# authority. PostgreSQL cannot rename or demote that special role, so it is
# retained without LOGIN after its ownership is transferred below.
if ! connect_as "$bootstrap_role" "$bootstrap_password_file" -c 'SELECT 1' >/dev/null 2>&1; then
  legacy_source_password_file="postgres_legacy_source_password"
  bootstrap_password="$(escaped_secret "$bootstrap_password_file" "$bootstrap_role")"
  echo "[cluster-bootstrap] establishing isolated bootstrap authority for upgrade." >&2
  printf 'DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '\''%s'\'') THEN CREATE ROLE %s LOGIN SUPERUSER NOINHERIT; END IF; END $$;\nALTER ROLE %s WITH LOGIN SUPERUSER NOINHERIT PASSWORD '\''%s'\'';\n' \
    "$bootstrap_role" "$bootstrap_role" "$bootstrap_role" "$bootstrap_password" \
    | connect_as jarvis "$legacy_source_password_file"
fi

ensure_owner_roles
provision_login jarvis_platform_runtime postgres_platform_runtime_password
provision_login jarvis_research_runtime postgres_research_runtime_password
provision_login jarvis_learning_runtime postgres_learning_runtime_password
provision_login jarvis_migrator postgres_migrator_password
provision_login jarvis_legacy_rollback postgres_legacy_rollback_password
provision_login jarvis_backup_reader postgres_backup_reader_password
provision_login jarvis_restore_operator postgres_restore_operator_password
provision_login jarvis_erasure_executor postgres_erasure_executor_password
provision_login jarvis_litellm_runtime litellm_runtime_password
provision_login jarvis_litellm_migrator litellm_migrator_password

# Restore is an exceptional, on-demand database swap authority. It is never
# mounted into the scheduled backup container and is not a superuser.
connect_as "$bootstrap_role" "$bootstrap_password_file" -c "
  ALTER ROLE jarvis_restore_operator WITH CREATEDB INHERIT;
  GRANT pg_signal_backend TO jarvis_restore_operator;
  GRANT jarvis_legacy_rollback TO jarvis_restore_operator;
  GRANT jarvis_litellm_migrator TO jarvis_restore_operator;"

# The v1.2.5 database and objects follow the renamed bootstrap role. Transfer
# them to the isolated rollback login before migration 0114 assumes that owner.
prepared_floor="$(migration_floor)" || exit 1
if [ "$prepared_floor" = "113" ]; then
  if ! role_exists jarvis; then
    echo "[cluster-bootstrap] v1.2.5 bootstrap owner is unavailable." >&2
    exit 1
  fi
  transfer_v125_objects "$database" jarvis_legacy_rollback
  connect_as "$bootstrap_role" "$bootstrap_password_file" -c 'ALTER ROLE jarvis NOLOGIN'
fi
connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
  "ALTER DATABASE ${database} OWNER TO jarvis_legacy_rollback"

# The bootstrap login remains the only persistent cluster-level superuser.
bootstrap_password="$(escaped_secret "$bootstrap_password_file" "$bootstrap_role")"
printf "ALTER ROLE %s WITH LOGIN SUPERUSER NOINHERIT PASSWORD '%s';\n" \
  "$bootstrap_role" "$bootstrap_password" \
  | connect_as "$bootstrap_role" "$bootstrap_password_file"

if ! connect_as "$bootstrap_role" "$bootstrap_password_file" -d postgres -At \
  -c "SELECT 1 FROM pg_database WHERE datname = 'litellm'" | grep -qx 1; then
  connect_as "$bootstrap_role" "$bootstrap_password_file" -d postgres -c \
    'CREATE DATABASE litellm OWNER jarvis_litellm_migrator'
fi

connect_as "$bootstrap_role" "$bootstrap_password_file" -d postgres -c "
  REVOKE CONNECT, TEMPORARY ON DATABASE ${database} FROM PUBLIC;
  GRANT CONNECT ON DATABASE ${database} TO
    jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime,
    jarvis_migrator, jarvis_legacy_rollback, jarvis_backup_reader, jarvis_restore_operator,
    jarvis_erasure_executor;
  REVOKE CONNECT, TEMPORARY ON DATABASE litellm FROM PUBLIC;
  GRANT CONNECT ON DATABASE litellm TO
    jarvis_litellm_runtime, jarvis_litellm_migrator,
    jarvis_backup_reader, jarvis_restore_operator;
  ALTER DATABASE litellm OWNER TO jarvis_litellm_migrator;"

# Existing LiteLLM objects were owned by the v1.2.5 cluster login. Transfer them
# before the pinned migration job and establish least-privilege future grants.
if role_exists jarvis; then
  transfer_v125_objects litellm jarvis_litellm_migrator
fi
connect_as "$bootstrap_role" "$bootstrap_password_file" -d litellm -c "
  REASSIGN OWNED BY jarvis_cluster_bootstrap TO jarvis_litellm_migrator;
  REASSIGN OWNED BY jarvis_legacy_rollback TO jarvis_litellm_migrator;
  REVOKE CREATE ON SCHEMA public FROM PUBLIC;
  REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
  REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
  REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
  GRANT USAGE ON SCHEMA public TO jarvis_litellm_runtime;
  GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO jarvis_litellm_runtime;
  GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO jarvis_litellm_runtime;
  GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO jarvis_litellm_runtime;
  GRANT USAGE ON SCHEMA public TO jarvis_backup_reader, jarvis_restore_operator;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO jarvis_backup_reader, jarvis_restore_operator;
  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO jarvis_backup_reader, jarvis_restore_operator;
  ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_litellm_migrator IN SCHEMA public
    REVOKE ALL ON TABLES FROM PUBLIC;
  ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_litellm_migrator IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
  ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_litellm_migrator IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
  ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_litellm_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO jarvis_litellm_runtime;
  ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_litellm_migrator IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO jarvis_litellm_runtime;
  ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_litellm_migrator IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO jarvis_litellm_runtime;
  ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_litellm_migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO jarvis_backup_reader, jarvis_restore_operator;
  ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_litellm_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO jarvis_backup_reader, jarvis_restore_operator;"

live_floor="$(migration_floor)" || exit 1
case "$live_floor" in
  113)
    normalize_memberships
    connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
      'GRANT SELECT, INSERT, UPDATE ON TABLE public.schema_migrations TO jarvis_migrator'
    for owner_role in $owner_roles; do
      connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
        "GRANT CREATE ON DATABASE ${database} TO ${owner_role}; REVOKE ${owner_role} FROM jarvis_migrator; GRANT ${owner_role} TO jarvis_migrator WITH ADMIN OPTION, INHERIT FALSE; GRANT ${owner_role} TO jarvis_legacy_rollback WITH INHERIT FALSE"
    done
    connect_as "$bootstrap_role" "$bootstrap_password_file" -c \
      'GRANT jarvis_legacy_rollback TO jarvis_migrator WITH ADMIN OPTION, INHERIT FALSE'
    echo "[cluster-bootstrap] temporary migration authority prepared for floor 113." >&2
    ;;
  ''|*[!0-9]*)
    echo "[cluster-bootstrap] migration floor is invalid." >&2
    exit 1
    ;;
  *)
    if [ "$live_floor" -lt 114 ]; then
      echo "[cluster-bootstrap] unsupported migration floor." >&2
      exit 1
    fi
    normalize_memberships
    assert_final_memberships
    ;;
esac

assert_recovery_roles
echo "[cluster-bootstrap] roles and databases are ready." >&2
