# Koyura media engine (Flask + yt-dlp + ffmpeg)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8899 \
    DOWNLOADS_PATH=/downloads

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd --system --gid 1000 koyura \
    && useradd --system --uid 1000 --gid koyura --home-dir /app --shell /usr/sbin/nologin koyura \
    && mkdir -p /downloads \
    && chown -R koyura:koyura /app /downloads

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=koyura:koyura . .

USER 1000:1000

EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8899/healthz', timeout=4)"]

CMD ["python", "app.py"]
