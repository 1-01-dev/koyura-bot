# Deployment

Three ways to run Koyura, from easiest to most manual:

1. [Docker Compose](#1-docker-compose) — the canonical setup, four containers.
2. [Native venv + systemd](#2-native-venv--systemd) — for hosts without Docker,
   for example an unprivileged LXC container.
3. [Building `telegram-bot-api` from source](#3-telegram-bot-api-without-docker)
   — optional, raises the upload limit from 20 MB to 2 GB.

Everything is configured with environment variables; see
[configuration.md](configuration.md) for the full list.

## 1. Docker Compose

```bash
git clone https://github.com/1-01-dev/koyura-bot.git
cd koyura-bot
cp .env.example .env
```

Fill in `.env`: `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`INTERNAL_API_TOKEN`, `DASHBOARD_SECRET_KEY`, and `DASHBOARD_PASSWORD` (or
`DASHBOARD_PASSWORD_HASH`). Then:

```bash
docker compose up -d
docker compose ps                     # wait for dashboard/engine to report healthy
docker compose logs -f telegram-bot-api
```

| Service | Tech | Published port | Role |
| --- | --- | --- | --- |
| `dashboard` | FastAPI + aiosqlite | `127.0.0.1:8080` | Admin UI and event ingestion |
| `engine` | Flask + yt-dlp + ffmpeg | none (internal) | Download engine |
| `bot` | python-telegram-bot | none | Telegram client |
| `telegram-bot-api` | aiogram/telegram-bot-api | none | Self-hosted Bot API for 2 GB uploads |

Only the dashboard is reachable from the host, and only on loopback; the engine,
the bot and the Bot API server share the internal compose network.

Notes:

- The Bot API server downloads Telegram server data on its first start. This can
  take 30–60 seconds; the bot waits up to 60 s before it starts answering.
- The containers run as UID 1000. Named volumes created by an older version may
  be root-owned — `docker compose down && docker volume rm koyura-bot_downloads`
  recreates them.
- `docker compose config` validates the file; CI runs it on every pull request.

## 2. Native venv + systemd

Tested layout:

| What | Where |
| --- | --- |
| Application checkout | `/opt/koyura` |
| Virtualenv | `/opt/koyura/.venv` |
| Configuration | `/etc/koyura/koyura.env` (mode `0640`, owner `root:koyura`) |
| Runtime state, downloads, SQLite | `/var/lib/koyura` (created by systemd) |
| Units | `/etc/systemd/system/koyura-*.service` |

### 2.1 System packages

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip ffmpeg
```

`ffmpeg` and `ffprobe` are **required**: the engine's conversion/remux step and
the `hls:ffmpeg` downloader call them by name, so they must be installed
system-wide and resolvable through the unit's `Environment=PATH=` (`/usr/bin`).
yt-dlp itself is invoked as `python -m yt_dlp` and does not depend on `PATH`.
Python 3.12+ is required by the dashboard.

A conversion is **not** part of a normal download: `TRANSCODE_MODE=off` (the
default) only remuxes (`+faststart`), and the one CPU-bounded x264 pass runs
only when the operator allowed it or the user pressed *Convert* in the chat.
See [configuration](configuration.md#conversion-and-why-it-is-off-by-default)
for the modes and the CPU budget.

### 2.2 Application and virtualenv

```bash
sudo useradd --system --home /opt/koyura --shell /usr/sbin/nologin koyura
sudo git clone https://github.com/1-01-dev/koyura-bot.git /opt/koyura
sudo python3 -m venv /opt/koyura/.venv
sudo /opt/koyura/.venv/bin/pip install --upgrade pip
sudo /opt/koyura/.venv/bin/pip install \
  -r /opt/koyura/koyura/requirements.txt \
  -r /opt/koyura/bot/requirements.txt \
  -r /opt/koyura/dashboard/requirements.txt
sudo chown -R koyura:koyura /opt/koyura
```

### 2.3 Configuration

Create `/etc/koyura/koyura.env` from `.env.example` and adjust the storage paths
— and, because no compose layer renames them, the dashboard secrets:

```ini
# dashboard: internal names, not the DASHBOARD_* ones
ADMIN_USER=admin
ADMIN_PASSWORD=…            # or ADMIN_PASSWORD_HASH=pbkdf2_sha256$…
SECRET_KEY=…

DOWNLOADS_PATH=/var/lib/koyura/downloads
DB_PATH=/var/lib/koyura/koyura_bot.db
BOT_STATE_DB_PATH=/var/lib/koyura/koyura_bot_state.db
# Queue throughput EWMA. Must NOT be DOWNLOADS_PATH: the bot's cleanup and the
# panel's purge delete files there, which would erase the measured timings.
KOYURA_STATE_DIR=/var/lib/koyura
KOYURA_ENGINE_URL=http://127.0.0.1:8899
DASHBOARD_URL=http://127.0.0.1:8080
TELEGRAM_BOT_API_URL=http://127.0.0.1:8081
HOST=127.0.0.1
```

```bash
sudo install -d -m 0750 -o root -g koyura /etc/koyura
sudo install -m 0640 -o root -g koyura .env.example /etc/koyura/koyura.env
sudo editor /etc/koyura/koyura.env     # fill in the required secrets
```

The units run as the `koyura` user, so the environment file must be readable by
that group (`0640 root:koyura`). `0600` root-only is **not** enough for
`EnvironmentFile=` to be readable by the service.

### 2.4 Units

The units ship in [`deploy/systemd/`](../deploy/systemd):

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemd-analyze verify koyura-engine.service koyura-dashboard.service \
  koyura-bot.service koyura-telegram-bot-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now koyura-engine koyura-dashboard koyura-bot
```

Each unit already sets what it needs:

- `StateDirectory=koyura` creates `/var/lib/koyura` owned by the service user and
  keeps it writable under `ProtectSystem=strict`; no manual `mkdir`/`chown`.
  That directory also holds `koyura_queue_stats.json`, the queue's measured
  throughput (see `KOYURA_STATE_DIR` in
  [configuration.md](configuration.md#media-engine-and-limits)).
- `Environment=PATH=/opt/koyura/.venv/bin:…` — systemd does not inherit a login
  shell's `PATH`.
- `Restart=always` keeps the services alive across crashes, with the usual
  hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, …).

### 2.5 Operations

```bash
systemctl status koyura-dashboard koyura-engine koyura-bot
journalctl -u koyura-bot -f
sudo systemctl restart koyura-dashboard          # after editing /etc/koyura/koyura.env
```

To deploy a new revision: `sudo git -C /opt/koyura pull`, reinstall the
requirements if they changed, then restart the units. The dashboard marks
interrupted downloads as failed on start (`MARK_STALE_ON_STARTUP`), so no rows
stay stuck in `downloading`.

## 3. `telegram-bot-api` without Docker

A self-hosted Bot API server raises the upload limit from Telegram's 20 MB to
2 GB. It is optional: without it, set
`TELEGRAM_BOT_API_URL=https://api.telegram.org` in `koyura.env` and the bot falls
back to the standard 20 MB limit.

Build it from source on Ubuntu 24.04:

```bash
sudo apt-get install -y build-essential cmake gperf libssl-dev zlib1g-dev
git clone --recursive https://github.com/tdlib/telegram-bot-api.git
cd telegram-bot-api
# Submodules (TDLib and friends) must be present before cmake:
git submodule update --init --recursive
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/usr/local ..
cmake --build . --target install -j"$(nproc)"
```

The binary is installed as `/usr/local/bin/telegram-bot-api`. Then:

```bash
sudo cp deploy/systemd/koyura-telegram-bot-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now koyura-telegram-bot-api
```

The unit already passes the required flags:

```text
--local --http-ip-address=127.0.0.1 --http-port=8081 --dir=/var/lib/koyura/bot-api
```

`TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are read from
`/etc/koyura/koyura.env`, and the bot reaches the server at
`http://127.0.0.1:8081` (`TELEGRAM_BOT_API_URL`). Keep the HTTP port on
loopback: the Bot API server has no authentication of its own.

## 4. HTTPS in front of the dashboard

The dashboard speaks plain HTTP and binds to `127.0.0.1`; it has no TLS of its
own. For remote access, put a reverse proxy in front of it and let the proxy
terminate TLS.

Two variables matter:

| Variable | Value behind a proxy |
| --- | --- |
| `SESSION_COOKIE_SECURE` | `true` (or leave `auto`, which already sets `Secure` when the proxy forwards `X-Forwarded-Proto: https`) |
| `TRUST_PROXY_HEADERS` | `true` — only when the proxy overwrites `X-Forwarded-For`/`X-Forwarded-Proto` for every request |

With `TRUST_PROXY_HEADERS=false` the dashboard sees the proxy's address as the
client, so login rate limiting would throttle everyone together; with it set to
`true` behind a proxy that does *not* sanitise those headers, any client can
spoof its address. Set it only when both statements are true.

### Caddy

```caddyfile
koyura.example.com {
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Forwarded-Proto {scheme}
    }
}
```

Caddy sets `X-Forwarded-For` and `X-Forwarded-Proto` itself and reloads
certificates automatically.

### Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name koyura.example.com;

    ssl_certificate     /etc/letsencrypt/live/koyura.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/koyura.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Add a second `server` block on port 80 that redirects to `https://`. After
changing anything in the proxy or the variables above, restart the dashboard so
the new cookie settings take effect:

```bash
sudo systemctl restart koyura-dashboard     # native
docker compose up -d dashboard              # compose
```

Never publish the engine (`:8899`) or the Bot API server (`:8081`) through the
proxy: both trust the internal network and the shared token instead of user
authentication.

## 5. Troubleshooting

- **`dashboard` exits with "SECRET_KEY is required"** — set `DASHBOARD_SECRET_KEY`
  in `.env` for compose, or `SECRET_KEY` in the environment file for a native
  run. The same split applies to `ADMIN_PASSWORD` and `INTERNAL_API_TOKEN`.
- **Engine returns 401 or 503** — `INTERNAL_API_TOKEN` differs between services,
  or is unset (503 means "no token configured", 401 means "wrong token"). It
  must be identical for `engine`, `bot` and `dashboard`.
- **`Permission denied` writing `/downloads` or `/data`** — the containers run as
  UID 1000. Named volumes created by an older version may be root-owned:
  `docker compose down && docker volume rm koyura-bot_downloads`.
- **Bot cannot reach the Bot API server on first start** — the server pulls
  Telegram's data on first run and the bot waits up to 60 seconds. Check
  `docker compose logs telegram-bot-api` (or
  `journalctl -u koyura-telegram-bot-api`).
- **Old files are not deleted** — a file is removed only once it is older than
  `CLEANUP_MAX_AGE_HOURS` *and* untouched for `CLEANUP_ACTIVE_GRACE_SECONDS`;
  check the bot log for cleanup errors and the cleanup interval.
- **The dashboard shows 0 downloads** — the rows come from events the bot posts;
  a bot that cannot reach `DASHBOARD_URL` still downloads fine but leaves the
  panel empty. Check the bot log for `dashboard` connection warnings.
