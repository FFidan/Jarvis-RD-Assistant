ARG PYTHON_BASE_IMAGE=python:3.12.9-slim

# --- Stage 1: build jarvis_common as a wheel so we can install it WITHOUT
# breaking --require-hashes in the runtime stage. The runtime install uses
# `--no-deps` so all transitive deps come from constraints.txt (which IS
# hash-pinned). See SEC-CRIT-02/03/04.
FROM ${PYTHON_BASE_IMAGE} AS jarvis-common-builder
WORKDIR /build
COPY libs/jarvis_common/ ./jarvis_common/
COPY services/_shared/constraints-builder.txt ./
RUN pip install --no-cache-dir --require-hashes -r constraints-builder.txt \
    && python -m build --wheel --outdir /tmp/dist ./jarvis_common/

# --- Stage 2: runtime
FROM ${PYTHON_BASE_IMAGE}

# Set to "true" to install sentence-transformers + PyTorch (adds ~2 GB).
# Required only when RERANKER_ENABLED=true at runtime.
ARG INSTALL_OPTIONAL=false

# Set to "true" to install py-spy profiling tool.
# Not needed in production; only install for debugging/profiling.
ARG INSTALL_PROFILING=false

WORKDIR /app

COPY services/paper_ingestion/requirements.txt services/paper_ingestion/constraints.txt \
     services/paper_ingestion/requirements-optional.txt services/paper_ingestion/constraints-optional.txt \
     services/paper_ingestion/constraints-profiling.txt ./
COPY --from=jarvis-common-builder /tmp/dist/*.whl /tmp/dist/
# Hash-pinned install of every transitive dep, then install the local
# jarvis_common wheel with --no-deps (its transitives are already covered by
# constraints.txt; --no-deps prevents pip from pulling unverified packages).
# The cu124 torch pre-install was removed: constraints-optional.txt pins
# torch==2.11.0 (PyPI) with its bundled nvidia-* libs, so the cu124 download
# was redundantly overwritten on the next line.
RUN pip install --no-cache-dir --require-hashes -r constraints.txt \
    && pip install --no-cache-dir --no-deps /tmp/dist/jarvis_common-*.whl \
    && rm -rf /tmp/dist \
    && if [ "$INSTALL_PROFILING" = "true" ]; then \
         pip install --no-cache-dir --require-hashes -r constraints-profiling.txt; \
       fi \
    && if [ "$INSTALL_OPTIONAL" = "true" ]; then \
         pip install --no-cache-dir --require-hashes -r constraints-optional.txt; \
       fi

RUN adduser --disabled-password --no-create-home appuser

# Writable caches for HuggingFace, Surya/Marker model downloads, and Marker fonts
ENV HF_HOME=/tmp/hf_cache HOME=/tmp/appuser
RUN mkdir -p /tmp/hf_cache /tmp/appuser && chown -R appuser:appuser /tmp/hf_cache /tmp/appuser
# Marker downloads a font to its package static/ dir on first use
RUN SITE_PKGS=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    mkdir -p "$SITE_PKGS/static" && \
    chown -R appuser:appuser "$SITE_PKGS/static"

COPY services/paper_ingestion/paper_ingestion/ ./paper_ingestion/
COPY services/paper_ingestion/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "paper_ingestion.main:app", "--host", "0.0.0.0", "--port", "8000"]
