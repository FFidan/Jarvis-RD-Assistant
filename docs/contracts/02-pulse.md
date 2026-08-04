# 02 — Pulse Pipeline Contract
**Status:** LIVING
**Reviewers must update this contract in the same patch as any change to:**
- The 8 numbered steps in [pulse/job.py:run_pulse](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L100)
- `_DEFAULT_WEIGHTS` in [pulse/profile.py:19-30](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L19-L30)
- `_llm_concurrency()` / `_stage2_timeout()` lazy getters / `PULSE_STAGE2_*` scoring knobs / per-call timeouts
- The `signals` dict shape on `ScoredCandidate`
- The `stats` dict keys produced by `run_pulse` (drives the Settings → Pulse Diagnostics panel)

---

## 0. What this contract covers (and what it does NOT)

**In scope.** The overnight Pulse pipeline that produces a daily card deck —
8 stages from cron-trigger to deck persist. Stage I/O contracts, signal
definitions, weight schema, timeout/concurrency policy, fallback semantics,
and the diagnostics surface the Settings UI reads.

**Out of scope.**
- Source plugin implementations (arXiv / OpenAlex / S2 / PubMed / Local) —
  each plugin owns its own contract for query shape, rate limits, and
  metadata translation.
- Pulse card UI (frontend rendering of `pulse_cards` rows).
- Telegram-side digest delivery — separate code path that reads the same
  `pulse_decks` / `pulse_cards` tables.
- The recommender (`refresh_recommendations`) — a sibling system; documented
  briefly in [01-settings.md §2.1](01-settings.md#21-active-keys-written-and-read-by-code-that-affects-user-visible-behavior) under the `recommendation.*` keys.

---

## 1. Pipeline shape

`run_pulse` is the single entry point. Eight numbered stages, each individually
wrapped in `try/except` so any one stage can degrade without crashing the run.

| # | Stage | File:line | Output | Failure handling |
|---|---|---|---|---|
| 1 | Profile load | [job.py:114-129](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L114-L129) | `UserProfile` | **Fatal.** Sets `last_error`; returns immediately with `duration_s` populated. |
| 2 | Discovery (source fan-out) | [job.py:137-151](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L137-L151) | `list[PaperCreate]` + per-source counts + `source_diagnostics` | Degraded. Empty candidate list; pipeline continues. If every enabled source is empty, rate-limited, unsupported, or unconfigured, `degraded_reason` is set even when the job itself did not fail. |
| 3 | Stage 1 — embedding filter | [job.py:158-170](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L158-L170); [scoring.py:94-217](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L94-L217) | top-`stage2_top_k` `ScoredCandidate`s | Degraded. Empty `stage1_out`; Stage 2 short-circuits. |
| 4 | Stage 2 — LLM rerank | [job.py:331-354](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L331-L354); [scoring.py:252-334](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L252-L334) | `ScoredCandidate`s with LLM signals filled | **Degraded.** On `TimeoutError` (outer wall-clock cap, default 900 s) OR any exception, `_fallback_stage2` clears LLM signals. `degraded_reason` set. |
| 5 | Optional citation + classifier signals | [job.py:231-294](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L231-L294) | `signals` dict augmented with `citation_pagerank`, `citation_count`, `citation_adamic_adar`, `classifier` | Degraded. Failures preserve LLM signals; `degraded_reason` set if no prior reason. |
| 6 | Stage 3 — weighted combine | [job.py:301-307](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L301-L307); [scoring.py:342-380](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L342-L380) | `ScoredCandidate`s with `final_score` | Degraded. Fall back to `stage2_out`; sets `last_error`. |
| 7 | Assemble deck | [job.py:314-320](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L314-L320); [deck.py:18-40](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/deck.py#L18-L40) | top-`deck_size` cards | Degraded. Empty deck; sets `last_error`. |
| 8 | Persist deck | [job.py:327-367](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L327-L367); [deck.py:102-204](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/deck.py#L102-L204) | `pulse_decks` row + N `pulse_cards` rows | Degraded. Outer txn failure → `card_count=0`; per-card savepoint isolates upsert failures so one bad card doesn't poison the deck. The 60-day negative-feedback exclusion happens earlier, during candidate selection, so this stage persists a deck that has already survived it. |

A scheduled run is invoked via APScheduler under job id `pulse_overnight`
([scheduler.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/scheduler.py)); on-demand via the jobs subsystem under handler `"pulse.generate"` (`_pulse_generate_job` at [job.py:555](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L555)).

---

## 2. Stage I/O contracts

### 2.1 `ScoredCandidate` (the cross-stage envelope)

Defined at [scoring.py:69](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L69). Mutated cumulatively by stages 3 → 4 → 5 → 6.

| Field | Set by | Type |
|---|---|---|
| `paper` | Stage 1 | `PaperCreate` (carrier from sources) |
| `signals` | Stages 1, 4, 5 | `dict[str, float]` |
| `llm_relevance`, `llm_novelty`, `reasoning` | Stage 4 | `int 1-10`, `int 1-10`, `str` (or `None` on fallback) |
| `final_score` | Stage 6 | `float` (preliminary in Stage 1, overwritten in Stage 6) |
| `reasoning_verified`, `reasoning_confidence` | Stage 4 | `bool | None`, `RagConfidence | None` |

Stages MUST NOT mutate `ScoredCandidate` in place across stages — they
return new instances. The `signals` dict is intentionally string-keyed so
new signals can be added without schema migration.

### 2.2 `UserProfile` (the per-user context)

Defined at [profile.py:35-60](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L35-L60). Loaded once per run by `load_profile` ([profile.py:63](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L63)).

| Field | Source |
|---|---|
| `topics` | `topics` table |
| `tracked_author_names`, `tracked_author_s2_ids` | `tracked_authors WHERE enabled = TRUE` |
| `library_centroid` | mean embedding of abstracts where `paper_user_state.starred OR state ∈ ('to_read','reading','done')` |
| `weights`, `deck_size`, `stage2_top_k` | `user_config` (`pulse.weights`, `pulse.deck_size`, `pulse.stage2_top_k`) |
| `recent_positive_titles`, `recent_negative_titles` | `recommendation_feedback` 90-day window |
| `negative_centroid` | mean embedding of papers with negative `recommendation_feedback` |
| `negative_topics`, `negative_authors`, `dampened_topics` | L3 dampening signals; `dampened_topics` is consumed by stage-1 topic similarity (multiplicative 0.5 on the positive domain) |
| `liked_paper_ids` | starred papers |

The HTTP call to embed library abstracts is intentionally outside any DB
connection scope — the connection is acquired twice in `load_profile`,
embedding round-trips happen between acquisitions.

---

## 3. Signal catalog

Every entry in `_DEFAULT_WEIGHTS` MUST have a populating stage, OR be
explicitly marked **CONDITIONAL** with the gate documented below. `stage3_combine`
([scoring.py:342-380](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L342-L380)) treats missing signals as 0.0 — invariant 4.3.

### 3.1 Always-populated signals (LIVE)

| Signal | Default weight | Populated by | Range |
|---|---|---|---|
| `embedding` | 0.20 | Stage 1 [scoring.py:168-174, 195](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L168-L174) — cosine(candidate, library_centroid) − l2_lambda·cosine(candidate, negative_centroid) | typically [-1, 1]; clamped per use |
| `topic` | 0.20 | Stage 1 [scoring.py:177-179](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L177-L179) — max cosine over topic embeddings | [0, 1] |
| `recency` | 0.05 | Stage 1 [scoring.py:182](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L182) — `exp(-age_days/30)` clamped to [0, 1] | [0, 1] |
| `author_bonus` | 0.15 | Stage 1 [scoring.py:184-192](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L184-L192) — 1.0 iff candidate authors intersect tracked_authors (by name OR S2 id) | {0.0, 1.0} |
| `llm_relevance` | 0.30 | Stage 4 [scoring.py:296](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L296) — LLM-scored 1–10, normalized to [0, 1] | [0.1, 1.0] (None on Stage 4 fallback) |
| `llm_novelty` | 0.10 | Stage 4 [scoring.py:297](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L297) — same | same |
| `l2_penalty` | (informational; not a weight) | Stage 1 [scoring.py:199](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L199) — `l2_lambda * cosine(candidate, negative_centroid)` | [0, l2_lambda] |

`l2_penalty` is stored in `signals` for diagnostics but is NOT consumed by
`stage3_combine` — its effect is already baked into `embedding` per the
subtraction at scoring.py:174. The Settings UI exposes the `l2_lambda`
multiplier separately; see [01-settings.md §2.1](01-settings.md#21-active-keys-written-and-read-by-code-that-affects-user-visible-behavior) row `pulse.l2_lambda`.

### 3.2 Conditional signals (LIVE-CONDITIONAL)

These four signals default to weight 0.0 in `_DEFAULT_WEIGHTS` and `_PULSE_REQUIRED_WEIGHT_KEYS`
(absent from `_PULSE_REQUIRED_WEIGHT_KEYS` at [config_validators.py:45-47](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_validators.py#L45-L47), so they are OPTIONAL on PUT). They are **populated only when the user assigns a non-zero weight** AND the gating dependency is available.

| Signal | Computed by | Activation gate | Dependency | Failure mode |
|---|---|---|---|---|
| `citation_pagerank` | [citation_signals.py:compute_citation_signals](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/citation_signals.py#L11-L107) | `any(profile.weights[name] > 0 for name in ("citation_pagerank", "citation_count", "citation_adamic_adar"))` ([job.py:233-236](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L233-L236)) | `networkx` Python package + populated `paper_citations` table | If `networkx` missing → empty signals dict → all three citation signals stay 0.0; if no edges in the graph → PageRank = 0.0 for all; degrades silently |
| `citation_count` | Same | Same | `papers.citation_count` column populated by S2 ingestion | Normalized as `min(1.0, count / max_count_in_batch)` |
| `citation_adamic_adar` | Same | Same | `paper_citations` edges + at least one liked paper in `recommendation_feedback` | Returns 0.0 if no liked papers or no edges |
| `classifier` | [training.py:classifier_scores](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/training.py) | `profile.weights["classifier"] > 0` ([job.py:258](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L258)) | `scikit-learn` + ≥30 rows in `recommendation_feedback` with both positive and negative labels | If sklearn missing → `available=False`, all candidates score 0.0; if not enough ratings → same; trained model persisted via `pulse.train_classifier` job after each Pulse run ([job.py:373](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L373)) |

**Important contract:** these signals are NOT ghost UI. The Settings sliders
defaulting to 0.0 (per [SettingsPage Pulse tab](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/frontend/src/components/settings/PulseSection.tsx)) is intentional. The user opts in by raising
the weight; the pipeline respects the opt-in but degrades gracefully when
the optional dependency is missing.

The Pulse diagnostics surface (Settings → Pulse → Diagnostics panel; backed
by `pulse_decks.stats`) MUST report:
- `classifier.available` (bool) — sklearn installed
- `classifier.degradation_reason` (str | null) — why classifier scored zero
([job.py:266-294](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L266-L294))

### 3.3 The signal-catalog completeness invariant

Every key in `_DEFAULT_WEIGHTS` is present in §3.1 or §3.2 above. Adding a
new weight key MUST add the corresponding row to one of those tables AND to
`_PULSE_WEIGHT_KEYS` in [config_validators.py:31-44](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_validators.py#L31-L44). The validator and contract are
the two halves of the same allow-list.

---

## 4. Weight schema

`user_config['pulse.weights']` is a JSONB object whose keys are a subset of
`_PULSE_WEIGHT_KEYS` ([config_validators.py:31-44](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_validators.py#L31-L44)). Validation:

- Required keys: `embedding`, `topic`, `llm_relevance`, `llm_novelty`,
  `author_bonus`, `recency` (the always-populated set, §3.1)
- Optional keys: `citation_pagerank`, `citation_count`,
  `citation_adamic_adar`, `classifier` (the conditional set, §3.2)
- Each value: `float ∈ [0.0, 1.0]` (per `_validate_pulse_weights`)
- Sum is **NOT** required to equal 1.0 — the UI offers a "Normalize to 1.0"
  button ([PulseSection.tsx:738-746](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/frontend/src/components/settings/PulseSection.tsx#L738-L746)) but the contract permits any combination

Load path ([profile.py:221-231](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L221-L231)):

1. Read JSONB; if absent, fall back to `_DEFAULT_WEIGHTS`
2. Merge user values over defaults (so missing optional keys default to 0.0)
3. Clamp every value to `[0, 1]`; log a warning if any was out of range

`pulse.l2_lambda` is stored as a dedicated `UserProfile` field outside the `weights` dict ([profile.py:60](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L60), loaded at [profile.py:236-237](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L236-L237)); `stage3_combine` never iterates it as a scoring signal.

---

## 5. Timeout, concurrency, and budget policy

Two of these knobs are **environment variables** (deployment-level, not
user-controllable settings keys): `PULSE_LLM_CONCURRENCY` and
`PULSE_STAGE2_TIMEOUT_SECONDS`, both defined as pydantic-settings fields on
`PaperIngestionSettings` ([config.py:190-208](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/config.py#L190-L208)).
The lookback/grace knobs ARE user_config keys (see [01-settings.md §2.1](01-settings.md#21-active-keys-written-and-read-by-code-that-affects-user-visible-behavior)).

| Knob | Value | Source | Purpose |
|---|---|---|---|
| Per-LLM-call timeout | 120 s | `LLM_TIMEOUT_DEFAULT` at [llm_client.py:70](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/libs/jarvis_common/jarvis_common/llm_client.py#L70) | Single chat completion request (or single retry) cannot exceed this |
| Stage-2 concurrency | default 4 (env var `PULSE_LLM_CONCURRENCY`) | `_llm_concurrency()` lazy getter at [scoring.py:45](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L45) → `_get_cfg().pulse_llm_concurrency` | Semaphore-bounded parallel scorers |
| Stage-2 model alias | `smart` | `_llm_model()` at [scoring.py:49](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L49) reads `PULSE_STAGE2_MODEL` | Defaults to `smart` because Stage 2 must emit structured JSON and the `fast` alias schema-echoes instead of scoring; operators can override via `PULSE_STAGE2_MODEL` |
| Stage-2 retry budget | 1 | `_stage2_max_retries()` at [scoring.py:59](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L59) reads `PULSE_STAGE2_MAX_RETRIES` | Caps structured-output retries so one bad candidate does not expand into a long manual Pulse run |
| Stage-2 orchestrator call | 1 call over all Stage-1 survivors | inner concurrency at [scoring.py:289](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L289) | `run_pulse` no longer slices candidates into outer batches; `stage2_llm_rerank` owns per-candidate concurrency |
| Stage-2 wall-clock cap | default 900 s (env var `PULSE_STAGE2_TIMEOUT_SECONDS`) | `_stage2_timeout()` lazy getter at [job.py:55](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L55) → `_get_cfg().pulse_stage2_timeout_seconds`; applied at [job.py:333](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L333) | Outer `asyncio.wait_for` around all Stage-2 work; on timeout → `_fallback_stage2` |
| Discovery lookback window | default 7 days (user_config key `pulse.lookback_days`; int, validated [1, 90]) | `_validate_lookback_days` at [config_validators.py:153](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_validators.py#L153); default from [profile.py:55](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L55) | Controls how far back Stage 1 looks for candidate papers |
| Startup grace | default 0 s (user_config key `pulse.startup_grace_seconds`; float, validated [0, 300]) | `_validate_startup_grace_seconds` at [config_validators.py:158](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/services/config_validators.py#L158); default from [profile.py:57](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L57) | Warmup pause before first outbound HTTP burst |

### 5.1 Worst-case math

With `pulse.stage2_top_k = 40` (default), `PULSE_LLM_CONCURRENCY = 4` (default),
`PULSE_STAGE2_MODEL=smart`, `PULSE_STAGE2_MAX_RETRIES=1`, and per-call timeout
120 s, the theoretical worst-case Stage-2 wall-clock is

```
  ceil(40 / 4) waves × 120 s/wave × up to 2 attempts = 2400 s
```

The outer 900 s cap still wins. In practice the smart alias is expected to score
well below the per-call timeout; if structured-output validation repeatedly
fails, the pipeline degrades to embedding-only fallback instead of blocking the
job indefinitely. `run_pulse` makes one Stage-2 call and lets the scorer's
semaphore control parallelism (no outer batch loop).

Instructor retry count (`PULSE_STAGE2_MAX_RETRIES`) is part of this latency
budget — keep it low unless you have measured validation failures and accepted
the longer wall-clock risk.

---

## 6. Failure modes

| Failure | Stage | Effect | UI surface |
|---|---|---|---|
| Profile load throws | 1 | **Fatal.** `last_error` set; pipeline returns immediately. Pulse run does NOT produce a deck. | Settings → Pulse "Last run: Failed" badge |
| Discovery throws | 2 | Empty candidate list. `last_error` set; pipeline continues; Stage 1 will get nothing → empty Stage 2 → empty deck. | Last-run badge "Failed" iff this set `last_error`; otherwise just zero candidates |
| Discovery exhausts all sources without throwing | 2 | Empty candidate list. `last_error` remains null; `source_diagnostics` records each source status and `degraded_reason` explains the zero-card deck. | Settings → Pulse shows "Degraded"; Pulse deck shows the reason and top source messages. |
| Embedder throws | 3 | All Stage 1 candidates get zero signals (`embedding=0`, `topic=0`, etc.) but all candidates are RETURNED. They will rank purely on weights of zeros (i.e., their `final_score` will be ~0). | Diagnostics shows `stage1_survivors` ≠ 0 with all-zero scores |
| Stage 2 LLM `TimeoutError` (outer wall-clock cap, default 900 s) | 4 | `_fallback_stage2` clones every Stage 1 survivor with `llm_relevance=None`, `llm_novelty=None`, `reasoning=None`. `degraded_reason = f"LLM scoring timed out at {timeout}s; deck used embedding-only fallback."` Deck is still produced from Stage 1 scores only. | Last-run badge: "Failed" (because `last_error` is sometimes also set on tail exceptions). Diagnostics shows `degraded_reason`. |
| Stage 2 LLM other exception | 4 | Same as TimeoutError but `degraded_reason = f"stage2 error (embedding-only fallback used): {exc}"` | Same |
| Citation/classifier exception | 5 | `degraded_reason` set if not already; LLM signals preserved; affected signals stay 0.0 | Diagnostics: `classifier.available` may be False |
| Stage 3 throws | 6 | Falls back to `stage2_out` order; `last_error` set | Last-run badge "Failed" |
| Assemble throws | 7 | Empty deck; `last_error` set | Last-run badge "Failed" |
| Per-card upsert throws | 8 | Savepoint rollback isolates the bad card; `last_error` set; remaining cards still persist; if ALL fail, 0-card deck is persisted with `last_error` set | Last-run badge "Failed" with a 0-card deck |
| Outer-transaction failure (DB unreachable) | 8 | `last_error = f"persist: {exc}"`; `card_count=0`; whole pipeline result returned but no deck row exists | Last-run badge "Failed"; Diagnostics may be missing entirely |

### 6.1 Degraded vs fatal — the difference that matters

The frontend `Last Pulse run: Failed` badge fires on **`stats.last_error`**
([frontend/src/components/settings/PulseSection.tsx](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/frontend/src/components/settings/PulseSection.tsx)).
The backend has TWO
distinct fields:

- **`last_error`** — terminal failure. Pipeline did not complete normally.
  Typically set when Stage 1, 3, 6, 7, or 8 throws.
- **`degraded_reason`** — Pulse completed and produced a deck, but with
  reduced fidelity or explicit source exhaustion. Set for all-source-empty
  discovery runs, Stage 4 LLM fallback, and Stage 5 citation/classifier
  degradation.

A run can show "Failed" in the UI while being technically
**degraded, not fatal** — e.g. a deck of cards produced purely from Stage 1
scores. The Settings UI distinguishes the two fields: `last_error` is
Failed; `degraded_reason` without `last_error` is Degraded. That distinction is
intentional for source exhaustion, where a zero-card deck can be operationally
correct but still needs an explanation instead of looking healthy.

---

## 7. Diagnostics shape

`run_pulse` returns a `stats: dict[str, Any]`. It is persisted into
`pulse_decks.stats` JSONB ([deck.py:73-89](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/deck.py#L73-L89)) and read by the Settings → Pulse Diagnostics panel.

| Key | Type | Meaning | Set by |
|---|---|---|---|
| `candidate_count` | int | Raw fan-out from `discover_candidates` | Stage 2 |
| `stage1_survivors` | int | Output of `stage1_embedding_filter` | Stage 3 |
| `stage2_scored` | int | Output of `stage2_llm_rerank` (or `_fallback_stage2`) | Stage 4 |
| `llm_calls` | int | Number of candidates that received non-None `llm_relevance` | Stage 4 ([job.py:209](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L209)) |
| `duration_s` | float | Wall-clock seconds for the full pipeline | Stage 7 (before persist) |
| `last_error` | str \| null | Terminal failure description | Various |
| `degraded_reason` | str \| null | Non-terminal degradation, including all-source exhaustion | Stage 2 / Stage 4 / Stage 5 |
| `deck_date` | str (ISO date) | Date of the deck just produced | Stage 7 |
| `card_count` | int | `pulse_cards` rows actually persisted | Stage 8 |
| `source_counts` | `dict[str, int]` | Per-source candidate count | Stage 2 |
| `source_diagnostics` | `dict[str, {status, message, status_code, retry_after_s, settings_hint}>` | Per-source operational state for rate limits, unconfigured sources, unsupported sources, and empty results | Stage 2 |
| `classifier` | dict | Classifier metadata (`available`, `degradation_reason`, `sample_count`) | Stage 5 |
| `classifier_training_enqueued` | bool | Whether the post-run training job was enqueued | End of pipeline |
| `verification_stats` | `dict[str, int \| float]` | Per-run verification outcomes; keys: `pass_rate` (float 0-1), `total` (int), `passed` (int), `failed` (int) | End of pipeline ([job.py:524-529](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L524-L529)) |

The `stats` dict is the primary structured surface for Pulse
observability. When the optional Langfuse profile is enabled
([04-observability.md](04-observability.md)), traces add a parallel surface for
per-call latency and per-stage timing.

Provider diagnostics in `source_diagnostics` are user-visible and MUST be
sanitized. They may include provider names, status classes, HTTP status codes,
and retry hints; they must not include raw exception strings, full request URLs,
tokens, or provider response bodies. Raw exception detail belongs in service
logs only.

---

## 8. Deck persist semantics

Documented separately because the persist step has its own correctness
invariants beyond mere row insertion.

- **Deck identity** is `(deck_date, user_id)` — composite UNIQUE constraint with `NULLS NOT DISTINCT`
  semantics (added by migration 043). One deck per user per day.
- **Idempotent replace.** A second `persist_deck` for the same `(date, user_id)`
  pair updates the existing row's `card_count`, `generated_at`, `stats`,
  `degraded_reason`, then DELETEs and re-inserts cards.
- **60-day negative-feedback exclusion** ([deck.py:57-99](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/deck.py#L57-L99)). Applied during candidate selection, before the deck is cut to the user's deck size, so persisting inserts a deck that has already survived it. There is no candidate-count threshold and no bypass; a deck left short reports it through `degraded_reason`. Spec §7.3.1.
- **Per-card savepoint.** [job.py:332-345](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py#L332-L345) wraps each card upsert in a SAVEPOINT so a single failing card doesn't poison the whole transaction.

---

## 9. Invariants

The implementation MUST satisfy these. Testable.

1. **Signal coverage.** Every key in `_DEFAULT_WEIGHTS` MUST be either:
   (a) populated by some always-running stage (§3.1), OR
   (b) explicitly gated as conditional with a documented activation gate (§3.2).
   No silent omissions.
2. **Stage-2 never raises.** `stage2_llm_rerank` MUST handle every per-candidate
   exception and return a `ScoredCandidate` for every input — failures degrade,
   they do not propagate.
3. **Missing signals = 0.0.** `stage3_combine` MUST treat any signal name in
   `weights` but missing from a candidate's `signals` as 0.0 ([scoring.py:362](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L362) `signals.get(k, 0.0)`).
4. **Weights clamped.** `pulse.weights` values MUST be clamped to `[0, 1]` before any signal multiplication ([profile.py:182-186](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/profile.py#L182-L186)).
5. **Run degrades rather than crashes.** A failing pipeline stage MUST be
   recorded in `stats['last_error']` and the run MUST continue from the
   best-known state; unrecoverable failures (Stage 1) MUST return early with
   `stats` populated. Two exceptions propagate by design and MUST NOT be
   swallowed: `asyncio.CancelledError` on cancellation (a `BaseException`, so
   `except Exception` does not catch it) and `RuntimeError` when
   `JARVIS_STRICT_MODELS=1` and Stage 2 returned candidates without the scoring
   model being called once, meaning the embedding-only fallback produced them —
   an empty candidate set returns early and does not raise. This is a
   degrade-on-stage-failure guarantee, not a blanket "never raises" one —
   progress reporting through `ctx` is itself unguarded.
6. **Per-card isolation.** Stage 8 MUST use SAVEPOINTs for per-card upserts
   so a single bad card does not roll back the deck.
7. **Negative-feedback exclusion.** The 60-day negative-feedback filter MUST be
   applied during candidate selection, before the deck is truncated to the
   user's deck size, and MUST NOT be bypassed. A deck left short because too
   many candidates were dismissed MUST report that through `degraded_reason`
   rather than refill itself with dismissed papers.
8. **Diagnostics completeness.** Every key listed in §7 MUST be present in
   the `stats` dict at run-end (with `null` rather than absent for keys with
   no value). The Settings UI assumes shape stability.
9. **Zero-card decks are explicit.** A completed run with zero cards and no
   `last_error` MUST carry a `degraded_reason` when source diagnostics show
   rate limits, unconfigured enabled sources, unsupported source modes, or no
   source candidates. The UI MUST NOT render this state as a plain empty deck.

---

## 10. Cleanup decisions deferred

Documenting; not prescribing.

| Item | Candidate dispositions |
|---|---|
| `last_error` vs `degraded_reason` UI conflation | Adjust the frontend "Failed" badge to distinguish "Degraded" from "Failed" |
| The 4 conditional signals UX | (a) Hide them in the UI until a data threshold is met (e.g., S2 citation data populated); (b) Show them with a tooltip "0.0 default — requires citation data / classifier ratings"; (c) Keep as-is |
| `classifier` activation threshold | Documented at 30 in `MIN_RATINGS` ([training.py:21](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/training.py#L21)). Could surface to Settings UI; today users have no way to know how close they are to threshold |

These dispositions are the implementation plan's call. The contract's job
is to surface the choices.

---

## 11. Cross-contract references

- **[01-settings.md §2.1](01-settings.md#21-active-keys-written-and-read-by-code-that-affects-user-visible-behavior)** — `pulse.*` and `recommendation.*` user_config keys; runtime read sites.
- **[03-llm.md §2 / §4](03-llm.md)** — Stage-2 LLM rerank is one of the LLM call sites; the per-site contract there governs the `PulseScoringOutput` Pydantic shape, retry policy, and timeout.
- **[04-observability.md §3](04-observability.md)** — `run_pulse` is the canonical "one trace per Pulse run" boundary.

---

## 12. Verified Identifiers

| Citation | File:line | One-line behavior |
|---|---|---|
| `run_pulse` orchestrator | services/paper_ingestion/paper_ingestion/pulse/job.py:100 | 8-stage pipeline with degraded/fatal handling |
| `_stage2_timeout()` lazy getter | services/paper_ingestion/paper_ingestion/pulse/job.py:55 | Stage-2 wall-clock cap; default 900 s via env `PULSE_STAGE2_TIMEOUT_SECONDS` |
| `_fallback_stage2` | services/paper_ingestion/paper_ingestion/pulse/job.py:64 | Clears LLM signals, preserves Stage 1 final_score |
| `asyncio.wait_for(..., timeout=_stage2_timeout())` | services/paper_ingestion/paper_ingestion/pulse/job.py:331-346 | Outer Stage-2 timeout enforcement |
| `_pulse_generate_job` job handler | services/paper_ingestion/paper_ingestion/pulse/job.py:555 | On-demand entry point via jobs subsystem |
| `ScoredCandidate` dataclass | services/paper_ingestion/paper_ingestion/pulse/scoring.py:73 | Cross-stage envelope |
| `_llm_concurrency()` lazy getter | services/paper_ingestion/paper_ingestion/pulse/scoring.py:45 | Stage-2 semaphore; default 4 via env `PULSE_LLM_CONCURRENCY` |
| `_llm_model()` reads `PULSE_STAGE2_MODEL` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:49 | Stage-2 model alias defaults to `smart` (must be structured-output-capable; the `fast` 4B alias schema-echoes → `llm_calls=0`) |
| `_stage2_max_retries()` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:63 | Stage-2 structured-output retry budget defaults to 1 (env `PULSE_STAGE2_MAX_RETRIES`) |
| `_LLM_MAX_TOKENS = 1536`, `_LLM_TEMPERATURE = 0.0` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:56-57 | Stage-2 LLM options |
| `stage1_embedding_filter` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:125 | Stage 1: embed + cosine + recency + author bonus + L2/L3 (dampened topics halved) |
| `_DAMPENED_TOPIC_FACTOR = 0.5` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:60 | L3: halves a dampened topic's positive similarity at stage-1; negatives pass through |
| `stage2_llm_rerank` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:277 | Stage 2 with bounded concurrency (semaphore at scoring.py:318) |
| `_score_one` LLM call | services/paper_ingestion/paper_ingestion/pulse/scoring.py:339 | Per-candidate `call_llm_structured` call inside the semaphore |
| `stage3_combine` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:417 | Weighted-sum final_score; missing signals → 0.0 |
| `_DEFAULT_STAGE2_TOP_K = 40` | services/paper_ingestion/paper_ingestion/pulse/profile.py:19 | Top-K default for Stage 2 |
| `_DEFAULT_WEIGHTS` | services/paper_ingestion/paper_ingestion/pulse/profile.py:20-31 | 10 weight keys; 4 default to 0.0 |
| `_RATING_HISTORY_LIMIT = 10` | services/paper_ingestion/paper_ingestion/pulse/profile.py:32 | Recent positive/negative title cap |
| `UserProfile` BaseModel (incl. `lookback_days`, `startup_grace_seconds`, `l2_lambda` fields) | services/paper_ingestion/paper_ingestion/pulse/profile.py:35-60 | Per-user context envelope; `l2_lambda` default 0.5, `lookback_days` default 7, `startup_grace_seconds` default 0.0 |
| `load_profile` | services/paper_ingestion/paper_ingestion/pulse/profile.py:63 | Two-phase DB+HTTP centroid load |
| Weights/lookback/grace load + clamp | services/paper_ingestion/paper_ingestion/pulse/profile.py:221-242 | Merge with `_DEFAULT_WEIGHTS`; clamp to [0,1]; read `pulse.lookback_days` / `pulse.startup_grace_seconds` |
| `compute_citation_signals` | services/paper_ingestion/paper_ingestion/pulse/citation_signals.py:11-107 | networkx-backed PageRank + count + Adamic-Adar |
| Citation signals graceful degrade on missing networkx | services/paper_ingestion/paper_ingestion/pulse/citation_signals.py:22-25 | `ImportError` → `{}` |
| `FEATURE_NAMES` (classifier) | services/paper_ingestion/paper_ingestion/pulse/training.py:10-20 | 9-element feature vector |
| `MIN_RATINGS = 30` | services/paper_ingestion/paper_ingestion/pulse/training.py:21 | Activation threshold for classifier |
| Classifier sklearn-missing degrade | services/paper_ingestion/paper_ingestion/pulse/training.py:31-39 | Returns `available=False` with reason |
| `assemble_deck` | services/paper_ingestion/paper_ingestion/pulse/deck.py:18-40 | Top-N by final_score |
| `_persist_deck_inner` upsert | services/paper_ingestion/paper_ingestion/pulse/deck.py:102-204 | Composite `(deck_date, user_id)` UPSERT |
| `_select_deck_cards` | services/paper_ingestion/paper_ingestion/pulse/job.py | 60-day negative-feedback exclusion, applied before the deck is truncated |
| `LLM_TIMEOUT_DEFAULT = 120.0` | libs/jarvis_common/jarvis_common/llm_client.py:70 | Per-call timeout default |
| `build_scoring_prompt` | services/paper_ingestion/paper_ingestion/pulse/prompts.py:36-136 | Two-message chat list; system + user |
| `PULSE_SCORING_SYSTEM_PROMPT` | services/paper_ingestion/paper_ingestion/pulse/prompts.py:18-33 | Strict-JSON instruction |
| `_PULSE_REQUIRED_WEIGHT_KEYS` | services/paper_ingestion/paper_ingestion/services/config_validators.py:45-47 | 6 always-required weights |
| `_validate_pulse_weights` | services/paper_ingestion/paper_ingestion/services/config_validators.py:83 | Required + optional + range check |
| `pulse_overnight` scheduler job id | services/paper_ingestion/paper_ingestion/scheduler.py (registration site) | Cron-triggered Pulse run |
