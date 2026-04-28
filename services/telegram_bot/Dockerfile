FROM python:3.12.9-slim

WORKDIR /app

COPY libs/jarvis_common/ ./libs/jarvis_common/
COPY services/telegram_bot/requirements.txt ./requirements.txt
COPY services/telegram_bot/constraints.txt ./constraints.txt
RUN pip install --no-cache-dir -c constraints.txt ./libs/jarvis_common/ \
    && pip install --no-cache-dir -c constraints.txt -r requirements.txt \
    && rm -rf ./libs/jarvis_common

RUN adduser --disabled-password --no-create-home appuser

COPY services/telegram_bot/telegram_bot ./telegram_bot
COPY services/telegram_bot/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "telegram_bot.main"]
