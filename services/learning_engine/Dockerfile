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

WORKDIR /app

COPY services/learning_engine/requirements.txt services/learning_engine/constraints.txt ./
RUN pip install --no-cache-dir --require-hashes -r constraints.txt
COPY --from=jarvis-common-builder /tmp/dist/*.whl /tmp/dist/
RUN pip install --no-cache-dir --no-deps /tmp/dist/jarvis_common-*.whl \
    && rm -rf /tmp/dist

RUN adduser --disabled-password --no-create-home appuser

COPY services/learning_engine/learning_engine/ ./learning_engine/
COPY services/learning_engine/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "learning_engine.main:app", "--host", "0.0.0.0", "--port", "8001"]
