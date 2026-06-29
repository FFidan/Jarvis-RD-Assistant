# LLM tier benchmark — tier defaults update

**Date:** 2026-05-22

---

## Summary

| Tier | Prior winner | Final winner | Δ score | Verdict |
|---|---|---|---|---|
| `cpu` | qwen3:1.7b (static) | qwen3:1.7b (static) | 0 | held |
| `lt-8` | qwen3:1.7b (static) | qwen3:1.7b (static) | 0 | held |
| `8-16` | qwen2.5:7b-instruct (67/144) | qwen2.5:7b-instruct (67) **+** deepseek-r1:7b (79, new thinking option) | — | **expanded** |
| `16-24` | (static) | Qwen/Qwen2.5-7B-Instruct-AWQ (112/144 sim) | **NEW** | **NEW** |
| `24-48 sim` | Qwen2.5-7B-Instruct-AWQ (119/144, earlier run) | unchanged | 0 | held |
| `ge-48` | Qwen/Qwen3-14B-AWQ (105/144 earlier run) | Qwen/Qwen3-14B-AWQ (92/144 this run) | **−13** | held (winner unchanged; honest score) |

---

## Streaming `<think>` strip — what shipped

An earlier bench documented a P0: "`<think>` blocks leaked into SSE token events despite the `_strip_think_blocks` regex passing unit tests."

### Root-cause investigation

The working hypothesis was that `strip_think_streaming` (the streaming-path filter at `libs/jarvis_common/jarvis_common/llm_client.py:159-193`) had a chunk-boundary leak. Investigation falsified that — 8/8 byte-position adversarial tests passed on every input across every split point.

The actual bug was in the **non-streaming** companion `strip_think_blocks` at `libs/jarvis_common/jarvis_common/llm_client.py:156`. Its regex `<think>.*?</think>` required both `<think>` AND `</think>` to match. When Qwen3 hit `max_tokens=700` mid-think, the close tag was never emitted, the regex matched nothing, and raw think content leaked into `ask_paper` / `ask_cross_paper` responses.

### Fix (commit `e1aedb4d`)

```diff
- return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
+ return re.sub(r"<think>.*?(?:</think>|$)", "", raw, flags=re.DOTALL).strip()
```

The non-capturing alternation `(?:</think>|$)` matches either the close tag OR end-of-string. With `re.DOTALL`, `$` anchors at the absolute end of `raw`. Six new parametrized tests across two layers (`libs/jarvis_common/tests/test_llm_client.py` + `services/paper_ingestion/tests/test_rag_think_strip.py`) prove unclosed-tag handling.

Zero `<think>` tag leakage confirmed across all 48 prompts (4 cells × 12 prompts).

---

## Per-cell judge results

### `ge-48` Qwen/Qwen3-14B-AWQ — 92/144 (was 105/144 in an earlier run)

**Why the score dropped:** Three complete pipeline failures on q03/q04/q10 returned raw JSON with empty `"answer": ""` field. The strip-fix correctly suppresses `<think>` content; for these three prompts the entire output was inside the think block (`max_tokens=700` cap fired before the model emitted any visible answer). Strip-fix → empty answer → 0 across all 6 dimensions for those prompts.

**Why this is honest:** The earlier run's 105 was inflated by counting `<think>` content as "completeness" — evaluators saw text and rated it. This run reveals the actual user-visible behavior.

**Three additional truncations** (q05, q06, q08) — partial answer + mid-sentence cut at max_tokens. The strip-fix is innocent here; the cap is the limit.

**Persistent paper-label scrambling** across all 12 — model-generated paper labels did not match the seed JSON order. Lower citation-grounding scores. Out-of-scope for this run (RAG retrieval ordering, not response cleanup).

### `ge-48` Qwen/Qwen3-8B-AWQ — 84/144 (was 63/144 in an earlier run)

**+21 points** — the strip-fix delivers visible value on this smaller thinking model. Same 3-pipeline-failure pattern (q08/q09/q10) and 2 catastrophic synthesis truncations (q04/q05) limit further upside.

Strong on single-paper focused tasks (q02/q03/q11 = 10-11/12 each). Same paper-number misattribution as 14B.

### `16-24` Qwen/Qwen2.5-7B-Instruct-AWQ — 112/144 (NEW sim cell)

First 16-24 sim data at `gpu_memory_utilization=0.42` (~19.3 GB ceiling). Strong:

- Perfect scores on q03 (methodology critique of Lite Transformer) and q11 (Trees-in-Transformers explanation)
- No `<think>` tags (Qwen2.5 is non-thinking — strip-fix neutral)
- Mild hallucinations only (RAG checklist artifacts, not outright fabrication)

Weakest cell: q04 synthesis (7/12) — misidentified "recurring methods" as generic academic-paper sections rather than research patterns. Same RAG retrieval ordering issue surfaces here.

### `8-16` deepseek-r1:7b — 79/144 (was qwen2.5:7b 67/144 in an earlier run)

Substitute for the non-existent `qwen3:7b` (Qwen3 family has no 7b Ollama variant). DeepSeek-R1 qwen-7b distill at ~5 GB Q4 native.

**+12 points** over qwen2.5:7b-instruct baseline. Becomes the 8-16 thinking title-holder; non-thinking qwen2.5 stays #1 for citation-quality (weak DeepSeek grounding — internal "Paper N" labels, no arxiv ids cited).

---

## Carried forward

- **Empty-answer-on-truncation** — when a Qwen3 thinking model hits `max_tokens=700` inside a `<think>` block, `full_answer` stays empty. Fix candidates: (a) raise `max_tokens` for thinking models; (b) detect `in_think=True` at stream end and retry with `extra_body: think=false`.
- **Paper-number scrambling** across every cell — RAG retrieval ordering issue; separate follow-up.
