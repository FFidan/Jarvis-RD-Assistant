ARG PYTHON_BASE_IMAGE=python:3.12.9-slim
FROM ${PYTHON_BASE_IMAGE}

# Set to "true" to install sentence-transformers + PyTorch (adds ~2 GB).
# Required only when RERANKER_ENABLED=true at runtime.
ARG INSTALL_OPTIONAL=false

# Set to "true" to install py-spy profiling tool.
# Not needed in production; only install for debugging/profiling.
ARG INSTALL_PROFILING=false

WORKDIR /app

COPY libs/jarvis_common/ ./libs/jarvis_common/
COPY services/paper_ingestion/requirements.txt services/paper_ingestion/constraints.txt \
     services/paper_ingestion/requirements-optional.txt services/paper_ingestion/constraints-optional.txt ./
RUN pip install --no-cache-dir -c constraints.txt ./libs/jarvis_common/ \
    && pip install --no-cache-dir --require-hashes -r constraints.txt \
    && if [ "$INSTALL_PROFILING" = "true" ]; then \
         pip install --no-cache-dir py-spy; \
       fi \
    && if [ "$INSTALL_OPTIONAL" = "true" ]; then \
         pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu124 \
         && pip install --no-cache-dir --require-hashes -r constraints-optional.txt; \
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
