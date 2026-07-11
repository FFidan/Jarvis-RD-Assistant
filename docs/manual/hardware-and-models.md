<!-- verified-against-UI: 2026-06-13 | routes: /settings?section=models -->

# What your hardware gets you

JARVIS runs its AI models locally on your GPU. This page explains which models are
selected for your machine, what quality and speed to expect, and when it makes sense
to add a cloud model instead.

---

## Hardware tiers and default models

At first boot, JARVIS probes your GPU's VRAM and automatically selects models suited
to your hardware. The recommendation is advisory — you can change any assignment at
any time in **Settings → Models → AI models**.

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

- **CPU / < 4 GB:** Everything works, but first-paper analysis can take many minutes —
  tens of minutes for a long paper. This tier is supported for offline or resource-constrained
  setups, not for comfortable daily use.
- **10–19 GB (mid GPU):** A ~60 000-character paper summarises in roughly 45 seconds on
  a mid-range GPU. This is the reference tier (validated on a 16 GB card).
- **20–39 GB (mid-high GPU):** `qwen3:14b` fits alongside the embedder, giving noticeably
  richer summaries at similar latency.
- **≥ 40 GB (large GPU):** `qwen3:30b-a3b` — a 30B mixture-of-experts model — fits with
  ample headroom. Validated on a 48 GB deployment at a 16k context window.

---

## GPU vendor support

The tiers and models above apply regardless of GPU vendor — JARVIS auto-detects your GPU and VRAM at first boot. NVIDIA (CUDA) is the fully **supported** path. AMD (ROCm or Vulkan) and Intel (Vulkan) acceleration for Ollama are also auto-detected and available, labeled **[Experimental]**: same tier-based model selection, lower validation confidence. See the [hardware support matrix](hardware-support-matrix.md) for what "Experimental" means, known caveats, and how to report your results.

**Windows + AMD GPU currently falls back to CPU** — Docker on WSL2 does not expose the AMD kernel driver the ROCm overlay needs. NVIDIA GPUs are unaffected under WSL2.

`paper_ingestion` (PDF parsing, reranking) stays CPU-only on AMD and Intel hosts regardless of which overlay Ollama uses — only Ollama inference is GPU-accelerated on those vendors today.

---

## Two tier systems — not the same scale

You may notice two different ways JARVIS numbers hardware tiers. They are two views
of the same hardware, measured differently:

- **Settings / API tiers (0–4):** ordinal labels from the backend (`0 = CPU`, `1 = 4–10 GB`,
  `2 = 10–20 GB`, `3 = 20–40 GB`, `4 = ≥ 40 GB`). Shown on the Settings page as
  `Tier 0` … `Tier 4`.

- **Setup-script buckets:** string labels used by the installer (`cpu`, `lt-8`, `8-16`,
  `16-24`, `24-48`, `ge-48`). These appear in installer output and log lines during
  first-time setup.

Both describe the same hardware; they use different names because the installer script
and the backend were written independently. A `16-24` bucket corresponds to backend Tier 3;
`ge-48` corresponds to Tier 4. Neither scale has more tiers than the other — they just
carve the VRAM range at slightly different boundary points.[^tiers]

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

Optional cloud providers are a good fit when:

- Your local GPU is too small for the summary quality you want, and you do not want
  to upgrade hardware.
- You need a frontier model for a specific task and are comfortable with the data
  leaving your machine.
- You want to experiment with a larger model before committing to pulling it locally.

To add cloud capacity, use **Settings → Models → Providers & Routing**. JARVIS supports OpenAI, Anthropic, Google Gemini, OpenRouter, DeepSeek, Mistral, Kimi/Moonshot, Z.ai/GLM, and a Custom OpenAI-compatible endpoint. Provider settings are admin-wide and keys are stored encrypted at rest. Once a provider key is active, matching cloud models can be assigned to the `smart` or `fast` role alongside local Ollama models.

!!! note "Privacy"
    When a cloud model is assigned to a role, the prompts and relevant paper excerpts for that role's requests are sent to the configured provider. Local models keep model inference on infrastructure controlled by the operator. The embedding model is separate: changing it requires a deliberate re-index workflow, not a runtime provider switch.
