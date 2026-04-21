FROM python:3.12.9-slim

WORKDIR /app

COPY libs/jarvis_common/ ./libs/jarvis_common/
RUN pip install --no-cache-dir ./libs/jarvis_common/

COPY services/learning_engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --no-create-home appuser

COPY services/learning_engine/learning_engine/ ./learning_engine/

USER appuser

CMD ["uvicorn", "learning_engine.main:app", "--host", "0.0.0.0", "--port", "8001"]
