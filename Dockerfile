FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/assistant.sqlite3
WORKDIR /app
RUN groupadd --gid 10001 assistant && useradd --uid 10001 --gid assistant --no-create-home assistant \
    && mkdir /data && chown assistant:assistant /data
COPY telegram_assistant/ /app/telegram_assistant/
USER 10001:10001
VOLUME ["/data"]
CMD ["python", "-m", "telegram_assistant"]
