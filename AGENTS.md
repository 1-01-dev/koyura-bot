# AGENTS.md — Koyura

Instructions for coding agents working in this repository. Read this before you
touch anything; the invariants at the bottom are the ones that break production.

## What this is

A self-hosted Telegram bot that saves video and audio from YouTube, TikTok,
Instagram and ~1000 other sites, with an admin dashboard and a web downloader
next to it. Four services, each a directory at the repository root:

| Directory | Tech | Port | Role |
| --- | --- | --- | --- |
| `koyura/` | Flask + yt-dlp + ffmpeg | 8899 | Media engine: info, download, queue, web loader |
| `bot/` | python-telegram-bot + httpx | — | Telegram client |
| `dashboard/` | FastAPI + aiosqlite + Jinja2 | 8080 | Admin panel: stats, history, errors, limits |
| `telegram-bot-api` | upstream aiogram image | 8081 | Self-hosted Bot API, lifts uploads to 2 GB |

`deploy/systemd/` holds the native units, `docs/` the operator documentation,
`.github/` CI plus the config tests. `deploy/systemd/koyura-engine.service` is the
engine unit; its `WorkingDirectory` is `/opt/koyura/koyura`.

## Tests

Four suites, each independent. Run the one you touched, then all four before a
pull request:

```bash
cd bot       && python -m pytest tests/ -q
cd dashboard && python -m pytest tests/ -q
cd koyura    && python -m pytest tests/ -q
python -m pytest .github/tests -q          # workflows, compose, units, Dockerfiles
```

- Dashboard tests **must** set `DB_PATH`, `ADMIN_PASSWORD` and `SECRET_KEY`
  before importing `db.py` or `main.py`; a new test module copies that
  `os.environ` block from an existing one or it writes a database in the wrong
  place.
- `bot` and `koyura` tests put their own directory on `sys.path`; do not import
  across services.
- No linting, formatting, type-checking or pre-commit is configured. Match the
  surrounding style; keep functions documented the way their neighbours are.
- The config suite is the only place that reads YAML and Dockerfiles. If you
  rename a service, directory, image or unit, run it — and update the constants
  at the top of `.github/tests/test_ci_config.py`, not just the workflow.

## Running locally without Docker

Use `docs/development.md` for the full recipe. Short version: start `koyura/`
(`python app.py`), then `dashboard/` (`uvicorn main:create_app --factory`), then
`bot/` (`python bot.py`). `INTERNAL_API_TOKEN` must be identical in all three,
`DOWNLOADS_PATH` must point at the same directory, and ffmpeg must be installed.
The engine and the dashboard refuse to start without a strong `INTERNAL_API_TOKEN`
and `SECRET_KEY` (32+ characters).

## Commits

- One logical change per commit, imperative subject, body explains *why* when it
  is not obvious: `fix(dashboard): make /history?date=today filter the day it
  names`. Scopes in use: `bot`, `dashboard`, `engine`, `docs`, `deploy`, `ci`,
  `config`, `release`.
- Never commit `.env`, a real token, a password, a database or anything from
  `downloads/`. CI fails on a tracked `.env`.
- Test-only changes are commits too — do not mix them into a feature commit if
  the tests pin behaviour that already existed.
- Branch off `main`, open a pull request, do not merge it yourself.

## What you must not break

These are load-bearing for a running deployment:

1. **HTTP contracts.** `/api/info`, `/api/download`, `/api/status/<job_id>`,
   `/api/queue`, `/api/settings`, `/api/file/<job_id>`, `/healthz` and every
   JSON field they return. The bot, the dashboard and the web loader all parse
   them; a rename is a breaking change even when the tests pass.
2. **The internal token.** Every `/api/*` call carries `X-Internal-Token` unless
   `INTERNAL_API_AUTH_DISABLED=true` (sandbox only). A missing token is 503, a
   wrong one is 401. Never log a URL or header built from a secret.
3. **Sessions and CSRF.** The dashboard authenticates with a signed cookie
   (itsdangerous `URLSafeTimedSerializer`), not JWT/OAuth, and every form and
   state-changing fetch carries the double-submit CSRF token
   (`koyura_csrf` cookie + `X-CSRF-Token` header or `csrf_token` field). Every
   rendered form must keep its token. Do not weaken the login rate limit or the
   loopback-only binding.
4. **The database schema.** `dashboard/db.py` creates its tables idempotently and
   runs migrations on every start. Add columns with a migration that is safe to
   replay; never rewrite or drop an existing table, and keep SQLite in WAL mode.
   The bot's `BOT_STATE_DB_PATH` schema is likewise additive only.
5. **Environment names.** `KOYURA_ENGINE_URL` (default
   `http://koyura-engine:8899`) and `KOYURA_STATE_DIR` are the only names the
   code reads. Renaming either one is a deployment change: the operator updates
   `/etc/koyura/koyura.env` with the merge.
6. **URL validation.** `koyura/url_safety.py` is authoritative (scheme, length,
   credentials, private/loopback/link-local addresses, `.local`/`.internal`
   hosts); `bot/url_safety.py` is a mirror for fast user feedback. Change both
   together.
7. **Log hygiene.** `bot/logging_setup.py` must stay in the startup path: it pins
   `httpx`/`httpcore` to WARNING (their INFO lines contain
   `.../bot<token>/getMe`) and redacts `bot<id>:<secret>` plus configured
   secrets. Never build a log message from `BOT_TOKEN`.
8. **The stage chain.** A download is one stage per message, and each stage
   *replaces* the previous one (`bot/handlers/stages.py`): the preview card is
   deleted once the progress message is up, the progress message once the file
   is up. Progress ticks only ever **edit** that one progress message — posting a
   new message per tick is spam and hits Telegram's edit limits. Delivery is
   send-then-delete, so a refused send never leaves the chat without a card, and
   deletion stays best effort (48-hour-old, already-deleted and undeletable
   messages are a DEBUG line, never a failure). Never delete the file: it is the
   result.

## Gotchas

- The engine's job registry (`jobs = {}` in `koyura/app.py`) is in memory.
  Restarting it loses active jobs; the dashboard marks those rows failed on its
  next start (`MARK_STALE_ON_STARTUP`).
- `downloads` is a shared volume between all three services. Locally all three
  must point at the same filesystem path, or uploads silently 404.
- The download queue's EWMA file lives in `KOYURA_STATE_DIR`, deliberately not in
  `DOWNLOADS_PATH`: the bot's cleanup and the panel's purge delete files there.
- Concurrency is a non-blocking semaphore (`MAX_CONCURRENT_DOWNLOADS`, default 3
  in code, 2 in compose); over the limit answers 429 with a `retry_after` the bot
  shows verbatim. Each job has a hard `DOWNLOAD_TIMEOUT` after which the whole
  yt-dlp/ffmpeg process group is killed.
- The web UI may pass `?token=` because a plain download link cannot set a
  header; nothing else may.
