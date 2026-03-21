FROM python:3.12.9-slim

WORKDIR /app

COPY libs/jarvis_common/ ./libs/jarvis_common/
COPY services/telegram_bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir ./libs/jarvis_common/ \
    && pip install --no-cache-dir -r requirements.txt \
    && rm -rf ./libs/jarvis_common

RUN adduser --disabled-password --no-create-home appuser

COPY services/telegram_bot/app ./app

USER appuser

CMD ["python", "-m", "app.main"]
