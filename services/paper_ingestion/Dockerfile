FROM python:3.12.9-slim

WORKDIR /app

# Install PyTorch with CUDA support first (before other deps that might pull CPU-only torch)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu124

COPY libs/jarvis_common/ ./libs/jarvis_common/
RUN pip install --no-cache-dir ./libs/jarvis_common/

COPY services/paper_ingestion/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --no-create-home appuser

# Writable caches for HuggingFace, Surya/Marker model downloads, and Marker fonts
ENV HF_HOME=/tmp/hf_cache HOME=/tmp/appuser
RUN mkdir -p /tmp/hf_cache /tmp/appuser && chown -R appuser:appuser /tmp/hf_cache /tmp/appuser
# Marker downloads a font to its package static/ dir on first use
RUN SITE_PKGS=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    mkdir -p "$SITE_PKGS/static" && \
    chown -R appuser:appuser "$SITE_PKGS/static"

COPY services/paper_ingestion/app/ ./app/

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
