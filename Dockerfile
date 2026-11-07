FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY app/ ./app/
COPY migrations/ ./migrations/
RUN pip install --no-cache-dir . && useradd --system --uid 10001 app \
    && mkdir -p /var/lib/remont-monitor && chown 10001 /var/lib/remont-monitor \
    && chmod 700 /var/lib/remont-monitor
USER 10001
CMD ["python", "-u", "-m", "app.api.webhook"]
