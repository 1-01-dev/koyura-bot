# Configuration

Everything is configured through environment variables, so the same values work
for Docker Compose and for native venv/systemd runs. The annotated template is
[`.env.example`](../.env.example); this page explains every variable in it.

Copy it first — never commit the filled-in file:

```bash
cp .env.example .env
```

## Required variables

The dashboard and the media engine refuse to start when a required secret is
missing, too short, or a well-known placeholder. There are no default
passwords anywhere.

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `BOT_TOKEN` | yes | — | Telegram bot token from [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_API_ID` | yes | — | API id from [my.telegram.org](https://my.telegram.org) → API Development Tools. |
| `TELEGRAM_API_HASH` | yes | — | API hash from the same page; used by the self-hosted Bot API server. |
| `INTERNAL_API_TOKEN` | yes | — | Shared secret for the engine API. The bot and the dashboard send it as `X-Internal-Token`; the engine answers 401 without it and 503 when it is not configured. |
| `DASHBOARD_SECRET_KEY` | yes | — | Signing key for session and CSRF cookies, at least 32 random characters. |
| `DASHBOARD_PASSWORD` | one of the two | — | Plain admin password, at least 8 characters, compared in constant time. |
| `DASHBOARD_PASSWORD_HASH` | one of the two | — | PBKDF2-SHA256 hash instead of a plain password (`pbkdf2_sha256$…`). Wins over `DASHBOARD_PASSWORD` when both are set. |

Set either `DASHBOARD_PASSWORD` or `DASHBOARD_PASSWORD_HASH`, not both.

## Admin dashboard

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `DASHBOARD_USER` | no | `admin` | Login name. |
| `DASHBOARD_PORT` | no | `8080` | Host port. The port is always bound to `127.0.0.1`. |
| `SESSION_MAX_AGE_SECONDS` | no | `43200` | Session lifetime in seconds (12 h); values below 300 are raised to 300. |
| `SESSION_COOKIE_SECURE` | no | `auto` | `auto` marks the session cookie `Secure` whenever the request is HTTPS; `true`/`false` force it. |
| `LOGIN_RATE_LIMIT_ATTEMPTS` | no | `5` | Failed logins allowed per client address inside one window. |
| `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | no | `300` | Length of that window. |
| `TRUST_PROXY_HEADERS` | no | `false` | Honour `X-Forwarded-For` / `X-Forwarded-Proto`. Enable only behind a reverse proxy you control, otherwise clients can spoof their address. |
| `MARK_STALE_ON_STARTUP` | no | `true` | On start, mark downloads left in `started`/`downloading` as failed. The engine's job table is in memory, so those rows can never finish. |

## Media engine and limits

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `KOYURA_ENGINE_URL` | no | `http://koyura-engine:8899` | Address the bot and the dashboard use to reach the engine. Point it at `http://127.0.0.1:8899` for native runs. |
| `HOST` | no | `0.0.0.0` | Engine bind address. Use `127.0.0.1` when a local proxy or a native install fronts it. |
| `PORT` | no | `8899` | Engine port. |
| `MAX_CONCURRENT_DOWNLOADS` | no | `2` | Slots that download at the same time. yt-dlp and ffmpeg are CPU-bound, so one slot per vCPU is the sweet spot. Overridable at runtime from **Admin → Download limits**. |
| `PER_USER_MAX_ACTIVE` | no | `1` | Jobs one user may run at once (1…`MAX_CONCURRENT_DOWNLOADS`). |
| `PER_USER_MAX_QUEUED` | no | `1` | Jobs one user may keep waiting (0…3). |
| `MAX_QUEUE_LENGTH` | no | `6` | Hard cap on waiting jobs. `0` disables queueing entirely. |
| `QUEUE_MAX_WAIT_SECONDS` | no | `120` | Longest predicted wait a new job may be queued for. |
| `QUEUE_ESTIMATE_MBPS` | no | `2` | Fallback throughput for the wait estimate, used until real timings exist. |
| `QUEUE_ESTIMATE_OVERHEAD_SECONDS` | no | `6` | Fixed per-job overhead in the estimate (merge, transcode, probe). |
| `QUEUE_EXPIRY_SECONDS` | no | `120` | A job waiting longer than this is dropped with a readable error. |
| `KOYURA_STATE_DIR` | no | `/var/lib/koyura` | Engine state directory; holds the queue throughput EWMA. In compose it is the `koyura-engine-state` volume. |
| `QUEUE_STATS_PATH` | no | `$KOYURA_STATE_DIR/koyura_queue_stats.json` | Explicit path for that EWMA file. Never point it at `DOWNLOADS_PATH`: the bot's cleanup and the panel's purge delete files there. |
| `DOWNLOAD_TIMEOUT` | no | `900` | Hard limit per download in seconds. On expiry the whole yt-dlp/ffmpeg process group is killed. |
| `INFO_TIMEOUT` | no | `60` | Limit for metadata lookups. |
| `MAX_FILESIZE_MB` | no | `2000` | Passed to yt-dlp; larger files are refused. `0` means unlimited. |
| `MAX_DURATION_SECONDS` | no | `7200` | Passed to yt-dlp as a duration filter. `0` turns it off. |
| `JOB_TTL_SECONDS` | no | `3600` | How long a finished job stays queryable in memory. |

### The download queue

The engine keeps a FIFO queue instead of answering "busy" as soon as the slots
are taken, so the bot and the web downloader always see the same state. A new
job is started, queued, or refused with HTTP `429`:

```text
wait = (remaining time of running jobs + estimates of the jobs ahead) / slots
```

| Refusal `code` | When |
| --- | --- |
| `user_busy` | The user already runs (or already queued) the allowed number of jobs |
| `queue_full` | `MAX_QUEUE_LENGTH` jobs are already waiting |
| `too_long` | The predicted wait exceeds `QUEUE_MAX_WAIT_SECONDS` |
| `busy` | Queueing is disabled (`MAX_QUEUE_LENGTH=0`) and every slot is taken |

A refusal body is `{"error", "code", "retry_after", "wait_seconds",
"queue_position": null}` and carries a `Retry-After` header. An accepted job
answers `202` with `status` (`downloading` or `queued`), `queue_position` and
`wait_seconds`. `GET /api/queue` returns `active`, `queued`, `limits`, `counts`,
`next_wait_seconds` and the current EWMA samples; `POST /api/settings` changes
the limits at runtime (the admin panel calls it after every save).

Estimates are `overhead + size_MB / MBps`, clamped to 5…900 s. `MBps` and the
duration come from an EWMA of measured finished jobs, kept per media kind
(video/audio) in `KOYURA_STATE_DIR`. Until a kind has a sample the fallback
`QUEUE_ESTIMATE_MBPS` is used and the estimate is reported as approximate, so
the interface can say "~2 min, estimated" instead of inventing precision.

### Recommended limit combinations

Starting points for the server sizes Koyura is usually deployed on. They are
only advice: any valid combination works, and the admin panel can apply a
preset for you.

| Preset | `MAX_CONCURRENT_DOWNLOADS` | `MAX_QUEUE_LENGTH` | `QUEUE_MAX_WAIT_SECONDS` | `PER_USER_MAX_ACTIVE` |
| --- | --- | --- | --- | --- |
| 1 vCPU / 2 GB | 1 | 4 | 90 | 1 |
| 2 vCPU / 4 GB | 2 | 6 | 120 | 1 |
| 4 vCPU / 8 GB | 4 | 12 | 180 | 2 |
| 8 vCPU / 16 GB | 6 | 20 | 240 | 3 |

Per-user active slots follow `max(1, slots // 2)` capped at 3, and stay freely
editable inside `1…MAX_CONCURRENT_DOWNLOADS`. `1` is the honest choice when more
than one person uses the bot: one user cannot occupy both slots.

The environment is read once at startup; raise `MAX_CONCURRENT_DOWNLOADS` by
one at a time and watch the load, since every extra slot competes for the same
CPU with the transfers already running.

### Setting the limits from the admin panel

**Admin → Download limits** edits six values — concurrent downloads, per-user
active, per-user queued, queue length, max wait and the estimate throughput —
and applies them to a running engine without a restart:

1. the form is validated server-side with the same bounds the engine enforces
   (1…8 slots, 0…50 queue, 15…900 s wait, 0.2…50 MB/s, per-user active ≤ slots,
   per-user queued 0…3); an invalid value is explained next to the field and
   nothing that was typed is lost;
2. the values are stored in the dashboard's SQLite `settings` table
   (`key`, `value`, `updated_at`);
3. the panel calls the engine's `POST /api/settings` with the internal token, so
   the change takes effect immediately; one log line records who changed what
   (`download limits updated by admin: max_concurrent 2->4 (...)`)
4. on its own startup the dashboard re-applies whatever was stored, so a restart
   of the engine or the box keeps the saved limits. The environment is only the
   fallback for a fresh install.

The panel also detects the server (`os.cpu_count()`, `/proc/meminfo`, cgroup
limits in a container), names the preset that fits and fills the form with it on
one click.

### Setting the progress fields from the admin panel

**Admin → Progress fields** decides which lines the bot's live progress message
carries — eight checkboxes (`bot_progress.*` in the same `settings` table),
default title + bar. The bot reads them from `GET /api/bot-settings` and caches
the answer for 45 s, so saving is enough: there is nothing to push to the engine
or the bot, and no restart. See `docs/bot-behavior.md` §9 for the table of keys,
the rendered shapes and the "everything off still shows the stage" rule.

## Bot uploads

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `MAX_SEND_MB` | no | `2000` | Largest file the bot offers and sends, in megabytes. |

The bot refuses to offer a format it cannot deliver. `MAX_SEND_MB` is that
ceiling: the format keyboard hides every format whose **known** size is above
it, and hides the MP3 button when the audio estimate is above it. A format whose
size yt-dlp did not report is still offered — the engine refuses it with its own
`MAX_FILESIZE_MB`, and taking the choice away would be worse than one failed
download.

`2000` is the limit of the self-hosted Bot API server (`telegram-bot-api`, which
lifts uploads to 2 GB). On the public `api.telegram.org` a bot may upload 20 MB,
so set `MAX_SEND_MB=20` there. When everything is over the limit the bot says so
instead of drawing an empty keyboard, and `/mp4` / `/best` fall back to the
highest quality that fits, naming the quality they skipped in the caption.

## Cleanup (shared downloads volume)

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `CLEANUP_MAX_AGE_HOURS` | no | `1` | Delete finished files older than this. |
| `CLEANUP_MAX_DISK_MB` | no | `5000` | If the downloads directory grows past this, delete the oldest files first. The admin page also shows usage against it. |
| `CLEANUP_INTERVAL_SECONDS` | no | `300` | Cleanup cycle interval. |
| `CLEANUP_ACTIVE_GRACE_SECONDS` | no | `300` | Files modified more recently than this are treated as in-flight and never deleted. |

## Storage

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `DOWNLOADS_PATH` | no | `/downloads` | Directory shared by the engine, the bot and the dashboard. For native runs point all three at one real path. |
| `DB_PATH` | no | `/data/koyura_bot.db` | Dashboard SQLite database (WAL mode). |
| `BOT_STATE_DB_PATH` | no | `/data/koyura_bot_state.db` | Bot preference store; survives restarts. |
| `LOG_LEVEL` | no | `INFO` | Log level for all Python services. |

## Images

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `GHCR_OWNER` | no | `1-01-dev` | Owner part of the image path in `docker-compose.yml`: `ghcr.io/<GHCR_OWNER>/koyura-bot/<service>`. |
| `IMAGE_TAG` | no | `latest` | Tag used for the `engine`, `bot` and `dashboard` images. Pin a release tag in production. |

## Variables that live outside `.env.example`

A few values are set by `docker-compose.yml` or only matter for local work:

| Variable | Default | What it does |
| --- | --- | --- |
| `DASHBOARD_URL` | `http://dashboard:8080` | Where the bot posts download events. Native runs: `http://127.0.0.1:8080`. |
| `TELEGRAM_BOT_API_URL` | `http://telegram-bot-api:8081` | Self-hosted Bot API server. Set it to `https://api.telegram.org` to skip that server; uploads are then capped at 20 MB. |
| `INTERNAL_API_AUTH_DISABLED` | `false` | **Sandbox only.** Accepts unauthenticated engine API calls. Never enable it on a host reachable by anyone else. |
| `ALLOW_PRIVATE_URLS` | `false` | **Sandbox only.** Skips the private/loopback address check in URL validation. |

## Internal names for native runs

Compose maps the `.env` names onto the names the code reads. A native run has no
compose in front of it, so the environment file for systemd must use the internal
names — otherwise the dashboard exits with `SECRET_KEY is required`:

| `.env.example` (compose) | Name the code reads (native) |
| --- | --- |
| `DASHBOARD_USER` | `ADMIN_USER` |
| `DASHBOARD_PASSWORD` | `ADMIN_PASSWORD` |
| `DASHBOARD_PASSWORD_HASH` | `ADMIN_PASSWORD_HASH` |
| `DASHBOARD_SECRET_KEY` | `SECRET_KEY` |

Everything else (`BOT_TOKEN`, `TELEGRAM_API_*`, `INTERNAL_API_TOKEN`, the engine,
cleanup and storage variables) keeps its name in both cases. See
[deployment.md](deployment.md) for the full native recipe.

## Generating secrets

Three values must be random and unique. `token_urlsafe` output is fine for all of
them:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # INTERNAL_API_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY
```

Do not reuse one value for two variables, and do not put them in shell history
on a shared host — write them straight into `.env`.

## Generating the dashboard password hash

Storing a hash instead of a plain password keeps the credential out of the
process environment:

```bash
# with Docker
docker compose run --rm dashboard python -m hash_password

# from a checkout with the dashboard dependencies installed
cd dashboard && python -m hash_password
```

The helper asks for the password twice (or takes it as an argument), requires at
least 8 characters, and prints a single `pbkdf2_sha256$…` line. Put that line in
`.env` as `DASHBOARD_PASSWORD_HASH` (native: `ADMIN_PASSWORD_HASH`) and leave the
plain password variable empty.
