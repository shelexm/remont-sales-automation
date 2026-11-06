FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY app/ ./app/
COPY migrations/ ./migrations/
RUN pip install --no-cache-dir . && useradd --system --uid 10001 app
USER 10001
CMD ["python", "-u", "-m", "app.api.webhook"]
