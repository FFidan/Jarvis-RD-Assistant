ARG PYTHON_BASE_IMAGE=python:3.12.9-slim

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

WORKDIR /app

COPY services/paper_ingestion/requirements.txt services/paper_ingestion/constraints.txt \
     services/paper_ingestion/requirements-optional.txt services/paper_ingestion/constraints-optional.txt \
     services/paper_ingestion/constraints-profiling.txt ./
# Hash-pinned install of every transitive dep FIRST so this multi-GB layer is
# immune to jarvis_common changes (the wheel COPY below busts only the cheap
# --no-deps install). Final package set is identical to the prior ordering.
# The cu124 torch pre-install was removed: constraints-optional.txt pins
# torch==2.11.0 (PyPI) with its bundled nvidia-* libs.
# A BuildKit cache mount keeps pip's wheel cache across builds, so a
# constraints.txt change re-uses already-downloaded wheels (the multi-GB
# torch/docling set) instead of re-fetching them — dep-bump rebuilds drop from
# ~8 min to ~2 min. --require-hashes still verifies every wheel, so the cache
# cannot introduce an unpinned package. The mount is build-only, so the image
# stays small without --no-cache-dir.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r constraints.txt \
    && if [ "$INSTALL_PROFILING" = "true" ]; then \
         pip install --require-hashes -r constraints-profiling.txt; \
       fi \
    && if [ "$INSTALL_OPTIONAL" = "true" ]; then \
         pip install --require-hashes -r constraints-optional.txt; \
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

RUN adduser --disabled-password --no-create-home appuser

# Writable HuggingFace cache: Docling's layout/table models (docling-ibm-models)
# and transformers weights download here on first use (volume-backed in compose).
ENV HF_HOME=/tmp/hf_cache HOME=/tmp/appuser
RUN mkdir -p /tmp/hf_cache /tmp/appuser && chown -R appuser:appuser /tmp/hf_cache /tmp/appuser
# RapidOCR downloads its ONNX models into its own site-packages dir on first use;
# make that dir writable so appuser can populate it at runtime.
RUN SITE_PKGS=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    mkdir -p "$SITE_PKGS/rapidocr/models" && \
    chown -R appuser:appuser "$SITE_PKGS/rapidocr/models"

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
