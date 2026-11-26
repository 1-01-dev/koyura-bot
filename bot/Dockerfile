# Koyura Telegram bot (python-telegram-bot + httpx)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOWNLOADS_PATH=/downloads \
    BOT_STATE_DB_PATH=/data/koyura_bot_state.db

WORKDIR /app

RUN groupadd --system --gid 1000 koyura \
    && useradd --system --uid 1000 --gid koyura --home-dir /app --shell /usr/sbin/nologin koyura \
    && mkdir -p /data /downloads \
    && chown -R koyura:koyura /app /data /downloads

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=koyura:koyura . .

USER 1000:1000

CMD ["python", "bot.py"]
