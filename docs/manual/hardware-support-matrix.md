<!-- verified-against-UI: 2026-07-11 | routes: /settings?section=models -->

# Hardware support matrix

JARVIS auto-detects your GPU vendor at first boot and engages the matching acceleration
overlay. Not every combination gets the same level of support — this page is the honest
picture, including what "Experimental" means and what to expect on Windows.

---

## Backend × accelerator

|  | CUDA (NVIDIA) | ROCm (AMD) | Vulkan (AMD / Intel) | CPU |
|---|---|---|---|---|
| **Ollama** (default backend) | Supported | [Experimental] | [Experimental] | Supported |
| **vLLM** (advanced, opt-in overlay) | Supported | Not yet available | Not available | Unsupported |

- **Ollama** is the default local inference backend and ships in the base stack.
- **vLLM** is an opt-in overlay for advanced throughput comparisons (`docker-compose.vllm.yml`, `--profile vllm`); it currently ships an NVIDIA-only image. AMD ROCm images exist upstream but are not wired into JARVIS yet.
- **CPU** is always available as the fallback tier and needs no overlay.

---

## Validation tiers

We label each cell above using three honest tiers instead of a single "works/doesn't":

- **Supported** — the default, actively used path; validated on real hardware.
- **[Experimental]** — the overlay and vendor-detection logic exist and are exercised by automated CI (compose config validation and a no-GPU boot-fallback smoke test on every release). This proves the plumbing is correct; it does not prove inference is fast or correct on your specific card — no CI runner has a real AMD or Intel GPU attached.
- **Community-reported** — a JARVIS user with matching hardware confirmed it works, via a [hardware compatibility report](https://github.com/limitcycle-oss/jarvis-rd-assistant/issues/new?template=hardware-report.yml).
- **Untested** — neither CI wiring nor a community report exists yet for that specific card.

We do not run paid cloud-GPU validation for AMD or Intel hardware. CI-wiring validation plus community reports are the honest, sustainable alternative — please [file a report](https://github.com/limitcycle-oss/jarvis-rd-assistant/issues/new?template=hardware-report.yml) if you run JARVIS on AMD or Intel silicon; it directly improves this page for the next person.

---

## Known caveats

- **Narrow official ROCm gfx matrix.** ROCm officially supports a specific list of GPU architectures (RDNA3 / `gfx1100`, RDNA4 / `gfx120x`, and select older cards via `HSA_OVERRIDE_GFX_VERSION`). Cards outside the supported list may fail to initialize.
- **WSL2 + AMD = CPU tier.** Docker on Windows via WSL2 does not expose `/dev/kfd` to containers, so the ROCm overlay cannot engage. AMD GPUs are not currently usable through the Docker stack on Windows — JARVIS falls back to CPU. This is a WSL2/Docker limitation, not a JARVIS-specific bug.
- **RDNA4 discovery timeouts reported upstream.** Some RDNA4-class cards have reported GPU-discovery timeouts on recent ROCm releases; a retry or driver update sometimes resolves it.
- **Vulkan is a compatibility tier, not a performance tier.** It reaches more hardware — older AMD cards without ROCm, Intel Arc, Intel/AMD integrated graphics — but runs slower than ROCm or CUDA.
- **`paper_ingestion` (PDF parsing, reranking) stays CPU-only on AMD and Intel.** The overlays accelerate Ollama only. An AMD/Intel-accelerated ingestion pipeline is not shipped: the PyTorch ROCm wheel is several times larger than the CUDA one, and Docling has an open ROCm-specific crash upstream — both make the tradeoff unfavorable today.

---

## Windows

**Windows + AMD GPU is CPU tier.** Use WSL2 + Docker Desktop as documented in [REQUIREMENTS.md](../REQUIREMENTS.md); the GPU overlays require a Linux kernel driver (`/dev/kfd` for ROCm, `/dev/dri` for Vulkan) that WSL2 does not forward for AMD hardware. NVIDIA GPUs work under WSL2 via the standard CUDA overlay — this limitation is AMD-specific.

---

## Reporting your hardware

If you run JARVIS on AMD or Intel hardware, [file a hardware compatibility report](https://github.com/limitcycle-oss/jarvis-rd-assistant/issues/new?template=hardware-report.yml) — GPU model, driver/ROCm version, the overlay that engaged, and the outcome. Reports feed directly into this page.

---

## Related pages

- [What your hardware gets you](hardware-and-models.md) — model tiers, default assignments, and GPU vendor support.
