# 05 — Models and Hardware Contract
**Status:** LIVING
**Reviewers must update this contract in the same patch as any change to:**
- The curated catalog `libs/jarvis_common/jarvis_common/data/model_catalog.json`
- `recommend_models()` in [hardware_fit.py](../../libs/jarvis_common/jarvis_common/hardware_fit.py) or
  `recommendations_for_role()` / `build_model_statuses()` in [model_lifecycle.py](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py)
- The `GET /api/system/models`, `GET /api/system/hardware`, or pull/delete endpoints
- The per-machine `num_ctx` / thinking-mode key handling

This contract covers two adjacent concerns that share the same data model and
API surface: the **model lifecycle** (curated catalog, hardware-aware
recommendations, pull/delete, active defaults) and the **hardware-aware Settings
UX** (per-machine VRAM fit, context-size controls, thinking-mode toggle).

---

## 0. Scope and non-goals

### In scope
1. **Curated catalog** — local Ollama + cloud models, role-tagged, VRAM-annotated.
2. **Hardware detection** — VRAM probe at startup, TTL-cached, drives recommendations.
3. **Recommendation functions** — map detected VRAM to tiers and rank candidates per role.
4. **Status enum** — pull/assignment state per catalog entry.
5. **Pull / delete lifecycle** — explicit user action; pull is a background job; delete fails loudly if assigned.
6. **Cloud model integration** — catalog entries for Anthropic / OpenAI cloud aliases.
7. **Per-machine VRAM fit** — fit indicators, `num_ctx` slider per role, thinking-mode toggle.

### Non-goals (hard)
- **Benchmark theater** — no per-discipline benchmark claims, no ranking by generic STEM scores.
  The curated catalog is the authority for model identity, roles, assignability, and Ollama lifecycle.
  An operator-only settings plane (`GET/POST /api/settings/ai`) may consume
  `config/llm-tier-candidates.yaml` as an empirical overlay for backend/model candidates that were
  actually bench-run on target hardware; those rows are allow-list inputs, not marketing claims.
- **Work-style onboarding wizard** — tier-based recommendation is sufficient.
- **Per-task fallback chains** — the `smart` / `fast` / `embed` alias system already decouples
  tasks from concrete models at the LiteLLM layer.
- **Multi-machine aggregation** — Settings show hardware for the machine the backend runs on.
- **Disk-budget enforcement** — no auto-eviction. The user decides what to pull and delete.
- **Model pinning per paper** — global alias assignment only.
- **Automatic pull on config change** — changing the assigned model does NOT trigger a pull;
  an `unfit` status and a "Pull" CTA handle the gap.
- **Auto-switching mid-session** — the slider/picker is informational; a change to a `num_ctx`
  key takes effect on the next chat completion only.
- **No cloud VRAM accounting**, **no hostname display in UI**, **no per-task `num_ctx` override**.
- **No `temperature` / `top_p` UI** — orthogonal to VRAM safety; out of scope.

---

## 1. Model catalog

### 1.1 Location

```
libs/jarvis_common/jarvis_common/data/model_catalog.json
```

Bundled inside the Python package, loaded via `importlib.resources` so it works in Docker,
installed wheel, and editable installs identically. No runtime fetch; no auto-update. **The
catalog is NOT a live registry** — we do not fetch the Ollama search API at runtime.

### 1.2 Entry schema

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique key. Ollama: `name:tag`. Cloud: `provider/model-id`. |
| `name` | string | Human display name. |
| `provider` | `"ollama" \| "anthropic" \| "openai"` | Drives status computation. |
| `ollama_tag` | string \| null | Null for cloud entries. Must match `ollama list` NAME exactly. |
| `roles` | `("smart" \| "fast" \| "embed")[]` | Which LiteLLM aliases this entry can serve. |
| `vram_gb` | number | Peak active VRAM (FP16 weights + KV cache at 4k context). 0 for cloud. |
| `disk_gb` | number | Compressed model file on disk. 0 for cloud. |
| `context_tokens` | number | Published context window. |
| `license` | string | SPDX or name. Apache 2.0 = commercial OK; CC BY-NC = flag in UI. |
| `tier` | 0–4 | Minimum hardware tier required (see §3). |
| `description` | string | 1 sentence. No benchmark theater. |
| `embedding_dimension` | integer \| null | Embedding output dimension when known. |
| `last_reviewed` | ISO date | When this entry was last verified against the Ollama registry. |

Cloud entries omit `ollama_tag`, set `vram_gb=0`, `disk_gb=0`, `tier=0`.

The catalog also carries the optional hardware-fit fields documented in §6 used to compute
fit at a chosen `num_ctx` (`min_vram_gb_at_default_ctx`, `kv_cache_bytes_per_token`,
`default_num_ctx`, `max_num_ctx`, `supports_thinking`). Entries without these degrade to the
coarse tier-only fit decision.

### 1.3 Curated entries

The catalog ships a small curated set of local and cloud models. Local entries are Qwen3
family (smart/fast at 4B–72B, embedding at 0.6B/4B) plus `gemma3:12b`, `llama4:scout`, and
`mxbai-embed-large` as an embed fallback; cloud entries cover Anthropic Claude, OpenAI GPT-4o,
and OpenAI text-embedding.

Notes on selection:
- `qwen3-embedding:4b` (2560-dimensional) is the local default for notation-heavy scientific
  retrieval on Tier-1+ hardware. Upgrading an existing 1024d collection requires a matching
  Qdrant collection and a re-embed checkpoint.
- `qwen3-embedding:0.6b` (1024d) remains catalogued as an explicit smaller-machine fallback.
- `mxbai-embed-large` is kept as a future embed fallback but is not assignable by default
  because its dimension differs from Qwen3 and requires matching runtime/Qdrant config.
- `llama4:scout` is included for its long context window (long paper chains).
- Cloud embed (`openai/text-embedding-3-small`) is a future option only — not assignable
  without an explicit dimension and rebuild policy.

### 1.4 Catalog staleness

`last_reviewed` on each entry is the protection mechanism. If it is > 90 days old, the backend
logs a WARNING on startup (not an error — staleness ≠ broken).

---

## 2. Hardware detection

### 2.1 VRAM probe

At startup (and once per hour TTL), the backend probes VRAM in this order:

1. **`nvidia-smi`** (Linux + Windows) — `--query-gpu=memory.total --format=csv,noheader,nounits`
   returns MiB; divided to GB.
2. **macOS Metal** (`system_profiler SPDisplaysDataType`) — imprecise; source marked `macos-approx`.
3. **No GPU** — `vram_gb=0`, source `cpu`.

`nvidia-smi` is called with a fixed argument list via `subprocess.run([...], timeout=2.0)`,
not via shell string expansion — no subprocess-injection risk.

The probe populates the `HardwareInfo` dataclass
([model_lifecycle.py:123-147](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L123-L147)):
`vram_gb`, `vram_source` (`"nvidia-smi" | "macos-approx" | "cpu"`), `tier`, `detected_at`, and
an internal `machine_id` (the hostname; see §3). It is cached on `app.state.hw_info` with a
1-hour TTL by `get_cached_hardware()`.

**Failure mode:** `nvidia-smi` not installed → `vram_source="cpu"`, `vram_gb=0`. The
recommendation logic degrades gracefully to cloud-only / unknown-fit.

### 2.2 API surface

```
GET /api/system/hardware
```

Returns `HardwareInfo.to_dict()` — `vram_gb`, `vram_source`, `tier`, `detected_at`, and
`machine_id`. The frontend calls this once on Settings page load (no polling) and uses
`machine_id` to compose per-machine config keys. **The frontend MUST NOT render `machine_id`.**

---

## 3. Hardware tiers and recommendations

### 3.1 Tier table

`hardware_tier(vram_gb)` ([model_lifecycle.py:172-192](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L172-L192))
maps VRAM GB → ordinal 0..4:

| Tier | VRAM GB | Label |
|---|---|---|
| 0 | < 4 | CPU / low-VRAM — cloud recommended |
| 1 | 4–10 | Small GPU (e.g. 8 GB) |
| 2 | 10–20 | Mid GPU (e.g. 16 GB) |
| 3 | 20–40 | High GPU (e.g. 24 GB) |
| 4 | ≥ 40 | Large GPU (e.g. 48 GB) |

### 3.2 Recommendation functions

There are two complementary recommendation entry points:

- **`recommend_models(vram_mb)`** ([hardware_fit.py:207-243](../../libs/jarvis_common/jarvis_common/hardware_fit.py#L207-L243))
  — advisory, data-only. Given total VRAM in MiB (or `None` when the GPU probe failed entirely),
  it classifies VRAM into a `VramBucket` and returns a `HardwareRecommendation` with the suggested
  `smart`/`fast`/`embed` alias assignments for that bucket. It never mutates config, env, or DB; it
  never raises. A per-alias `confirm_on_target` flag signals candidates whose live bench is still
  outstanding. Callers surface the result (e.g. the system/models API, `setup.sh --check`).

- **`recommendations_for_role(role, ...)`** ([model_lifecycle.py:586-645](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L586-L645))
  — per-role ranking. It calls `build_model_statuses(...)`, filters to entries supporting the role,
  and sorts by status priority, then tier, then name. This is what `GET /api/system/models` and the
  recommendations endpoint return.

**What recommendations deliberately do NOT do:** no per-discipline scoring, no weighting by
benchmark score, no "auto-select and pull best model". The user selects; the backend recommends.

---

## 4. Status enum

Every catalog entry, when returned by the API, carries a `status` computed at request time by
crossing catalog state against Ollama's `/api/tags` response and the current LiteLLM config
(`build_model_statuses()` at
[model_lifecycle.py:480-585](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L480-L585)).

| Status | Meaning |
|---|---|
| `active` | Assigned to at least one role alias in LiteLLM config; running in Ollama. |
| `pulled` | Downloaded in Ollama but not currently assigned to any role alias. |
| `downloadable` | In catalog, not yet pulled; hardware tier is sufficient. |
| `unfit` | In catalog, not yet pulled; hardware tier is insufficient. |
| `cloud_active` | Cloud entry; API key present; assigned to at least one role alias. |
| `cloud_required` | Cloud entry; not active. A `provider_key_present` boolean tells the UI whether it can be selected immediately or must send the user to Providers first. |

`unfit` is not shown as an error if the model is merely recommended but not downloaded — it is the
status so the user understands why the "Pull" button is styled differently. Cloud entries without a
key appear in the available-models table with a Providers CTA but are not selectable in role
dropdowns until the key exists; cloud entries with a key are selectable even when `cloud_required`.

**Computation order:** fetch Ollama `/api/tags` → `pulled_tags`; read current LiteLLM config →
`active_tags`; read `hw.tier`; compute status per entry.

---

## 5. Pull / delete / assign lifecycle

### 5.1 Pull (download model)

```
POST /api/system/models/{ollama_tag}/pull
```

- Returns immediately with `{ "job_id": "<uuid>", "status": "queued" }`.
- Dispatches the `model.pull` task (registry-driven; the paper-ingestion service owns pull jobs).
- The task calls Ollama `POST /api/pull` with `{"name": ollama_tag, "stream": true}`, parses
  streamed JSON lines, reports per-layer progress, and on failure raises (the job is marked
  failed and the SSE bridge emits an error event).
- Progress is streamed via the existing `GET /api/jobs/{job_id}/stream` SSE bridge — no new
  machinery.

A frontend confirm dialog shows model name + version, disk GB, required VRAM vs detected VRAM,
and (when unfit) a CPU-offload warning before the POST.

No automatic pull on Settings change. Docker bootstrap MAY still preload the configured default
Ollama models during stack startup — that is install bootstrap behavior, not a Settings side effect.

### 5.2 Delete (remove local model)

```
DELETE /api/system/models/{ollama_tag}
```

- **Guard:** fails with 409 if the tag is currently assigned to an active role alias
  (`"Cannot delete model currently assigned to role '<role>'. Reassign first."`).
- If unassigned: calls Ollama `DELETE /api/delete`. Synchronous (file removal). Returns 204.

### 5.3 Assign (change role mapping)

Assignment goes through the existing `PUT /api/config/{key}` path for `llm.smart_model` /
`llm.fast_model` / `llm.embed_model`, which runs `update_litellm_model()` + `reload_litellm()`.
The Settings dropdown sends the catalog entry `id`; the backend maps it to the Ollama tag →
LiteLLM `ollama/<tag>` or to a cloud alias `anthropic/<model>` / `openai/<model>`.

If the selected model is `downloadable` (not yet pulled), the write is rejected and the frontend
guides the user to the Pull CTA.

### 5.4 Cloud model assignment

`llm.anthropic.api_key`, `llm.openai.api_key`, and `llm.google.api_key` are stored encrypted in
`user_config` (see [01-settings.md §2.2](01-settings.md#22-partial-keys-consulted-only-at-startup-only-on-a-non-core-endpoint-or-pushed-elsewhere-on-write)).
`POST /api/providers/{provider}/test` validates connectivity. When the user picks a cloud entry in
a role dropdown, `update_litellm_model` runs the cloud path (`get_provider_api_key` decrypt + key
injection into LiteLLM). The supported providers are `{"anthropic", "openai", "google"}`.

---

## 6. Hardware-aware Settings UX

The Models tab in Settings adds a per-machine VRAM readout, a per-model fit indicator, a
`num_ctx` slider per role, and a thinking-mode toggle. These are additive on top of the catalog
and lifecycle above; an absent backend field never breaks the UI.

### 6.1 Per-machine identity

`machine_id = socket.gethostname()`, populated by `detect_hardware()`
([model_lifecycle.py:248-267](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L248-L267)).
It namespaces per-machine `user_config` keys but is **never displayed in the UI** — the user
knows which machine they are on. If multi-user concerns ever emerge, swap `socket.gethostname()`
for a hash-anonymized signature in the key-derivation helper (single-line change).

Per-machine override keys (namespaced under the existing `llm.*` convention):

```
llm.{machine_id}.smart_num_ctx          e.g. llm.my-workstation.smart_num_ctx = 8192
llm.{machine_id}.fast_num_ctx
llm.{machine_id}.embed_num_ctx
llm.{machine_id}.thinking_disabled.{model_id}
```

Per-machine (not machine-agnostic) keys are used because machines differ significantly in VRAM —
a single shared key would force one tier to be wrong. The allow-list classifier
(`_classify_litellm_runtime_key` at
[config_metadata.py:103-127](../../services/paper_ingestion/paper_ingestion/services/config_metadata.py#L103-L127))
accepts the prefix patterns `llm.*.{smart,fast,embed}_num_ctx` and
`llm.*.thinking_disabled.{model_id}`. Runtime LiteLLM writes are applied *before* the `user_config`
row is persisted; if the LiteLLM update or reload fails, the write aborts and the DB value is not
advanced.

### 6.2 Catalog fit fields

Three optional fields per catalog entry let the backend compute "fit at chosen `num_ctx`":

| Field | Type | Default if missing | Purpose |
|---|---|---|---|
| `min_vram_gb_at_default_ctx` | number | falls back to `vram_gb` | VRAM to load the model at `default_num_ctx`. |
| `kv_cache_bytes_per_token` | number | `1024` (~1 KB/token) | KV-cache marginal cost per extra token. |
| `default_num_ctx` | number | `min(8192, context_tokens)` | Slider initial position. |
| `max_num_ctx` | number | `context_tokens` | Slider upper bound. |

`supports_thinking: bool` marks thinking-capable entries (Qwen3 family) for the toggle in §6.5.
`tier` is kept as the coarse fallback — entries without the new fields still get a tier-only fit
decision. We never remove `tier`.

### 6.3 Fit math

A pure function on the backend (`compute_vram_fit()` at
[model_lifecycle.py:393-479](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L393-L479)),
mirrored verbatim on the frontend for live what-if previews:

```python
required_vram_gb = (
    entry.min_vram_gb_at_default_ctx
    + max(0, chosen_num_ctx - entry.default_num_ctx)
      * entry.kv_cache_bytes_per_token / 1e9
)
```

| Status | Predicate | Rationale |
|---|---|---|
| `fits` | `required_vram_gb <= hw.vram_gb * 0.85` | 15% headroom for OS, browser, dashboard, Ollama runtime. |
| `partial` | `required_vram_gb <= hw.vram_gb * 1.20` | Ollama CPU offload; up to ~20% over actual VRAM is "slow but works". |
| `unfit` | otherwise | Hard-blocked in UI: option disabled in picker; slider clamps to highest fitting value. |

Special cases:
- **`vram_gb == 0.0`** (probe failure or CPU-only): fit is `unknown` for every local model.
- **Cloud models** (`provider != "ollama"`): skip the math; render a "Cloud" badge. Selectability
  still requires `provider_key_present`.
- **macOS approximate VRAM**: math runs as normal; the badge tooltip surfaces "approximate".

### 6.4 API shape (additive)

Each catalog entry returned from `GET /api/system/models`
([SystemModelsResponse at models/papers.py:404-413](../../services/paper_ingestion/paper_ingestion/models/papers.py#L404-L413))
gains an optional `fit_detail` object:

```jsonc
{
  "id": "qwen3:14b",
  // ... existing fields (provider, vram_gb, tier, status, can_assign, fit, ...)
  "fit_detail": {
    "default": "fits" | "partial" | "unfit" | "cloud" | "unknown",
    "at_num_ctx": 8192,
    "required_vram_gb": 12.0,
    "base_vram_gb": 9.5,
    "base_num_ctx": 8192,
    "default_num_ctx": 8192,
    "max_num_ctx": 32768,
    "kv_cache_bytes_per_token": 1024
  }
}
```

`fit` (the existing string) is unchanged for backward compatibility; `fit_detail` is the new
structured extension. The frontend tolerates an absent `fit_detail` via TypeScript optional typing
— a partially-rolled-out backend never breaks the UI. `GET /api/system/hardware` carries `machine_id`.

Per-machine `num_ctx` writes go through the existing `PUT /api/config/{key}` path; the frontend
constructs the key as `llm.{machineId}.{role}_num_ctx` from the `machine_id` in the hardware response.

### 6.5 UI surface

The fit indicator lives in an in-card "Configure" expander inside each role card in
`IngestionSection.tsx`. The `num_ctx` slider snaps to powers of two
(`{2048, 4096, 8192, 16384, 32768, 65536}`); the fit badge re-renders live as the slider moves.

| Status | Color | Copy |
|---|---|---|
| `fits` | green | "Fits — {required_vram} GB / {available_vram} GB" |
| `partial` | yellow | "Partial offload — {required_vram} GB / {available_vram} GB · slower" |
| `unfit` | red | "Won't fit · try {largest_fitting_num_ctx}" — option disabled in dropdown; slider clamps |
| `cloud` | gray | "Cloud" |
| `unknown` | gray | "Unknown VRAM" |

A "Won't fit" model is a **hard block**: the option renders disabled in the picker and the slider
clamps at the highest fitting snap-step. The user should not be able to silently pick a config that
forces heavy CPU offload — make the failure mode visible at config-time, not at request-latency-time.

The Models tab top shows a single-line readout `{vram_gb} GB VRAM · Tier {tier}` (hostname NOT
displayed); hover/click expands to `vram_source` + `detected_at`. The readout reuses the data
fetched by `IngestionSection` — no new endpoint.

### 6.6 Thinking-mode toggle

Rendered only for catalog entries with `supports_thinking: true`, inside the same expander as the
slider. When checked, the backend includes `extra_body: {think: false}` in the LiteLLM
`/config/update` payload through `update_litellm_model`. This generalizes the hand-tuned default in
`litellm/config.yaml`, which still ships `think: false` for the Qwen3 smart/fast aliases as the
first-boot fallback. Persisted as `llm.{machine_id}.thinking_disabled.{model_id}` (boolean);
first-boot default is `true` for thinking-capable entries, `false` for everything else.

---

## 7. API summary

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/system/hardware` | Detected VRAM, tier, source, `machine_id`. |
| `GET` | `/api/system/models` | All catalog entries with computed `status` + `fit_detail`. |
| `GET` | `/api/system/models/recommendations?role=smart` | Per-role ranked recommendations. |
| `POST` | `/api/system/models/{tag}/pull` | Enqueue the `model.pull` job. |
| `DELETE` | `/api/system/models/{tag}` | Delete if unassigned (409 otherwise). |
| `GET` | `/api/settings/ai` | Operator plane: resolved tier candidates, configured + observed model, candidate issues. |
| `POST` | `/api/settings/ai` | Apply one resolved tier candidate; rejects arbitrary backend/model strings and rolls back on apply failure. |

Existing endpoints unchanged:
- `PUT /api/config/{key}` — handles `llm.{smart,fast,embed}_model` and the per-machine `num_ctx` /
  thinking-mode keys.
- `POST /api/providers/{provider}/test` — provider connectivity probe.
- `GET /api/jobs/{id}/stream` — pull progress.

---

## 8. Active runtime defaults

The runtime authority for model assignment is the LiteLLM config (`litellm/config.yaml`); the
`user_config` rows exist for UI read-back (see [01-settings.md §2.2](01-settings.md#22-partial-keys-consulted-only-at-startup-only-on-a-non-core-endpoint-or-pushed-elsewhere-on-write)
and [03-llm.md §1](03-llm.md)). Current active aliases:

| Alias | Default model | Notes |
|---|---|---|
| `smart` | `ollama/qwen3:8b` | `num_ctx: 8192`, `extra_body.think: false`. 8b (not 14b) is the default because 14b starved the GPU-resident embedder on 16 GB cards; admins restore 14b via Settings → `llm.smart_model`. |
| `fast` | `ollama/qwen3:4b` | `num_ctx: 4096`, `extra_body.think: false`. |
| `embed` | `ollama/qwen3-embedding:4b` | `EMBEDDING_DIMENSION=2560`; `qwen3-embedding:0.6b` (1024d) is the documented smaller-machine fallback. |

Cloud aliases ship commented-out in `litellm/config.yaml` as templates. Changing the embed model to
a different dimension requires a matching Qdrant collection and a re-embed checkpoint (see
`scripts/reembed.py`, which defaults to `qwen3-embedding:4b` / `2560` and gates collection recreation
behind explicit snapshot-confirmation flags).

---

## 9. Risks — accepted

- **Disk sprawl.** Users can pull many models and forget to delete them; no auto-eviction. The
  "Storage: X.X GB used" readout is the only nudge. Acceptable — disk is cheap.
- **Multi-machine label confusion.** A user running JARVIS on two machines sees different Settings
  pages; a shared config file may reference a model not pulled on the other machine. The
  409-on-assigned-delete guard, the `unfit` status, and per-machine `num_ctx` keys are the mitigations.
- **Catalog staleness between releases.** The catalog is static in the package; if Ollama renames or
  removes a tag, pulls fail until a new release ships an updated catalog. `last_reviewed` + the
  startup warning for entries > 90 days old reduce the blast radius. Pull failures surface the
  Ollama error verbatim in the UI.
- **macOS / probe imprecision.** `vram_source` is surfaced; recommendations are still computed but
  flagged. A probe failure yields `unknown` fit, not a false `unfit`.

---

## 10. Cross-contract references

- **[01-settings.md §2.2](01-settings.md#22-partial-keys-consulted-only-at-startup-only-on-a-non-core-endpoint-or-pushed-elsewhere-on-write)**
  — `llm.{smart,fast,embed}_model` and cloud-provider keys at the `user_config` storage plane;
  per-machine `num_ctx` keys.
- **[03-llm.md §1](03-llm.md#1-the-choke-point)** — the LiteLLM call surface that consumes these aliases.

---

## 11. Verified Identifiers

| Citation | File:line | One-line behavior |
|---|---|---|
| `recommend_models(vram_mb)` | libs/jarvis_common/jarvis_common/hardware_fit.py:207-243 | Advisory VRAM-bucket → alias recommendation; returns `HardwareRecommendation`; never mutates state. |
| `recommendations_for_role(role, ...)` | services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:586-645 | Per-role ranking via `build_model_statuses`; sorts by status priority, tier, name. |
| `build_model_statuses(...)` | services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:480-585 | Computes `status`, `fit`, `can_assign`, `assign_blocker`, `fit_detail` per entry. |
| `compute_vram_fit(...)` | services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:393-479 | Computes `fit_detail` including `base_vram_gb` / `base_num_ctx`. |
| `HardwareInfo` dataclass | services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:123-147 | `vram_gb`, `vram_source`, `tier`, `detected_at`, internal `machine_id`. |
| `hardware_tier(vram_gb)` | services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:172-192 | Maps VRAM GB → tier ordinal 0..4. |
| `detect_hardware()` | services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:248-267 | nvidia-smi → macOS → CPU fallback; returns hostname as internal `machine_id`. |
| `get_cached_hardware(state)` | services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:268-283 | 1-hour TTL on `app.state.hw_info`. |
| `_probe_nvidia_smi()` | services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:196-220 | Fixed-arg `subprocess.run`; no shell expansion. |
| `_classify_litellm_runtime_key` | services/paper_ingestion/paper_ingestion/services/config_metadata.py:103-127 | Classifies model-role + per-machine `num_ctx` / `thinking_disabled` keys. |
| `_NUM_CTX_PATTERN` / `_THINKING_DISABLED_PATTERN` | services/paper_ingestion/paper_ingestion/services/config_metadata.py:96-100 | Regex for the dynamic per-machine key patterns. |
| `update_litellm_model(alias, model, db_pool)` | services/paper_ingestion/paper_ingestion/services/litellm_config.py | Rewrites LiteLLM config / POSTs `/config/update`; injects cloud key; applies `num_ctx` + thinking flags. |
| `get_provider_api_key(provider, db_pool)` | services/paper_ingestion/paper_ingestion/services/litellm_config.py | Decrypts a cloud-provider key from `user_config`. |
| `model_catalog.json` (14 entries) | libs/jarvis_common/jarvis_common/data/model_catalog.json | Curated local + cloud catalog; no `mistral-nemo` / `nomic-embed-text`. |
| `GET /api/system/models` | services/paper_ingestion/paper_ingestion/routers/system.py | Returns installed, hardware, current, issues, catalog, recommendations. |
| `GET /api/system/hardware` | services/paper_ingestion/paper_ingestion/routers/system.py | Returns `HardwareInfo.to_dict()` incl. `machine_id`. |
| `SystemModelsResponse` | services/paper_ingestion/paper_ingestion/models/papers.py:404-413 | Loose Pydantic response; additive `fit_detail` does not break clients. |
| Active runtime defaults | litellm/config.yaml | smart=`ollama/qwen3:8b`, fast=`ollama/qwen3:4b`, embed=`ollama/qwen3-embedding:4b`; cloud examples commented out. |
| `PaperIngestionSettings.embedding_model_name` / `embedding_dimension` | services/paper_ingestion/paper_ingestion/config.py | Defaults `qwen3-embedding:4b` / `2560`. |
