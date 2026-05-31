# 09 — LLM Prompt Shape Contract
**Status:** LIVING
**Date:** 2026-05-26
**Reviewers must update this contract in the same patch as any change to:**
- The public surface of [libs/jarvis_common/jarvis_common/llm_client.py](../../libs/jarvis_common/jarvis_common/llm_client.py) (`call_llm_structured`, `ChatCompletionOptions`)
- The public surface of [libs/jarvis_common/jarvis_common/prompt_safety.py](../../libs/jarvis_common/jarvis_common/prompt_safety.py) (`wrap_delimited`, `safe_for_prompt`)
- The carve-out registry in §4 (Shape B callsites)
- The AST checker in [scripts/check-llm-prompt-shape.py](../../scripts/check-llm-prompt-shape.py)

This contract describes **what a structured-LLM callsite must look like** in this repo. Every new `call_llm_structured` callsite added from 2026-05-26 onward MUST comply. The convention is **machine-enforceable** via an AST check ([scripts/check-llm-prompt-shape.py](../../scripts/check-llm-prompt-shape.py)) wired into pre-commit and the CI full-gate.

The contract pairs with [03-llm.md](03-llm.md), which governs the choke-point itself; this contract governs only the *shape* of each callsite.

---

## 0. What this contract covers (and what it does NOT)

**In scope.**
- The shape of every Python callsite that invokes `call_llm_structured` from [libs/jarvis_common/jarvis_common/llm_client.py](../../libs/jarvis_common/jarvis_common/llm_client.py).
- The split between instruction text (system role) and data text (user role).
- The carve-out marker `# llm-prompt-shape: SINGLE-USER` and the docstring-rationale requirement that gates it.
- The pre-commit AST check that enforces the convention.

**Out of scope.**
- Raw streaming calls via `request_chat_completion_content` — those carry no Instructor schema and are governed by [03-llm.md](03-llm.md) §retry/timeout policy.
- Prompt *content* quality (clarity, length, few-shot examples) — convention is structural, not semantic.
- Test code under `tests/` or `test_*.py` files — skipped by the checker; tests routinely build minimal prompts and are not a public-bound surface.
- LLM responses (validation, retry, observability) — governed by [03-llm.md](03-llm.md) and [04-observability.md](04-observability.md).

---

## 1. Motivation

Untrusted text reaching the LLM unmarked is the root cause of the prompt-injection class of failures: a paper abstract, a Telegram message, or a Zotero note can carry instructions that the model executes alongside the legitimate prompt. The defence is layered:

1. **Escape and delimit** — `wrap_delimited(...)` in [libs/jarvis_common/jarvis_common/prompt_safety.py](../../libs/jarvis_common/jarvis_common/prompt_safety.py) escapes XML-style brackets via `safe_for_prompt(mode='escape')` and wraps the result in a named tag (`<paper_abstract>...</paper_abstract>`).
2. **Role-firewall** — keep instruction text in the *system* role and untrusted data in the *user* role. Modern instruction-tuned models are far less likely to follow injected instructions when they arrive in a user message.
3. **No interpolation of untrusted text into the instruction string** — the system prompt is a constant or template that contains no f-string holes for user data.

This contract codifies layer 2 as a callsite shape. Layer 1 is governed by the source of `wrap_delimited`; layer 3 is enforced by review against the anti-patterns in §6.

The audit finding **SEC-HIGH-06** (2026-05 audit round) inventoried the 9 production callsites that mix instruction + data in a single user-role `prompt=` argument. This contract is the steady-state guard that prevents reintroduction once those callsites are migrated.

---

## 2. The convention

Every `call_llm_structured(...)` callsite under `services/` or `libs/` (excluding tests) MUST satisfy **exactly one** of two shapes.

### 2.1 Shape A — split-role (default)

The instruction head lives in a system-role message and the `prompt=` argument carries only data (typically wrapped via `wrap_delimited(...)` when interpolating untrusted text).

Two equivalent forms are accepted:

- **2.1a — `options.system`** — pass `options=ChatCompletionOptions(system="...instruction head...")`. The choke point at [llm_client.py:362-367](../../libs/jarvis_common/jarvis_common/llm_client.py) prepends a `{"role":"system","content":options.system}` message when `prompt=` is set and `messages=` lacks a system entry.
- **2.1b — explicit `messages=` with a system entry** — pass `messages=[{"role":"system","content":"..."}, {"role":"user","content":"..."}]`. The first form is preferred for one-shot calls; the second is appropriate when the user-role content is built up from multiple parts (history + retrieved context + question).

### 2.2 Shape B — carve-out

The callsite is intentionally single-user-role and:

1. carries the literal marker `# llm-prompt-shape: SINGLE-USER` on the line of the `call_llm_structured(` opening paren **or** on the line immediately above, AND
2. lives inside a function whose docstring explains why a split-role shape is unsuitable.

Shape B is rare and exists for callsites where the prompt text is fully trusted (no untrusted interpolation), the instruction-data split would add noise, and the audit risk is documented.

---

## 3. Examples

### 3.1 ACCEPT — Shape A via `options.system`

```python
from jarvis_common.llm_client import ChatCompletionOptions, call_llm_structured
from jarvis_common.prompt_safety import wrap_delimited

SYSTEM = (
    "You are a research query decomposer. Break the following complex research "
    "question into 2-4 simpler, self-contained sub-queries that together cover "
    "the original question. Return ONLY a JSON array of strings."
)

safe_question, _ = wrap_delimited("user_question", question)
return await call_llm_structured(
    openai_client,
    response_model=RootModel[list[str]],
    prompt=safe_question,
    options=ChatCompletionOptions(model="fast", system=SYSTEM),
)
```

The instruction lives in `system`. The user-role `prompt=` carries only escaped, delimited untrusted data.

### 3.2 ACCEPT — Shape A via explicit `messages=`

```python
messages = [
    {"role": "system", "content": SYSTEM_RAG},
    *retrieved_context_messages,
    {"role": "user", "content": safe_question},
]
return await call_llm_structured(
    openai_client,
    response_model=AskResponse,
    messages=messages,
    options=ChatCompletionOptions(model="smart", max_tokens=700),
)
```

Use this form when the user message is composed of multiple parts assembled upstream.

### 3.3 ACCEPT — Shape B (carve-out)

```python
async def summarise_changelog_entry(client, entry: str) -> Summary:
    """Summarise a trusted, machine-generated changelog entry.

    Carve-out rationale (SHAPE-B): the input is generated by our own tooling
    from git history; no untrusted user text is interpolated. A split-role
    shape would add overhead without security benefit.
    """
    return await call_llm_structured(  # llm-prompt-shape: SINGLE-USER
        client,
        response_model=Summary,
        prompt=f"Summarise this changelog entry in one sentence:\n{entry}",
        options=ChatCompletionOptions(model="fast"),
    )
```

### 3.4 REJECT — single-user with interpolated untrusted data

```python
# REJECTED: instruction and untrusted data share the user role.
prompt = f"You are an assistant. Answer this question: {user_question}"
await call_llm_structured(client, response_model=Answer, prompt=prompt,
                          options=ChatCompletionOptions(model="smart"))
```

The injection surface is `user_question`; the AST checker emits a violation pointing at this callsite. Migration: move the instruction head into `options.system` and wrap `user_question` via `wrap_delimited("question", user_question)`.

### 3.5 REJECT — carve-out without docstring rationale

```python
async def call_thing(client):
    return await call_llm_structured(  # llm-prompt-shape: SINGLE-USER
        client, response_model=Out, prompt="...", options=ChatCompletionOptions(),
    )
```

The marker is present but the enclosing function has no docstring; the checker flags this as `[carve-out missing docstring rationale]`. Fix: add a docstring naming the trust boundary and the reason Shape A is unsuitable.

---

## 4. Carve-out registry

Every Shape B callsite — the entire population, not a sample — MUST appear here. Adding a row is a contract-update commit that pairs with the carve-out itself.

| File:line | Function | Rationale |
|---|---|---|
| `services/paper_ingestion/paper_ingestion/routers/rag.py:132` | `_call_rag_llm` | `messages` is constructed upstream by `prepare_single_paper_rag` / `prepare_cross_paper_rag` which already emit Shape A `[system, user]` pairs (PI-02 / PI-03). The static checker cannot follow the variable reference across functions; the carve-out documents that the role-firewall is satisfied one frame up. |

If you find a Shape B callsite that is NOT in this table, that is itself a contract violation: either add the row in the same patch or migrate the callsite to Shape A.

---

## 5. Enforcement

The AST checker [scripts/check-llm-prompt-shape.py](../../scripts/check-llm-prompt-shape.py) walks every `.py` file under `services/` and `libs/` (skipping tests) and classifies each `call_llm_structured` invocation as Shape A, Shape B, or a violation. It runs:

- **Pre-commit** — the `check-llm-prompt-shape` local hook in [.pre-commit-config.yaml](../../.pre-commit-config.yaml) fires on every `.py`-touching commit. Exit code 1 blocks the commit.
- **CI full-gate** — the CI workflow runs `uv run python3 scripts/check-llm-prompt-shape.py services/ libs/` and expects exit 0.

The check is structural, not semantic. It does NOT detect:

- Instruction text *inside* `options.system` that is itself unsafe (e.g. embedding untrusted data in the system message). This is enforced by review against §6.
- Prompts that pass untrusted data via `messages=` without `wrap_delimited`. Detecting this requires data-flow analysis; review the call against §6.4.

---

## 6. Migration playbook

### 6.1 New code

Default to Shape A 2.1a (`options.system`). Reach for 2.1b only when the user message is composed of multiple parts upstream. Reach for Shape B only when both conditions in §2.2 are genuinely true; if in doubt, prefer Shape A.

### 6.2 Existing single-user callsites

1. Extract the instruction head — the part of the current `prompt=` string that describes *the task* and *the output format* — into a module-level constant (e.g. `_SYSTEM_DECOMPOSE`).
2. Reduce the remaining `prompt=` to the *data* — typically a `wrap_delimited(...)` block around the untrusted input.
3. Pass the constant as `options=ChatCompletionOptions(system=_SYSTEM_DECOMPOSE, ...)`.
4. Re-run the checker; expect the violation to disappear.

Example before/after:

```python
# BEFORE — single-user, mixed instruction + data
prompt = (
    "You are a research query decomposer. Break the following question into 2-4 "
    f"sub-queries. Return ONLY a JSON array.\nQuestion: {question}"
)
await call_llm_structured(client, response_model=M, prompt=prompt,
                          options=ChatCompletionOptions(model="fast"))

# AFTER — split-role, wrapped data
_SYSTEM_DECOMPOSE = (
    "You are a research query decomposer. Break the following question into 2-4 "
    "self-contained sub-queries. Return ONLY a JSON array of strings."
)
safe_question, _ = wrap_delimited("user_question", question)
await call_llm_structured(
    client,
    response_model=M,
    prompt=safe_question,
    options=ChatCompletionOptions(model="fast", system=_SYSTEM_DECOMPOSE),
)
```

### 6.3 When to use the carve-out

Reach for `# llm-prompt-shape: SINGLE-USER` only when ALL of these hold:

- The input is generated by trusted internal code (our own tooling, our own DB rows that were validated on write, fixed test fixtures).
- No untrusted user text is interpolated anywhere in the prompt.
- The split-role refactor would meaningfully obscure the call (typically a one-off tool call that doesn't fit the system/user mental model).

Document the rationale in the enclosing function's docstring and add the row to §4.

### 6.4 Wrapping untrusted data

`wrap_delimited(tag, text)` in [libs/jarvis_common/jarvis_common/prompt_safety.py](../../libs/jarvis_common/jarvis_common/prompt_safety.py) returns `(delimited_text, truncated)`:

- It escapes XML-style brackets in `text` via `safe_for_prompt(mode='escape')`.
- It wraps the escaped text in `<tag>...</tag>`.
- It honours `max_chars` (truncation policy lives in `prompt_safety.py`; do NOT pre-truncate at the callsite — see §7.2).

Always pair untrusted data with `wrap_delimited` and a tag name that reflects the data's origin (`<paper_abstract>`, `<user_question>`, `<zotero_note>`). The tag name also helps the model attend to provenance.

---

## 7. Anti-patterns

### 7.1 Interpolating untrusted data into the instruction string

```python
# DO NOT WRITE
SYSTEM = f"You are an assistant. The user said: {user_question}"
```

The system role is for *trusted task description*. Interpolating untrusted text into it defeats the role-firewall. Move the data to the user role; wrap it with `wrap_delimited`.

### 7.2 Pre-truncating before `wrap_delimited`

```python
# DO NOT WRITE
truncated = abstract[:2000]
wrapped, _ = wrap_delimited("abstract", truncated)
```

`wrap_delimited` already accepts a `max_chars` argument and handles truncation atomically with escaping. Pre-truncation risks cutting in the middle of an escape sequence and produces a wrapped block that the caller doesn't know was truncated. Pass `max_chars` to `wrap_delimited` and read the returned `truncated` boolean.

### 7.3 `verifier=None` without log

When skipping verifier-style post-validation on an LLM response, log the skip with the rationale. Silent skips erode the post-incident audit trail.

### 7.4 Falling back to Shape B as a shortcut

The carve-out exists for callsites where Shape A is genuinely unsuitable, not as an escape hatch for a noisy migration. If a callsite can be migrated to Shape A in <20 LOC, migrate it. Carve-outs accumulated as shortcuts hollow out the contract.

---

## 8. Cross-contract references

- [docs/contracts/03-llm.md](03-llm.md) — LLM choke point, retry/timeout policy, observability
- [docs/contracts/04-observability.md](04-observability.md) — Langfuse trace boundaries (the `@observe()` wrapper on `call_llm_structured`)
- [docs/contracts/07-testing.md](07-testing.md) — test-shape policy; `call_llm_structured` boundary-adapter tests
- [libs/jarvis_common/jarvis_common/llm_client.py](../../libs/jarvis_common/jarvis_common/llm_client.py) — source of truth for `call_llm_structured` + `ChatCompletionOptions`
- [libs/jarvis_common/jarvis_common/prompt_safety.py](../../libs/jarvis_common/jarvis_common/prompt_safety.py) — source of truth for `wrap_delimited` + `safe_for_prompt`
- [scripts/check-llm-prompt-shape.py](../../scripts/check-llm-prompt-shape.py) — pre-commit enforcement

---

## 9. Verified Identifiers

Every cited symbol has been verified at HEAD `f6ef8870`.

| Citation | File:line | Behavior |
|---|---|---|
| `call_llm_structured` | [libs/jarvis_common/jarvis_common/llm_client.py:328](../../libs/jarvis_common/jarvis_common/llm_client.py) | `@observe(as_type="generation")`-decorated coroutine. Signature `(openai_client, *, response_model, prompt=None, messages=None, options=None, config=None, max_retries=2)`. |
| `ChatCompletionOptions` | [libs/jarvis_common/jarvis_common/llm_client.py:92](../../libs/jarvis_common/jarvis_common/llm_client.py) | Frozen dataclass with `system: str \| None = None` and `model / max_tokens / temperature / timeout / response_format` fields. |
| System-prompt prepend | [libs/jarvis_common/jarvis_common/llm_client.py:362-367](../../libs/jarvis_common/jarvis_common/llm_client.py) | When `prompt` is set and `options.system` is truthy and `messages` lacks a system entry, a `{"role":"system","content":options.system}` message is prepended. |
| `wrap_delimited` | [libs/jarvis_common/jarvis_common/prompt_safety.py](../../libs/jarvis_common/jarvis_common/prompt_safety.py) | Escapes the text via `safe_for_prompt(mode='escape')` and wraps in `<tag>...</tag>`. Returns `(delimited_text, truncated)`. |
| `safe_for_prompt` | [libs/jarvis_common/jarvis_common/prompt_safety.py](../../libs/jarvis_common/jarvis_common/prompt_safety.py) | Escape / sanitise primitive for untrusted prompt fragments. |
| AST checker | [scripts/check-llm-prompt-shape.py](../../scripts/check-llm-prompt-shape.py) | Walks Python under given roots; classifies every `call_llm_structured` callsite as Shape A, Shape B, or violation; emits `path:line: ...` and exits 1 on any violation. |
| Pre-commit hook | [.pre-commit-config.yaml](../../.pre-commit-config.yaml) | `check-llm-prompt-shape` local hook runs the AST checker against `services/` and `libs/` on every `.py`-touching commit. |

---

**Updates to this contract must be paired with the code change that motivated them.** A contract that goes stale is worse than no contract.
