FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5001

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libjpeg-dev zlib1g-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/private_media/uploads /app/private_media/generated \
    && useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5001/api/auth/session || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
