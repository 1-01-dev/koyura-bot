# Security

Koyura is meant to run on your own host, next to your own files, so most of the
security model is about not letting the network reach things it should not.

## What is already in place

**Internal API token.** `/api/info`, `/api/download`, `/api/status/<job>` and
`/api/file/<job>` on the engine require `X-Internal-Token` matching
`INTERNAL_API_TOKEN`. Without a configured token the engine fails closed (HTTP
503) instead of running open; a wrong token gets 401. Event ingestion on the
dashboard (`POST /api/events`) verifies the same header, and the loopback web UI
is the only caller allowed to pass `?token=` — a plain download link cannot set a
header.

**No public ports.** Only the dashboard is published, and only on `127.0.0.1`.
The engine, the bot and the Bot API server share the internal compose network and
are not reachable from the host or the LAN by default.

**Dashboard authentication.** PBKDF2-SHA256 password hashes (or a plain password
compared in constant time), signed session cookies (`HttpOnly`, `SameSite=Lax`,
`Secure` on HTTPS) with a 12-hour default lifetime, per-IP login rate limiting,
and CSRF tokens on every form and on state-changing API calls. A missing or
placeholder `SECRET_KEY` stops the process at startup rather than falling back to
a default.

**URL validation (SSRF).** Only `http`/`https`, length-limited, no embedded
credentials, and requests to loopback, private, link-local or otherwise reserved
addresses and to `.local`/`.internal` hostnames are rejected. The bot validates
for fast user feedback; `koyura/url_safety.py` is the authoritative check and
runs before yt-dlp ever sees the URL.

**Containers.** All images run as a non-root user (`1000:1000`) with
`cap_drop: ALL`, `no-new-privileges`, healthchecks and pinned base images;
`.dockerignore` files keep `.env`, `.git`, tests and downloads out of the build
context. The Bot API image is pinned to a release tag, never `:latest`.

**Limits and lifecycle.** A semaphore caps parallel downloads (`MAX_CONCURRENT_DOWNLOADS`,
429 above it), every download has a hard timeout, file size and duration caps are
passed to yt-dlp, and a timeout kills the whole yt-dlp/ffmpeg process group so
nothing keeps writing to the volume. Partial files are deleted immediately; the
cleanup task removes old files and trims the directory when it grows past
`CLEANUP_MAX_DISK_MB`.

**No secrets in logs.** Failed logins log neither the username nor the password.
The bot pins the `httpx`/`httpcore` loggers to `WARNING` — their INFO request
lines contain `.../bot<token>/getMe` — and a filter strips `bot<id>:<secret>`
fragments and the configured tokens from every record before it reaches a
handler.

**Response headers.** The dashboard sends `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: same-origin`,
`Cross-Origin-Opener-Policy: same-origin` and a Content-Security-Policy that
allows scripts only from the dashboard itself and the Chart.js CDN.

## What you have to do yourself

- Keep `.env` out of version control and readable only by the service user
  (`0640 root:koyura` for the native layout).
- Set `TRUST_PROXY_HEADERS=true` only behind a reverse proxy that overwrites
  `X-Forwarded-For` and `X-Forwarded-Proto`; otherwise clients can spoof their
  address and sidestep rate limiting.
- Terminate TLS in front of the dashboard for any access beyond localhost — see
  [deployment.md](deployment.md#4-https-in-front-of-the-dashboard).
- Never enable `INTERNAL_API_AUTH_DISABLED` or `ALLOW_PRIVATE_URLS` outside a
  throwaway sandbox. They exist for local development only.
- Keep the Bot API server on loopback: it has no authentication of its own and
  hands out Telegram credentials implicitly through the bot token.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
reporting on the repository — **Security → Advisories → Report a vulnerability**
on <https://github.com/1-01-dev/koyura-bot> — or contact the maintainers
directly if that path is unavailable.

Include what you need to make the report reproducible: affected version or
commit, the configuration that matters (`INTERNAL_API_AUTH_DISABLED` and
`ALLOW_PRIVATE_URLS` in particular), the steps, and the impact you expect. A
proof of concept helps; a working exploit against a live third-party host does
not — please test only against your own instance.

Expect an acknowledgement within a few days. Once a fix is available it is
released on `main` and mentioned in the advisory, and reporters are credited
unless they prefer otherwise.
