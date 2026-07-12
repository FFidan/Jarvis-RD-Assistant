# 01 — Settings Contract
**Status:** LIVING
**Reviewers must update this contract in the same patch as any change to:**
- `_ALLOWED_CONFIG_KEYS` in [config_metadata.py](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_metadata.py) or `_CONFIG_VALIDATORS` in [config_validators.py](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_validators.py)
- DDL on `user_config`, `topics`, `tracked_authors`, `paper_sources`, `scheduled_nudges`
- Any new code reading `user_config.value` in any service

The `settings_service` package is split into submodules
([settings_service.py](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/settings_service.py)
re-exports them): `config_metadata` (allow-list + key classification),
`config_validators` (validators), `config_db` (row I/O), `config_write`
(write orchestration), `model_assignment`, `scheduler_effects`, `provider_test`,
`analytics_queries`, `data_export`.

---

## 0. What this contract covers (and what it does NOT)

**In scope.** Every user-controllable setting that backs a control in the
frontend Settings UI or shapes runtime behavior. Storage shape, validation,
who writes it, who reads it, and current wiring status (Active / Partial / Unwired).

**Out of scope.**
- Per-paper user state (`paper_user_state` columns) — covered by the
  paper-lifecycle redesign spec.
- Pomodoro timer settings (`usePomodoroStore` Zustand) — UI-local only,
  never persisted server-side.
- Authentication (`JARVIS_API_KEY` from Docker Secret / env fallback /
  `auth-store`) — bootstrap, not user-controllable.
- HTTP request/response Pydantic shapes — covered by service-local models,
  not this contract.

---

## 1. Storage tables

The Settings UI writes to **two physical surfaces**: `user_config` (key/value
JSONB store) and a small set of typed tables for entities that have their
own row-level state.

| Table | Source-of-truth migration | Purpose |
|---|---|---|
| `user_config` | [db/init.sql:33-48](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/db/init.sql#L33-L48); `user_id` scope added by migration 073 | Key/value store for system defaults (`user_id IS NULL`) and per-user overrides (`user_id=<users.id>`). JSON values for plain settings; ciphertext bytes for encrypted secrets. Browser callers read/write personal keys in their own scope with fallback to system rows; system keys stay admin-only and system-scoped. |
| `topics` | [db/init.sql:90-97](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/db/init.sql#L90-L97); `description TEXT` added by migration 018 | User-defined research topics. `enabled` filters which topics feed Pulse + recommendation. |
| `tracked_authors` | `db/init.sql` (post-merger schema); `source` column tracks `manual / auto_starred / auto_rated` | Authors the user wants alerts for. `enabled` filters which feed Stage-1 `author_bonus`. |
| `paper_sources` | [db/init.sql:71-88](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/db/init.sql#L71-L88); `display_order` added by migration 023 | Pluggable source registry (arXiv, Semantic Scholar, OpenAlex, PubMed, Local). `enabled` toggles ingestion. `config` JSONB holds per-source settings (e.g. `requires_key`). |
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
must appear in `_ALLOWED_CONFIG_KEYS` ([config_metadata.py:34-85](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_metadata.py#L34-L85)). Status reflects whether the value is consulted by runtime
behavior somewhere.

Scope is part of the contract:

- `PERSONAL_KEYS` are stored as `(user_id, key)` when a browser session has a
  real `user_id`; API-key-only callers use the legacy system/default row.
- `SYSTEM_KEYS` and dynamic hardware keys always use `user_id IS NULL` and
  require admin role for browser sessions.
- `GET /api/config` hides system rows from non-admin browser users; `GET
  /api/config/{key}` applies the same server-side role check.

**`telegram.bot_token` is intentionally absent from `_ALLOWED_CONFIG_KEYS`.** It is written
Fernet-encrypted via the dedicated `POST /api/setup/telegram-bot-token` endpoint, which applies
additional validation (token format check, Telegram API reachability test) before persisting.
Routing a bot token through the general `PUT /api/config/{key}` surface would bypass that
validation and is an intentional non-regression: the omission is by design, not a gap.

### 2.1 Active keys (written and read by code that affects user-visible behavior)

| Key | Default | Validator | Read sites | Notes |
|---|---|---|---|---|
| `pulse.enabled` | (none) | `_validate_bool` | scheduler.py — gates whether the `pulse_overnight` job runs | Master switch for Pulse subsystem |
| `pulse.cron` | `"0 4 * * *"` | `_validate_cron` (rejects sub-hourly) | scheduler.py | On write, `apply_pulse_cron` ([scheduler_effects.py:53](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/scheduler_effects.py#L53)) reschedules the live job AND validates `next_run_time ∈ [now, now+366d]`; rolls back on failure |
| `pulse.deck_size` | `_DEFAULT_DECK_SIZE` (10) | `_validate_positive_int` | [profile.py:230](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L230) | Final card count |
| `pulse.stage2_top_k` | `_DEFAULT_STAGE2_TOP_K` (40) | `_validate_positive_int` | [profile.py:231](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L231) | Stage-1 cut feeding Stage-2 LLM rerank |
| `pulse.weights` | `_DEFAULT_WEIGHTS` (in profile.py) | `_validate_pulse_weights` (requires 6 core keys; permits 4 optional; values ∈ [0,1]) | [profile.py:221-231](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L221-L231) merge-with-defaults; clamped to `[0,1]` (out-of-range logged) | Carries the 10 weight sliders. **4 are CONDITIONAL** (default 0.0; require user opt-in AND optional dep) — see [02-pulse.md §3.2](02-pulse.md#32-conditional-signals-live-conditional) |
| `pulse.l2_lambda` | 0.5 | `_validate_l2_lambda` (range `[0.0, 2.0]`) | [profile.py:236-237](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L236-L237) | Negative-centroid cosine penalty multiplier |
| `pulse.lookback_days` | 7 | `_validate_lookback_days` (int in `[1, 90]`) | [profile.py:239-240](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L239-L240) | Discovery window: how far back Stage 1 looks for candidate papers |
| `pulse.startup_grace_seconds` | 0.0 | `_validate_startup_grace_seconds` (float in `[0, 300]`) | [profile.py:241-242](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L241-L242) | Warmup pause before the first outbound HTTP burst |
| `recommendation.enabled` | `True` (implicit) | (none — bool coerced) | [recommender.py:141-142](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/ingestion/recommender.py#L141-L142) | Gates `refresh_recommendations()` |
| `recommendation.liked_weight` | (default in recommender) | (none) | [recommender.py:135-136](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/ingestion/recommender.py#L135-L136) | Weighted score: `liked_s * weight` |
| `recommendation.project_weight` | (default in recommender) | (none) | [recommender.py:138-139](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/ingestion/recommender.py#L138-L139) | Weighted score: `proj_s * weight` |
| `user.timezone` | `"UTC"` ([init.sql:48](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/db/init.sql#L48)) | (none) | telegram_bot scheduler (cron timezone) | On write, calls `reload_telegram_nudges()` ([model_assignment.py:21](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/model_assignment.py#L21)) — best-effort POST |
| `setup.completed` | (absent → false) | `_validate_bool` | [routers/system.py:144-149](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/routers/system.py#L144-L149) | Drives setup-wizard gate |
| `telegram.owner_chat_id` | (none) | `_validate_optional_int` | telegram_bot/owner.py:48-51, helpers.py:65, system_commands.py:73 + 95-110 | Pairing flow writes; bot resolves owner via this row |
| `zotero.api_key` | (none) | `_validate_nonempty_str` | [zotero_service.py:38, 87](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/integrations/zotero_service.py#L38) (encrypted; decrypted on read) | Encrypted: stored in `encrypted_value` BYTEA, plaintext NULL'd |
| `zotero.user_id` | (none) | `_validate_nonempty_str` | [zotero_service.py:88](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/integrations/zotero_service.py#L88) | |
| `zotero.library_type` | `"user"` (implicit on push) | `_validate_library_type` (`"user"` / `"group"`) | [zotero_service.py:89](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/integrations/zotero_service.py#L89) | |
| `zotero.group_id` | (none) | `_validate_group_id` (positive int or null) | `zotero_service.py` (consumed when `library_type="group"`) | `null` clears the field. `ZoteroClient` requires a non-null id at construction time when `library_type="group"`. |
| `zotero.poll_enabled` | `False` | `_validate_bool` | `scheduler.py` per-user polling readiness | Per-user gate for scheduled Zotero polling. The scheduler job itself always runs; per-run fan-out only includes users with `poll_enabled=true` and usable Zotero credentials. |
| `zotero.poll_cron` | (no default; cron string when set) | `_validate_zotero_cron` | [scheduler.py:103](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/scheduler.py#L103) | On write, `apply_zotero_cron` ([scheduler_effects.py:113](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/scheduler_effects.py#L113)) reschedules the `zotero_library_sync` job |
| `zotero.auto_push_on_star` | `False` (key absent → off) | `_validate_bool` | star handler in [routers/papers.py](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/routers/papers.py) — on `starred=False→True` transition AND key truthy AND project link present, enqueues existing `zotero.push` job | Default-off; idempotent on already-starred state. |
| `fsrs.desired_retention` | `0.9` ([init.sql:49](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/db/init.sql#L49)) | `_validate_fsrs_retention` (range `(0, 1)`) | Per-review fetch in `_build_fsrs_manager_from_db` ([learning_engine/routers/review.py](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/learning_engine/learning_engine/routers/review.py)) — fresh `FSRSManager` built inside the review transaction | Live-edit reactive |
| `fsrs.learning_steps` | `[1, 10]` ([init.sql:50](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/db/init.sql#L50)) | `_validate_fsrs_learning_steps` (`list[int]`, length 1–10, all positive) | Per-review fetch in `_build_fsrs_manager_from_db`; passed to fsrs `Scheduler(learning_steps=[timedelta(minutes=m) for m in steps])` | Default `[1, 10]` matches the fsrs library default |

### 2.2 Partial keys (consulted only at startup, only on a non-core endpoint, or pushed elsewhere on write)

| Key | Default | How / where consumed | Why it is Partial |
|---|---|---|---|
| `llm.smart_model` | (no seed — first boot auto-configures a best-fit model id; the reconciler falls back to `JARVIS_SMART_MODEL` env, then `qwen3:8b`) | On write: calls `update_litellm_model` ([litellm_config.py](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/litellm_config.py)) which delivers via LiteLLM's admin DB — `GET /v1/model/info`, `POST /model/new`, `POST /model/delete` (the YAML carries no smart/fast aliases and is never written) | Runtime authority is LiteLLM's admin DB, not `user_config`. The `user_config` row is the desired state the persistent ~30 s reconciler converges LiteLLM onto (plus the UI's read-back display). On delivery failure the role is marked in `llm.delivery_pending` and the reconciler keeps retrying. See §9. |
| `llm.fast_model` | (no seed — same auto-configure; env fallback `JARVIS_FAST_MODEL`, then `qwen3:4b`) | Same as above | Same |
| `llm.embed_model` | (no seed) | Dimension-locked: the `embed` alias stays YAML-seeded; `update_litellm_model` refuses any runtime re-route (re-selecting the routed model is a no-op). Switching embedders = edit `litellm/config.yaml` + re-embed | Runtime authority for `embed` is the YAML, by design (a DB copy would stack with the YAML deployment). See §9. |
| `llm.anthropic.api_key` | (none) | Encrypted deployment-wide admin credential. Read by [litellm_config.py:get_provider_api_key](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/litellm_config.py) when an `anthropic/...` model alias is selected; also read by `POST /api/providers/anthropic/test`. | Conditional cloud-provider credential. Stored as a system row (`user_id IS NULL`), admin-only, and dormant unless a matching cloud model is assigned or tested. |
| `llm.openai.api_key` | (none) | Same deployment-wide encrypted pattern for `openai/...` aliases. | Same |
| `llm.google.api_key` | (none) | Same deployment-wide encrypted pattern for `gemini/...` aliases. | Same |
| `llm.providers.<provider>.api_key` | (none) | Registry-derived deployment-wide encrypted keys for `openrouter`, `deepseek`, `mistral`, `moonshot`, `zai`, and `custom_openai_compatible`. | New provider integrations use namespaced system keys instead of adding more flat key names. |
| `llm.providers.custom_openai_compatible.base_url` | (none) | Registry-derived endpoint URL used only with the Custom OpenAI-compatible provider. | Validated before save; blocks credentials/fragments, invalid schemes, and unsafe IP ranges. Stored as an encrypted system row with the provider settings. |

### 2.3 System-wide admin keys (one deployment-wide value, admin-only)

These keys are `SYSTEM_KEYS` (always `user_id IS NULL`, admin role required for browser sessions).
Some are collected by the onboarding wizard ([routers/setup.py](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/routers/setup.py)) and editable afterwards via `PUT /api/config/{key}`.

**`GET /api/setup/status` response shape** (pre-auth, no session required):

| Field | Type | Notes |
|---|---|---|
| `configured` | bool | True when ≥ 1 admin user exists. |
| `setup_completed` | bool | True when `setup.completed` user_config key is set to true. The unified onboarding gate keys on this field. |
| `setup_mode` | `"single" \| "multi"` | Deployment access mode. |
| `hw_tier_baseline` / `hw_tier_current` / `hw_tier_changed` | string / string / bool | Hardware tier at first-boot vs. current detected tier. |
| `recommended_backend` / `current_backend` / `observed_backend` | string | LLM backend tier recommendation and observation. |
| `observed_recent_share` | float | Recent share of traffic on the observed backend. |

| Key | Default | Validator | How / where consumed |
|---|---|---|---|
| `smtp.host` | (none) | `_validate_nonempty_str` | Outbound mail relay host. Written by the setup wizard; key names match `_SMTP_PLAINTEXT_KEYS` / `_SMTP_ENCRYPTED_KEYS` in `routers/setup.py`. |
| `smtp.port` | (none) | `_validate_positive_int` | Relay port. |
| `smtp.user` | (none) | `_validate_nonempty_str` | Relay auth user. |
| `smtp.from` | (none) | `_validate_nonempty_str` | Default From address. |
| `smtp.pass` | (none) | `_validate_nonempty_str` | Relay auth password. **Encrypted** (`_ENCRYPTED_KEYS`): stored as Fernet ciphertext in `encrypted_value`, masked on GET. |
| `smtp.reply_to` | (none — optional) | `_validate_optional_email` (empty string clears; valid email or absent) | Optional Reply-To address set in outgoing magic-link emails. Routes replies away from the From address. Written by the setup wizard and Settings → Email / SMTP; also readable from `SMTP_REPLY_TO` env at startup. Empty string clears the stored value. Added in v0.8.6. |
| `smtp.from_name` | (none — optional) | `_validate_optional_header_str` (empty string clears; ≤ 255 chars, no control chars) | Optional sender display name prepended to the From header (e.g. `JARVIS RD <login@your-domain.dev>`). Written by the setup wizard and Settings → Email / SMTP; also readable from `SMTP_FROM_NAME` env at startup. Empty string clears the stored value. Added in v0.8.6. |
| `automation.fetch_interval_hours` | (none) | `_validate_positive_int` | Interval for the system-wide auto-fetch pipeline scheduler. |
| `observability.langfuse_dashboard_url` | (none) | `_validate_langfuse_dashboard_url` (empty / `https://` / `http://localhost` / `http://127.0.0.1`) | The "Open Langfuse dashboard" link target in Settings → Observability. See [04-observability.md §8](04-observability.md#8-settings-ui-integration). |

### 2.4 Unwired keys (allowed by API; no consumer reads them)

**None currently.** No allow-listed key lacks a consumer. Any future Unwired→Active
promotion MUST add a validator at the same time, and SHOULD be backed by a
focused test that live-edits the value and asserts the new behavior appears
without a service restart.

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
| `author_name` | `POST/PUT /api/authors` | Stage-1 `author_bonus` lookup ([scoring.py:184-192](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L184-L192) — `tracked_author_names` set) |
| `s2_author_id` | Same | Stage-1 alternate lookup via Semantic Scholar ID set |
| `enabled` | `PUT /api/authors/{id}` | Filter applied in profile.py author-set construction |
| `source` | (set by code: `manual` / `auto_starred` / `auto_rated`) | Display only; auto-detect endpoints set it |

### 3.3 `paper_sources`

| Column | UI write endpoint | Consumer |
|---|---|---|
| `enabled` | `PUT /api/sources/{id}` | `pulse/discovery.py` skips disabled sources during candidate fetch |
| `priority` | Same | Sort key for source dispatch |
| `config` (JSONB) | Same | Per-source plugin configuration (e.g., `requires_key`, `mailto`) |
| `display_order` | `PATCH /api/sources/reorder` ([routers/settings.py:336-361](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/routers/settings.py#L336-L361)) | Source list UI ordering |

### 3.4 `scheduled_nudges`

| Column | UI write endpoint | Consumer |
|---|---|---|
| `enabled` | `PUT /api/nudges/{id}` | telegram_bot scheduler |
| `cron_expression` | Same | telegram_bot APScheduler trigger; on write, [routers/settings.py:315](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/routers/settings.py#L315) calls `reload_telegram_nudges()` |

### 3.5 `extraction_templates`

| Column | UI write endpoint | Consumer |
|---|---|---|
| `name`, `description`, `fields` (JSONB) | `POST/PUT /api/extraction-templates` | [extraction/core.py:97-110](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/extraction/core.py#L97-L110) loads template by id and feeds `fields` into the LLM prompt |
| `is_default` | Same | Default-template selection logic |

---

## 4. Validators and constraints

Defined in `_CONFIG_VALIDATORS` ([config_validators.py:231-271](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_validators.py#L231-L271)). A `PUT /api/config/{key}` with no entry in this dict accepts any JSONB value once the key passes the `_ALLOWED_CONFIG_KEYS` allowlist.

| Validator | Applied to | Rule |
|---|---|---|
| `_validate_cron` | `pulse.cron` | Must parse via `CronTrigger.from_crontab`. Sub-hourly schedules rejected (Pulse runs are expensive) |
| `_validate_pulse_weights` | `pulse.weights` | Dict; required keys: `embedding`, `topic`, `llm_relevance`, `llm_novelty`, `author_bonus`, `recency`. Optional: `citation_pagerank`, `citation_count`, `citation_adamic_adar`, `classifier`. Each value ∈ [0,1] |
| `_validate_positive_int` | `pulse.deck_size`, `pulse.stage2_top_k`, `automation.fetch_interval_hours`, `smtp.port` | Strict positive int |
| `_validate_l2_lambda` | `pulse.l2_lambda` | Number in `[0.0, 2.0]`. Excludes bool. |
| `_validate_lookback_days` | `pulse.lookback_days` | Int in `[1, 90]` |
| `_validate_startup_grace_seconds` | `pulse.startup_grace_seconds` | Number in `[0, 300]` |
| `_validate_bool` | `pulse.enabled`, `setup.completed`, `zotero.poll_enabled`, `zotero.auto_push_on_star` | Strict bool |
| `_validate_optional_int` | `telegram.owner_chat_id` | int or null |
| `_validate_nonempty_str` | `llm.{smart,fast,embed}_model`, `zotero.{api_key,user_id}`, `llm.{anthropic,openai,google}.api_key`, `smtp.{host,user,from,pass}` | Non-empty trimmed string |
| `_validate_optional_email` | `smtp.reply_to` | Empty string or `null` clears; otherwise must match `\S+@\S+\.\S+` with no control characters |
| `_validate_optional_header_str` | `smtp.from_name` | Empty string or `null` clears; otherwise ≤ 255 printable chars, no `\r`/`\n`/`\x00` |
| `_validate_library_type` | `zotero.library_type` | `"user"` or `"group"` |
| `_validate_group_id` | `zotero.group_id` | Positive int or null |
| `_validate_zotero_cron` | `zotero.poll_cron` | Must parse via `CronTrigger.from_crontab` (no sub-hourly limit, unlike Pulse) |
| `_validate_langfuse_dashboard_url` | `observability.langfuse_dashboard_url` | Empty / `https://` / loopback `http://` URL; rejects other schemes |
| `_validate_fsrs_retention` | `fsrs.desired_retention` | Number in open range `(0, 1)`; rejects bool, 0, 1, and out-of-range floats |
| `_validate_fsrs_learning_steps` | `fsrs.learning_steps` | `list[int]`, length 1–10, all strictly positive (minutes). Default `[1, 10]` matches the fsrs library default |

Keys without a custom validator (per `_CONFIG_VALIDATORS`):
`recommendation.{liked_weight,project_weight,enabled}`, `user.timezone`. Any
future Unwired→Active promotion MUST add a validator at the same time — that's the
contract.

---

## 5. Side-effects on update

Some keys trigger work beyond the row write:

| Key(s) | Side effect on PUT |
|---|---|
| `llm.{smart,fast,embed}_model` | `update_litellm_model` delivers via the LiteLLM admin DB: `GET /v1/model/info` to resolve the current deployment, `POST /model/new` for the replacement (tuned params carried), then `POST /model/delete` for the old DB row (`litellm_config.py`; YAML is never written — the model name is validated against injection). The `embed` alias is dimension-locked and refused at runtime. While LiteLLM runs DB-less the write commits with `delivery: "pending_restart"` and the boot reconciler re-delivers (see contract 05 §5.5) |
| `pulse.cron` | `scheduler.reschedule_job("pulse_overnight", trigger=...)` + bounds check + DB rollback if `next_run_time` is invalid (`apply_pulse_cron`, [scheduler_effects.py:53](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/scheduler_effects.py#L53)) |
| `zotero.poll_cron` | `scheduler.reschedule_job("zotero_library_sync", trigger=...)` (best-effort; if job not yet registered, warning only) (`apply_zotero_cron`, [scheduler_effects.py:113](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/scheduler_effects.py#L113)) |
| `user.timezone` | Best-effort `POST /internal/reload-nudges` to telegram_bot (`reload_telegram_nudges`, [model_assignment.py:21](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/model_assignment.py#L21)) |
| Any `_ENCRYPTED_KEYS` member | `value` column NULL'd; ciphertext written to `encrypted_value` BYTEA via `encrypt_secret`, in `write_config` ([config_write.py:132](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_write.py#L132)) |
| `_SOURCE_JSONB_COLUMNS` `"config"` | `dynamic_update` JSON-encodes the value before write |

GET responses for `_ENCRYPTED_KEYS` return `mask_secret(plaintext)`; GET for `_SECRET_KEYS \ _ENCRYPTED_KEYS` returns `"****"` if non-null. Plaintext **never** leaves the API.

---

## 6. Failure modes

| Failure | What happens | Where the error reaches |
|---|---|---|
| Unknown key on PUT | HTTP 400, `"Unknown config key: '<key>'"` | [routers/settings.py:210](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/routers/settings.py#L210) |
| Validator raises `ValueError` | HTTP 400 with message | `write_config` ([config_write.py:132](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_write.py#L132)) |
| LiteLLM delivery fails on PUT | HTTP 400 — write rejected, user_config row NOT updated (fail-closed). Carve-out: the "No DB Connected" degraded state commits the row, marks the role in `llm.delivery_pending`, and the ~30 s reconciler retries until LiteLLM recovers | `update_litellm_model` → `RuntimeError` → mapped in `write_config` |
| Pulse cron produces invalid `next_run_time` | HTTP 400; DB row rolled back to old value; live trigger reverted | `apply_pulse_cron` ([scheduler_effects.py:53](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/services/scheduler_effects.py#L53)) |
| Encrypted-secret decrypt fails on GET | NULL returned (read fails closed) | `_resolve_config_value` |
| Unknown nudge id / source id | HTTP 404 | [routers/settings.py:293](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/routers/settings.py#L293), [routers/settings.py:376](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/routers/settings.py#L376) |
| Zotero `LIKE 'zotero.%'` row decrypt fails | Whole config treated as missing → caller skips operation gracefully | [zotero_service.py:46-53](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/services/paper_ingestion/paper_ingestion/integrations/zotero_service.py#L46-L53) |

---

## 7. Invariants

The implementation MUST satisfy these. Testable.

1. **Allow-list closure.** Every key in `_CONFIG_VALIDATORS` MUST also be in `_ALLOWED_CONFIG_KEYS`. (Reverse not required — a key may be in the allow-list without a custom validator.)
2. **Encrypted closure.** Every key in `_ENCRYPTED_KEYS` MUST also be in `_SECRET_KEYS` (encryption implies secret handling on GET).
3. **GET masking.** No GET endpoint may return plaintext for any key in `_SECRET_KEYS`. Verifier: `mask_secret` or `"****"` is the only path returning a non-None value for a secret key.
4. **JSONB no double-encode.** Plain (non-encrypted) keys MUST be written via the `$2::jsonb` parametric cast WITHOUT `json.dumps()`. The asyncpg JSONB codec handles serialization. (See [ENGINEERING_STANDARDS.md "Database"](../ENGINEERING_STANDARDS.md#database).)
5. **Live cron reschedule preconditions.** A successful `PUT pulse.cron` MUST result in `next_run_time ∈ [now, now + 366 days]` or the write is rolled back.
6. **No raw env reads at request time** for keys that are user-controllable. Bootstrap-only env reads — `JARVIS_API_KEY`, `LITELLM_BASE_URL`, and legacy provider-key env vars used before a user stores encrypted Settings credentials — are exempt.

---

## 8. Status table (canonical roll-up)

| Key | Status |
|---|---|
| `pulse.enabled` | Active |
| `pulse.cron` | Active |
| `pulse.deck_size` | Active |
| `pulse.stage2_top_k` | Active |
| `pulse.weights` | Active (4 of 10 weights are CONDITIONAL — see [02-pulse.md §3.2](02-pulse.md#32-conditional-signals-live-conditional)) |
| `pulse.l2_lambda` | Active |
| `recommendation.enabled` | Active |
| `recommendation.liked_weight` | Active |
| `recommendation.project_weight` | Active |
| `user.timezone` | Active |
| `setup.completed` | Active |
| `telegram.owner_chat_id` | Active |
| `zotero.api_key` | Active (encrypted) |
| `zotero.user_id` | Active |
| `zotero.library_type` | Active |
| `zotero.poll_enabled` | Active |
| `zotero.poll_cron` | Active |
| `llm.smart_model` | Partial (LiteLLM's admin DB is the runtime authority; the row is the reconciler's desired state) |
| `llm.fast_model` | Partial (same) |
| `llm.embed_model` | Partial (dimension-locked; the YAML-seeded `embed` alias is the runtime authority) |
| `zotero.group_id` | Active (consumed when `library_type="group"`) |
| `zotero.auto_push_on_star` | Active |
| `fsrs.desired_retention` | Active (per-review DB read) |
| `fsrs.learning_steps` | Active (wired into the fsrs Scheduler) |
| `smtp.{host,port,user,from,pass}` | Active (system-wide outbound-mail relay; `smtp.pass` encrypted) |
| `smtp.reply_to` | Active (optional Reply-To header on magic-link emails; empty value clears) |
| `smtp.from_name` | Active (optional sender display name in From header; empty value clears) |
| `automation.fetch_interval_hours` | Active (system-wide auto-fetch scheduler) |
| `observability.langfuse_dashboard_url` | Active (Settings → Observability link target) |
| `llm.anthropic.api_key` | Partial (admin-wide encrypted provider key; used only when model alias is `anthropic/*` or for `/test`) |
| `llm.openai.api_key` | Partial (admin-wide encrypted provider key; used only when model alias is `openai/*` or for `/test`) |
| `llm.google.api_key` | Partial (admin-wide encrypted provider key; used only when model alias is `gemini/*` or for `/test`) |
| `llm.providers.<provider>.api_key` | Partial (admin-wide encrypted provider key for namespaced provider registry entries) |
| `llm.providers.custom_openai_compatible.base_url` | Partial (admin-wide custom endpoint URL, validated before save) |

---

## 9. Accepted Partial keys (status quo)

| Item | Why accepted |
|---|---|
| `llm.{smart,fast,embed}_model` Partial | LiteLLM's admin DB is the deliberate runtime authority for smart/fast (deployments delivered via `/model/new` + `/model/delete` and re-verified by the ~30 s reconciler); the YAML-seeded `embed` alias is dimension-locked. The `user_config` row is the desired state the reconciler converges on, plus UI read-back. See [03-llm.md §1](03-llm.md). |
| Cloud-provider keys Partial | Conditional secrets by design — only consumed when a matching cloud-provider model alias is selected or a `/test` endpoint is invoked. In v1.0.3 these settings are deployment-wide, admin-only system rows; per-user BYOK/routing is future work, not current behavior. `.env` provider-key variables are bootstrap/legacy only and must not become the request-time authority for user-controllable provider credentials. |

---

## 10. Cross-contract references

- **[02-pulse.md §3.2](02-pulse.md#32-conditional-signals-live-conditional)** — the four conditional weights inside `pulse.weights` (`citation_pagerank`, `citation_count`, `citation_adamic_adar`, `classifier`) — UI-exposed and validator-accepted; populated only when the user opts in by raising the weight AND the optional dependency is present.
- **[03-llm.md](03-llm.md) §1** — `llm.{smart,fast,embed}_model` and the cloud-provider keys behave at the LiteLLM layer; this contract documents only the `user_config` storage plane.
- **[04-observability.md](04-observability.md)** — privacy rules forbid logging raw `user_config.value` for any key in `_SECRET_KEYS` / `_ENCRYPTED_KEYS`.
- **Note:** `paper_user_state` columns (state, starred, state_before_trash) are NOT in this contract; they are per-paper user state, not user-controllable settings.

---

## 11. Verified Identifiers

| Citation | File:line | One-line behavior |
|---|---|---|
| `_ALLOWED_CONFIG_KEYS` frozenset | services/paper_ingestion/paper_ingestion/services/config_metadata.py:34-85 | Allow-list of writeable user_config keys (static keys; dynamic `llm.*` patterns accepted via `_classify_litellm_runtime_key`) |
| `PERSONAL_KEYS` / `SYSTEM_KEYS` / `_classify_config_key` | services/paper_ingestion/paper_ingestion/services/config_metadata.py:149-224 | Per-key scope: personal (per-user row) vs system (admin-only, `user_id IS NULL`) |
| `_SECRET_KEYS` / `_ENCRYPTED_KEYS` | services/paper_ingestion/paper_ingestion/services/config_metadata.py:227-247 | Keys masked on GET; subset gets ciphertext on PUT (incl. `smtp.pass`) |
| `_CONFIG_VALIDATORS` | services/paper_ingestion/paper_ingestion/services/config_validators.py:231-271 | Per-key validator dispatch; missing entry = no custom validation |
| `_PULSE_WEIGHT_KEYS` / `_PULSE_REQUIRED_WEIGHT_KEYS` | services/paper_ingestion/paper_ingestion/services/config_validators.py:31-47 | The 10 allowed weight keys; 6 are required |
| `_validate_pulse_weights` | services/paper_ingestion/paper_ingestion/services/config_validators.py:83 | Enforces shape + value range on `pulse.weights` |
| `_validate_cron` | services/paper_ingestion/paper_ingestion/services/config_validators.py:65 | Parses cron; rejects sub-hourly |
| `_validate_l2_lambda` | services/paper_ingestion/paper_ingestion/services/config_validators.py:144 | Range [0.0, 2.0]; rejects bool |
| `_validate_lookback_days` / `_validate_startup_grace_seconds` | services/paper_ingestion/paper_ingestion/services/config_validators.py:153-160 | Pulse discovery window [1,90] int; startup grace [0,300] |
| `_validate_group_id` | services/paper_ingestion/paper_ingestion/services/config_validators.py:173 | `zotero.group_id`: positive int or null |
| `_validate_langfuse_dashboard_url` | services/paper_ingestion/paper_ingestion/services/config_validators.py:188 | Empty / `https://` / loopback `http://`; rejects other schemes |
| `_validate_fsrs_retention` / `_validate_fsrs_learning_steps` | services/paper_ingestion/paper_ingestion/services/config_validators.py:213-228 | Range `(0,1)` for retention; `list[int]` length 1–10 positive for learning_steps |
| `write_config` | services/paper_ingestion/paper_ingestion/services/config_write.py:132 | Allow-list + validator + encrypted-vs-plain write + side-effect dispatch |
| `apply_pulse_cron` | services/paper_ingestion/paper_ingestion/services/scheduler_effects.py:53 | Reschedules live `pulse_overnight` job; bounds-checks; DB+scheduler rollback on invalid next_run_time |
| `apply_zotero_cron` | services/paper_ingestion/paper_ingestion/services/scheduler_effects.py:113 | Best-effort `zotero_library_sync` reschedule on poll_cron PUT |
| `reload_telegram_nudges` | services/paper_ingestion/paper_ingestion/services/model_assignment.py:21 | POST `/internal/reload-nudges` to telegram_bot |
| `update_litellm_model` | services/paper_ingestion/paper_ingestion/services/litellm_config.py:477 | Delivers a role→model change via LiteLLM's admin DB (`GET /v1/model/info` + `POST /model/new` + `POST /model/delete`); no-ops when the alias already routes the requested signature; refuses re-routing the dimension-locked `embed` alias; raises RuntimeError on delivery failure (reconciler retries) |
| `get_provider_api_key` | services/paper_ingestion/paper_ingestion/services/litellm_config.py:149 | Decrypts cloud-provider key from user_config |
| `pulse.weights` / lookback / grace load | services/paper_ingestion/paper_ingestion/pulse/profile.py:221-242 | Loads from user_config, merges with `_DEFAULT_WEIGHTS`, clamps to [0,1]; reads `pulse.lookback_days` (default 7) + `pulse.startup_grace_seconds` (default 0.0) |
| `_build_fsrs_manager_from_db` | services/learning_engine/learning_engine/routers/review.py | Per-review fetch of `fsrs.desired_retention` + `fsrs.learning_steps`; constructs a fresh `FSRSManager` inside the review transaction |
| `setup_completed` resolution | services/paper_ingestion/paper_ingestion/routers/setup.py (get_status) | Reads `setup.completed` from `user_config`; returned in pre-auth `/api/setup/status`; unified `OnboardingGate` keys on this field |
| SMTP keys persisted | services/paper_ingestion/paper_ingestion/routers/setup.py | First-run wizard writes `smtp.*` rows; `smtp.pass` as Fernet ciphertext |
| `user_config` table schema | db/init.sql:33-48 | `(user_id, key) UNIQUE NULLS NOT DISTINCT`, nullable `value`, `encrypted_value BYTEA`, updated_at |
| `paper_sources` table + seeds | db/init.sql:71-88 | sources seeded; arxiv enabled; others disabled |
| `topics` table | db/init.sql:90-97 | name, query_terms, category, enabled |
