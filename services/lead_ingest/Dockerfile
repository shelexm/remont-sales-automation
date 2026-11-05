FROM python:3.12-slim
WORKDIR /app
COPY services/lead_ingest/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --uid 10001 app
COPY services/lead_ingest/app.py /app/app.py
USER 10001
EXPOSE 8080
CMD ["python", "-u", "/app/app.py"]
