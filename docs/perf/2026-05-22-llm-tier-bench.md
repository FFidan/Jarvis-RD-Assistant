# LLM tier benchmark — tier defaults update

**Date:** 2026-05-22
**Driver:** streaming `<think>` strip fix + community-AWQ candidate expansion + 16-24 sim coverage
**Prior state:** yaml at HEAD `306de0b0` (rendered tier-defaults report deferred; raw bundle: `vllm-confirmatory-20260519T120505Z`)

---

## Summary

| Tier | Prior winner | Final winner | Δ score | Verdict |
|---|---|---|---|---|
| `cpu` | qwen3:1.7b (static) | qwen3:1.7b (static) | 0 | held |
| `lt-8` | qwen3:1.7b (static) | qwen3:1.7b (static) | 0 | held |
| `8-16` | qwen2.5:7b-instruct (67/144 bench) | qwen2.5:7b-instruct (67) **+** deepseek-r1:7b (79, new thinking option) | — | **expanded** |
| `16-24` | (static) | Qwen/Qwen2.5-7B-Instruct-AWQ (112/144 sim) | **NEW** | **NEW** |
| `24-48 sim` | Qwen2.5-7B-Instruct-AWQ (119/144, earlier run) | unchanged (earlier run stands) | 0 | held |
| `ge-48` | Qwen/Qwen3-14B-AWQ (105/144 earlier run) | Qwen/Qwen3-14B-AWQ (92/144 this run) | **−13** | held (winner unchanged; honest score) |

---

## Streaming `<think>` strip — what shipped

An earlier bench documented a P0: "`<think>` blocks leaked into SSE token events despite the `_strip_think_blocks` regex passing unit tests."

### Root-cause investigation

The plan's working hypothesis was that `strip_think_streaming` (the streaming-path filter at `libs/jarvis_common/jarvis_common/llm_client.py:159-193`) had a chunk-boundary leak. **Investigation falsified that** — 8/8 byte-position adversarial tests passed on every input across every split point.

The actual bug was in the **non-streaming** companion `strip_think_blocks` at `libs/jarvis_common/jarvis_common/llm_client.py:156`. Its regex `<think>.*?</think>` required both `<think>` AND `</think>` to match. When Qwen3 hit `max_tokens=700` mid-think, the close tag was never emitted, the regex matched nothing, and raw think content leaked into `ask_paper` / `ask_cross_paper` responses.

### Fix (commit `e1aedb4d`)

```diff
- return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
+ return re.sub(r"<think>.*?(?:</think>|$)", "", raw, flags=re.DOTALL).strip()
```

The non-capturing alternation `(?:</think>|$)` matches either the close tag OR end-of-string. With `re.DOTALL`, `$` anchors at the absolute end of `raw`. Six new parametrized tests across two layers (`libs/jarvis_common/tests/test_llm_client.py` + `services/paper_ingestion/tests/test_rag_think_strip.py`) prove unclosed-tag handling.

### Bench-level verification

All four judges explicitly confirmed: **zero `<think>` tag leakage** across all 48 prompts (4 cells × 12 prompts). The strip-fix is empirically effective.

---

## Per-cell judge results

### `ge-48` Qwen/Qwen3-14B-AWQ — 92/144 (was 105/144 in an earlier run)

**Why the score dropped:** Three complete pipeline failures on q03/q04/q10 returned raw JSON with empty `"answer": ""` field. The strip-fix correctly suppresses `<think>` content; for these three prompts the entire output was inside the think block (max_tokens=700 cap fired before the model emitted any visible answer). Strip-fix → empty answer → 0 across all 6 dimensions for those prompts.

**Why this is honest:** The earlier run's 105 was inflated by counting `<think>` content as "completeness" — evaluators saw text and rated it. This run reveals the actual user-visible behavior.

**Three additional truncations** (q05, q06, q08) — partial answer + mid-sentence cut at max_tokens. The strip-fix is innocent here; the cap is the limit.

**Persistent paper-number scrambling** across all 12 — the model's internal "Paper N" labels don't match the seed JSON order. Lower citation-grounding scores. Out-of-scope for this run (RAG retrieval ordering, not streaming-strip).

### `ge-48` Qwen/Qwen3-8B-AWQ — 84/144 (was 63/144 in an earlier run)

**+21 points** — the strip-fix delivers visible value on this smaller thinking model. Same 3-pipeline-failure pattern (q08/q09/q10) and 2 catastrophic synthesis truncations (q04/q05) limit further upside.

Strong on single-paper focused tasks (q02/q03/q11 = 10-11/12 each). Same paper-number misattribution as 14B.

### `16-24` Qwen/Qwen2.5-7B-Instruct-AWQ — 112/144 (NEW sim cell)

First-ever 16-24 sim data on the ≈48 GB tier (RTX 5880 Ada) at `gpu_memory_utilization=0.42` (~19.3 GB ceiling). Strong:
- Perfect scores on q03 (methodology critique of Lite Transformer) and q11 (Trees-in-Transformers explanation)
- No `<think>` tags (Qwen2.5 is non-thinking — strip-fix neutral)
- Mild hallucinations only (RAG checklist artifacts, not outright fabrication)

Weakest cell: q04 synthesis (7/12) — misidentified "recurring methods" as generic academic-paper sections (acknowledgments, references) rather than research patterns. Same RAG retrieval ordering issue surfaces here.

### `8-16` deepseek-r1:7b — 79/144 (was qwen2.5:7b 67/144 in an earlier run)

Substitute for the non-existent `qwen3:7b` (Qwen3 family on Ollama has no 7b variant — 0.6b/1.7b/4b/8b/14b/30b/32b). DeepSeek-R1 qwen-7b distill at ~5GB Q4 native.

**+12 points** over qwen2.5:7b-instruct baseline. Becomes the 8-16 *thinking* title-holder; non-thinking qwen2.5 stays #1 for citation-quality (weak DeepSeek grounding — internal "Paper N" labels, no arxiv ids cited).

One pipeline failure on q10 + hallucinated "optimal 8 attention layers" claim on q11 not in Lite Transformer.

---

## Candidate pool changes

Three candidates in the original pool were invalid HF model ids. Substitutions:

| Original | Substitute | Outcome |
|---|---|---|
| `deepseek-ai/DeepSeek-R1-Distill-Qwen-14B-AWQ` | `casperhansen/deepseek-r1-distill-qwen-14b-awq` | **Failed both attempts** — 1st: loaded successfully but barely missed 900s boot timeout by ~25s; 2nd: hang past 1800s with HF cache warm. Worth a 3rd attempt at `VLLM_BOOT_TIMEOUT_S=3600` on a clean docker volume reset. |
| `Qwen/Qwen3-30B-A3B-AWQ` | `ELVISIO/Qwen3-30B-A3B-AWQ` (879K downloads) | **Failed** — Engine core init crash on async-mp client (MoE-AWQ + vLLM v0.11.0 compatibility). Defer to future vllm release. |
| `microsoft/Phi-4-AWQ` | DROPPED | No published AWQ exists; BF16 would break apples-to-apples (~28 GB). |

Additionally `qwen3:7b` in the 8-16 8-16 row was REPLACED with `deepseek-r1:7b` (same size class, real model id).

---

## NEW 16-24 sim cell

First-ever 16-24 sim evidence on the ≈48 GB tier (RTX 5880 Ada) at `gpu_memory_utilization=0.42`:

| Candidate | Result |
|---|---|
| Qwen/Qwen2.5-7B-Instruct-AWQ | **112/144** — clean run |
| Qwen/Qwen3-8B-AWQ | **Failed boot** at util=0.42 — confirms Qwen3-8B-AWQ doesn't fit the sim slice (also failed at 24-48 sim util=0.53). Viable only at ge-48 native (util≥~0.75). |

**Real finding:** Qwen3-8B-AWQ is a **ge-48-only model** at the bench's current `max_model_len=8192`. Not a 16-24 / 24-48 candidate.

---

## Also fixed during this bench run

The bench surfaced three additional deferred bugs:

1. **`services/paper_ingestion/paper_ingestion/routers/settings_ai.py:32`** — used `Path(__file__).resolve().parents[4]` which raised IndexError inside the docker container at `/app/paper_ingestion/routers/` (only 3 parent dirs). Earlier tests ran on host filesystem so missed this; bench's `make profile-stack-up` exposed it as a paper_ingestion crash-loop. Fix in commit `9ca9150c` plus `./config:/app/config:ro` bind-mount in `docker-compose.yml`.

2. **`scripts/render-litellm-config.sh`** — wrote `vllm/<model>` provider prefix (requires the `vllm` Python package locally, NOT installed in LiteLLM container; this is for in-process vLLM, not HTTP-proxied) and omitted `api_base` (LiteLLM defaulted to `localhost:11434` for ollama → connection refused since ollama is a sibling container at `ollama:11434`). Every `/api/ask` 502'd. Fix in commit `6a21e6a9`: `openai/<model>` + `api_base: http://vllm:8080/v1` for vllm, `ollama/<model>` + `api_base: http://ollama:11434` for ollama.

3. **Bench-aggregation path** — a cell_dir path missed the model-safe-name component (a prior pair-name collision fix never propagated). A bench-aggregation path was aligned in a later commit.

---

## Carried forward to a follow-up

- **Empty-answer-on-truncation** (new finding) — when a Qwen3 thinking model hits `max_tokens=700` INSIDE a `<think>` block, the streaming generator's `in_think=True` suppression keeps the carry hidden (correct, no leak), but `full_answer` stays empty. Fix candidates: (a) raise `max_tokens` for thinking models; (b) detect `in_think=True` at stream end and retry with `extra_body: think=false`. Out of scope here.
- **Paper-number scrambling** across every cell — RAG retrieval ordering issue; separate RAG-grounding follow-up.
- **casperhansen/deepseek-r1-distill-qwen-14b-awq** — worth a 3rd attempt at longer timeout.
- **ELVISIO/Qwen3-30B-A3B-AWQ** — defer until vLLM MoE-AWQ support improves.

---

## Bench bundles

```
vllm-confirmatory-20260522T134039Z.tar.gz   # 8-16 GB VRAM tier, deepseek-r1:7b
vllm-confirmatory-20260522T123333Z.tar.gz   # ge-48 GB VRAM tier (2 winners + 2 timeouts)
vllm-confirmatory-20260522T144456Z.tar.gz   # sim-only (16-24 Qwen2.5-7B win + 2 timeouts)
```

---

## References

- A1 root-cause findings: derived from the `_strip_think_blocks` regex re-read against HEAD.
