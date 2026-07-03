# Local Model and Scientific RAG Benchmark Protocol

**Date:** 2026-07-03
**Status:** initialized; route smoke only; no scientific RAG benchmark results yet
**Scope:** local-first model, backend, reranker, and embedding validation for
JARVIS scientific-paper RAG.

This document defines the checked-in protocol for Step 0.5. It is not a
benchmark result report and it does not promote a model, backend, reranker, or
embedding default. The full scientific RAG benchmark still has to run over the
fixed paper/question pack with reproducible answer rows, source-backed scoring,
and clear promote/defer/reject decisions.

`docs/perf/` is excluded from the rendered MkDocs site. The protocol and fixed
eval inputs are versioned in the repository so future benchmark results can be
reproduced against the same questions. Raw outputs and scratch judging notes
belong under ignored `artifacts/perf/<run-id>/` or `docs/audit/exec/<run-id>/`.

## Current Decision

No default changes are made by this protocol PR. The fixed manifest and harness
exist so a later benchmark run can compare the current baseline against
candidate local models, serving backends, rerankers, and embedding/retrieval
choices before any default is changed.

A minimal local LiteLLM route smoke was run on 2026-07-03 only to classify
runtime availability and visible hidden-reasoning risk. It did not execute the
fixed paper/question pack through retrieval and scoring. Raw smoke output is
stored at `artifacts/perf/2026-07-03-litellm-smoke/runtime_smoke.json`
(ignored).

## Candidate Readiness Table

| candidate | hardware tier | benchmark role | current status | next required evidence |
|---|---|---|---|---|
| current-fast-local | all | current route baseline | route smoke returned reasoning-like visible prefix | fix or configure hidden-reasoning suppression, then run the fixed RAG pack |
| current-smart-local | configured hardware | current route baseline | route smoke returned exact `OK` | run the fixed RAG pack before any promotion claim |
| current-smart-fallback-local | configured fallback | current route baseline | route smoke returned reasoning-like visible prefix | fix or configure hidden-reasoning suppression, then run the fixed RAG pack |
| qwen3:30b-a3b | high-vram local | existing local candidate | not benchmarked in this protocol PR | measure quality, grounding, latency, and VRAM on the fixed pack |
| Qwen3-30B-A3B-Instruct-2507 | high-vram local candidate | external candidate to evaluate | not benchmarked in this protocol PR | install/configure locally, then measure the fixed pack |
| gpt-oss:20b | 16GB-plus compatibility candidate | external candidate to evaluate | not benchmarked; Harmony and visible-reasoning handling required | prove user-visible answer suppression before scientific scoring |
| reranker-off | all baseline | retrieval ablation | not benchmarked in this protocol PR | run as a fixed baseline for reranker comparison |
| Qwen3-Reranker-0.6B | optional reranker candidate | low-cost reranker candidate | not benchmarked in this protocol PR | measure cold start, memory, top-k latency, and grounding deltas |
| Qwen3-Reranker-4B | high-vram reranker candidate | stronger reranker candidate | not benchmarked in this protocol PR | measure cold start, memory, top-k latency, and grounding deltas |
| vLLM backend behind LiteLLM alias | operator/high-vram serving candidate | serving backend comparison | not benchmarked in this protocol PR | compare latency, structured output, and route stability behind the same alias |
| BGE-M3 embedding/retrieval | reembed-plan-only candidate | retrieval/indexing candidate | not benchmarked; adoption needs reindex planning | create Qdrant snapshot/reembed plan before quality comparison |

## Local Runtime Smoke on 2026-07-03

This smoke used the local LiteLLM gateway with a constrained prompt: "Reply with
exactly: OK". It measured route availability and visible hidden-reasoning risk
only. It did not score scientific correctness, evidence grounding, citations,
retrieval order, or paper-label stability.

| route | result | elapsed_ms | promotion impact |
|---|---|---:|---|
| `fast` | returned reasoning-like prefix instead of exact `OK` | 4804 | blocks promotion until hidden-reasoning suppression is fixed and rerun |
| `smart` | returned exact `OK` | 7299 | eligible for full benchmark only; not promoted from smoke |
| `smart-fallback` | returned reasoning-like prefix instead of exact `OK` | 565 | blocks promotion until hidden-reasoning suppression is fixed and rerun |

Additional environment checks: product Ollama was not reachable on host port
`11434`; the separate claude-context Ollama on `11437` only had
`nomic-embed-text:latest`, so it was not used as a product benchmark runtime.
LiteLLM listed `embed`, `embed-4b`, `fast`, `smart-fallback`, and `smart`.

## Fixed Eval Inputs

- Manifest: `docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl`
- Harness: `scripts/perf/llm_retrieval_eval.py`
- Operator notes: `scripts/perf/llm_retrieval_eval_README.md`

The manifest contains 10 open-access paper slots and 25 fixed scientific RAG
questions. The question pack covers method explanation, limitations, numeric
extraction, cross-paper synthesis, contradiction/tension, reproducibility detail,
and adversarial unanswerable prompts.

## Prior Regression Checks Carried Forward

The May 2026 benchmark remains part of the gate for future runs:

- Empty visible answers when thinking output is truncated and stripped.
- Paper-number or paper-label scrambling across cross-paper answers.

A candidate that improves prose but regresses either of these is not a default
candidate.

## Current External Inputs

- Qwen's `Qwen3-30B-A3B-Instruct-2507` model card describes an Apache-2.0,
  non-thinking MoE model with 30.5B total parameters, 3.3B active parameters,
  and native 262K context. It is a high-priority benchmark candidate, not a
  default until local quality, latency, and VRAM are measured.
- Qwen's `Qwen3-Reranker-4B` card describes an Apache-2.0, 4B, multilingual,
  instruction-aware reranker with SentenceTransformers and vLLM usage paths. It
  needs cold-start, memory, and top-k latency measurement before any default.
- BGE-M3 is MIT licensed and supports dense, sparse, and multi-vector retrieval
  with 8192-token inputs. It is not a drop-in embedder because adoption changes
  retrieval/indexing assumptions and needs a Qdrant snapshot plus reembed plan.
- `gpt-oss:20b` is an Apache-2.0 local candidate, but it requires Harmony-format
  handling and explicit hidden-reasoning suppression before user-facing tests.
- vLLM is an OpenAI-compatible serving backend with structured-output support;
  LiteLLM is the gateway/router over local endpoints. This supports measured
  backend comparison behind aliases, not cloud or vLLM defaulting by assumption.

## Promotion Rules

A promotion requires measured rows over the fixed pack. Dry-run fixture output
and route smoke output are never promotion evidence. A default candidate must
beat the current baseline by at least 10 percent total weighted score and must
not regress evidence grounding, citation-label stability, structured-output
validity, visible hidden-reasoning suppression, or hardware fit.
