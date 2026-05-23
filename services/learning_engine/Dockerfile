ARG PYTHON_BASE_IMAGE=python:3.12.9-slim
FROM ${PYTHON_BASE_IMAGE}

WORKDIR /app

COPY libs/jarvis_common/ ./libs/jarvis_common/
COPY services/learning_engine/requirements.txt services/learning_engine/constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt ./libs/jarvis_common/ \
    && pip install --no-cache-dir --require-hashes -r constraints.txt

RUN adduser --disabled-password --no-create-home appuser

COPY services/learning_engine/learning_engine/ ./learning_engine/
COPY services/learning_engine/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "learning_engine.main:app", "--host", "0.0.0.0", "--port", "8001"]
