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

| VRAM | Example hardware | Main model (smart) | Quick model (fast) | Embedding model (embed) |
|------|-----------------|-------------------|--------------------|------------------------|
| CPU / < 4 GB | No GPU, or very small GPU | `qwen3:4b` | `qwen3:4b` | `qwen3-embedding:4b` |
| 4–9 GB | GTX 1060 8 GB | `qwen3:4b` | `qwen3:4b` | `qwen3-embedding:4b` |
| 10–19 GB | RTX 3080 10 GB · RTX 4070 · 16 GB workstation | `qwen3:8b` | `qwen3:4b` | `qwen3-embedding:4b` |
| 20–39 GB | RTX 3090 · A10 24 GB | `qwen3:14b` | `qwen3:4b` | `qwen3-embedding:4b` |
| ≥ 40 GB | A40 · 48 GB-class GPU | `qwen3:30b-a3b` | `qwen3:4b` | `qwen3-embedding:4b` |

**Main model (smart)** — writes summaries, cards, and Ask answers.  
**Quick model (fast)** — scores and triages incoming papers.  
**Embedding model (embed)** — powers semantic search; it is fixed to the dimension of
your Qdrant collection and changing it requires re-indexing your library.

### What to expect

- **CPU / < 4 GB:** This tier is supported, but first-paper analysis can take
  tens of minutes for a long paper.
- **10–19 GB:** A roughly 60,000-character paper took about 45 seconds on the
  16 GB reference card. Results vary with model, context, and driver.
- **20–39 GB:** Setup recommends `qwen3:14b` while retaining the smaller quick
  model and embedder.
- **≥ 40 GB:** Setup recommends `qwen3:30b-a3b`. The reference validation used
  a 48 GB card and a 16k context window.

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

To add cloud capacity, use **Settings → Models → Providers & Routing**. JARVIS supports OpenAI, Anthropic, Google Gemini, OpenRouter, DeepSeek, Mistral, Kimi/Moonshot, Z.ai/GLM, and a Custom OpenAI-compatible endpoint. Provider settings are admin-wide and keys are stored encrypted at rest. Once a provider key is active, matching cloud models can be assigned to the `smart` or `fast` role alongside local Ollama models.

!!! note "Privacy"
    When a cloud model is assigned to a role, the prompts and relevant paper excerpts for that role's requests are sent to the configured provider. Local models keep model inference on infrastructure controlled by the operator. The embedding model is separate: changing it requires a deliberate re-index workflow, not a runtime provider switch.
