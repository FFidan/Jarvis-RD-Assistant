FROM python:3.12.9-slim

WORKDIR /app

COPY libs/jarvis_common/ ./libs/jarvis_common/
RUN pip install --no-cache-dir ./libs/jarvis_common/

COPY services/paper_ingestion/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --no-create-home appuser

# Writable cache for HuggingFace models (cross-encoder reranker)
ENV HF_HOME=/tmp/hf_cache
RUN mkdir -p /tmp/hf_cache && chown appuser:appuser /tmp/hf_cache

COPY services/paper_ingestion/app/ ./app/

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
