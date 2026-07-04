# Scientific RAG Local Model Benchmark Results

**Date:** 2026-07-04
**Status:** internal evidence gate; no default model promoted
**Scope:** product RAG route over the fixed 10-paper / 25-question scientific
question pack.

## Decision

No local model is promoted as the default smart model from this run.

The monitored product-route candidates show that smaller local models can satisfy
basic structured-output and fixed-corpus retrieval gates, but each candidate that
was scientifically scored still has at least one promotion blocker. The current
larger baseline and the newly staged `gpt-oss:20b` candidate were rejected before
scientific scoring because they produced too many failed or empty product-route
answers.

## Method

- Fixed corpus: 10 open-access papers from
  `docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl`.
- Fixed questions: 25 scientific RAG questions covering single-paper,
  cross-paper, quantitative, reproducibility, limitation, and unanswerable
  cases.
- Capture path: the normal product RAG API through the `smart` route and the
  Ollama backend.
- Isolation: cross-paper and unanswerable questions passed an explicit fixed
  `paper_ids` scope; captured source rows were checked for outside-corpus paper
  ids.
- VRAM: monitored with one-second GPU memory sampling while resident local
  models were unloaded before each monitored rerun.
- Judging: executor-reviewed against the checked-in answer key and returned
  source excerpts. Scores are internal gate evidence and should receive an
  independent maintainer review before being presented as a public benchmark.

## Reproducibility Hashes

| artifact | sha256 |
|---|---|
| manifest | `99a4f32376dc1e21fb3d8364af847c4be4cb378a0c263f66239e82f41c8be799` |
| answer key | `abbf3a1c641f857c838efd53d0626bc015e6d54bfe39a29129ac67e1a0d5e8df` |
| judged rows | `32ec9ec7a5c7989915c70193a009861311cc8117121c676210187fa8268b2587` |

Raw answers, local paper ids, cookies, long source excerpts, and GPU monitor CSVs
remain under ignored `artifacts/perf/<run-id>/`.

## Monitored Product-Route Candidates

| candidate | quality | grounding | wrong-paper rows | empty rows | visible reasoning leaks | p95 latency ms | peak VRAM MB | decision |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `qwen3:1.7b` | 83.08 | 90.00 | 1 | 0 | 0 | 5244.92 | 18434 | defer: synthesis error blocks promotion |
| `qwen2.5:7b-instruct` | 80.96 | 82.50 | 0 | 0 | 1 | 9765.71 | 20822 | reject: visible control-token continuation |
| `deepseek-r1:7b` | 83.06 | 85.50 | 1 | 0 | 0 | 26305.64 | 20822 | defer: unanswerable overclaim and latency |

## Hard-Gate Rejections Before Scientific Scoring

| candidate | reason |
|---|---|
| `qwen3:30b-a3b` | Failed the product-route hard gate: 12 HTTP 200 rows, 3 HTTP 502 rows, 10 HTTP 500 rows, 13 empty answers, and 11 visible working-note rows. |
| `qwen3:4b` | Failed the product-route hard gate: 24 HTTP 200 rows, 1 HTTP 502 row, 1 empty answer, and 6 visible working-note rows. |
| `gpt-oss:20b` | Failed the product-route hard gate: 12 HTTP 200 rows, 13 HTTP 502 rows, and 13 empty answers. |

## vLLM Product-Route Addendum

A follow-up run on the benchmark GPU host routed the same product `smart` alias through the
LiteLLM admin DB to a loopback vLLM OpenAI-compatible backend. This was a raw
capture gate only: rows have not received independent scientific judging, so no
vLLM candidate is promoted or scored here. Raw answers, local ids, cookies, route
snapshots, boot logs, and monitor CSVs remain ignored under
`artifacts/perf/2026-07-04-vllm-product-route/`.

Benchmark setup notes:

- The vLLM compose overlay paused the LiteLLM settings reconciler only during
  the benchmark window so the temporary admin-DB `smart` route stayed stable.
- The authenticated benchmark library was isolated to the fixed 10-paper pack
  before valid capture. Earlier failed-auth and owner-scope attempts were kept
  as ignored diagnostic artifacts and are not evidence rows.
- The product route was restored to the normal Ollama `smart` deployment after
  the run, and the normal reconciler was restarted.

| vLLM candidate | raw rows | HTTP status counts | empty rows | visible reasoning leaks | median latency ms | max latency ms | peak VRAM MB | gate result |
|---|---:|---|---:|---:|---:|---:|---:|---|
| `Qwen/Qwen2.5-7B-Instruct-AWQ` | 25 | 23x 200, 2x 502 | 2 | 0 | 5147.17 | 39108.76 | 45369 | reject before judging: incomplete product-route capture |
| `Qwen/Qwen3-8B-AWQ` | 25 | 25x 200 | 0 | 0 | 4112.99 | 13196.46 | 45374 | eligible for judged-row review; not promoted |
| `Qwen/Qwen3-14B-AWQ` | 25 | 24x 200, 1x 502 | 1 | 0 | 5212.47 | 46839.06 | 45350 | reject before judging: incomplete product-route capture |
| `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` | 0 | boot failed | n/a | n/a | n/a | n/a | n/a | not runnable at 8192 tokens: insufficient KV-cache memory |

Interpretation: `Qwen/Qwen3-8B-AWQ` is the only vLLM candidate from this pass
that cleared the raw product-route completeness gate. It still needs judged-row
review against the answer key and returned sources before it can affect model
defaults. The 30B FP8 candidate should not be retried at a lower context length
inside this fixed-pack comparison unless the benchmark protocol explicitly adds
a separate reduced-context hardware tier.

## Main Findings

- `qwen3:1.7b` is the strongest follow-up candidate for local-first RAG: it was
  fastest, used the least monitored VRAM among scored candidates, and had the
  best grounding score. It still made a central cross-paper synthesis error on
  Transformer versus LoRA efficiency, so it must not be promoted without prompt,
  retrieval, or scoring follow-up.
- `qwen2.5:7b-instruct` is usable in many single-paper answers, but one row
  emitted visible chat/control-token continuation text. That is a hard product
  quality failure even though the row returned HTTP 200.
- `deepseek-r1:7b` produced complete rows but is substantially slower and made
  an overclaim on an unanswerable Adam/U-Net question.
- The larger Qwen3 baseline and `gpt-oss:20b` need structured-output and visible
  reasoning suppression work before they are meaningful product-route RAG
  candidates.

## Follow-Up

1. Keep current defaults unchanged.
2. Treat `qwen3:1.7b` as the next prompt/route-hardening candidate, not a
   default replacement.
3. Add an independent maintainer review of judged rows before publishing this as
   public benchmark evidence.
4. Judge the complete `Qwen/Qwen3-8B-AWQ` vLLM capture before any default
   discussion; do not publish vLLM results until that review is complete.
