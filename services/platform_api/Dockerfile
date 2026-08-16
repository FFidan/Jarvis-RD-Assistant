ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_BASE_IMAGE} AS jarvis-common-builder
WORKDIR /build
COPY libs/jarvis_common/ ./jarvis_common/
COPY services/_shared/constraints-builder.txt ./
RUN pip install --no-cache-dir --require-hashes -r constraints-builder.txt \
    && python -m build --wheel --outdir /tmp/dist ./jarvis_common/

FROM ${PYTHON_BASE_IMAGE}

WORKDIR /app

COPY services/platform_api/requirements.txt services/platform_api/constraints.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r constraints.txt
COPY --from=jarvis-common-builder /tmp/dist/*.whl /tmp/dist/
RUN pip install --no-cache-dir --no-deps /tmp/dist/jarvis_common-*.whl \
    && rm -rf /tmp/dist

RUN adduser --disabled-password --no-create-home --gecos "" appuser

COPY services/platform_api/platform_api/ ./platform_api/
COPY services/platform_api/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "platform_api.main:app", "--host", "0.0.0.0", "--port", "8003"]
