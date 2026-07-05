# Local Model and Scientific RAG Benchmark Protocol

**Date:** 2026-07-03
**Status:** maintained protocol; July 2026 evidence run completed separately; no default promoted
**Scope:** local-first model, backend, reranker, and embedding validation for
JARVIS scientific-paper RAG.

This document defines the checked-in protocol for the model evidence gate. Result
reports are separate evidence records and are not added to public site navigation
by default. A result report does not promote a model, backend, reranker, or
embedding default unless its decision section explicitly says so.

`docs/perf/` is excluded from the rendered MkDocs site. The protocol and fixed
eval inputs are versioned in the repository so future benchmark results can be
reproduced against the same questions. Raw outputs and reviewer notes
belong under ignored `artifacts/perf/<run-id>/`.

## Current Decision

No default changes are made by this protocol. The fixed manifest and harness
exist so benchmark runs can compare the current baseline against
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
| qwen3:30b-a3b | high-vram local | existing local candidate | not covered by the protocol initialization | measure quality, grounding, latency, and VRAM on the fixed pack |
| Qwen3-30B-A3B-Instruct-2507 | high-vram local candidate | external candidate to evaluate | not covered by the protocol initialization | install/configure locally, then measure the fixed pack |
| gpt-oss:20b | 16GB-plus compatibility candidate | external candidate to evaluate | not benchmarked; Harmony and visible-reasoning handling required | prove user-visible answer suppression before scientific scoring |
| reranker-off | all baseline | retrieval ablation | not covered by the protocol initialization | run as a fixed baseline for reranker comparison |
| Qwen3-Reranker-0.6B | optional reranker candidate | low-cost reranker candidate | not covered by the protocol initialization | measure cold start, memory, top-k latency, and grounding deltas |
| Qwen3-Reranker-4B | high-vram reranker candidate | stronger reranker candidate | not covered by the protocol initialization | measure cold start, memory, top-k latency, and grounding deltas |
| vLLM backend behind LiteLLM alias | operator/high-vram serving candidate | serving backend comparison | not covered by the protocol initialization | compare latency, structured output, and route stability behind the same alias |
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

Additional environment checks: the product benchmark runtime must be the JARVIS
stack's own LiteLLM/Ollama path. A separate non-product embedding-only daemon
was intentionally excluded. LiteLLM listed `embed`, `embed-4b`, `fast`,
`smart-fallback`, and `smart`.

## Fixed Eval Inputs

- Manifest: `docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl`
- Harness: `scripts/perf/llm_retrieval_eval.py`
- Answer key: `docs/perf/eval_sets/2026-07-03-scientific-rag-answer-key.jsonl`
- Operator notes: `scripts/perf/llm_retrieval_eval_README.md`

The manifest contains 10 open-access paper slots and 25 fixed scientific RAG
questions. The question pack covers method explanation, limitations, numeric
extraction, cross-paper synthesis, contradiction/tension, reproducibility detail,
and adversarial unanswerable prompts.

## Execution Workflow

The harness now separates three phases:

1. `--dry-run` validates manifest and aggregation plumbing with fixture rows.
2. `--capture-only` calls product RAG endpoints and writes raw rows with
   `scores: null` under ignored `artifacts/perf/<run-id>/raw_answers.jsonl`.
   Library-wide capture requires `--fixed-pack-library-confirmed`, because the
   product cross-paper route searches the authenticated library rather than an
   explicit paper-id list.
3. `--answers-jsonl` aggregates only complete judge-reviewed rows with real
   score objects, exact fixed-question coverage, non-empty source/citation
   evidence, numeric latency/VRAM metadata, and evaluation-scope markers for
   library-wide questions.

The capture phase requires a local mapping from stable `paper_key` values
to deployment-specific paper identifiers. Raw responses, local runtime logs, and
review notes are run artifacts and must not be committed. Reranker,
backend, and model candidates should be measured behind existing aliases or
explicit temporary overrides, then rolled back before normal product use.

Cloud providers are not part of default promotion. They may be used only as
optional BYOK comparison inputs if the owner asks, and any such comparison must
be labeled as optional rather than local-first baseline evidence.

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

A promotion requires measured rows over the fixed pack. Dry-run fixture output,
route smoke output, and capture-only rows with `scores: null` are never
promotion evidence. Rows from cross-paper or unanswerable questions are not
fixed-pack evidence unless the benchmark library was isolated to the fixed pack
and the judged rows preserve that scope marker. A default candidate must beat
the current baseline by at
least 10 percent total weighted score and must not regress evidence grounding,
citation-label stability, structured-output validity, visible hidden-reasoning
suppression, or hardware fit.

No MkDocs/public link should be added unless a maintainer review verifies that the
result packet is reproducible, source-backed, free of unverifiable scoring prose, and
free of raw local traces or environment-specific details.
