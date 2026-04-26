FROM python:3.12.9-slim

WORKDIR /app

COPY libs/jarvis_common/ ./libs/jarvis_common/
RUN pip install --no-cache-dir ./libs/jarvis_common/

COPY services/learning_engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --no-create-home appuser

COPY services/learning_engine/learning_engine/ ./learning_engine/
COPY services/learning_engine/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "learning_engine.main:app", "--host", "0.0.0.0", "--port", "8001"]
