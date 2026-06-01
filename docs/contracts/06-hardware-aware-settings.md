# Contract 06 — Hardware-Aware Settings

**Status:** RATIFIED 2026-05-07
**Date:** 2026-05-07
**Scope:** Per-machine VRAM transparency in Settings — fit indicators, `num_ctx` slider per role, thinking-mode toggle for thinking-capable models; validated on the ≈16 GB tier (RTX 5060 Ti) and the ≈48 GB tier (RTX 5880 Ada).
**Depends on:** Contract 05 (Model Lifecycle) — additive only; does not contradict it.
**Related:** [`docs/contracts/01-settings.md`](01-settings.md), [`docs/contracts/03-llm.md`](03-llm.md), [`docs/contracts/05-model-lifecycle.md`](05-model-lifecycle.md).

This contract was reviewed and ratified by the user on 2026-05-07. The
six open questions in §10 have all been resolved; recommendations the user
overturned are documented in §10 with the user's chosen resolution.

---

## 0. Origin

The current Settings UI ([`IngestionSection.tsx`](../../frontend/src/components/settings/IngestionSection.tsx) +
[`ModelSelector.tsx:192-199`](../../frontend/src/components/shared/ModelSelector.tsx#L192-L199))
shows VRAM totals and a tier label, but offers **no `num_ctx` slider** and
**no per-model fit indicator**. An early hardware-fit smoke test demonstrated the consequence:
`qwen3:14b` at 32 768 tokens demanded ~20 GB on a 16 GB GPU, forcing 76% CPU
offload silently. Mitigation today is a hand-edited `litellm/config.yaml`
([`litellm/config.yaml:30-34`](../../litellm/config.yaml#L30-L34)) — not a
user-facing surface. This contract designs the user-facing surface.

---

## 1. Scope and Non-Scope

### 1.1 In scope

1. **Per-machine VRAM readout** at the top of the Models tab in Settings —
   single line: "{vram_gb} GB VRAM · Tier {tier}" (no hostname displayed; see §3).
   Source: existing `GET /api/system/hardware`
   ([`system.py:323-327`](../../services/paper_ingestion/paper_ingestion/routers/system.py#L323-L327)).
2. **Per-model fit indicator** (green/yellow/red badge) inside each role-card in
   `IngestionSection`. Computed from new catalog fields × current `num_ctx` × probed VRAM.
3. **`num_ctx` slider per role** (`smart` / `fast` / `embed`) with live re-render
   of the fit badge as the slider moves. Power-of-2 snap (see §10).
4. **Hard block on "unfit" models.** Unfit options render disabled in the
   model picker with a tooltip explaining why; the slider clamps to the
   highest fitting value if the user drags past the wall. Ollama-supports-CPU-
   offload is no longer the user's problem — they should not be able to
   silently pick a config the box cannot run well.
5. **Per-machine, per-role num_ctx persistence** in `user_config` so the same
   account on the 16 GB box and the 48 GB box keeps distinct values.
6. **Thinking-mode toggle** for thinking-capable models (Qwen3 family).
   Settings exposes a per-model "Disable thinking mode" checkbox; backend
   propagates to LiteLLM via `extra_body.think: false`. Default: thinking
   disabled for known-capable models (matches today's hand-tuned default
   in `litellm/config.yaml:35-37`).

### 1.2 Non-scope (hard)

- **No auto-switching mid-session.** Slider/picker is informational; it does
  not redirect inflight requests. A change to `smart_num_ctx` takes effect on
  the next chat completion only.
- **No new multi-tenant semantics.** The auth/user model exists, but this
  contract only adds per-machine model-runtime controls. It must not redefine
  ownership, sharing, or cross-user visibility.
- **No hostname display.** Internal `machine_id` only — the user knows
  which machine they're on; they don't need their UI to tell them.
- **No cloud VRAM accounting.** Cloud entries (Anthropic / OpenAI) render a
  "Cloud" badge with no VRAM number.
- **No auto-OOM detection from logs.** Out of scope for v1; the user will
  notice via latency. (A future workstream could parse `ollama logs` for
  CPU-offload events; not this sprint.)
- **No per-paper / per-task `num_ctx` override.** Global per-role only.
- **No `temperature` / `top_p` UI in v1.** Those are sampling parameters
  that affect output style (deterministic vs creative); they are orthogonal
  to VRAM safety. Adding them now would touch ~30 LLM call-sites. Deferred
  to a separate session — see §10.6.

---

## 2. Data Model

Today's [`model_catalog.json`](../../libs/jarvis_common/jarvis_common/data/model_catalog.json)
entry carries `vram_gb`, `context_tokens`, and `tier` but no fields that let
us compute "fit at chosen num_ctx". Three new optional fields are added per
entry:

| Field | Type | Default if missing | Purpose |
|---|---|---|---|
| `min_vram_gb_at_default_ctx` | number | falls back to existing `vram_gb` | VRAM needed to load the model with its `default_num_ctx` (FP16 weights + KV cache at that ctx). Filled offline from public model docs / `ollama show`. |
| `kv_cache_bytes_per_token` | number | `1024` (conservative ~1 KB/token) | KV cache marginal cost per extra token. Used to extrapolate VRAM at higher num_ctx. |
| `default_num_ctx` | number | `min(8192, context_tokens)` | Slider initial position; matches what LiteLLM would request if the user had not changed anything. |
| `max_num_ctx` | number | `context_tokens` | Slider upper bound. The model's architecturally supported max. |

`tier` ([`model_lifecycle.py:62-71`](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L62-L71))
is **kept** as the fallback: catalog entries without the new fields filled in
still get a coarse fit decision via the existing tier ordinal logic. We never
remove `tier`.

**Per-machine override store.** New `user_config` keys, namespace conforming
to the existing `llm.*` convention from
[`01-settings.md` §1](01-settings.md#1-storage-tables):

```
llm.{machine_id}.smart_num_ctx        e.g. llm.host-rtx5060.smart_num_ctx = 8192
llm.{machine_id}.fast_num_ctx
llm.{machine_id}.embed_num_ctx
```

**Why per-machine, not machine-agnostic.** Two options were considered:

| Option | Pros | Cons |
|---|---|---|
| (a) `llm.{machine_id}.{role}_num_ctx` | Machines differ significantly in VRAM; a single shared key would either underuse the ≈48 GB tier or over-stretch the ≈16 GB tier. Each backend reads its own machine's key. | Two writes from Settings if the operator explicitly wants the same value on both machines. |
| (b) `llm.{role}_num_ctx` | One key. Simpler. | Forces one tier to be wrong. The 32 768 → 8 192 mitigation only applies to the ≈16 GB tier. |

**Recommendation: (a).** When machines differ by 3× in VRAM, per-machine keys
are strictly better. A future settings revision will need to write three keys
(`smart_num_ctx`, `fast_num_ctx`, `embed_num_ctx`) per machine, each gated on
the live machine_id at request time.

---

## 3. Per-Machine Identity

The `machine_id` segment of the new `user_config` keys must be derived
stably. The `HardwareInfo` dataclass
([`model_lifecycle.py:43-51`](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L43-L51))
gains a `machine_id: str` field populated by `detect_hardware()`. The
`GET /api/system/hardware` response carries it; the frontend uses it to
key reads and writes against `user_config`.

**Resolved 2026-05-07: hostname (internal-only, never displayed in UI).**

`machine_id = socket.gethostname()`. The user explicitly opted to drop the
"this machine: {hostname}" UI strip — the user knows which machine they're
on; they don't need the UI telling them. The hostname is therefore an
internal namespace separator only:

- It appears in DB rows (`user_config.key` like
  `llm.host-rtx5060.smart_num_ctx = 8192`).
- It is returned by `GET /api/system/hardware` so the frontend can compose
  the right key — but the frontend MUST NOT render it.
- See §6.2 for the user-visible machine readout (VRAM + tier only).

If multi-user concerns ever emerge, swap `socket.gethostname()` for a
hash-anonymized signature — single line change in the key-derivation helper.

---

## 4. Fit Math

Pure function on the backend (deterministic; mirrored verbatim on the frontend
for live what-if previews):

```python
required_vram_gb = (
    entry.min_vram_gb_at_default_ctx
    + max(0, chosen_num_ctx - entry.default_num_ctx)
      * entry.kv_cache_bytes_per_token / 1e9
)
```

Three thresholds, with rationale:

| Status | Predicate | Rationale |
|---|---|---|
| `fits` | `required_vram_gb <= hw.vram_gb * 0.85` | 15% headroom for the OS, X server, browser, dashboard nginx, and Ollama runtime overhead. Empirically derived: qwen3:14b at 8k = ~12 GB on 16 GB card → no CPU offload. |
| `partial` | `required_vram_gb <= hw.vram_gb * 1.20` | Ollama supports CPU offload; up to ~20% over actual VRAM is "slow but works". The 32 768-ctx case lived in this band before the mitigation (≈ 20 / 16 = 125%). |
| `unfit` | otherwise | Hard-blocked in UI: option disabled in picker, slider clamps to highest fitting value. |

Special cases:

- **`vram_gb == 0.0`** (probe failure or CPU-only): fit status is `unknown`
  for every local model. Cloud models report `cloud-only`. Probe failure
  defaults to `vram_gb=0.0` and `vram_source="cpu"` per
  [`detect_hardware()` lines 132-138](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L132-L138).
- **Cloud models** (`provider != "ollama"`): skip math entirely. Render a
  "Cloud" badge. Selectability still requires `provider_key_present` per
  [`build_model_statuses` lines 252-275](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L252-L275).
- **macOS approximate VRAM** (`vram_source="macos-approx"`): math runs as
  normal; the badge tooltip surfaces "approximate" so the user can disregard
  borderline calls.

Hardware-fit sanity-check (derived; user can verify offline):

| Case | required_vram | hw.vram | 85% threshold | Status |
|---|---|---|---|---|
| `qwen3:14b` @ 32 768 ctx, 16 GB box | ~20 GB | 16 GB | 13.6 GB | unfit |
| `qwen3:14b` @ 8 192 ctx, 16 GB box | ~12 GB | 16 GB | 13.6 GB | fits |
| `qwen3:72b` @ 40 960 ctx, 48 GB box | ~46 GB | 48 GB | 40.8 GB | partial |
| `qwen3:8b` @ 32 768 ctx, 16 GB box | ~7 GB | 16 GB | 13.6 GB | fits |

These match the observed behavior in `litellm/config.yaml:30-34`. The numbers
in `min_vram_gb_at_default_ctx` and `kv_cache_bytes_per_token` will be filled
in T3-A; the contract guarantees the **shape** of the math, not the exact
constants per model (those are the catalog data fill).

---

## 5. API Surface

Two design options were on the table:

**(a) Extend `SystemModelsResponse.catalog[i]` with a `fit` object.** Single
endpoint, single round-trip. Frontend recomputes "what if num_ctx = X"
client-side using the catalog's `kv_cache_bytes_per_token`.

**(b) New endpoint `GET /api/system/hardware/recommendations?role=X&num_ctx=Y`**
returning per-model fit at the requested num_ctx. One round-trip per slider
move (debounce required).

**Recommendation: (a).** Single round-trip, no API chatter on slider drag,
and the formula stays in lockstep between backend and frontend (both
implement the same closed-form expression — there is nothing the backend can
compute that the frontend cannot).

### 5.1 Additive shape

Each catalog entry returned from `GET /api/system/models`
([`SystemModelsResponse` definition at `models/papers.py:404-413`](../../services/paper_ingestion/paper_ingestion/models/papers.py#L404-L413))
gains an optional field:

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

`fit` (existing string per [`model_lifecycle.py:24`](../../services/paper_ingestion/paper_ingestion/services/model_lifecycle.py#L24))
is unchanged for backward compatibility; `fit_detail` is the new structured
extension. Frontend tolerates an absent `fit_detail` via TypeScript optional
typing — a partially-rolled-out backend never breaks the UI.

`GET /api/system/hardware` gains the new `machine_id` field per §3.

### 5.2 Writes

Per-machine num_ctx writes go through the existing `PUT /api/config/{key}`
path ([`01-settings.md` §1](01-settings.md#1-storage-tables)). The frontend
constructs the key as `llm.{machineId}.{role}_num_ctx` from the
`machine_id` returned in `GET /api/system/hardware`.

`_ALLOWED_CONFIG_KEYS` plus the dynamic classifier in
[`settings_service.py`](../../services/paper_ingestion/paper_ingestion/services/settings_service.py#L117-L164)
accept the **prefix pattern** `llm.*.{smart,fast,embed}_num_ctx` and the
model-scoped pattern `llm.*.thinking_disabled.{model_id}`. Runtime writes are
applied to LiteLLM before the `user_config` row is persisted; if the LiteLLM
update or reload fails, the write aborts and the DB value is not advanced.

---

## 6. UI Surface

Two layout options:

**(a) In-card expander.** Inside each role card in
[`IngestionSection.tsx`](../../frontend/src/components/settings/IngestionSection.tsx),
a "Configure" toggle reveals the slider + fit badge.

**(b) Dedicated "Hardware" tab.** A separate Settings tab.

**Recommendation: (a).** Closer to the model picker; fewer clicks for the
common case ("I picked qwen3:14b, does it fit?"). One-line ASCII sketch
(no hostname per §3; thinking-mode toggle per §6.3):

```
┌─ 15.9 GB VRAM · Tier 2 ──────────────────────────────────────┐
└──────────────────────────────────────────────────────────────┘

Smart model
┌──────────────────────────────────────────────────────────────┐
│ [Qwen3 14B  ▾]   pulled · active · 9.5 GB VRAM · Tier 2     │
│   Configure ▾                                                │
│   ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌  │
│   Context length (num_ctx):                                  │
│     [2048]──[4096]──[8192●]──[16384]──[32768]──[65536]       │
│                                       │ disabled │ disabled  │
│     12 GB / 15.9 GB VRAM · [● Fits]                          │
│                                                              │
│   ☑ Disable thinking mode (recommended for Qwen3)            │
└──────────────────────────────────────────────────────────────┘

Picker dropdown — unfit options visibly disabled:
┌──────────────────────────────────────────────────────────────┐
│ Qwen3 14B           pulled · active · 9.5 GB                 │
│ Qwen3 30B-A3B       pulled · 19 GB · won't fit (try 8K)  ⊘   │
│ Mistral Nemo 12B    pulled · 8 GB                            │
└──────────────────────────────────────────────────────────────┘
```

### 6.1 Fit badge color and copy

| Status | Color | Copy |
|---|---|---|
| `fits` | green | "Fits — {required_vram} GB / {available_vram} GB" |
| `partial` | yellow | "Partial offload — {required_vram} GB / {available_vram} GB · slower" |
| `unfit` | red | "Won't fit · try {largest_fitting_num_ctx}" — option is **disabled** in dropdown; slider clamps |
| `cloud` | gray | "Cloud" |
| `unknown` | gray | "Unknown VRAM" |

The clamp value is the largest snap-step (see §10.2) that satisfies
`required_vram_gb <= 0.85 * hw.vram_gb`. A snap step that produces
`partial` is allowed (yellow); only steps that produce `unfit` are blocked.

### 6.2 Hardware readout

Top of the **Models tab only** (not other Settings tabs), single line:
`{vram_gb} GB VRAM · Tier {tier}`. Hover or click expands to show
`vram_source` (`nvidia-smi` / `macos-approx` / `cpu`) and `detected_at`
from `HardwareInfo`. Not sticky — scrolls with the page. Re-uses the
existing data fetched by `IngestionSection` (no new endpoint).

**Hostname is NOT displayed.** The user knows which machine they're on.

### 6.3 Thinking-mode toggle

Per-model, only rendered for entries with `supports_thinking: true` in the
catalog. Inside the same in-card expander as the slider:

```
☑ Disable thinking mode (recommended for Qwen3)
```

When checked, backend includes `extra_body: {think: false}` in the
LiteLLM `/config/update` payload through `update_litellm_model`
([`litellm_config.py:251-292`](../../services/paper_ingestion/paper_ingestion/services/litellm_config.py#L251-L292)).
This generalizes the hand-tuned default at `litellm/config.yaml:35-37` —
the YAML still ships with `think: false` for `qwen3:14b`, but the
authoritative source becomes the user_config key.

Persisted as: `llm.{machine_id}.thinking_disabled.{model_id}` boolean.
First-boot default: `true` for known thinking-capable models (Qwen3
family — to be marked in the catalog by T3-A), `false` for everything else.

---

## 7. Migration

No database migration required. `user_config` is a free-form key-value JSONB
table per [`01-settings.md` §1](01-settings.md#1-storage-tables); new keys
appear simply by `PUT`-ing them.

### 7.1 First-boot defaults

When a per-machine num_ctx key is **absent**, the backend serves
`default_num_ctx` from the catalog entry. The frontend renders the slider at
that position. The user sees a sensible starting point on a fresh box; no
migration runs. (For `qwen3:14b` on a 16 GB box, `default_num_ctx = 8192`
will reproduce today's `litellm/config.yaml` mitigation automatically.)

### 7.2 Existing config keys

`llm.smart_model`, `llm.fast_model`, `llm.embed_model` are unchanged. The
new `llm.{machine_id}.{role}_num_ctx` keys live alongside them.

### 7.3 Catalog data fill

T3-A fills `min_vram_gb_at_default_ctx`, `kv_cache_bytes_per_token`,
`default_num_ctx`, `max_num_ctx` for all 15 entries. Defaults per §2 mean
**any entry left unfilled still works** (degrades to today's tier-only fit
decision). Backward-compatible.

### 7.4 LiteLLM config integration

Runtime writes now propagate per-machine `num_ctx` into LiteLLM's runtime
config (`litellm_params.extra_body.num_ctx`) via `update_litellm_model` before
DB persistence. Cloud-assigned models intentionally skip `num_ctx` propagation
while still allowing the UI state to persist. Thinking toggles update every
currently assigned role using the same model id; `thinking_disabled=True`
adds `extra_body.think: false`, and `False` removes only that flag while
preserving other hardware parameters such as `num_ctx`.

---

## 8. Test Plan

### 8.1 Backend unit tests

In `services/paper_ingestion/tests/test_model_lifecycle.py` (extend existing):

- `test_compute_vram_fit_qwen3_14b_partial_at_32768_on_16gb` — regression.
  Asserts `unfit` (the actual measured state) at 32 768 on 16 GB.
- `test_compute_vram_fit_qwen3_14b_fits_at_8192_on_16gb` — mitigation.
  Asserts `fits`.
- `test_compute_vram_fit_falls_back_to_tier_when_field_absent` — entries
  without `min_vram_gb_at_default_ctx` use existing tier ordinal.
- `test_compute_vram_fit_skips_cloud_models` — cloud entries return
  `"cloud"`.
- `test_compute_vram_fit_handles_zero_vram_probe_failure` — `vram_gb=0.0`
  → all local entries return `"unknown"`, not `"unfit"`.
- `test_machine_id_uses_hostname` — verifies `detect_hardware().machine_id`
  matches `socket.gethostname()`.

### 8.2 Frontend RTL tests

In `frontend/src/components/settings/__tests__/IngestionSection.test.tsx`:

- Slider renders for each LLM role; persists num_ctx via `setConfig` on
  release; re-reads on mount.
- Fit badge color flips green → yellow → red as the slider crosses the 85%
  and 120% thresholds (mock `system-models` query response).
- Hardware strip shows hostname + VRAM + tier; clicking expands to show
  source + detected_at.

### 8.3 E2E

Extend `frontend/e2e/settings/ingestion.spec.ts`:

- The Hardware strip shows hostname + VRAM on Settings load.
- Moving the `qwen3:14b` slider from 8 k to 32 k flips the badge to red and
  surfaces the suggested-fit num_ctx hint.
- Reload the page; the slider remembers its previous position (proves
  `user_config` round-trip).

---

## 9. Rollout Sequence

Three independently reversible phases:

| Phase | What ships | Backward compatibility |
|---|---|---|
| T3-A | Catalog data fill: new optional fields on all 15 entries. | Field-additive; old code reads existing fields untouched. |
| T3-B | Backend `compute_vram_fit` + `SystemModelsResponse.catalog[i].fit_detail` + `GET /api/system/hardware` adds `machine_id`. | Response-additive; old frontend ignores new field. |
| T3-C | Frontend slider, fit badge, machine strip. Tolerates missing `fit_detail` via TS optional. | Backend can roll back without breaking the new UI (it just renders without fit indicators). |

Each phase is independently mergeable. T3-A and T3-B may be parallelized
(independent files); T3-C must dispatch after T3-B since the frontend reads
the new response field.

---

## 10. Resolutions (user-ratified 2026-05-07)

The six open questions from the design pass were resolved by the user.
Recorded here for executor reference.

### 10.1 Per-machine identity → hostname (internal-only, not displayed)
`socket.gethostname()` is the `machine_id`. It namespaces `user_config`
keys (`llm.{hostname}.{role}_num_ctx`) but the UI **never displays it**.
The user explicitly opted to drop the hostname strip — they know which
machine they're on. See §3.

### 10.2 Slider granularity → power-of-2 snap
Snap to `{2048, 4096, 8192, 16384, 32768, 65536}`. The slider only stops
at these values. Matches typical Ollama num_ctx, avoids confusing
sub-power VRAM jumps, gives clear "try N" suggestions when blocking.

### 10.3 "Won't fit" → hard block
Originally recommended as soft warning. **User overruled: hard block.**
Unfit options render disabled in the model picker (grayed out, not
clickable) with tooltip "Won't fit at current num_ctx — try {N}". Slider
clamps at the highest fitting snap-step. The user should not be able to
silently pick a config that forces 76% CPU offload; make the failure mode
visible at config-time, not at request-latency-time.

### 10.4 Thinking-mode toggle → IN scope (originally declared out of scope)
Originally declared out of scope on the rationale that
`extra_body.think: false` is already shipped via `litellm/config.yaml`.
**User overruled:** behavior should be transparent and toggleable. UI
implications:
- Add `supports_thinking: bool` flag to the catalog (T3-A populates it
  for Qwen3 family).
- New per-model `user_config` key:
  `llm.{machine_id}.thinking_disabled.{model_id}` (boolean).
- New checkbox in the in-card expander (§6.3).
- Backend: `update_litellm_model` reads the flag and includes
  `extra_body: {think: false}` when set. The hand-edited
  `litellm/config.yaml:35-37` default stays as the first-boot fallback
  for the known-broken Qwen3 case.
First-boot default: `true` for `supports_thinking: true` entries; `false`
for the rest.

### 10.5 Sticky Hardware strip → not sticky
The strip renders once at the top of the **Models tab only** (not Topics,
Authors, etc.) and scrolls with the page. With hostname dropped (§10.1),
the strip is just a thin VRAM/tier readout — no need for sticky
positioning. See §6.2.

### 10.6 Temperature / top_p → out of v1 scope
`temperature` and `top_p` control LLM output style (deterministic vs
creative); they are orthogonal to VRAM safety. Adding them now would
touch ~30 LLM call-sites across both services. Deferred to a future
session — the v1 ship is "VRAM safety + thinking-mode toggle".

---

## Verified Identifiers

Every cited identifier was Read in this design session against the
2026-05-07 working tree (no Edits applied during the session). Re-Read
before consuming this contract as evidence for implementation.

| Citation | File:line | Behavior |
|---|---|---|
| `HardwareInfo` dataclass | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:121-147` | Frozen dataclass: `vram_gb`, `vram_source`, `tier`, `detected_at`, and internal `machine_id`; `to_dict()` returns all fields. |
| `hardware_tier(vram_gb)` | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:171-192` | Maps VRAM GB → ordinal 0..4. Kept as fallback when new fields missing. |
| `_probe_nvidia_smi()` | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:195-220` | `subprocess.run([...], timeout=2.0)` — no shell expansion; returns max(memory.total) / 1024 GB or None. |
| `_probe_macos_vram()` | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:223-247` | Parses `system_profiler SPDisplaysDataType` "VRAM" line; imprecise. |
| `detect_hardware()` | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:250-267` | Probes nvidia-smi → macOS → CPU fallback; returns hostname as internal `machine_id`. |
| `get_cached_hardware(state)` | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:270-283` | 1-hour TTL on `app.state.hw_info` / `hw_info_at`; refreshes silently on miss. |
| `compute_vram_fit(...)` | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:395-479` | Computes `fit_detail`, including `base_vram_gb` and `base_num_ctx` for frontend what-if math. |
| `build_model_statuses(...)` | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:482-585` | Computes `status`, `fit`, `can_assign`, `assign_blocker`, and `fit_detail` per entry. |
| `_classify_litellm_runtime_key` | `services/paper_ingestion/paper_ingestion/services/settings_service.py:125-149` | Classifies role assignments plus per-machine `num_ctx` and `thinking_disabled` keys. |
| `_apply_litellm_runtime_update` | `services/paper_ingestion/paper_ingestion/services/settings_service.py:1082-1148` | Applies LiteLLM runtime updates before DB writes; cloud model `num_ctx` writes return without alias mutation. |
| `update_litellm_model` | `services/paper_ingestion/paper_ingestion/services/litellm_config.py:178-291` | Applies pending `num_ctx` and thinking flags; sends cloud aliases through `/config/update` without `num_ctx`. |
| `recommendations_for_role(role, ...)` | `services/paper_ingestion/paper_ingestion/services/model_lifecycle.py:294-322` | Sorts entries by readiness priority for one role. |
| `model_catalog.json` (15 entries) | `libs/jarvis_common/jarvis_common/data/model_catalog.json:1-242` | Today's schema — no `min_vram_gb_at_default_ctx` / `kv_cache_bytes_per_token`. T3-A adds them. |
| `GET /api/system/models` | `services/paper_ingestion/paper_ingestion/routers/system.py:229-320` | Returns `installed`, `hardware`, `current`, `issues`, `catalog`, `recommendations`. T3-B extends `catalog[i]`. |
| `GET /api/system/hardware` | `services/paper_ingestion/paper_ingestion/routers/system.py:323-327` | Returns `HardwareInfo.to_dict()`. T3-B adds `machine_id`. |
| `GET /api/system/models/recommendations?role=` | `services/paper_ingestion/paper_ingestion/routers/system.py:330-350` | Per-role recommendations; unchanged by this contract. |
| `SystemModelsResponse` | `services/paper_ingestion/paper_ingestion/models/papers.py:404-413` | Pydantic response with `catalog: list[dict[str, Any]]` — schema is loose so additive `fit_detail` does not break clients. |
| `IngestionSection` | `frontend/src/components/settings/IngestionSection.tsx:97-305` | Renders LLM model dropdowns via `ModelSelector`; no slider today. T3-C adds in-card expander. |
| `ModelSelector` | `frontend/src/components/shared/ModelSelector.tsx:152-464` | Wraps `<Select>` with hardware summary at lines 192-199 (VRAM + Tier display today). T3-C feeds it `fit_detail` per option. |
| `litellm/config.yaml smart` | `litellm/config.yaml:25-37` | Today's hand-tuned mitigation: `num_ctx: 8192`, `extra_body.think: false`. T3-B replaces hardcoded num_ctx with the user-configured value. |
| 16 GB VRAM oversubscription finding | qwen3:14b at 32k context → ~20 GB → 76% CPU offload on 16 GB box; mitigated by setting `num_ctx: 8192`. |
| Qwen-thinking output-budget finding | Thinking-mode burns output budget; solved by `extra_body.think: false` (already shipped). Out of scope here. |
| Hardware-aware Settings user request | The user request that motivates this contract. |
