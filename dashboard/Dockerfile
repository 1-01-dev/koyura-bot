# Koyura admin dashboard (FastAPI + aiosqlite)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/koyura_bot.db \
    DOWNLOADS_PATH=/downloads

WORKDIR /app

RUN groupadd --system --gid 1000 koyura \
    && useradd --system --uid 1000 --gid koyura --home-dir /app --shell /usr/sbin/nologin koyura \
    && mkdir -p /data /downloads \
    && chown -R koyura:koyura /app /data /downloads

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=koyura:koyura . .

USER 1000:1000

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/login', timeout=4)"]

CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
