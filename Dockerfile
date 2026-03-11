FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPENWEBUI_DISABLE_LOCAL_MODELS=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libssl-dev ca-certificates curl git && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "open-webui" "open-webui[server]" fastapi uvicorn motor passlib[bcrypt] python-dotenv

COPY . /app

RUN mkdir -p /app/config /app/brand

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
