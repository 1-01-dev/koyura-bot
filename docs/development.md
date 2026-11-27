# Development

## Repository layout

```
koyura-bot/
├── docker-compose.yml       # canonical deployment (4 services)
├── .env.example             # every supported variable, with comments
├── deploy/
│   ├── README.md            # pointer to the native guide
│   └── systemd/             # koyura-engine, koyura-dashboard, koyura-bot, …
├── docs/                    # this documentation
├── images/                  # demo recordings used by the README
├── bot/                     # Telegram bot (python-telegram-bot)
│   ├── bot.py               # entry point, command registration
│   ├── handlers/            # url/download/callback/config/stats handlers
│   ├── koyura_client.py     # async HTTP client for the engine
│   ├── event_client.py      # fire-and-forget events to the dashboard
│   ├── cleanup.py           # age/disk cleanup task
│   ├── state_store.py       # SQLite-backed user preferences
│   ├── logging_setup.py     # log levels + bot-token redaction
│   └── url_safety.py        # user URL validation (bot-side copy)
├── dashboard/               # admin panel (FastAPI)
│   ├── main.py              # app factory, security headers, lifespan
│   ├── auth.py              # sessions, CSRF, rate limiting
│   ├── db.py                # SQLite queries (WAL, idempotent migrations)
│   ├── download_limits.py   # admin-editable queue limits pushed to the engine
│   ├── hash_password.py     # ADMIN_PASSWORD_HASH generator
│   ├── routes/              # pages + JSON API
│   ├── templates/           # Jinja2 pages
│   └── static/              # CSS, JS, self-hosted fonts
└── koyura/                  # media engine (Flask + yt-dlp + ffmpeg)
    ├── app.py               # Flask API, job runner, post-processing
    ├── download_queue.py    # FIFO queue, admission control, wait estimates
    ├── media_meta.py        # /api/info response shape (shared with the bot)
    ├── url_safety.py        # authoritative URL/SSRF validation
    ├── templates/           # web downloader UI
    └── static/              # loader page assets
```

## Architecture

```
Telegram user ──▶ Telegram cloud ──▶ self-hosted Bot API (internal :8081)
                                            │
                                            ▼
                              bot (python-telegram-bot, internal)
                                │                      │
                     internal API                 events
                                ▼                      ▼
                    media engine (Flask :8899)   dashboard (FastAPI :8080)
                                │                      │
                                ▼                      ▼
                        downloads volume         SQLite (WAL)
```

The engine is reachable only from the bot, the dashboard and the loopback web UI.
The bot posts download lifecycle events (`download_start`, `progress`,
`download_done`, `download_error`) to the dashboard; those posts are
fire-and-forget with a 2-second timeout and must never break a download.

### How a download works

1. The user sends a link (or `/mp4`, `/mp3`, `/best`).
2. The bot validates the URL and asks the engine for metadata.
3. The bot shows the thumbnail and format buttons; MP4 opens a quality picker.
4. The engine downloads with yt-dlp in its own process group and streams progress.
5. AV1/VP9/VP8 output is converted to H.264 **only when the policy says so**
   (`TRANSCODE_MODE`, off by default) or the user pressed *Convert*; otherwise
   the file is delivered as a document and other MP4s still get `+faststart`,
   so Telegram can stream them. MP3 output is tagged with title, artist and
   cover.
6. The bot uploads the file through the Bot API server and deletes the local
   copy; the cleanup task removes anything left behind.

### Reliability notes

- Each yt-dlp/ffmpeg invocation runs in a separate session; a timeout kills the
  whole process group, so no orphaned ffmpeg keeps writing to the volume.
- Partial files from failed or timed-out jobs are removed immediately.
- The download semaphore is released on every exit path, including crashes.
- SQLite runs in WAL mode with a busy timeout; schema creation is idempotent and
  safe on every start.
- On startup the dashboard marks rows left in `started`/`downloading` as failed,
  because the engine's job table and the bot's picker sessions are in memory.
- Bot preferences (`/setquality`, `/setformat`) persist in `BOT_STATE_DB_PATH`;
  per-message picker sessions are intentionally ephemeral (10-minute TTL) and are
  recreated by resending a link.

### Engine address and state

`KOYURA_ENGINE_URL` (default `http://koyura-engine:8899`) is where the bot and
the dashboard reach the engine; `KOYURA_STATE_DIR` is where the engine keeps its
queue throughput EWMA. Image names follow this repository:
`ghcr.io/<owner>/koyura-bot/<service>`, so the engine is published as
`koyura-engine`. Everything user-visible is branded **Koyura**.

## Running locally without Docker

Python 3.12+ is required by the dashboard. Start the three services in this
order, each in its own terminal:

```bash
# Shared secret for the internal engine API (never commit a real value)
export INTERNAL_API_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')

# 1. Media engine
cd koyura
pip install -r requirements-dev.txt
INTERNAL_API_TOKEN=$INTERNAL_API_TOKEN python app.py          # 127.0.0.1:8899

# 2. Dashboard
cd dashboard
pip install -r requirements-dev.txt
INTERNAL_API_TOKEN=$INTERNAL_API_TOKEN \
  ADMIN_PASSWORD='choose-a-strong-password' \
  SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))') \
  DB_PATH=./koyura_bot.db DOWNLOADS_PATH=./downloads \
  uvicorn main:create_app --factory --host 127.0.0.1 --port 8080

# 3. Bot
cd bot
pip install -r requirements-dev.txt
BOT_TOKEN=<token> INTERNAL_API_TOKEN=$INTERNAL_API_TOKEN \
  KOYURA_ENGINE_URL=http://127.0.0.1:8899 DOWNLOADS_PATH=../koyura/downloads \
  BOT_STATE_DB_PATH=./bot_state.db python bot.py
```

The dashboard and the engine refuse to start without strong secrets. Locally the
names are the internal ones (`SECRET_KEY`, `ADMIN_PASSWORD`), not the
`DASHBOARD_*` names from `.env.example` — see
[configuration.md](configuration.md#internal-names-for-native-runs).

All three services must point at the same filesystem path (`DOWNLOADS_PATH`) and
`ffmpeg`/`ffprobe` must be installed (`apt install ffmpeg`, `brew install ffmpeg`).
The bot talks to the Bot API server at `TELEGRAM_BOT_API_URL` (default
`http://telegram-bot-api:8081`); locally, either run that container or accept the
warning — uploads over 20 MB will then fail.

## Tests

```bash
cd bot        && pip install -r requirements-dev.txt && python -m pytest tests/ -v
cd dashboard  && pip install -r requirements-dev.txt && python -m pytest tests/ -v
cd koyura     && pip install -r requirements-dev.txt && python -m pytest tests/ -v

pip install -r .github/requirements-dev.txt
python -m pytest .github/tests -v
```

Dashboard tests must set `DB_PATH`, `ADMIN_PASSWORD` and `SECRET_KEY` **before**
importing `db.py` or `main.py`; the existing test files do this with `os.environ`
at the top of the file. A new dashboard test file must copy that pattern or the
import will create a database in the wrong place.

CI (`.github/workflows/ci.yml`) runs the four suites and validates
`docker compose config` on every pull request; releases
(`.github/workflows/release.yml`) publish the `koyura-engine`, `bot` and `dashboard`
images to GHCR.

## Development flags

Two variables exist to make local work bearable. Both are **sandbox only**:

| Variable | Effect | Risk outside a sandbox |
| --- | --- | --- |
| `INTERNAL_API_AUTH_DISABLED=true` | The engine accepts `/api/*` calls without `X-Internal-Token`. | Anything that can reach port 8899 can start downloads and read finished files. |
| `ALLOW_PRIVATE_URLS=true` | URL validation stops rejecting loopback, private, link-local and `.local`/`.internal` targets. | Turns the engine into an SSRF tool against your own network. |

Never set either on a host that is reachable by other people. There is no
supported configuration in which they are needed in production; on a throwaway
machine they save a step.

## Code conventions

- No linting, formatting or type-checking tooling is configured: no black, ruff,
  mypy or pre-commit. Match the surrounding style and keep lines readable.
- Test files insert the parent directory into `sys.path` to import the module
  under test.
- All `event_client` calls are fire-and-forget; they must never raise into a
  handler.
- `bot/logging_setup.py` must stay in the startup path: it pins the
  `httpx`/`httpcore` loggers to `WARNING` (their INFO lines contain
  `.../bot<token>/getMe`) and attaches a filter that redacts `bot<id>:<secret>`
  and the configured secrets. Never log a URL built from `BOT_TOKEN`.
- URL validation exists twice: `koyura/url_safety.py` is authoritative and
  `bot/url_safety.py` mirrors it for fast user feedback. Change both together.
