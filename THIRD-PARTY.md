# Third-party components

Koyura is MIT-licensed (see [LICENSE](LICENSE)) and stands on the work below.
Each entry lists what it is used for and the licence the project publishes.

## Runtime dependencies

| Component | Used for | Licence |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Download backend for every supported site | [Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE) |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | Telegram bot framework | [LGPL-3.0](https://github.com/python-telegram-bot/python-telegram-bot/blob/master/LICENSE) |
| [aiogram/telegram-bot-api](https://github.com/aiogram/telegram-bot-api) | Docker packaging of the self-hosted Bot API server that lifts uploads to 2 GB | No licence file in that repository; it builds and ships [tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api), [BSL-1.0](https://github.com/tdlib/telegram-bot-api/blob/master/LICENSE_1_0.txt) |
| [Flask](https://github.com/pallets/flask) | Media engine HTTP API and web UI | [BSD-3-Clause](https://github.com/pallets/flask/blob/main/LICENSE.txt) |
| [FastAPI](https://github.com/fastapi/fastapi) | Admin dashboard | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| [Starlette](https://github.com/encode/starlette) | ASGI toolkit under FastAPI | [BSD-3-Clause](https://github.com/encode/starlette/blob/master/LICENSE.md) |
| [Uvicorn](https://github.com/encode/uvicorn) | ASGI server for the dashboard | [BSD-3-Clause](https://github.com/encode/uvicorn/blob/master/LICENSE.md) |
| [Jinja2](https://github.com/pallets/jinja) | Dashboard and engine templates | [BSD-3-Clause](https://github.com/pallets/jinja/blob/main/LICENSE.txt) |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | Async SQLite access for the dashboard | [MIT](https://github.com/omnilib/aiosqlite/blob/main/LICENSE) |
| [itsdangerous](https://github.com/pallets/itsdangerous) | Signing for session and CSRF cookies | [BSD-3-Clause](https://github.com/pallets/itsdangerous/blob/main/LICENSE.txt) |
| [httpx](https://github.com/encode/httpx) | Bot and dashboard HTTP clients | [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| [Chart.js](https://github.com/chartjs/Chart.js) | Dashboard charts (loaded from jsDelivr) | [MIT](https://github.com/chartjs/Chart.js/blob/master/LICENSE.md) |

A full, pinned list is in each service's `requirements.txt`; those files also
pull in transitive dependencies (for example Werkzeug, AnyIO and Pydantic) under
their own permissive licences.

## System dependencies

| Component | Used for | Licence |
| --- | --- | --- |
| [ffmpeg](https://ffmpeg.org/) | Transcoding, remuxing, MP3 tagging and metadata probes; called by name, installed by the operator (`apt install ffmpeg`, `brew install ffmpeg`) | [LGPL-2.1-or-later](https://ffmpeg.org/legal.html) by default; the usual distribution builds (Debian/Ubuntu, Homebrew) enable GPL components, making them GPL-2.0-or-later |
| [TDLib / telegram-bot-api](https://github.com/tdlib/telegram-bot-api) | Compiled by the Bot API image or by hand when it is not run from Docker | [BSL-1.0](https://github.com/tdlib/telegram-bot-api/blob/master/LICENSE_1_0.txt) |

ffmpeg is never bundled or linked; it is executed as a separate program, so its
licence does not extend to Koyura's own code. Distributing a GPL-enabled ffmpeg
build alongside Koyura does not relicense Koyura.

## Fonts

The dashboard self-hosts Inter, JetBrains Mono and Shippori Mincho instead of
calling a font CDN. They are distributed under the
[SIL Open Font License 1.1](https://openfontlicense.org/).

## Trademarks

Telegram is a trademark of Telegram Messenger LLP. YouTube, TikTok, Instagram
and the other site names used in the user interface are trademarks of their
respective owners. Koyura is not affiliated with, endorsed by, or sponsored by any
of them.
