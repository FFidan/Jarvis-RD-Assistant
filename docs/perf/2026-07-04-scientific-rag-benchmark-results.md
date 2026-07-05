# Scientific RAG Local Model Benchmark Results

**Date:** 2026-07-05
**Status:** internal evidence gate; no default model, backend, reranker, or embedding setting promoted
**Scope:** product RAG route over the fixed 10-paper / 25-question scientific question pack.

## Decision

No local model is promoted as the default smart model from this run.

The fixed-pack rerun produced useful evidence, but every judged candidate still
has promotion blockers. Runtime failures are kept separate from scientific answer
quality: candidates with HTTP failures, empty visible answers, or visible
reasoning/control-token leakage were not judged as scientific-quality rows.

`vllm:Qwen/Qwen3-8B-AWQ` was the strongest judged row set in this pass, but it
still had wrong-paper/source-support blockers and is not a default candidate.
`qwen2.5:7b-instruct` remains the strongest Ollama local candidate that cleared
the raw product-route gate, but it also remains below the promotion bar.

## Method

- Fixed corpus: 10 open-access papers from
  `docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl`.
- Fixed questions: 25 scientific RAG questions covering single-paper,
  cross-paper, quantitative, reproducibility, limitation, and unanswerable
  cases.
- Capture path: the normal product RAG API through the product `smart` route.
- Backend comparison: Ollama and benchmark-only vLLM routes were both exercised
  through the product LiteLLM boundary; vLLM was not made a default.
- Judging: executor-reviewed against the checked-in answer key and returned
  source evidence, followed by an adversarial reproducibility review.
- Raw answers, local paper ids, cookies, route snapshots, long source excerpts,
  review notes, and GPU monitor CSVs remain under ignored `artifacts/perf/`.

Benchmark captures were run from the branch revision recorded in the generated
summary. A later route-restoration fix in the same PR prevents known Ollama
catalog models from inheriting a temporary vLLM API base after benchmark routing;
it does not change the captured answer rows.

## Reproducibility Hashes

| artifact | sha256 |
|---|---|
| manifest | `99a4f32376dc1e21fb3d8364af847c4be4cb378a0c263f66239e82f41c8be799` |
| answer key | `abbf3a1c641f857c838efd53d0626bc015e6d54bfe39a29129ac67e1a0d5e8df` |
| judged rows | `c359e1a5914c5b96b31428625d847ab9c5ae1221e787d76568bbacc0cba78d2d` |

## Judged Fixed-Pack Candidates

These rows passed the raw product-route gate before judging: 25 HTTP 200 rows,
non-empty visible answers, no visible hidden-reasoning/control-token leakage,
numeric latency, numeric VRAM, and fixed-pack scope markers.

| candidate | quality | grounding | wrong-paper rows | empty rows | visible reasoning leaks | p95 latency ms | peak VRAM MB | decision |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `deepseek-r1:7b` | 73.06 | 71.00 | 2 | 0 | 0 | 12711.68 | 34542 | reject |
| `qwen2.5:7b-instruct` | 80.77 | 83.00 | 2 | 0 | 0 | 6979.91 | 34542 | defer: row-level promotion blockers |
| `vllm:Qwen/Qwen3-14B-AWQ` | 83.72 | 71.00 | 2 | 0 | 0 | 14136.10 | 44970 | defer: row-level promotion blockers |
| `vllm:Qwen/Qwen3-8B-AWQ` | 84.89 | 86.00 | 1 | 0 | 0 | 13001.73 | 45196 | defer: row-level promotion blockers |

The most common blockers were wrong-paper or weak source support, unsupported
numeric context, incomplete cross-paper support, and overconfident answers on
unanswerable or weakly supported prompts. These are model/retrieval quality
issues, not transport failures.

## Hard-Gated Before Scientific Scoring

These candidates were captured or started through the product route but were not
judged as scientific-quality evidence.

| candidate | raw gate result |
|---|---|
| current `smart` route (`qwen3:30b-a3b`) | 22 HTTP 200 rows, 3 HTTP 502 rows, 3 empty answers, and 19 visible hidden-reasoning/control-token rows. |
| `qwen3:4b` | 25 HTTP 200 rows, but 8 visible hidden-reasoning/control-token rows. |
| `qwen3:1.7b` | 24 HTTP 200 rows, 1 HTTP 502 row, and 1 empty answer. |
| `gpt-oss:20b` | 11 HTTP 200 rows, 14 HTTP 502 rows, and 14 empty answers. |
| `vllm:Qwen/Qwen2.5-7B-Instruct-AWQ` | 23 HTTP 200 rows, 2 HTTP 502 rows, and 2 empty answers. |
| `vllm:Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` | Not runnable at the 8192-token protocol tier: vLLM loaded weights but reported no available KV-cache memory before serving. |

The 30B FP8 vLLM candidate should not be retried at a lower context length inside
this fixed-pack comparison unless the protocol adds a separate reduced-context
tier.

## Main Findings

- Keep current defaults unchanged. The current large local route still needs
  visible-reasoning and 502/empty-answer hardening before it can be judged.
- `qwen2.5:7b-instruct` is the best current Ollama candidate from this rerun,
  but it does not clear the source-support and wrong-paper gates.
- vLLM is operationally viable for Qwen3 8B/14B AWQ product-route captures after
  resident answer models are unloaded, but the judged answers still do not clear
  the promotion rule.
- `gpt-oss:20b` remains a compatibility candidate only. It needs response-format
  and empty-answer hardening before scientific RAG judging.
- Reranker and embedding changes remain separate gates. This run does not promote
  a reranker or embedding default.

## Follow-Up

1. Keep local-first defaults unchanged for v1.0.2.
2. Fix route/runtime hard gates before spending more effort on public benchmark
   presentation.
3. Treat vLLM Qwen3 8B/14B AWQ as measured backend candidates, not product
   defaults.
4. Keep BGE-M3, reranker ablations, and re-embedding work as separate plans with
   Qdrant snapshot and rollback requirements.
5. Do not add a public MkDocs/GH Pages benchmark link until a maintainer approves
   publication wording and independently reviews the judged rows.
