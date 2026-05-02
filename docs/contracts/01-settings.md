# 01 — Settings Contract
**Status:** LIVING
**Date:** 2026-05-02
**Reviewers must update this contract in the same patch as any change to:**
- `_ALLOWED_CONFIG_KEYS` or `_CONFIG_VALIDATORS` in [routers/settings.py](../../services/paper_ingestion/paper_ingestion/routers/settings.py)
- DDL on `user_config`, `topics`, `tracked_authors`, `paper_sources`, `scheduled_nudges`
- Any new code reading `user_config.value` in any service

---

## 0. What this contract covers (and what it does NOT)

**In scope.** Every user-controllable setting that backs a control in the
frontend Settings UI or shapes runtime behavior. Storage shape, validation,
who writes it, who reads it, and current LIVE/GHOST/PARTIAL status.

**Out of scope.**
- Per-paper user state (`paper_user_state` columns) — covered by the
  [paper-lifecycle redesign spec](../specs/2026-04-29-paper-lifecycle-redesign.md).
- Pomodoro timer settings (`usePomodoroStore` Zustand) — UI-local only,
  never persisted server-side.
- Authentication (`JARVIS_API_KEY` in env / `auth-store`) — bootstrap, not
  user-controllable.
- HTTP request/response Pydantic shapes — covered by service-local models,
  not this contract.

---

## 1. Storage tables

The Settings UI writes to **two physical surfaces**: `user_config` (key/value
JSONB store) and a small set of typed tables for entities that have their
own row-level state.

| Table | Source-of-truth migration | Purpose |
|---|---|---|
| `user_config` | [db/init.sql:33-42](../../db/init.sql#L33-L42); `encrypted_value BYTEA` added by migration 033 | Single-row key/value store. JSON values for plain settings; ciphertext bytes for encrypted secrets. Single-user system today; multi-tenant gating tracked separately in [ARCHITECTURE.md](../ARCHITECTURE.md) "Authentication And Ownership". |
| `topics` | [db/init.sql:90-97](../../db/init.sql#L90-L97); `description TEXT` added by migration 018 | User-defined research topics. `enabled` filters which topics feed Pulse + recommendation. |
| `tracked_authors` | `db/init.sql` (post-merger schema); `source` column tracks `manual / auto_starred / auto_rated` | Authors the user wants alerts for. `enabled` filters which feed Stage-1 `author_bonus`. |
| `paper_sources` | [db/init.sql:71-88](../../db/init.sql#L71-L88); `display_order` added by migration 023 | Pluggable source registry (arXiv, Semantic Scholar, OpenAlex, PubMed, Local). `enabled` toggles ingestion. `config` JSONB holds per-source settings (e.g. `requires_key`). |
| `scheduled_nudges` | `db/init.sql` + migration 016 | Telegram notification cron schedules (6 nudge types). `enabled` and `cron_expression` are the user-controllable columns. |
| `extraction_templates` | `db/init.sql` + migration 011 | User-defined LLM extraction schemas. `is_default` flags the system default. |
| `recommendation_feedback` | Migration 049 | 👍/👎 signals per paper/topic. Drives the recommender L1+L2+L3 dampening. Not exposed as a Settings tab; included here because it shapes Pulse behavior the user can see. |
| `pulse_decks` / `pulse_cards` | Migrations 018 / 043 / 049 | Nightly Pulse deck state + diagnostics; `pulse_decks.stats` JSONB drives the Settings → Pulse "Last Pulse run" panel. |

`user_config` is the dominant storage for settings; the typed tables exist
because their entities have richer state than a JSON value can carry
(per-row enable toggles, ordering, per-source secrets).

---

## 2. `user_config` key catalog

The headline table. Every key the API may write through `PUT /api/config/{key}`
must appear in `_ALLOWED_CONFIG_KEYS` ([settings.py:49-90](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L49-L90)). Status reflects whether the value is consulted by runtime
behavior somewhere.

### 2.1 LIVE keys (written and read by code that affects user-visible behavior)

| Key | Default | Validator | Read sites | Notes |
|---|---|---|---|---|
| `pulse.enabled` | (none) | `_validate_bool` | scheduler.py — gates whether the `pulse_overnight` job runs | Master switch for Pulse subsystem |
| `pulse.cron` | `"0 4 * * *"` | `_validate_cron` (rejects sub-hourly) | scheduler.py | On write, [settings.py:368-411](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L368-L411) reschedules the live job AND validates `next_run_time ∈ [now, now+366d]`; rolls back on failure |
| `pulse.deck_size` | `_DEFAULT_DECK_SIZE` (10) | `_validate_positive_int` | [profile.py:187](../../services/paper_ingestion/paper_ingestion/pulse/profile.py#L187) | Final card count |
| `pulse.stage2_top_k` | `_DEFAULT_STAGE2_TOP_K` (50) | `_validate_positive_int` | [profile.py:188](../../services/paper_ingestion/paper_ingestion/pulse/profile.py#L188) | Stage-1 cut feeding Stage-2 LLM rerank |
| `pulse.weights` | `_DEFAULT_WEIGHTS` (in profile.py) | `_validate_pulse_weights` (requires 6 core keys; permits 4 optional; values ∈ [0,1]) | [profile.py:178-184](../../services/paper_ingestion/paper_ingestion/pulse/profile.py#L178-L184) merge-with-defaults; clamped to `[0,1]` (out-of-range logged) | Carries the 10 weight sliders. **4 are CONDITIONAL** (default 0.0; require user opt-in AND optional dep) — see [02-pulse.md §3.2](02-pulse.md#32-conditional-signals-live-conditional) |
| `pulse.l2_lambda` | 0.5 | `_validate_l2_lambda` (range `[0.0, 2.0]`) | [profile.py:191-193](../../services/paper_ingestion/paper_ingestion/pulse/profile.py#L191-L193); applied at [scoring.py:161-174](../../services/paper_ingestion/paper_ingestion/pulse/scoring.py#L161-L174) | Negative-centroid cosine penalty multiplier |
| `recommendation.enabled` | `True` (implicit) | (none — bool coerced) | [recommender.py:141-142](../../services/paper_ingestion/paper_ingestion/ingestion/recommender.py#L141-L142) | Gates `refresh_recommendations()` |
| `recommendation.liked_weight` | (default in recommender) | (none) | [recommender.py:135-136](../../services/paper_ingestion/paper_ingestion/ingestion/recommender.py#L135-L136) | Weighted score: `liked_s * weight` |
| `recommendation.project_weight` | (default in recommender) | (none) | [recommender.py:138-139](../../services/paper_ingestion/paper_ingestion/ingestion/recommender.py#L138-L139) | Weighted score: `proj_s * weight` |
| `user.timezone` | `"UTC"` ([init.sql:48](../../db/init.sql#L48)) | (none) | telegram_bot scheduler (cron timezone) | On write, [settings.py:432-434](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L432-L434) calls `_reload_telegram_nudges()` (best-effort POST) |
| `setup.completed` | (absent → false) | `_validate_bool` | [routers/system.py:144-149](../../services/paper_ingestion/paper_ingestion/routers/system.py#L144-L149) | Drives setup-wizard gate |
| `telegram.owner_chat_id` | (none) | `_validate_optional_int` | telegram_bot/owner.py:48-51, helpers.py:65, system_commands.py:73 + 95-110 | Pairing flow writes; bot resolves owner via this row |
| `zotero.api_key` | (none) | `_validate_nonempty_str` | [zotero_service.py:38, 87](../../services/paper_ingestion/paper_ingestion/integrations/zotero_service.py#L38) (encrypted; decrypted on read) | Encrypted: stored in `encrypted_value` BYTEA, plaintext NULL'd |
| `zotero.user_id` | (none) | `_validate_nonempty_str` | [zotero_service.py:88](../../services/paper_ingestion/paper_ingestion/integrations/zotero_service.py#L88) | |
| `zotero.library_type` | `"user"` (implicit on push) | `_validate_library_type` (`"user"` / `"group"`) | [zotero_service.py:89](../../services/paper_ingestion/paper_ingestion/integrations/zotero_service.py#L89) | |
| `zotero.poll_enabled` | `False` | `_validate_bool` | [scheduler.py:98](../../services/paper_ingestion/paper_ingestion/scheduler.py#L98) | Gates the `zotero_library_sync` cron job |
| `zotero.poll_cron` | (no default; cron string when set) | `_validate_zotero_cron` | [scheduler.py:103](../../services/paper_ingestion/paper_ingestion/scheduler.py#L103) | On write, [settings.py:419-431](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L419-L431) reschedules `zotero_library_sync` job |

### 2.2 PARTIAL keys (consulted only at startup, only on a non-core endpoint, or pushed elsewhere on write)

| Key | Default | How / where consumed | Why it is PARTIAL |
|---|---|---|---|
| `llm.smart_model` | `"smart"` (init.sql seed) | On write: [settings.py:357-367](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L357-L367) calls `update_litellm_model` ([litellm_config.py:110-249](../../services/paper_ingestion/paper_ingestion/services/litellm_config.py#L110-L249)) which rewrites `litellm_config/config.yaml` (or POSTs `/config/update` for cloud) | Runtime authority is LiteLLM's config file, not `user_config`. The `user_config` row exists for the UI's read-back display. If the YAML mount is `:ro` (SEC-002) the write raises `RuntimeError` and 400 propagates. |
| `llm.fast_model` | `"fast"` | Same as above | Same |
| `llm.embed_model` | `"embed"` | Same as above | Same |
| `llm.anthropic.api_key` | (none) | Encrypted. Read by [litellm_config.py:get_provider_api_key](../../services/paper_ingestion/paper_ingestion/services/litellm_config.py#L52-L83) when a cloud model alias is selected; also read by `POST /api/providers/anthropic/test` ([settings.py:607-665](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L607-L665)) | Only consumed if the model alias is set to `anthropic/...` AND a cloud-provider POST is in flight; otherwise dormant |
| `llm.openai.api_key` | (none) | Same pattern | Same |
| `llm.google.api_key` | (none) | Same pattern | Same |
| `fsrs.desired_retention` | `0.9` ([init.sql:49](../../db/init.sql#L49)) | Read **once** at startup at [learning_engine/main.py:70-85](../../services/learning_engine/learning_engine/main.py#L70-L85); cached on `app.state._fsrs_desired_retention`. **Live updates do not propagate until the service restarts.** | Stale until restart |
| `zotero.enabled` | `False` (default in scheduler.py:97) | Read at [scheduler.py:97](../../services/paper_ingestion/paper_ingestion/scheduler.py#L97) — gates `zotero_library_sync` job registration | **Anomaly:** read by code, but **not in `_ALLOWED_CONFIG_KEYS`** — cannot be set through `PUT /api/config/{key}`. Effectively unreachable from the UI; defaults to False forever unless seeded by a manual SQL insert. Flag for cleanup. |

### 2.3 GHOST keys (allowed by API; no consumer reads them anywhere in services/ or libs/)

| Key | Default seed | Validator | Status verdict |
|---|---|---|---|
| `paper.max_daily` | 20 ([init.sql:51](../../db/init.sql#L51)) | (none) | GHOST. No code reads this key. Seed exists; no daily-fetch cap is enforced. |
| `paper.auto_generate_cards` | `true` ([init.sql:52](../../db/init.sql#L52)) | (none) | GHOST. No code reads this key. FSRS card generation in `learning_engine` is unconditional today. |
| `fsrs.learning_steps` | `[1, 10]` ([init.sql:50](../../db/init.sql#L50)) | (none) | GHOST. No code reads this key. FSRS uses hard-coded steps. |
| `ui.page_size` | (no seed) | (none) | GHOST. No code reads this key. Frontend uses local React state for pagination. |
| `ingestion.max_papers_per_run` | (no seed) | (none) | GHOST. No code reads this key. Ingesters use hard-coded batch sizes. |
| `ingestion.chunk_size` | (no seed) | (none) | GHOST. No code reads this key. Chunking logic uses hard-coded ~1024-token windows. |
| `zotero.auto_push_on_star` | (no seed) | `_validate_bool` | GHOST. The starring code path (`paper_user_state.starred`) does not consult this key. |

**Verification protocol used:** for each key above, ran `rg "<key>" services/ libs/ scripts/`. The only matches were in `routers/settings.py` (allowed-keys frozenset + validator). No consumer found.

---

## 3. Per-table settings (typed tables)

These are not key/value rows but column-level settings the UI writes through
typed REST endpoints.

### 3.1 `topics`

| Column | UI write endpoint | Consumer |
|---|---|---|
| `name` | `POST/PUT /api/topics` | Topic seeding for Pulse search; `pulse/profile.py` topic centroids |
| `query_terms` (TEXT[]) | Same | Source plugin queries |
| `category` | Same | Display only |
| `description` | Same | Pulse Stage-2 prompt context (`build_scoring_prompt`) |
| `enabled` | `PUT /api/topics/{id}` | `pulse/profile.py` filters disabled topics from the active set |

### 3.2 `tracked_authors`

| Column | UI write endpoint | Consumer |
|---|---|---|
| `author_name` | `POST/PUT /api/authors` | Stage-1 `author_bonus` lookup ([scoring.py:184-192](../../services/paper_ingestion/paper_ingestion/pulse/scoring.py#L184-L192) — `tracked_author_names` set) |
| `s2_author_id` | Same | Stage-1 alternate lookup via Semantic Scholar ID set |
| `enabled` | `PUT /api/authors/{id}` | Filter applied in profile.py author-set construction |
| `source` | (set by code: `manual` / `auto_starred` / `auto_rated`) | Display only; auto-detect endpoints set it |

### 3.3 `paper_sources`

| Column | UI write endpoint | Consumer |
|---|---|---|
| `enabled` | `PUT /api/sources/{id}` | `pulse/discovery.py` skips disabled sources during candidate fetch |
| `priority` | Same | Sort key for source dispatch |
| `config` (JSONB) | Same | Per-source plugin configuration (e.g., `requires_key`, `mailto`) |
| `display_order` | `PATCH /api/sources/reorder` ([settings.py:509-533](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L509-L533)) | Source list UI ordering |

### 3.4 `scheduled_nudges`

| Column | UI write endpoint | Consumer |
|---|---|---|
| `enabled` | `PUT /api/nudges/{id}` | telegram_bot scheduler |
| `cron_expression` | Same | telegram_bot APScheduler trigger; on write, [settings.py:486](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L486) calls `_reload_telegram_nudges()` |

### 3.5 `extraction_templates`

| Column | UI write endpoint | Consumer |
|---|---|---|
| `name`, `description`, `fields` (JSONB) | `POST/PUT /api/extraction-templates` | [extraction/core.py:97-110](../../services/paper_ingestion/paper_ingestion/extraction/core.py#L97-L110) loads template by id and feeds `fields` into the LLM prompt |
| `is_default` | Same | Default-template selection logic |

---

## 4. Validators and constraints

Defined in `_CONFIG_VALIDATORS` ([settings.py:230-254](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L230-L254)). A `PUT /api/config/{key}` with no entry in this dict accepts any JSONB value once the key passes the `_ALLOWED_CONFIG_KEYS` allowlist.

| Validator | Applied to | Rule |
|---|---|---|
| `_validate_cron` | `pulse.cron` | Must parse via `CronTrigger.from_crontab`. Sub-hourly schedules rejected (Pulse runs are expensive) |
| `_validate_pulse_weights` | `pulse.weights` | Dict; required keys: `embedding`, `topic`, `llm_relevance`, `llm_novelty`, `author_bonus`, `recency`. Optional: `citation_pagerank`, `citation_count`, `citation_adamic_adar`, `classifier`. Each value ∈ [0,1] |
| `_validate_positive_int` | `pulse.deck_size`, `pulse.stage2_top_k` | Strict positive int |
| `_validate_l2_lambda` | `pulse.l2_lambda` | Number in `[0.0, 2.0]`. Excludes bool. |
| `_validate_bool` | `pulse.enabled`, `setup.completed`, `zotero.poll_enabled`, `zotero.auto_push_on_star` | Strict bool |
| `_validate_optional_int` | `telegram.owner_chat_id` | int or null |
| `_validate_nonempty_str` | `llm.{smart,fast,embed}_model`, `zotero.api_key`, `zotero.user_id`, `llm.{anthropic,openai,google}.api_key` | Non-empty trimmed string |
| `_validate_library_type` | `zotero.library_type` | `"user"` or `"group"` |
| `_validate_zotero_cron` | `zotero.poll_cron` | Must parse via `CronTrigger.from_crontab` (no sub-hourly limit, unlike Pulse) |

Keys without validators (per `_CONFIG_VALIDATORS`): `ui.page_size`, `ingestion.max_papers_per_run`, `ingestion.chunk_size`, `paper.max_daily`, `paper.auto_generate_cards`, `fsrs.learning_steps`, `fsrs.desired_retention`, `recommendation.{liked_weight,project_weight,enabled}`, `user.timezone`. (The unvalidated set is largely the GHOST set — coincidence, not contract; the contract should bind the validator on any key promoted from GHOST to LIVE.)

---

## 5. Side-effects on update

Some keys trigger work beyond the row write:

| Key(s) | Side effect on PUT |
|---|---|
| `llm.{smart,fast,embed}_model` | `update_litellm_model` rewrites `litellm_config/config.yaml` or POSTs `/config/update`; then `reload_litellm()` ([settings.py:357-367](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L357-L367)). Fails 400 if mount is read-only (SEC-002) |
| `pulse.cron` | `scheduler.reschedule_job("pulse_overnight", trigger=...)` + bounds check + DB rollback if `next_run_time` is invalid ([settings.py:368-411](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L368-L411)) |
| `zotero.poll_cron` | `scheduler.reschedule_job("zotero_library_sync", trigger=...)` (best-effort; if job not yet registered, warning only) ([settings.py:419-431](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L419-L431)) |
| `user.timezone` | Best-effort `POST /internal/reload-nudges` to telegram_bot ([settings.py:432-434](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L432-L434)) |
| Any `_ENCRYPTED_KEYS` member | `value` column NULL'd; ciphertext written to `encrypted_value` BYTEA via `encrypt_secret` ([settings.py:337-348](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L337-L348)) |
| `_NUDGE_JSONB_COLUMNS` member (none today) | `dynamic_update` would JSON-encode (currently empty set) |
| `_SOURCE_JSONB_COLUMNS` `"config"` | `dynamic_update` JSON-encodes the value before write ([settings.py:114](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L114) + dynamic_update path) |

GET responses for `_ENCRYPTED_KEYS` return `mask_secret(plaintext)`; GET for `_SECRET_KEYS \ _ENCRYPTED_KEYS` returns `"****"` if non-null. Plaintext **never** leaves the API.

---

## 6. Failure modes

| Failure | What happens | Where the error reaches |
|---|---|---|
| Unknown key on PUT | HTTP 400, `"Unknown config key: '<key>'"` | [settings.py:319](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L319) |
| Validator raises `ValueError` | HTTP 400 with message | [settings.py:323-325](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L323-L325) |
| LiteLLM YAML read-only (SEC-002) | HTTP 400 — write rejected; user_config row NOT updated | `update_litellm_model` → `RuntimeError` → caught at [settings.py:363-365](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L363-L365) |
| Pulse cron produces invalid `next_run_time` | HTTP 400; DB row rolled back to old value; live trigger reverted | [settings.py:383-411](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L383-L411) |
| Encrypted-secret decrypt fails on GET | NULL returned (read fails closed) | `_resolve_config_value` |
| Unknown nudge id / source id | HTTP 404 | [settings.py:464](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L464), [settings.py:547](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L547) |
| Zotero `LIKE 'zotero.%'` row decrypt fails | Whole config treated as missing → caller skips operation gracefully | [zotero_service.py:46-53](../../services/paper_ingestion/paper_ingestion/integrations/zotero_service.py#L46-L53) |

---

## 7. Invariants

The implementation MUST satisfy these. Testable.

1. **Allow-list closure.** Every key in `_CONFIG_VALIDATORS` MUST also be in `_ALLOWED_CONFIG_KEYS`. (Reverse not required — a key may be in the allow-list without a custom validator.)
2. **Encrypted closure.** Every key in `_ENCRYPTED_KEYS` MUST also be in `_SECRET_KEYS` (encryption implies secret handling on GET).
3. **GET masking.** No GET endpoint may return plaintext for any key in `_SECRET_KEYS`. Verifier: `mask_secret` or `"****"` is the only path returning a non-None value for a secret key.
4. **JSONB no double-encode.** Plain (non-encrypted) keys MUST be written via the `$2::jsonb` parametric cast WITHOUT `json.dumps()`. The asyncpg JSONB codec handles serialization. (See [ENGINEERING_STANDARDS.md "Database"](../ENGINEERING_STANDARDS.md#L62-L70).)
5. **Live cron reschedule preconditions.** A successful `PUT pulse.cron` MUST result in `next_run_time ∈ [now, now + 366 days]` or the write is rolled back.
6. **No raw env reads at request time** for keys that are user-controllable. (Bootstrap-only env reads — `JARVIS_API_KEY`, `LITELLM_BASE_URL` — are exempt.)

---

## 8. Status table (canonical roll-up)

| Key | Status |
|---|---|
| `pulse.enabled` | LIVE |
| `pulse.cron` | LIVE |
| `pulse.deck_size` | LIVE |
| `pulse.stage2_top_k` | LIVE |
| `pulse.weights` | LIVE (4 of 10 weights are CONDITIONAL — see [02-pulse.md §3.2](02-pulse.md#32-conditional-signals-live-conditional)) |
| `pulse.l2_lambda` | LIVE |
| `recommendation.enabled` | LIVE |
| `recommendation.liked_weight` | LIVE |
| `recommendation.project_weight` | LIVE |
| `user.timezone` | LIVE |
| `setup.completed` | LIVE |
| `telegram.owner_chat_id` | LIVE |
| `zotero.api_key` | LIVE (encrypted) |
| `zotero.user_id` | LIVE |
| `zotero.library_type` | LIVE |
| `zotero.poll_enabled` | LIVE |
| `zotero.poll_cron` | LIVE |
| `llm.smart_model` | PARTIAL (LiteLLM YAML is the runtime authority) |
| `llm.fast_model` | PARTIAL (same) |
| `llm.embed_model` | PARTIAL (same) |
| `llm.anthropic.api_key` | PARTIAL (used only when model alias is `anthropic/*` or for `/test` endpoint) |
| `llm.openai.api_key` | PARTIAL |
| `llm.google.api_key` | PARTIAL |
| `fsrs.desired_retention` | PARTIAL (read at startup only; not refreshed live) |
| `zotero.enabled` | ANOMALY (read by scheduler.py but NOT in `_ALLOWED_CONFIG_KEYS` — unreachable from API) |
| `paper.max_daily` | GHOST |
| `paper.auto_generate_cards` | GHOST |
| `fsrs.learning_steps` | GHOST |
| `ui.page_size` | GHOST |
| `ingestion.max_papers_per_run` | GHOST |
| `ingestion.chunk_size` | GHOST |
| `zotero.auto_push_on_star` | GHOST |

---

## 9. Cleanup decisions deferred

This contract documents status; the **implementation plan** picks dispositions.
For each non-LIVE entry above, the candidate dispositions are:

| Item | Candidate dispositions |
|---|---|
| `paper.max_daily` | (a) WIRE-IT (enforce in `pulse/discovery.py` per-source caps); (b) DELETE-IT |
| `paper.auto_generate_cards` | (a) WIRE-IT (gate auto-card generation in `learning_engine`); (b) DELETE-IT |
| `fsrs.learning_steps` | (a) WIRE-IT (FSRS scheduler reads steps from this row); (b) DELETE-IT |
| `ui.page_size` | (a) WIRE-IT (frontend reads as default page-size); (b) DELETE-IT (truly UI-local state) |
| `ingestion.max_papers_per_run` | (a) WIRE-IT in source plugins; (b) DELETE-IT |
| `ingestion.chunk_size` | (a) WIRE-IT in chunker; (b) DELETE-IT |
| `zotero.auto_push_on_star` | (a) WIRE-IT in star handler (push to Zotero on `starred=TRUE` transition); (b) DELETE-IT |
| `fsrs.desired_retention` PARTIAL → LIVE | Re-read on every FSRS review or via a live config watcher. Defers a pyright/refactor cost. |
| `zotero.enabled` ANOMALY | (a) Add to `_ALLOWED_CONFIG_KEYS` so the UI can write it; (b) Replace consumer with `zotero.poll_enabled` (the existing API-writeable equivalent) and remove `zotero.enabled` from scheduler.py |
| `llm.{anthropic,openai,google}.api_key` PARTIAL | Document in 03-llm.md that these are conditional secrets — no contract violation |
| `llm.{smart,fast,embed}_model` PARTIAL | Status quo is acceptable; the LiteLLM YAML is a deliberate split. Documented in 03-llm.md. |

This table does **not** prescribe which option is chosen — that's the
implementation plan's call. The contract's job is to make the choice visible.

---

## 10. Cross-contract references

- **[02-pulse.md](02-pulse.md) §3** — the four GHOST weights inside `pulse.weights` (`citation_pagerank`, `citation_count`, `citation_adamic_adar`, `classifier`) — UI-exposed, validator-accepted, but no signal computation populates them.
- **[03-llm.md](03-llm.md) §2** — `llm.{smart,fast,embed}_model` and the cloud-provider keys behave at the LiteLLM layer; this contract documents only the `user_config` storage plane.
- **[04-observability.md](04-observability.md)** — privacy rules forbid logging raw `user_config.value` for any key in `_SECRET_KEYS` / `_ENCRYPTED_KEYS`.
- **[docs/specs/2026-04-29-paper-lifecycle-redesign.md](../specs/2026-04-29-paper-lifecycle-redesign.md)** — `paper_user_state` columns (state, starred, state_before_trash) are NOT in this contract; they are per-paper user state, not user-controllable settings.

---

## 11. Verified Identifiers

Every cited identifier was Read in the session producing this contract.
Future agents who edit this file MUST re-Read before re-citing.

| Citation | File:line | One-line behavior |
|---|---|---|
| `_ALLOWED_CONFIG_KEYS` frozenset | services/paper_ingestion/paper_ingestion/routers/settings.py:49-90 | Allow-list of writeable user_config keys (29 keys, including 7 GHOST and 7 PARTIAL) |
| `_SECRET_KEYS` / `_ENCRYPTED_KEYS` | services/paper_ingestion/paper_ingestion/routers/settings.py:92-108 | Keys that get masked on GET; subset gets ciphertext on PUT |
| `_CONFIG_VALIDATORS` | services/paper_ingestion/paper_ingestion/routers/settings.py:230-254 | Per-key validator dispatch; missing entry = no validation |
| `_PULSE_WEIGHT_KEYS` / `_PULSE_REQUIRED_WEIGHT_KEYS` | services/paper_ingestion/paper_ingestion/routers/settings.py:119-135 | The 10 allowed weight keys; 6 are required |
| `_validate_pulse_weights` | services/paper_ingestion/paper_ingestion/routers/settings.py:153-164 | Enforces shape + value range on `pulse.weights` |
| `_validate_cron` | services/paper_ingestion/paper_ingestion/routers/settings.py:138-150 | Parses cron; rejects sub-hourly |
| `_validate_l2_lambda` | services/paper_ingestion/paper_ingestion/routers/settings.py:184-193 | Range [0.0, 2.0]; rejects bool |
| `set_config` PUT handler | services/paper_ingestion/paper_ingestion/routers/settings.py:309-436 | Allow-list + validator + encrypted-vs-plain write + side-effect dispatch |
| Pulse cron rollback path | services/paper_ingestion/paper_ingestion/routers/settings.py:368-411 | Reschedules live job; bounds-checks; DB+scheduler rollback on invalid next_run_time |
| Zotero cron live reschedule | services/paper_ingestion/paper_ingestion/routers/settings.py:419-431 | Best-effort job reschedule on poll_cron PUT |
| Telegram nudge reload | services/paper_ingestion/paper_ingestion/routers/settings.py:206-218 | POST `/internal/reload-nudges` to telegram_bot |
| `update_litellm_model` | services/paper_ingestion/paper_ingestion/services/litellm_config.py:110-249 | Rewrites litellm_config/config.yaml OR POSTs /config/update; raises RuntimeError if mount :ro |
| `get_provider_api_key` | services/paper_ingestion/paper_ingestion/services/litellm_config.py:52-83 | Decrypts cloud-provider key from user_config |
| `_get_zotero_config` | services/paper_ingestion/paper_ingestion/integrations/zotero_service.py:24-59 | `LIKE 'zotero.%'` SELECT; decrypts `encrypted_value`; returns short-keyed dict |
| Zotero push consumer reads | services/paper_ingestion/paper_ingestion/integrations/zotero_service.py:82-94 | Consumes `enabled`, `api_key`, `user_id`, `library_type` from the dict |
| `pulse.weights` load + clamp | services/paper_ingestion/paper_ingestion/pulse/profile.py:166-194 | Loads from user_config, merges with `_DEFAULT_WEIGHTS`, clamps to [0,1] |
| `_read_weights` (recommendation.*) | services/paper_ingestion/paper_ingestion/ingestion/recommender.py:126-143 | Reads liked/project weights + enabled flag |
| `_get_zotero_poll_config` (zotero.enabled + poll_enabled + poll_cron) | services/paper_ingestion/paper_ingestion/scheduler.py:86-112 | Reads three keys; gates Zotero job registration; `zotero.enabled` is the orphan |
| `app.state._fsrs_desired_retention` startup cache | services/learning_engine/learning_engine/main.py:70-85 | Reads `fsrs.desired_retention` once; never refreshed |
| `setup_completed` resolution | services/paper_ingestion/paper_ingestion/routers/system.py:144-149 | Reads `setup.completed`; gates wizard |
| Telegram owner_chat_id resolution | services/telegram_bot/telegram_bot/owner.py:48-51, helpers.py:65, system_commands.py:73 + 95-110 | Resolver, fallback, pairing-write paths |
| `user_config` table schema | db/init.sql:33-42 | `key UNIQUE`, `value JSONB`, `encrypted_value BYTEA` (added M033), updated_at |
| `user_config` seeds | db/init.sql:43-53 | 8 seeded keys including 4 GHOST defaults |
| `paper_sources` table + seeds | db/init.sql:71-88 | 3 sources seeded; arxiv enabled; semantic_scholar + local disabled |
| `topics` table | db/init.sql:90-97 | name, query_terms, category, enabled |
