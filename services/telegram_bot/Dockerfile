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

WORKDIR /app

COPY services/telegram_bot/requirements.txt ./requirements.txt
COPY services/telegram_bot/constraints.txt ./constraints.txt
# BuildKit cache mount: re-use pip's wheel cache across builds so a
# constraints.txt change doesn't re-download every wheel. --require-hashes
# still verifies each wheel; the mount is build-only so the image stays small.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r constraints.txt
COPY --from=jarvis-common-builder /tmp/dist/*.whl /tmp/dist/
RUN pip install --no-cache-dir --no-deps /tmp/dist/jarvis_common-*.whl \
    && rm -rf /tmp/dist

RUN adduser --disabled-password --no-create-home --gecos "" appuser

COPY services/telegram_bot/telegram_bot ./telegram_bot

USER appuser

CMD ["python", "-m", "telegram_bot.main"]
