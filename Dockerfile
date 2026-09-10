FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

ENV PYTHONUNBUFFERED=1
ENV LOG_DIR=/app/logs
VOLUME ["/app/downloads", "/app/logs"]

CMD ["python", "-m", "bot"]
