<!-- verified-against-UI: 2026-06-13 | routes: /settings?section=models -->

# What your hardware gets you

JARVIS runs its default AI models locally through Ollama, on CPU or a configured
GPU. This page explains the model recommendation, expected speed, and when an
optional cloud model may help.

---

## Hardware tiers and default models

At first boot, JARVIS probes the available hardware and selects a model set that
fits its estimated memory. This model recommendation is separate from accelerator
selection: an AMD or Intel device can be detected while setup still chooses the
safe CPU path. You can change model assignments later in **Settings → Models → AI
models**.

| Setup bucket | Detected VRAM | Main model (smart) | Quick model (fast) | Embedding model (embed) |
|--------------|---------------|--------------------|--------------------|-------------------------|
| `cpu` | No usable GPU | `qwen3:1.7b` | `qwen3:4b` | `qwen3-embedding:4b` |
| `lt-8` | < 8 GB | `qwen3:1.7b` | `qwen3:4b` | `qwen3-embedding:4b` |
| `8-16` | 8 to < 16 GB | `qwen2.5:7b-instruct` | `qwen3:4b` | `qwen3-embedding:4b` |
| `16-24` | 16 to < 24 GB | `qwen2.5:7b-instruct` | `qwen3:4b` | `qwen3-embedding:4b` |
| `24-48` | 24 to < 48 GB | `qwen3:14b` | `qwen3:4b` | `qwen3-embedding:4b` |
| `ge-48` | ≥ 48 GB | `qwen3:30b-a3b` | `qwen3:4b` | `qwen3-embedding:4b` |

The default cold-install requirement across these tier-selected sets and the
supported pull/build paths is **27–54 GB**. Run `./setup.sh --check` for the
selected host and image path; custom models may require more.

**Main model (smart)** — writes summaries, cards, and Ask answers.  
**Quick model (fast)** — scores and triages incoming papers.  
**Embedding model (embed)** — powers semantic search; it is fixed to the dimension of
your Qdrant collection and changing it requires re-indexing your library.

### What to expect

- **CPU / < 8 GB:** This tier is supported, but first-paper analysis can take
  tens of minutes for a long paper.
- **8–24 GB:** Setup selects `qwen2.5:7b-instruct`; observed speed depends on
  the device, context, and driver.
- **24–47 GB:** Setup recommends `qwen3:14b` while retaining the smaller quick
  model and embedder.
- **≥ 48 GB:** Setup recommends `qwen3:30b-a3b`. The reference validation used
  a 48 GB card and a 16k context window.
- A roughly 60,000-character paper took about 45 seconds on the
  16 GB reference card. Results vary with model, context, and driver.

---

## GPU vendor support

NVIDIA CUDA is the supported accelerated path and is selected when the Docker
runtime is ready. AMD ROCm is experimental and is selected only when `/dev/kfd`
is available. AMD without that device and Intel default to CPU; Vulkan is an
explicit experimental choice (`./setup.sh --gpu vulkan`). See the [hardware
support matrix](hardware-support-matrix.md) for the exact selection table and
known caveats.

**Windows + AMD GPU currently falls back to CPU** — Docker on WSL2 does not expose the AMD kernel driver the ROCm overlay needs. NVIDIA GPUs are unaffected under WSL2.

`paper_ingestion` (PDF parsing, reranking) stays CPU-only on AMD and Intel hosts regardless of which overlay Ollama uses — only Ollama inference is GPU-accelerated on those vendors today.

---

## Two tier systems

You may notice two different ways JARVIS numbers hardware tiers. They are two views
of the same hardware, measured differently:

- **Settings / API tiers (0–4):** ordinal labels from the backend (`0 = CPU`, `1 = 4–10 GB`,
  `2 = 10–20 GB`, `3 = 20–40 GB`, `4 = ≥ 40 GB`). Shown on the Settings page as
  `Tier 0` … `Tier 4`.

- **Setup-script buckets:** string labels used by the installer (`cpu`, `lt-8`, `8-16`,
  `16-24`, `24-48`, `ge-48`). These appear in installer output and log lines during
  first-time setup.

Both describe detected memory, but their boundaries overlap rather than mapping
one to one. The setup bucket in terminal output can therefore differ from the
tier number shown in Settings.[^tiers]

[^tiers]: The setup-script boundaries are `< 8 GB`, `8–16 GB`, `16–24 GB`, `24–48 GB`, `≥ 48 GB`.
    The backend boundaries (used for model assignment and the Settings page) are
    `< 4 GB`, `4–10 GB`, `10–20 GB`, `20–40 GB`, `≥ 40 GB`. The bucket names you see in
    installer logs will not always match the tier number shown in Settings — this is expected.

---

## Models must be installed in Ollama

JARVIS routes every AI request through three role aliases (`smart`, `fast`, `embed`).
For local inference, each alias must point to a model that is actually downloaded in
Ollama — you cannot route to a model that is not installed.

**What JARVIS does automatically:** On first boot the installer pulls the recommended
models for your tier. You can see what is installed and pull additional models on the
**Settings → Models** page.

**What happens if a routed model is missing:** The Models page shows a warning next to
any role whose assigned model is not installed, with a **Pull** button to download it.
JARVIS will not silently fall back to a different model for a role — the warning stays
visible until you either pull the model or reassign the role to one that is already
installed.

The advanced backend and hardware panel on the same page is diagnostics only. It shows hardware fit, observed runtime traffic, and Ollama/vLLM guidance, but active role assignment stays in the Main, Quick, and Embedding model cards.

---

## When to use a cloud model instead

Consider an optional cloud provider when:

- Your local GPU is too small for the summary quality you want, and you do not want
  to upgrade hardware.
- You need a model that is not practical on the local host and accept that the
  relevant data leaves the machine.
- You want to experiment with a larger model before committing to pulling it locally.

To add cloud capacity, use **Settings → Models → Providers & Routing**. JARVIS supports OpenAI, Anthropic, Google Gemini, OpenRouter, DeepSeek, Mistral, Kimi/Moonshot, Z.ai/GLM, and a Custom OpenAI-compatible endpoint. Provider settings are admin-wide and keys are stored encrypted at rest.

Once a provider is reachable — a saved API key, or a base URL alone for the self-hosted
OpenAI-compatible endpoint, which needs a key only if its own server requires one — JARVIS asks
that provider for its own model list the next time the Models page loads, and offers the result in
the `smart` and `fast` dropdowns alongside local Ollama models. A cloud group in the dropdown says
when its list was fetched, and the provider's tile in Settings says how many models are available;
lists are re-used for a few minutes rather than re-fetched on every page load. You always choose
from a list — there is no free-text model field anywhere.

If a list cannot be fetched, nothing is silently dropped: the group heading in the dropdown says
the list is unavailable, the Settings tile says there are no models yet when that provider has
none, and the built-in catalog stays on offer as the offline fallback. That fallback carries cloud
entries for Anthropic and OpenAI only, so a provider with no reachable list and no bundled entries
still shows its name and the unavailable note rather than an empty gap.

What is deliberately left out or left unassignable:

- **Self-hosted endpoints must not point into a private network.** The base URL has to be
  `http://localhost…` on the same host, or an HTTPS address that resolves to a public one. A
  literal private address such as `http://10.0.0.5…` or `http://192.168.1.20…` is refused the
  moment you save it. A hostname is accepted when you save it and checked when it is used: if it
  resolves to a private address, fetching the model list and routing a request both refuse it, so
  the endpoint simply never works. Letting the server call into private networks on request is a
  well-known way to reach systems that were never meant to be exposed, so the refusal is by design
  and there is no override.
- **Only one vendor prefix.** OpenRouter and self-hosted endpoints publish ids like
  `vendor/model-name`. An id with more nesting than that is left out of the fetched list.
- **Very long lists are capped.** If a provider offers more models than JARVIS lists at once, the
  dropdown says there are more instead of implying the list is complete.
- **A model is only offered where it can actually work.** One whose capability the provider does
  not report appears in the `smart` and `fast` lists but cannot be assigned, and the entry says
  why. One the provider reports as embedding-only is not offered for those roles at all — the
  embedding model is fixed separately, as described above. Models that are plainly not chat
  models — speech, image, moderation and similar families — are left out entirely.

!!! note "Privacy"
    When a cloud model is assigned to a role, the prompts and relevant paper excerpts for that role's requests are sent to the configured provider. Local models keep model inference on infrastructure controlled by the operator. The embedding model is separate: changing it requires a deliberate re-index workflow, not a runtime provider switch.
