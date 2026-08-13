ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm

# --- Stage 1: build jarvis_common as a wheel so we can install it WITHOUT
# breaking --require-hashes in the runtime stage. The runtime install uses
# `--no-deps` so all transitive deps come from constraints.txt (which IS
# hash-pinned).
FROM ${PYTHON_BASE_IMAGE} AS jarvis-common-builder
WORKDIR /build
COPY libs/jarvis_common/ ./jarvis_common/
COPY services/_shared/constraints-builder.txt ./
RUN pip install --no-cache-dir --require-hashes -r constraints-builder.txt \
    && python -m build --wheel --outdir /tmp/dist ./jarvis_common/

# --- Stage 2: runtime
FROM ${PYTHON_BASE_IMAGE}

# Set to "true" to install the reranker extras (sentence-transformers, etc.).
# Required only when RERANKER_ENABLED=true at runtime. PyTorch is now a base
# dependency (Docling), so it is installed regardless of this flag.
ARG INSTALL_OPTIONAL=false

# Set to "true" to install py-spy profiling tool.
# Not needed in production; only install for debugging/profiling.
ARG INSTALL_PROFILING=false

# Torch flavor for the base install (torch is a Docling base dependency,
# hash-pinned in the base constraints). "cpu" selects constraints-cpu.txt
# (torch from the PyTorch CPU index, no nvidia-*/triton — ~3.7 GB smaller);
# "cuda" selects constraints.txt (PyPI torch with bundled CUDA libraries).
# cpu is the default so builds never ship CUDA blobs unless asked to.
ARG TORCH_VARIANT=cpu

WORKDIR /app

COPY services/paper_ingestion/requirements.txt services/paper_ingestion/constraints.txt \
     services/paper_ingestion/constraints-cpu.txt \
     services/paper_ingestion/requirements-optional.txt services/paper_ingestion/constraints-optional.txt \
     services/paper_ingestion/constraints-optional-cpu.txt \
     services/paper_ingestion/constraints-profiling.txt ./
# Hash-pinned install of every transitive dep FIRST so this multi-GB layer is
# immune to jarvis_common changes (the wheel COPY below busts only the cheap
# --no-deps install). Final package set is identical to the prior ordering.
# A BuildKit cache mount keeps pip's wheel cache across builds, so a
# constraints change re-uses already-downloaded wheels (the multi-GB
# torch/docling set) instead of re-fetching them — dep-bump rebuilds drop from
# ~8 min to ~2 min. --require-hashes still verifies every wheel, so the cache
# cannot introduce an unpinned package. The mount is build-only, so the image
# stays small without --no-cache-dir.
RUN --mount=type=cache,target=/root/.cache/pip \
    case "$TORCH_VARIANT" in \
      cpu)  BASE_CONSTRAINTS=constraints-cpu.txt ;; \
      cuda) BASE_CONSTRAINTS=constraints.txt ;; \
      *) echo "ERROR: unknown TORCH_VARIANT='$TORCH_VARIANT' (expected 'cpu' or 'cuda')" >&2; exit 1 ;; \
    esac \
    && pip install --require-hashes -r "$BASE_CONSTRAINTS" \
    && if [ "$INSTALL_PROFILING" = "true" ]; then \
         pip install --require-hashes -r constraints-profiling.txt; \
       fi \
    && if [ "$INSTALL_OPTIONAL" = "true" ]; then \
         case "$TORCH_VARIANT" in \
           cpu)  OPTIONAL_CONSTRAINTS=constraints-optional-cpu.txt ;; \
           cuda) OPTIONAL_CONSTRAINTS=constraints-optional.txt ;; \
         esac; \
         pip install --require-hashes -r "$OPTIONAL_CONSTRAINTS"; \
       fi

# jarvis_common wheel installed with --no-deps (its transitives are already
# covered by the hash-pinned constraints.txt above). This cheap layer is the
# only one a jarvis_common change rebuilds.
COPY --from=jarvis-common-builder /tmp/dist/*.whl /tmp/dist/
RUN pip install --no-cache-dir --no-deps /tmp/dist/jarvis_common-*.whl \
    && rm -rf /tmp/dist

# OpenCV (pulled transitively by Docling / docling-ibm-models) loads shared
# libraries the slim base omits; without them cv2 — and thus Docling's RapidOCR
# engine — fails to import at runtime. Placed after the (cached) pip layer so
# rebuilds don't re-download the heavy torch/CUDA wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --no-create-home --gecos "" appuser

# Writable HuggingFace cache: Docling's layout/table models (docling-ibm-models)
# and transformers weights download here on first use (volume-backed in compose).
ENV HF_HOME=/tmp/hf_cache HOME=/tmp/appuser
RUN mkdir -p /tmp/hf_cache /tmp/appuser && chown -R appuser:appuser /tmp/hf_cache /tmp/appuser

# Bake in Docling's layout and table-structure models so a firewalled or
# air-gapped deployment can convert a document on its very first run, and
# every other deployment skips the multi-hundred-megabyte stall on first
# import. The pinned docling version fixes which model revisions this
# downloads, so no separate revision pin is needed here.
RUN docling-tools models download layout tableformer -o /opt/docling_models \
    && chown -R appuser:appuser /opt/docling_models
ENV DOCLING_ARTIFACTS_PATH=/opt/docling_models

# RapidOCR's ONNX models ship inside its wheel, so nothing downloads here at
# runtime; the directory is still made writable in case a future engine
# revision needs to populate its own cache. Setting DOCLING_ARTIFACTS_PATH
# above also makes Docling look for the OCR models under that directory
# instead of RapidOCR's own package path, so symlink the wheel's copies into
# the spot Docling expects rather than fetching or duplicating them. `ln -s`
# never fails on a missing target, so a wheel revision that renames one of
# these files would otherwise build clean and only break the first document
# conversion at runtime; the loop below fails the build instead.
RUN SITE_PKGS=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    mkdir -p "$SITE_PKGS/rapidocr/models" && \
    chown -R appuser:appuser "$SITE_PKGS/rapidocr/models" && \
    mkdir -p /opt/docling_models/RapidOcr/onnx/PP-OCRv6/det \
             /opt/docling_models/RapidOcr/onnx/PP-OCRv6/rec \
             /opt/docling_models/RapidOcr/onnx/PP-OCRv4/cls && \
    ln -s "$SITE_PKGS/rapidocr/models/PP-OCRv6_det_small.onnx" \
          /opt/docling_models/RapidOcr/onnx/PP-OCRv6/det/PP-OCRv6_det_small.onnx && \
    ln -s "$SITE_PKGS/rapidocr/models/PP-OCRv6_rec_small.onnx" \
          /opt/docling_models/RapidOcr/onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx && \
    ln -s "$SITE_PKGS/rapidocr/models/ch_ppocr_mobile_v2.0_cls_mobile.onnx" \
          /opt/docling_models/RapidOcr/onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx && \
    for link in /opt/docling_models/RapidOcr/onnx/PP-OCRv6/det/PP-OCRv6_det_small.onnx \
                /opt/docling_models/RapidOcr/onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx \
                /opt/docling_models/RapidOcr/onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx; do \
      test -e "$link" || { echo "ERROR: RapidOCR model symlink does not resolve: $link" >&2; exit 1; }; \
    done

COPY services/paper_ingestion/paper_ingestion/ ./paper_ingestion/
COPY services/paper_ingestion/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Writable backup-trigger dir: the admin Backup panel writes /backup-trigger/.backup_now
# here to request an on-demand DR run. The named volume mounts root-owned by default,
# so chown the mount point to appuser (mirrors the HF cache chown above) — otherwise
# the non-root app's sentinel write raises PermissionError and trigger_backup 503s.
RUN mkdir -p /backup-trigger && chown appuser:appuser /backup-trigger

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "paper_ingestion.main:app", "--host", "0.0.0.0", "--port", "8000"]
