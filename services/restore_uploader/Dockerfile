# Off-host DR upload ingress — stdlib-only, no pip install (trust-boundary
# component that receives the encrypted archive set + operator key so those bytes
# never transit the app). See uploader.py for the boundary rationale.
ARG PYTHON_BASE_IMAGE=python:3.12.9-slim
FROM ${PYTHON_BASE_IMAGE}
WORKDIR /app
COPY uploader.py .

# Non-root. Pre-create + own the inbox mountpoint so a first-mount of the empty
# restore_inbox volume inherits this uid (mirrors services/paper_ingestion's
# /backup-trigger chown); the root sidecar still writes to it regardless.
RUN adduser --system --no-create-home --uid 10001 uploader \
    && mkdir -p /restore-inbox \
    && chown uploader /restore-inbox
USER uploader

# Read-only rootfs friendly: never write .pyc under the read-only /app.
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8090
ENTRYPOINT ["python", "uploader.py"]
