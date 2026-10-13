FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STATE_PATH=/data/events.sqlite3 BIND_HOST=0.0.0.0 PORT=8765
WORKDIR /app
RUN groupadd --gid 10001 events && useradd --uid 10001 --gid events --no-create-home events \
    && mkdir -m 700 /data && chown events:events /data
COPY pyproject.toml /app/pyproject.toml
COPY telegram_events/ /app/telegram_events/
RUN python -m pip install --no-cache-dir .
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "-m", "telegram_events"]
