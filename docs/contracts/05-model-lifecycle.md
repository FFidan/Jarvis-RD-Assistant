# Contract 05 — Model Lifecycle

**Status:** DRAFT — pending plan
**Date:** 2026-05-03
**Scope:** Model catalog, hardware-aware recommendations, pull-on-demand UI, cloud model integration, Settings UI GHOST surface fix
**Depends on:** Phase C (embedding swap, spec at `docs/specs/2026-05-03-c-embedding-upgrade.md`)
**Related:** `docs/contracts/03-llm.md`, `docs/specs/2026-05-03-c-embedding-upgrade.md`

---

## 0. Scope and Non-Goals

### In scope

1. **Curated catalog** (`~12 entries`) — local Ollama + cloud models, role-tagged, VRAM-annotated.
2. **Hardware detection** — VRAM probe at startup, TTL-cached, drives recommendations.
3. **Recommendation function** — maps detected VRAM to 3 tiers; returns ranked candidates per role (smart / fast / embed).
4. **Status enum** — 6 values: `active | pulled | downloadable | unfit | cloud_active | cloud_required`.
5. **Pull-on-demand lifecycle** — explicit user action with confirm dialog showing disk/VRAM cost; procrastinate `model.pull` task (#20); progress via existing SSE jobs stream.
6. **Delete lifecycle** — explicit user action; fails loudly if model is currently assigned.
7. **Cloud model integration** — catalog entries for Anthropic/OpenAI cloud aliases; Settings UI fix for GHOST surface (API keys exist but no model-select UI).
8. **Hard migration** — `nomic-embed-text` and `mistral-nemo:12b` removed from catalog and from `litellm/config.yaml` defaults. Phase C rebuilds Qdrant. No legacy user protection needed (single-user, pre-launch).

### Non-goals (hard)

- **Empirical eval harness** — no `scripts/eval_models.py`, no per-model STEM score tracking. No per-discipline benchmark claims. The honest answer is: benchmarks exist for math (AIME, MATH-500), nothing defensible for "ML paper summarization vs history paper summarization". We do not ship theater.
- **Work-style onboarding wizard** — "are you a historian or a physicist?" flows. Not worth the complexity. The tier-based recommendation is sufficient.
- **Per-task fallback chains** — "use Claude if qwen3:14b fails". Adds latency routing complexity; the existing `smart`/`fast`/`embed` alias system already handles this at the LiteLLM layer.
- **Multi-machine aggregation** — the 48GB machine is a second setup context, not a networked peer. Settings show hardware for the machine the backend runs on. The user labels machines manually.
- **Disk-budget enforcement** — no auto-eviction when storage exceeds a threshold. User decides what to pull and delete.
- **Model pinning per paper** — no per-document "re-analyze with model X" feature. Global alias assignment only.
- **Automatic pull on config change** — changing the assigned model does NOT trigger a pull. A clear error state (`unfit` status) and a "Pull" CTA handle the gap.

---

## 1. Model Catalog

### 1.1 Location

```
libs/jarvis_common/jarvis_common/data/model_catalog.json
```

Bundled inside the Python package. Loaded via `importlib.resources` so it works in Docker, installed wheel, and editable installs identically. No runtime fetch; no auto-update.

### 1.2 Entry Schema

```json
{
  "id": "qwen3:14b",
  "name": "Qwen3 14B",
  "provider": "ollama",
  "ollama_tag": "qwen3:14b",
  "roles": ["smart"],
  "vram_gb": 9.5,
  "disk_gb": 9.2,
  "context_tokens": 32768,
  "license": "Apache 2.0",
  "tier": 2,
  "description": "Strong reasoning for scientific text. Fits 16 GB VRAM.",
  "notes": "",
  "last_reviewed": "2026-05-03"
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique key. For Ollama: `name:tag`. For cloud: `provider/model-id`. |
| `name` | string | Human display name. |
| `provider` | `"ollama" \| "anthropic" \| "openai"` | Drives status computation. |
| `ollama_tag` | string \| null | Null for cloud entries. Must match `ollama list` NAME column exactly. |
| `roles` | `("smart" \| "fast" \| "embed")[]` | Which LiteLLM aliases this entry can serve. |
| `vram_gb` | number | Peak active VRAM (FP16 weights + KV cache at 4k context). 0 for cloud. |
| `disk_gb` | number | Compressed model file on disk. 0 for cloud. |
| `context_tokens` | number | Published context window. |
| `license` | string | SPDX or name. Apache 2.0 = commercial OK. CC BY-NC = flag in UI. |
| `tier` | 0–4 | Minimum hardware tier required (see §3). |
| `description` | string | 1 sentence. No benchmark theater. |
| `notes` | string | Caveats, known issues. E.g. "thinking model — strips `<think>` blocks". |
| `last_reviewed` | ISO date | When this entry was last verified against Ollama registry. |

Cloud entries omit `ollama_tag`, set `vram_gb=0`, `disk_gb=0`, `tier=0`.

### 1.3 Curated Entries (~12)

| id | Name | Role(s) | VRAM GB | Disk GB | Tier | License |
|---|---|---|---|---|---|---|
| `qwen3:4b` | Qwen3 4B | smart, fast | 3.5 | 2.5 | 1 | Apache 2.0 |
| `qwen3:8b` | Qwen3 8B | smart | 5.5 | 4.9 | 1 | Apache 2.0 |
| `qwen3:14b` | Qwen3 14B | smart | 9.5 | 9.2 | 2 | Apache 2.0 |
| `qwen3:30b-a3b` | Qwen3 30B-A3B (MoE) | smart | 19 | 17 | 3 | Apache 2.0 |
| `gemma3:12b` | Gemma 3 12B | smart | 8.5 | 8.1 | 2 | Apache 2.0 |
| `llama4:scout` | Llama 4 Scout | smart | 14 | 12 | 2 | Llama 4 Community |
| `qwen3-embedding:0.6b` | Qwen3 Embedding 0.6B | embed | 1.2 | 0.6 | 0 | Apache 2.0 |
| `mxbai-embed-large` | MXBai Embed Large v1 | embed | 0.8 | 0.7 | 0 | Apache 2.0 |
| `anthropic/claude-sonnet-4-6` | Claude Sonnet 4.6 | smart | 0 | 0 | 0 | Commercial |
| `anthropic/claude-haiku-4-5` | Claude Haiku 4.5 | smart, fast | 0 | 0 | 0 | Commercial |
| `openai/gpt-4o` | GPT-4o | smart | 0 | 0 | 0 | Commercial |
| `openai/text-embedding-3-small` | OpenAI Embed 3-Small | embed | 0 | 0 | 0 | Commercial |

**Why these and not others:**
- Gemma 4 / Qwen 3.6-Plus / DeepSeek V4 omitted — Ollama tags unconfirmed at time of writing. Add via `last_reviewed` update when tags stabilize.
- `mistral-nemo:12b` — excluded (hard path; nomic-embed-text likewise excluded).
- `mxbai-embed-large` — kept as embed fallback in case `qwen3-embedding:0.6b` tag is not yet in Ollama at Phase C execution time (risk §8.1).
- `llama4:scout` — included for 10M-token context window (long paper chains). Llama 4 Community license allows commercial with attribution.
- Cloud embed (`openai/text-embedding-3-small`) — included for users who prefer cloud-only. Requires Phase C's `EMBEDDING_DIMENSION` to be configurable (1536 for this model vs 1024 for Qwen3). Gated by cloud API key presence.

### 1.4 Catalog Staleness

`last_reviewed` on each entry is the protection mechanism. If it is > 90 days old, the backend logs a WARNING on startup (not an error — staleness ≠ broken). An implementation task may add a CI job that checks for newer Ollama tags.

**The catalog is NOT a live registry.** We do not fetch `https://ollama.com/api/search` at runtime. This is a deliberate non-goal.

---

## 2. Hardware Detection

### 2.1 VRAM Probe

At startup (and once per hour TTL via `_OllamaProbeCache`-style pattern), the backend runs:

```python
# Primary: nvidia-smi (Linux + Windows)
nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits
# Returns MB; divide by 1024 for GB

# Fallback: macOS Metal (approximate)
system_profiler SPDisplaysDataType | grep "VRAM"
# Returns "VRAM (Total): X MB" — imprecise; mark source as "approximate"

# Fallback: no GPU
# Returns vram_gb=0, source="cpu"
```

Result cached in `app.state.hw_info`:

```python
@dataclass
class HardwareInfo:
    vram_gb: float         # 0 if CPU-only
    vram_source: Literal["nvidia-smi", "macos-approx", "cpu"]
    detected_at: datetime
```

**Failure mode:** `nvidia-smi` not installed → `vram_source="cpu"`, `vram_gb=0`. The recommendation function degrades gracefully to cloud-only recommendations.

**No subprocess injection risk:** `nvidia-smi` is called with a fixed argument list via `subprocess.run([...], capture_output=True)`, not via shell string expansion.

### 2.2 API Surface

```
GET /api/system/hardware
```

Returns:
```json
{
  "vram_gb": 16.0,
  "vram_source": "nvidia-smi",
  "tier": 2,
  "detected_at": "2026-05-03T10:00:00Z"
}
```

Frontend calls this once on Settings page load. No polling.

---

## 3. Hardware Tiers and Recommendation Function

### 3.1 Tier Table

| Tier | VRAM GB | Label |
|---|---|---|
| 0 | < 4 | CPU / low-VRAM — cloud recommended |
| 1 | 4–10 | Small GPU (e.g. 8 GB) |
| 2 | 10–20 | Mid GPU (e.g. 16 GB RTX 5060 Ti) |
| 3 | 20–40 | High GPU (e.g. 24 GB) |
| 4 | ≥ 40 | Large GPU (e.g. 48 GB) |

### 3.2 Recommendation Function (~50 lines)

```python
def recommend_models(
    hw: HardwareInfo,
    role: Literal["smart", "fast", "embed"],
    catalog: list[CatalogEntry],
    cloud_api_keys: dict[str, bool],  # provider → key present?
) -> list[RecommendedModel]:
    """
    Returns catalog entries in preference order for the given role and hardware.
    """
    tier = hw.tier
    results = []

    for entry in catalog:
        if role not in entry.roles:
            continue
        if entry.provider == "ollama":
            if entry.tier <= tier:
                results.append(RecommendedModel(entry=entry, fit="recommended"))
            elif entry.tier == tier + 1:
                results.append(RecommendedModel(entry=entry, fit="stretch"))
            else:
                results.append(RecommendedModel(entry=entry, fit="unfit"))
        else:  # cloud
            provider = entry.provider
            key_present = cloud_api_keys.get(provider, False)
            if key_present:
                results.append(RecommendedModel(entry=entry, fit="available"))
            else:
                results.append(RecommendedModel(entry=entry, fit="key_required"))

    # Sort: recommended first, then stretch, then available (cloud with key),
    # then key_required, then unfit.
    PRIORITY = {"recommended": 0, "stretch": 1, "available": 2, "key_required": 3, "unfit": 4}
    results.sort(key=lambda r: PRIORITY[r.fit])
    return results
```

`fit` value is for sorting only; it does not map 1:1 to the status enum in §4 (status is about pull state; fit is about hardware suitability).

**What this deliberately does NOT do:**
- No per-discipline scoring ("better for STEM" vs "better for humanities"). No benchmark claims in UI.
- No weighting by benchmark score. AIME 89.2% for Gemma 4 is honest signal for math-heavy work; there is nothing defensible for "scientific literature review quality". We describe models by what we can verify: size, context window, license.
- No "auto-select and pull best model". The user selects; we recommend.

---

## 4. Status Enum

Every catalog entry, when returned via the API, carries a `status` field computed at request time by crossing catalog state against Ollama's `/api/tags` response and the current LiteLLM config.

| Status | Meaning |
|---|---|
| `active` | Currently assigned to at least one role alias in LiteLLM config; running in Ollama. |
| `pulled` | Downloaded in Ollama but not currently assigned to any role alias. |
| `downloadable` | In catalog, not yet pulled; hardware tier is sufficient. |
| `unfit` | In catalog, not yet pulled; hardware tier is insufficient (vram_gb too low). |
| `cloud_active` | Cloud entry; API key present; assigned to at least one role alias. |
| `cloud_required` | Cloud entry; API key present; not currently assigned. |

`unfit` is NOT shown as an error if the model is merely recommended but not yet downloaded. It is shown as the status so the user understands why the "Pull" button is styled differently.

Cloud entries without an API key are NOT shown in the UI at all (they provide no actionable path).

**Computation order:**
1. Fetch Ollama `/api/tags` → `pulled_tags: set[str]`
2. Read current LiteLLM config → `active_tags: set[str]`
3. Read `hw.tier`
4. For each catalog entry: compute status as above.

---

## 5. Pull / Delete Lifecycle

### 5.1 Pull (download model)

```
POST /api/system/models/{ollama_tag}/pull
```

- Returns immediately with `{ "job_id": "<uuid>", "status": "queued" }`.
- Dispatches procrastinate task `model.pull` (kind #20 in `task_registry.py`).
- Progress streamed via `GET /api/jobs/{job_id}/stream` (existing SSE bridge — no new machinery needed).

**Procrastinate task signature:**
```python
@app.task(name="model.pull")
async def model_pull(context, *, job_id: str, user_id: int | None, ollama_tag: str) -> None:
    # Calls POST http://ollama:11434/api/pull with {"name": ollama_tag, "stream": true}
    # Parses streamed JSON lines; calls update_progress() per layer
    # On completion: logs success
    # On failure: raises (procrastinate marks job "failed"; SSE bridge emits error event)
```

**Frontend confirm dialog** (shown before POST):
- Model name + version
- Disk: `X.X GB`
- VRAM required: `X.X GB` (vs detected: `Y.Y GB`)
- If unfit: "Warning: this model requires X.X GB VRAM; your system has Y.Y GB. It may run slowly via CPU offload."
- Button: "Download" / "Cancel"

No automatic pull on Settings change. No silent pull on startup.

### 5.2 Delete (remove local model)

```
DELETE /api/system/models/{ollama_tag}
```

- **Guard:** Fails with 409 if `ollama_tag` is currently assigned to an active role alias.
  ```json
  { "error": "Cannot delete model currently assigned to role 'smart'. Reassign first." }
  ```
- If unassigned: calls `DELETE http://ollama:11434/api/delete`.
- Synchronous (Ollama delete is fast — file removal, no inference).
- Returns 204 on success.

### 5.3 Assign (change role mapping)

```
POST /api/settings/llm.smart_model  (existing settings endpoint)
```

The existing `update_litellm_model()` + `reload_litellm()` path handles this. No new endpoint needed.

The Settings dropdown sends the catalog entry `id` as the new value. The backend maps `id` → Ollama tag → LiteLLM `ollama/<tag>` or `anthropic/<model>`.

If the selected model is `downloadable` (not yet pulled): the POST returns 422 with `{ "error": "Model not pulled. Pull it first." }`. Frontend guides user to the Pull CTA.

---

## 6. Frontend Contract

### 6.1 Settings — Models Tab

The Models section of Settings gets a structured layout:

```
Models
────────────────────────────────────────────────────────────────
  Smart model      [Qwen3 14B ▾]   [pulled]   [Make Active]
  Fast model       [Qwen3 4B  ▾]   [active]
  Embedding model  [Qwen3 Emb ▾]   [active]

  Storage: 22.4 GB used  |  Hardware: 16 GB VRAM (Tier 2)
  ─────────────────────────────────────────────────────────────
  Available models
  ┌────────────────┬──────────┬──────────┬─────────┬──────────┐
  │ Model          │ Role(s)  │ Status   │ VRAM    │ Action   │
  ├────────────────┼──────────┼──────────┼─────────┼──────────┤
  │ Qwen3 30B-A3B  │ smart    │ unfit    │ 19 GB   │ Pull ⚠  │
  │ Gemma 3 12B    │ smart    │ download │  8.5 GB │ Pull     │
  │ Claude Sonnet  │ smart    │ key req. │ cloud   │ →Provid. │
  └────────────────┴──────────┴──────────┴─────────┴──────────┘
```

- Role dropdowns show only entries whose status is NOT `unfit`. `unfit` entries appear in the table with a warning badge but cannot be selected as active.
- "→ Providers" CTA on cloud entries navigates to the Providers tab (GHOST surface fix).
- `cloud_active` / `cloud_required` entries show "cloud" in VRAM column.
- Status badges use color: `active` = green, `pulled` = blue, `downloadable` = grey, `unfit` = orange, `cloud_active` = purple, `cloud_required` = purple outline.

### 6.2 GHOST Surface Fix

Current state: `llm.anthropic.api_key`, `llm.openai.api_key`, `llm.google.api_key` are fully wired in `user_config` (encrypted BYTEA), `POST /api/providers/{provider}/test` exists, `get_provider_api_key()` + `update_litellm_model()` handle cloud paths — **but there is no dropdown in Settings to select a cloud model as smart/fast**. The API keys are useful today only for test-ping. This sprint adds the model-select dropdown so cloud entries become selectable.

Implementation: when the user picks a cloud entry in the smart/fast dropdown, `update_litellm_model("smart", "anthropic/claude-sonnet-4-6", db_pool)` runs the existing cloud path (`_post_config_update_for_cloud` which calls `get_provider_api_key` + writes key to LiteLLM). No new backend code; only frontend state wiring.

### 6.3 Download Progress UX

On click of "Pull" button (after confirm dialog):
1. `POST /api/system/models/{tag}/pull` → `job_id`
2. Subscribe `GET /api/jobs/{job_id}/stream` (reuse `lib/sse.ts`)
3. Show inline progress bar in the model table row (same pattern as paper processing jobs)
4. On completion: refresh model list via `GET /api/system/models`
5. On error: show toast with job error message

No new SSE infrastructure. Existing job store handles it.

---

## 7. Hard Non-Goals (why)

| Non-goal | Reason |
|---|---|
| Empirical eval harness | AIME/MATH benchmarks test narrow reasoning; no benchmark exists for "scientific paper understanding". Shipping a score would be theater. |
| Work-style onboarding | "Are you a historian?" is not useful for model selection. Tier + context window covers the real decision axis. |
| Per-task fallback chains | Latency routing is an operations concern. The `smart`/`fast`/`embed` aliases already decouple tasks from concrete models. Adding a fallback chain layer adds complexity with no clear user benefit. |
| Multi-machine aggregation | Two separate backend instances, two separate Settings pages. User understands this. Aggregation adds distributed-systems complexity for zero UX gain in a single-user system. |
| Disk-budget enforcement | Auto-eviction of models is dangerous — a model used by a running job could be deleted. User controls disk. |
| Model pinning per paper | Global alias assignment is sufficient. Per-document model tracking is a data model change with unclear user value. |

---

## 8. Risks — Pre-Mitigated

### 8.1 `qwen3-embedding:0.6b` not in Ollama at Phase C execution time

**Mitigation:** `mxbai-embed-large` is in the catalog as embed fallback. Phase C spec §6 already calls this out. If `qwen3-embedding:0.6b` is unavailable, executor falls back to `mxbai-embed-large` (1024d, Apache 2.0). Cloud embed (`openai/text-embedding-3-small`) is a third option for users with OpenAI keys.

### 8.2 Catalog entry VRAM values are wrong (model update, quantization variant)

**Mitigation:** `last_reviewed` field surfaces staleness. VRAM values in catalog are for the default Ollama quantization (Q4_K_M or equivalent). The confirm dialog shows the catalog value with a footnote "Approximate; actual usage may vary by ±20%." We do not claim precision we don't have.

### 8.3 Ollama renames a tag between catalog entry and pull

**Mitigation:** If `POST /api/system/models/{tag}/pull` returns an Ollama error (tag not found), the procrastinate task fails, the SSE bridge emits an error event, and the UI shows the error message verbatim from Ollama. The user knows what to do. The catalog's `last_reviewed` field is the long-term signal that something needs updating.

### 8.4 30 GB+ pull on a slow connection blocks the UI for a long time

**Mitigation:** The pull is a background procrastinate job. The user can close the Settings tab, come back, and the job is still streaming. The confirm dialog shows disk GB so the user knows what they're starting. There is no timeout on the procrastinate task (or it is very long — 30 min).

### 8.5 Cloud model selected but API key removed later

**Mitigation:** At Settings load, `GET /api/system/models` recomputes status. If the key is gone, the cloud entry becomes `cloud_required` and the UI shows "Key required" in the dropdown with a CTA to re-enter the key. LiteLLM will 401 on actual calls, which surfaces as a chat error toast. Not silent.

### 8.6 macOS VRAM detection is imprecise

**Mitigation:** `vram_source="macos-approx"` is surfaced in `GET /api/system/hardware`. The frontend shows "(approximate)" next to the VRAM readout on macOS. Recommendations are still computed — just flagged. A user on a Mac mini M4 Ultra (192 GB unified memory) will see Tier 4 recommendations, which is correct.

---

## 9. Risks — Accepted

### 9.1 Disk sprawl

Users can pull many models and forget to delete them. We do not auto-evict. The "Storage: X.X GB used" readout in Settings is the only nudge. This is acceptable — disk is cheap, deleted models cause more support burden than used disk.

### 9.2 Multi-machine label confusion

A user running JARVIS on both 16 GB and 48 GB machines sees different Settings pages. If they share a config file or copy `.env`, the LiteLLM config may reference a model not pulled on the other machine. The 409-on-unassigned-model guard and the `unfit` status badge are the only mitigations. This is acceptable for a single-user system.

### 9.3 Catalog becomes stale between releases

The catalog is static in the package. If Ollama renames `qwen3:14b` → `qwen3:14b-q4_K_M` or removes a model entirely, users see "Pull failed" errors until a new JARVIS release ships an updated catalog. The `last_reviewed` field and a startup warning for entries > 90 days old reduce the blast radius. Accepted as a known tradeoff of the static-catalog decision.

---

## 10. Migration Path (hard path — no existing user caution needed)

| Step | Action |
|---|---|
| Phase C | Pull `qwen3-embedding:0.6b`, rebuild Qdrant with 1024d, set `EMBEDDING_DIMENSION=1024`, update `litellm/config.yaml` embed alias. Full spec at `docs/specs/2026-05-03-c-embedding-upgrade.md`. |
| This sprint | Remove `nomic-embed-text` and `mistral-nemo:12b` from `litellm/config.yaml`. Delete their catalog entries (they were never in the catalog; they are just the current live config values). |
| This sprint | Set new defaults: `smart=qwen3:14b`, `fast=qwen3:4b`, `embed=qwen3-embedding:0.6b`. |
| This sprint | Pull new smart/fast defaults on first boot if not already present — this is the ONE exception to "no auto-pull": the initial default models are pulled silently during stack startup (analogous to current behavior where `docker compose up` downloads Ollama models). Not a background job; Ollama's own `OLLAMA_PRELOAD` or startup script. |

---

## 11. API Summary

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/system/hardware` | Detected VRAM, tier, source |
| `GET` | `/api/system/models` | All catalog entries with computed status |
| `GET` | `/api/system/models/recommendations?role=smart` | Sorted recommendations for role |
| `POST` | `/api/system/models/{tag}/pull` | Enqueue procrastinate `model.pull` job |
| `DELETE` | `/api/system/models/{tag}` | Delete if unassigned |

Existing endpoints unchanged:
- `POST /api/settings/{key}` — handles `llm.smart_model`, `llm.fast_model`, `llm.embed_model`
- `POST /api/providers/{provider}/test` — unchanged
- `GET /api/jobs/{id}/stream` — pull progress (no change needed)

---

## 12. Implementation Sequence

This contract does NOT prescribe task grouping — that is for the writing-plans sprint. The natural dependency order is:

1. `jarvis_common/data/model_catalog.json` + catalog loader module
2. Hardware detection endpoint + `HardwareInfo` dataclass
3. `GET /api/system/models` with status computation
4. `model.pull` procrastinate task (#20) + `POST /api/system/models/{tag}/pull`
5. `DELETE /api/system/models/{tag}` with guard
6. Frontend: Models tab layout + status badges + confirm dialog + pull progress
7. Frontend: GHOST surface fix (cloud model-select dropdown in role pickers)
8. Phase C execution (separate sprint, spec already exists)

Phase C (step 8) can execute before or after the UI work (steps 1–7) — the UI does not depend on the embedding swap. The embedding swap depends only on step 4 (pull machinery).

---

## Verified Identifiers

Every cited identifier was confirmed against HEAD (`master @ 030ea38c`) via Read or grep in this session.

| Citation | File:line | Behavior |
|---|---|---|
| `update_litellm_model(alias, model_name, db_pool)` | `services/paper_ingestion/paper_ingestion/services/litellm_config.py` | Rewrites LiteLLM config YAML + calls `_post_config_update`; cloud entries get API key injected via `get_provider_api_key` |
| `get_provider_api_key(provider, db_pool)` | `services/paper_ingestion/paper_ingestion/services/litellm_config.py` | Decrypts key from `user_config`; returns plaintext str or None |
| `reload_litellm()` | `services/paper_ingestion/paper_ingestion/services/litellm_config.py` | Best-effort POST to reload config; swallows errors |
| `_CLOUD_PREFIX_TO_PROVIDER` | `services/paper_ingestion/paper_ingestion/services/litellm_config.py` | Maps `openai/`, `anthropic/`, `gemini/` prefixes to provider name |
| `_probe_ollama()` | `services/paper_ingestion/paper_ingestion/routers/system.py` | TTL-cached `GET {OLLAMA_BASE_URL}/api/tags`; returns full tag list |
| `_OllamaProbeCache` | `services/paper_ingestion/paper_ingestion/routers/system.py` | TTL cache class used by `_probe_ollama` |
| `llm.anthropic.api_key` | `services/paper_ingestion/paper_ingestion/routers/settings.py:_ALLOWED_CONFIG_KEYS` | Encrypted BYTEA in `user_config`; live in production |
| `llm.openai.api_key` | `services/paper_ingestion/paper_ingestion/routers/settings.py:_ALLOWED_CONFIG_KEYS` | Same |
| `llm.google.api_key` | `services/paper_ingestion/paper_ingestion/routers/settings.py:_ALLOWED_CONFIG_KEYS` | Same |
| `POST /api/providers/{provider}/test` | `services/paper_ingestion/paper_ingestion/routers/settings.py` | Existing endpoint; provider test-ping only; no model-select today |
| `_SUPPORTED_PROVIDERS` | `services/paper_ingestion/paper_ingestion/routers/settings.py` | `frozenset({"anthropic", "openai", "google"})` |
| Cloud entries commented-out | `litellm/config.yaml:62-66` | `openai/gpt-4o`, `anthropic/claude-sonnet-4-6`, `anthropic/claude-haiku-4-5`, `openai/text-embedding-3-small` — all commented out; active: `ollama/nomic-embed-text` (embed), `ollama/mistral-nemo` (smart), `ollama/qwen3:4b` (fast) |
| `EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "768"))` | `services/paper_ingestion/paper_ingestion/ingestion/embedder.py:36` | Module-level constant; drives Qdrant collection size |
| `embed_dim_expected = 768` (inline literal) | `services/paper_ingestion/paper_ingestion/routers/pulse.py:356` | Must be replaced with `EMBEDDING_DIMENSION` import in Phase C |
| `task_registry.py` task count | `libs/jarvis_common/jarvis_common/task_registry.py` | 19 task kinds registered; `model.pull` will be #20 |
