SET client_min_messages = warning;
DROP TABLE IF EXISTS telegram_user_pairings CASCADE;
DROP TABLE IF EXISTS telegram_pairing_tokens CASCADE;
DROP TABLE IF EXISTS magic_link_tokens CASCADE;
DROP TABLE IF EXISTS sessions CASCADE;
DROP TABLE IF EXISTS webauthn_challenges CASCADE;
DROP TABLE IF EXISTS webauthn_credentials CASCADE;
DROP TABLE IF EXISTS cards CASCADE;
-- Objects added after schema 101 must be removed before rebuilding the
-- historical origin in a database that has already reached a newer schema.
DROP TABLE IF EXISTS focus_sessions CASCADE;
DROP TABLE IF EXISTS tasks CASCADE;
DROP TABLE IF EXISTS paper_notes CASCADE;
DROP TABLE IF EXISTS entity_relationships CASCADE;
DROP TABLE IF EXISTS paper_entities CASCADE;
DROP TABLE IF EXISTS paper_extractions CASCADE;
DROP TABLE IF EXISTS paper_summaries CASCADE;
DROP TABLE IF EXISTS paper_highlights CASCADE;
DROP TABLE IF EXISTS paper_user_zotero_links CASCADE;
DROP TABLE IF EXISTS paper_contradictions CASCADE;
DROP TABLE IF EXISTS papers CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS user_config CASCADE;
DROP TABLE IF EXISTS audit_log CASCADE;
DROP TABLE IF EXISTS roundtrip_marker CASCADE;
DROP TABLE IF EXISTS schema_migrations CASCADE;
CREATE TABLE schema_migrations(version int PRIMARY KEY, applied_at timestamptz DEFAULT now());
CREATE TABLE users(
  id bigint PRIMARY KEY,
  email text NOT NULL,
  role text NOT NULL,
  deleted_at timestamptz
);
CREATE TABLE user_config(
  id bigserial PRIMARY KEY,
  user_id bigint,
  key text NOT NULL,
  value jsonb,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX user_config_user_key_idx
  ON user_config (user_id, key) NULLS NOT DISTINCT;
-- Core paper columns already existed at schema 101. Keep this fixture minimal,
-- but realistic enough for every later migration to run without weakening a
-- production migration with IF EXISTS guards.
CREATE TABLE papers(
  id bigint PRIMARY KEY,
  external_id text NOT NULL UNIQUE,
  url text,
  source_type text,
  discovery_origin text NOT NULL DEFAULT 'direct'
);
CREATE TABLE paper_contradictions(
  id bigint PRIMARY KEY,
  paper_a_id bigint NOT NULL,
  paper_b_id bigint NOT NULL,
  quote_a text NOT NULL,
  quote_b text NOT NULL,
  user_id bigint
);
CREATE TABLE paper_user_zotero_links(
  id bigint PRIMARY KEY,
  updated_at timestamptz DEFAULT now()
);
CREATE TABLE paper_highlights(id bigint PRIMARY KEY);
CREATE TABLE paper_summaries(id bigint PRIMARY KEY);
CREATE TABLE paper_extractions(id bigint PRIMARY KEY);
CREATE TABLE paper_entities(id bigint PRIMARY KEY);
CREATE TABLE entity_relationships(id bigint PRIMARY KEY);
CREATE TABLE paper_notes(id bigint PRIMARY KEY);
CREATE TABLE cards(id bigint PRIMARY KEY);
-- Tasks predate schema 101 and are referenced by the durable focus migration.
-- The historical migration fixture needs only the stable identity column.
CREATE TABLE tasks(id integer PRIMARY KEY);
CREATE TABLE audit_log(
  id bigserial PRIMARY KEY,
  user_id text,
  action text NOT NULL,
  resource text NOT NULL,
  timestamp timestamptz DEFAULT now(),
  metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);
CREATE TABLE sessions(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz DEFAULT now() NOT NULL,
  revoked_at timestamptz
);
CREATE TABLE magic_link_tokens(
  token_hash text PRIMARY KEY,
  user_id bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  created_at timestamptz DEFAULT now() NOT NULL,
  pending_email text
);
CREATE TABLE telegram_pairing_tokens(
  token text PRIMARY KEY,
  user_id bigint NOT NULL,
  created_at timestamptz DEFAULT now() NOT NULL,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz
);
CREATE TABLE telegram_user_pairings(user_id bigint PRIMARY KEY, chat_id bigint NOT NULL);
CREATE TABLE roundtrip_marker(tag text);
INSERT INTO users(id, email, role) VALUES (1, 'roundtrip-owner@example.test', 'admin');
INSERT INTO papers(id, external_id, url, source_type)
VALUES (
  1,
  'local:aaaaaaaaaaaaaaaa',
  'local://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  'local'
);
INSERT INTO sessions(id, user_id, expires_at)
VALUES (gen_random_uuid(), 1, now() + interval '1 day');
INSERT INTO magic_link_tokens(token_hash, user_id, expires_at)
VALUES ('v101-ephemeral-link', 1, now() + interval '15 minutes');
INSERT INTO telegram_pairing_tokens(token, user_id, expires_at)
VALUES ('v101-ephemeral-pairing', 1, now() + interval '15 minutes');
INSERT INTO telegram_user_pairings(user_id, chat_id) VALUES (1, -100);
INSERT INTO roundtrip_marker(tag) VALUES ('schema-101-seed');
INSERT INTO schema_migrations(version) VALUES (101);
