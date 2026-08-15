# 06 — Evidence Anchor Contract
**Status:** LIVING
**Reviewers must update this contract in the same patch as any change to:**
- Findings-generation prompts in `services/paper_ingestion/paper_ingestion/services/summarization.py`
- Quote-to-passage resolution in `libs/jarvis_common/jarvis_common/verify.py`
- Evidence chips or source-passage rows in `frontend/src/components/paper/`
- The manual evidence-anchor benchmark in `scripts/eval-evidence-anchors.py`

This contract defines how a generated finding remains traceable from its exact quote to the
paper passage and page that support it.

---

## 1. Scope

**In scope.** Findings generated from paper chunks, quote and page verification, preference for
body evidence, passage identity and navigation, and the manual benchmark that measures anchor
placement.

**Out of scope.** Citation synchronization, citation-source ingestion, PDF acquisition, summary
prose without a quoted finding, and model selection. The shared LLM call contract remains in
[03-llm.md](03-llm.md).

---

## 2. Findings generation

The system role always requires an exact verbatim quote and a page number for each factual
finding. When the paper has a chunk after `chunk_index=0`, the findings system role adds these
requirements:

1. Supporting quotes come from body sections rather than the abstract.
2. Results and Methods are preferred.
3. An excerpt without body evidence returns no findings.

Both the single-pass generator and every map window receive the same conditional rule. Reduce
calls cannot create findings or quotes.

For an abstract-only paper, the extra rule is absent. The generator may therefore return a
finding anchored in that abstract, and normal quote verification still applies. If verification
produces no usable findings and generated summary prose is empty, the existing summary fallback
uses the stored abstract; it does not manufacture a quote or passage identity.

---

## 3. Quote and page verification

A candidate becomes a verified finding only when its quote matches the source text under the
shared verifier's exact or strict fuzzy matching rules. Successful verification resolves four
pieces of evidence together:

- the matched text;
- the stable database chunk id;
- the per-paper `chunk_index`;
- the page number from the matched chunk.

The verifier replaces a model-supplied page number with the matched chunk's page number. A quote
that cannot be verified remains rejected. Passage-index reporting does not change the acceptance
threshold or create another matching path.

---

## 4. Passage identity and navigation

The two passage identifiers have distinct jobs:

| Identifier | Contract |
|---|---|
| `chunk_id` | Stable storage identity. It keys the DOM anchor and is never rendered as a passage number. |
| `chunk_index` | Per-paper ordering identity. Every numbered user-facing passage label uses this value. |

Source-passage rows expose `source-passage-<chunk_id>` as their stable DOM id and carry
`chunk_index` separately. Their visible label is `Passage <chunk_index> of <paper passage count>`.
An evidence chip with a verified `chunk_id` targets that exact DOM id, expands the row when it is
closed, and scrolls it into view. It does not substitute the database id as a display number.

---

## 5. Manual benchmark

Run the model-backed benchmark manually against a configured LiteLLM instance:

```bash
uv run python scripts/eval-evidence-anchors.py
```

`--model <alias>` overrides the configured smart-model alias. The benchmark uses a small fixed
set containing body-complete papers and an abstract-only record, runs the production single-pass
findings prompt, verifies every quote, and reports verified anchors by section.

The acceptance bar is:

- at least 80% of verified anchors from body-complete fixtures land outside the abstract;
- at least 60% of those body anchors land in Results or Methods;
- the abstract-only fixture produces at least one verified anchor.

This benchmark is not invoked by CI. Deterministic tests retain the conditional prompt-shape and
passage-target contracts.

---

## 6. Invariants

1. Adding body chunks adds the body-evidence rule; an abstract-only chunk set does not.
2. Verification never accepts a quote because it has a passage index.
3. A displayed passage number is a `chunk_index`, never a database chunk id.
4. A passage chip that names a stored passage targets the row keyed by that finding's `chunk_id`.
5. Abstract-only input remains eligible to produce verified findings.

---

## 7. Cross-contract references

- [03-llm.md](03-llm.md) — structured LLM calls and anti-hallucination requirements.
- [07-testing.md](07-testing.md) — deterministic test shapes and prohibited test shortcuts.
- [ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md) — prompt and verification standards.

---

## 8. Verified Identifiers

| Citation | File:line | One-line behavior |
|---|---|---|
| `_findings_system_prompt` | services/paper_ingestion/paper_ingestion/services/summarization.py:140-149 | Adds the body rule only when a chunk follows the abstract chunk. |
| Map findings system role | services/paper_ingestion/paper_ingestion/services/summarization.py:538-559 | Applies the conditional rule to every map window. |
| Single-pass findings system role | services/paper_ingestion/paper_ingestion/services/summarization.py:1018-1034 | Applies the conditional rule to the single-pass generator. |
| `VerificationResult` | libs/jarvis_common/jarvis_common/verify.py:89-100 | Carries stable chunk id, per-paper chunk index, matched text, and page. |
| `QuoteVerifier._find_chunk_for_quote` | libs/jarvis_common/jarvis_common/verify.py:326-341 | Resolves exact quotes to chunk id, chunk index, and page. |
| `passageAnchorId` and `ChunkItem` | frontend/src/components/paper/ChunksTab.tsx:10-46 | Key rows by stable chunk id and label them with chunk index. |
| `jumpToPassage` | frontend/src/components/paper/EvidenceTab.tsx:22-40 | Expands and scrolls to the exact stable passage anchor. |
| Benchmark thresholds | scripts/eval-evidence-anchors.py:48-49 | Sets the 80% body and 60% preferred-section bars. |
| Benchmark acceptance | scripts/eval-evidence-anchors.py:227-231 | Requires both rate bars and an abstract-only verified anchor. |
