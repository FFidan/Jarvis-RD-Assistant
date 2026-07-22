<!-- verified-against-UI: 2026-07-11 | routes: /settings?section=models -->

# Hardware support matrix

JARVIS always has a supported CPU path. Setup enables NVIDIA CUDA when the
Docker runtime is ready and enables AMD ROCm only when the host exposes the
required `/dev/kfd` device. Other AMD and Intel hosts stay on CPU unless you
select the experimental Vulkan path.

---

## Backend × accelerator

|  | CUDA (NVIDIA) | ROCm (AMD) | Vulkan (AMD / Intel) | CPU |
|---|---|---|---|---|
| **Ollama** (default backend) | Supported | Experimental | Experimental | Supported |
| **vLLM** (advanced, opt-in overlay) | Supported | Not yet available | Not available | Unsupported |

- **Ollama** is the default local inference backend and ships in the base stack.
- **vLLM** is an opt-in overlay for advanced throughput comparisons (`docker-compose.vllm.yml`, `--profile vllm`); it currently ships an NVIDIA-only image. AMD ROCm images exist upstream but are not wired into JARVIS yet.
- **CPU** is always available as the fallback tier and needs no overlay.

## What setup selects

| Host | Default selection | Optional selection |
|---|---|---|
| NVIDIA with Container Toolkit | CUDA | CPU with `./setup.sh --gpu cpu` |
| NVIDIA without a ready runtime | CPU, with setup guidance | CUDA after installing the toolkit |
| AMD with `/dev/kfd` | ROCm | CPU or Vulkan |
| AMD without `/dev/kfd` | CPU | Vulkan with `./setup.sh --gpu vulkan` |
| Intel | CPU | Vulkan with `./setup.sh --gpu vulkan` |
| macOS | CPU inside Docker | None |

Setup records the selected Compose overlay so normal starts and updates keep the
same choice. A failed experimental overlay offers a CPU retry instead of leaving
the install unusable.

---

## Validation tiers

The table uses these validation labels:

- **Supported** — the default, actively used path; validated on real hardware.
- **Experimental** — the overlay and vendor-detection logic pass Compose and
  no-GPU fallback checks, but CI has no physical AMD or Intel GPU runner.
- **Community-reported** — a JARVIS user with matching hardware confirmed it works, via a [hardware compatibility report](https://github.com/limitcycle-oss/jarvis-rd-assistant/issues/new?template=hardware-report.yml).
- **Untested** — neither CI wiring nor a community report exists yet for that specific card.

We do not run cloud-GPU validation for AMD or Intel hardware. If you use one,
[file a hardware report](https://github.com/limitcycle-oss/jarvis-rd-assistant/issues/new?template=hardware-report.yml)
with the model, driver, selected overlay, and result.

---

## Known caveats

- **Narrow official ROCm gfx matrix.** ROCm officially supports a specific list of GPU architectures (RDNA3 / `gfx1100`, RDNA4 / `gfx120x`, and select older cards via `HSA_OVERRIDE_GFX_VERSION`). Cards outside the supported list may fail to initialize.
- **WSL2 + AMD = CPU tier.** Docker on Windows via WSL2 does not expose `/dev/kfd` to containers, so the ROCm overlay cannot engage. AMD GPUs are not currently usable through the Docker stack on Windows — JARVIS falls back to CPU. This is a WSL2/Docker limitation, not a JARVIS-specific bug.
- **RDNA4 discovery timeouts reported upstream.** Some RDNA4-class cards have reported GPU-discovery timeouts on recent ROCm releases; a retry or driver update sometimes resolves it.
- **Vulkan is a compatibility tier, not a performance tier.** It reaches more hardware — older AMD cards without ROCm, Intel Arc, Intel/AMD integrated graphics — but runs slower than ROCm or CUDA.
- **`paper_ingestion` (PDF parsing, reranking) stays CPU-only on AMD and Intel.** The overlays accelerate Ollama only. An AMD/Intel-accelerated ingestion pipeline is not shipped: the PyTorch ROCm wheel is several times larger than the CUDA one, and Docling has an open ROCm-specific crash upstream — both make the tradeoff unfavorable today.
- **The cross-encoder reranker ships pre-installed only in the CUDA-flavor image.** The published CUDA image includes `sentence-transformers`, so setting `RERANKER_ENABLED=true` works out of the box. The published CPU-flavor image omits it, and enabling the default `cross-encoder` backend there quietly falls back to plain retrieval ordering — the reranker never engages. To use it on a CPU-flavor install, set `INSTALL_OPTIONAL=true` in `.env` and rebuild from source with `./setup.sh --build-local`; note that this rebuild currently also downloads the multi-GB CUDA wheel set (the optional dependency pins are generated against the CUDA lockfile). The alternative `qwen3` reranker backend needs no extra packages and loads on either image, but is impractically slow without a GPU.

---

## Windows

**Windows + AMD GPU is CPU tier.** Use WSL2 + Docker Desktop as documented in [REQUIREMENTS.md](../REQUIREMENTS.md); the GPU overlays require a Linux kernel driver (`/dev/kfd` for ROCm, `/dev/dri` for Vulkan) that WSL2 does not forward for AMD hardware. NVIDIA GPUs work under WSL2 via the standard CUDA overlay — this limitation is AMD-specific.

---

## Reporting your hardware

If you run JARVIS on AMD or Intel hardware, [file a hardware compatibility report](https://github.com/limitcycle-oss/jarvis-rd-assistant/issues/new?template=hardware-report.yml) — GPU model, driver/ROCm version, the overlay that engaged, and the outcome. Reports feed directly into this page.

---

## Related pages

- [What your hardware gets you](hardware-and-models.md) — model tiers, default assignments, and GPU vendor support.
