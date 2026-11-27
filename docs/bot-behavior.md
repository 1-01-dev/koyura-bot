# Bot behaviour inventory

This document is the **contract** the bot has with its users. It was written
before the `bot/handlers.py` rewrite (wave 1) by reading the file as it stood at
commit `6274984` (the merge of `main` into `koyura/wave1-bot-handlers`), the
`setMyCommands` block in `bot/bot.py`, and the modules the handlers call
(`download_card`, `progress_card`, `handlers.stages`, `koyura_client`,
`event_client`, `state_store`, `url_safety`).

Everything listed here is behaviour the rewrite must preserve. Line references
(`handlers.py:NN`) point at the pre-rewrite file so a reviewer can diff the
wording character by character.

The rewrite keeps every string, every command name, every `callback_data`
value, every engine call and every dashboard event. What changed is only the
internal organisation (module split, function and variable names); see the
"Parity evidence" section.

---

## 1. Command surface

Commands registered with Telegram (`setMyCommands` in `bot/bot.py`, in this
order) and the matching `CommandHandler`s in `register_handlers`
(`handlers.py:1198`):

| Command | Menu description | Handler | What it does |
| --- | --- | --- | --- |
| `/start` | Start Koyura | `onboarding.start_command` | First contact: the language screen (when more than one language is enabled and the user has no saved choice), otherwise the welcome text with its inline keyboard — see §10. |
| `/help` | Help and commands | `cmd_help` (`:82`) | Static how-to, sent as **MarkdownV2** (the source is pre-escaped). |
| `/mp3` | Download as MP3 | `cmd_mp3` (`:213`) | Extracts links from the command or the replied-to message, validates them, starts one direct audio job per link (no picker). |
| `/mp4` | Download best quality MP4 | `cmd_mp4` (`:229`) | Same, video, no format id → engine's best. |
| `/best` | Best available quality | `cmd_best` (`:245`) | Alias: calls `cmd_mp4` verbatim. |
| `/platforms` | Supported platforms | `cmd_platforms` (`:109`) | Static list, one indented line per platform. |
| `/settings` | Your preferences | `commands.settings_command` | Prints quality, format, language and the armed MP3/MP4 mode, plus one button per enabled language (the current one ticked). |
| `/setquality` | Set default quality | `cmd_setquality` (`:166`) | `best/1080/720/480/360`; usage line when no argument, options list when invalid. |
| `/setformat` | Set default format | `cmd_setformat` (`:184`) | `video/audio`; same shape of usage/invalid replies. |
| `/stats` | Koyura statistics | `cmd_stats` (`:117`) | Uptime, in-process download/error counters, files and MB in `DOWNLOADS_PATH`, number of live sessions. |

Notes:

* **`/cancel` is documented but not implemented.** `cmd_help` ends with
  `/cancel - Cancel (coming soon)` and nothing registers a `/cancel` handler.
  This is preserved as-is: adding the command would be a user-visible change.
  "Cancel" today means: the card is locked while a job runs (see §4) and there
  is no way to abort one; the file arrives anyway.
* An **unknown** `/command` gets no reply at all: the free-text handler is
  `MessageHandler(filters.TEXT & ~filters.COMMAND, url_handler)` (`:1214`), so
  commands never reach it and no handler matches.
* Every command returns silently when `update.message` or `update.effective_user`
  is `None`.
* `/setquality` and `/setformat` read `context.args`; unknown preference keys
  cannot be reached from the chat.

---

## 2. `callback_data`

One handler and one pattern for every button (`:1216`):

```python
CallbackQueryHandler(download_callback, pattern=r"^(dl|fmt|qty):")
```

Format: `"<scheme>:<message_id>:<url_hash>:<action>"` — `_parse_callback`
(`:469`) reads `parts[1]` as the message id, `parts[-1]` as the action and
`parts[-2]` as the URL hash, and needs at least four parts. `download_card`
builds the `dl:` form (`build_callback_data`), ~30 bytes, well inside Telegram's
64-byte limit.

| Scheme | Origin | Semantics |
| --- | --- | --- |
| `dl:` | current `download_card.build_format_keyboard` | action = a yt-dlp format id (`137`), or `mp3`, or `best`. |
| `fmt:` | legacy buttons still sitting in old chats | historically the *kind* (`video` / `audio`). |
| `qty:` | legacy buttons still sitting in old chats | historically a quality (`best`, `720`, …). |

Actions and what `download_callback` (`:507`) does with them:

| Action | Behaviour |
| --- | --- |
| any id present in `info.formats` (e.g. `137`) | video download of that exact format, height resolved from `info`. |
| an id **not** in `info` (e.g. old `qty:` value, or a format the engine no longer lists) | still forwarded to the engine as `format_id`; the engine validates it, so an expired session keeps working. |
| `best` | video, `format_id=None` → engine picks. |
| `mp3` | audio. |
| `audio` | aliased to `mp3` (`:535`). |
| `video` | **legacy MP4 button**: does not download; re-renders the per-format keyboard on the same message (`:529`). |
| `back` | same as `video`: re-render the keyboard. |

Malformed data (fewer than four parts, non-integer id) is answered and ignored
— no message edit, no job.

---

## 3. Sessions (picker state)

In-memory only, `_state: dict[str, dict]` (`:49`), key
`"<chat_id>:<message_id>:<url_hash>"`, `url_hash = sha256(url)[:12]`. A restart
drops every session — by design: the entry only describes a message in a chat.

Each entry holds `url, chat_id, user_id, username, info, message_id, photo,
job_active, created`. `STATE_TTL = 600` seconds; `_evict_stale` runs at the
start of `url_handler` and `download_callback`, so an entry is dropped the next
time the bot sees any link or button.

Lookup (`_callback_chat_ids` `:482`, `_lookup_entry` `:499`) is defensive about
`query.message` being `None`/inaccessible: it probes the message's own chat id
first, then every stored chat id that owns a message with the same id, and tries
both the id from the callback data and the id Telegram reports.

When no entry is found the user sees
**"Session expired. Please send the link again."** on the card.

---

## 4. Download flow (the card)

### 4.1 Link intake

`url_handler` (`:387`) runs on any non-command text:

1. `_evict_stale()`.
2. `URL_REGEX = https?://[^\s<>"']+` — no link → silent return.
3. `_safe_urls` → `url_safety.validate_url` per link, in `asyncio.to_thread`
   (DNS must not block the loop), capped at `MAX_LINKS_PER_MESSAGE = 5`.
4. For each safe link: `_send_card` (`:406`).

Rejection notice (once per message, before the cards):

> I can only download from public http(s) links. That link was skipped because it is not allowed.

and, when the message held more than five links, appended on a new line:

> Only the first 5 links of a message are processed.

### 4.2 Preview card

`_send_card` posts **"Fetching info..."**, calls `koyura_client.get_info(url)`,
then renders `download_card.build_card(info)` with
`build_format_keyboard(info, message_id, url_hash)`.

| Engine outcome | User sees |
| --- | --- |
| `KoyuraServiceDown` | **"Download service temporarily unavailable."** (edits the status message) |
| `KoyuraInfoError` | **"Failed to fetch info: <engine message>"** |
| `KoyuraError` | **"Error: <engine message>"** |
| `info["thumbnail"]` set | the status message is deleted and a **photo** message is sent with the caption + buttons |
| thumbnail send fails | fall back to editing the status message into a text card |
| caption edit fails | **"Could not render this link. Try /mp4 or /mp3 instead."** |

The entry's `photo` flag records whether the card is a photo (caption edits) or
plain text (text edits). The card is the first stage of the one-message chain:
`handlers.stages.StageTracker` deletes the status line **after** the photo it is
replaced by has arrived, so a refused upload never leaves the chat without a
card.

#### The send limit

Telegram refuses a file above its own ceiling (`MAX_SEND_MB`, 2000 MB on the
self-hosted Bot API, 20 on the public one), so the keyboard is built from
`download_card.format_offer(info)` instead of the raw format list:

* a format whose **known** size is above the limit gets no button at all, and the
  filter runs before the ten-button cap, so a hidden format never takes a slot
  from a sendable one;
* a format with an **unknown** size stays — the engine still refuses it with its
  own `--max-filesize`, and removing it would take the choice away;
* the MP3 button is hidden when the expected audio size is above the limit. The
  estimate is the engine's `audio.size_bytes`, or bitrate × duration
  (`abr`/`tbr` × `duration` / 8) when the engine did not report one;
* nothing fits at all → no keyboard. The status line becomes the title plus
  **"All available formats are over the send limit (2000 MB). Try another link
  or MP3."** (the MP3 is still possible). When the MP3 is too large as well the
  line is **"This video is too large to send (limit 2000 MB)."** — or **"This
  audio is too large…"** for a link that never had video. Both are in the
  catalogue (`limit.*`), so a Russian chat reads “2000 МБ”.

### 4.3 Button press

`download_callback` (`:507`):

1. `_evict_stale()`, parse data (§2).
2. No session → answer + **"Session expired. Please send the link again."**
3. `video`/`back` → answer + re-render the keyboard, no job.
4. `audio` → `mp3`.
5. `entry["job_active"]` already true → answer with
   **"Already downloading — wait for the file."** (`show_alert=False`) and stop.
   This is the "repeat tap" guard: one card can only ever run one job, because a
   second job would race for the same caption.
6. Otherwise: `job_active = True`, plain `query.answer()`, and the download runs
   as an `asyncio` task. `query.answer()` is called **exactly once** per query
   on every path — Telegram rejects a second answer.

### 4.4 Stages

`run_download` → `_advance`. Every stage **replaces** the one before it: the
preview card is deleted once the progress message is up, the progress message is
deleted once the file (or the error card) is up. `handlers.stages.StageTracker`
owns that chain and enforces two rules: send first / delete second, and deletion
is always best effort (a message older than 48 h, already removed by the user or
undeletable is a DEBUG line, never a failure).

1. `replace_text(build_status_caption(info, "Starting download…"))`: the
   progress message is posted first, then the preview card is deleted — a stage
   that never arrived must not cost the user the card they can see.
2. `koyura_client.start_download(url, fmt, format_id, title, uploader=…,
   height=…, user_id=…, size_bytes=_size_hint(...))`.
3. Refusal (`KoyuraBusy`, HTTP 429) → the error card **replaces** the progress
   message: `finish_with_text(build_error_caption(...))` posts it, attaches
   the format keyboard to the new message and deletes the progress one. The
   wording comes from `progress_card.busy_message(e)`, **without** the "Tap a
   format to try again" hint (the buttons are there and the wait is known):
   * `code=user_busy` → "You already have a download running. Try again in ~2 min."
   * anything else (`busy`, `queue_full`, `too_long`, or no code at all) → "The service is overloaded right now — try again in ~2 min."
   * the wait is `retry_after`/`wait_seconds` rounded by
     `progress_card.format_queue_wait` (nearest 5 s under 90 s, minutes rounded
     **up** above), default `~2 min`.
   * `KoyuraServiceDown` → **"Download service temporarily unavailable."**
   * other `KoyuraError` → its message.
   * a payload without `job_id` → **"Download service returned an unexpected answer."**
4. `status == "queued"` → `progress_card.build_queued_caption(info,
   queue_position, wait_seconds, approximate=queue_estimate_approximate)`:
   `In queue №3 · ~45 s`, plus `Estimated wait` and
   `You can leave this chat — the file arrives here.` No buttons.
5. `event_client.send_download_start(job_id, user_id, username, chat_id, url,
   platform=info["extractor"] or "unknown", format, quality, title)` — inside
   `try/except: pass`, the dashboard must never break a download.
6. Poll loop (`_poll_progress` `:1009`): every 2 s, at most 450 iterations
   (15 min), `GET /api/status/<job_id>`:
   * `queued` → queue caption (same throttle);
   * `downloading`/other → `event_client.send_progress(...)` **on every poll**
     (the dashboard timeline is not throttled), then the caption is rewritten
     only when `ProgressThrottle` allows it: `PROGRESS_EDIT_INTERVAL` (3 s,
     env) **and** `PROGRESS_MIN_PERCENT_DELTA` (1.5, env) or a 5 s ETA drop;
   * `KoyuraServiceDown` → "Download service temporarily unavailable." + a
     `download_error` event, then stop;
   * other `KoyuraError` → ignored, keep polling (a single 500 is not fatal);
   * `done`/`error` → return.
   * Loop exhausted → **"Download timed out."** + `download_error` event,
     `_stats["errors"] += 1`.
7. `status == "error"` → error caption with the engine's message (or
   "Unknown error") + `download_error` event.
8. The finished file is located through the shared volume (`_local_file`):
   `DOWNLOADS_PATH/<basename>` first, then the raw path from the engine. Missing
   → **"File not found after download."** + `download_error` event.
9. Caption under the file: `download_card.build_file_caption(info,
   quality=…, size_bytes=…, duration=…)` → `*Title*`, `size · duration ·
   quality`, `via Koyura`. When the format was lowered to fit the send limit
   (§4.5) one more line says so: **"Selected 1440p: 2160p does not fit the send
   limit"**.
10. Delivery: the file is uploaded as its **own** message (`sendVideo` /
    `sendAudio` / `sendDocument`, one retry after 1 s on any exception) and the
    progress message is deleted **after** the upload succeeded. A failed upload
    keeps the progress message and turns it into the error card
    (**"Failed to upload file to Telegram."** + `download_error` event), and the
    local file is deleted. The file itself is never deleted.
11. `download_done` event with `file_size_bytes`, `duration_seconds`, `filename`;
    `_stats["downloads"] += 1`; the local file is deleted (best effort).
12. `entry["job_active"] = False` in a `finally` — the card is reusable after a
    failure, which is what makes "tap a format to try again" work.
10. Delivery: the file is uploaded as its **own** message (`sendVideo` /
    `sendAudio` / `sendDocument`, one retry after 1 s on any exception) and the
    progress message is deleted **after** the upload succeeded. A failed upload
    keeps the progress message and turns it into the error card
    (**"Failed to upload file to Telegram."** + `download_error` event), and the
    local file is deleted. The file itself is never deleted.

### 4.5 Direct commands (`/mp3`, `/mp4`, `/best`)

`cmd_mp3` / `cmd_mp4` (`:213`, `:229`):

* links come from the command text **or** from the message being replied to
  (`_extract_urls_from_command` `:201`, text or caption);
* the unsafe-link notice and the five-link cap apply exactly as above;
* no links → **`Usage: /mp3 <link>` + `Or reply to a message containing a link.`**
  (same wording with `/mp4`);
* otherwise the command replies **"Downloading MP3..."** or
  **"Downloading MP4 (best quality)..."** and `_direct_download` (`:575`) fetches
  info and runs the same stages with a **text** card (`photo=False`), no
  keyboard, and `started_at = now`;
* `get_info` failing → **"Download service temporarily unavailable."** or
  **"Error: <message>"** written onto that status message, `_stats["errors"] += 1`.

The requested format is resolved before the job starts
(`download_flow.plan_choice`):

* `/mp3` → refused on the status line when the MP3 estimate itself is over
  `MAX_SEND_MB` (the same line the hidden button would have explained);
* `/mp4` and `/best` → **the highest format that fits is pinned** by id instead
  of letting the engine pick its own best, because the engine's best may be a
  file Telegram refuses. When that is not the top format, the caption under the
  file says which one was skipped: **"Selected 1440p: 2160p does not fit the
  send limit"**;
* nothing fits at all → the job never starts and the status line becomes
  **"This video is too large to send (limit 2000 MB)."** (or "All available
  formats are over the send limit … try another link or MP3" when the audio
  still fits).

---

## 5. User-visible texts

The words themselves live in `bot/i18n.py`, in two catalogues (`en`, `ru`) with
the same keys and the same `{placeholders}` — see §10. `bot/handlers/texts.py`
only decides how the lines are arranged.

Two forms, and the rule for picking one:

* `texts.md(key, lang)` escapes for `parse_mode="MarkdownV2"` — every message the
  bot posts goes through it;
* `texts.t(key, lang)` does not — card captions (which escape their own inputs)
  and plain emitters (`query.answer` toasts) use it.

`progress_card`/`download_card` take a `lang=` keyword and default to English, so
the pure formatting tests keep working unchanged.

Pre-rewrite locations (the text is these days a catalogue entry):

| Constant (new) | Text | Old location |
| --- | --- | --- |
| `START` | `Koyura Bot — a quiet downloader.` + intro, platform summary, command list (see source) | `handlers.py:62-80` |
| `HELP` | MarkdownV2 how-to: numbered steps, Shortcuts, Preferences, Other, last line about several links | `handlers.py:82-107` |
| `PLATFORMS_HEADER` + `SUPPORTED_PLATFORMS` | `Supported platforms:` and 19 indented lines ending `and 1000+ more via yt-dlp` | `handlers.py:54-60`, `:109-115` |
| `STATS` | `Koyura Bot Stats:` / Uptime / Downloads / Errors / Cached files / Disk usage / Active sessions | `handlers.py:117-144` |
| `SETTINGS` | `Your preferences:` / Default quality / Default format / Change / setquality / setformat | `handlers.py:146-164` |
| `SETQUALITY_USAGE`, `SETQUALITY_INVALID`, `SETQUALITY_SET` | `Usage: /setquality <best/1080/720/480/360>`, `Invalid quality. Options: best, 1080, 720, 480, 360`, `Default quality set to: <q>` | `handlers.py:166-182` |
| `SETFORMAT_USAGE`, `SETFORMAT_INVALID`, `SETFORMAT_SET` | `Usage: /setformat <video/audio>`, `Invalid format. Options: video, audio`, `Default format set to: <f>` | `handlers.py:184-199` |
| `MP3_USAGE`, `MP4_USAGE` | `Usage: /mp3 <link>\nOr reply to a message containing a link.` (and `/mp4`) | `handlers.py:222`, `:238` |
| `MP3_STARTED`, `MP4_STARTED` | `Downloading MP3...`, `Downloading MP4 (best quality)...` | `handlers.py:225`, `:241` |
| `FETCHING_INFO` | `Fetching info...` | `handlers.py:420` |
| `SERVICE_DOWN` | `Download service temporarily unavailable.` | `handlers.py:428`, `:753`, `:1052` |
| `INFO_FAILED`, `ENGINE_ERROR` | `Failed to fetch info: {e}`, `Error: {e}` | `handlers.py:431`, `:434`, `:594` |
| `CARD_RENDER_FAILED` | `Could not render this link. Try /mp4 or /mp3 instead.` | `handlers.py:465` |
| `UNSAFE_LINK`, `TOO_MANY_LINKS` | see §4.1 | `handlers.py:270-281` |
| `SESSION_EXPIRED` | `Session expired. Please send the link again.` | `handlers.py:529` |
| `ALREADY_DOWNLOADING` | `Already downloading — wait for the file.` | `handlers.py:544` |
| `STARTING` | `Starting download…` (inside `build_status_caption`) | `handlers.py:732` |
| `UNEXPECTED_ANSWER` | `Download service returned an unexpected answer.` | `handlers.py:766` |
| `FILE_MISSING` | `File not found after download.` | `handlers.py:801` |
| `UPLOAD_FAILED` | `Failed to upload file to Telegram.` | `handlers.py:851` |
| `TIMED_OUT` | `Download timed out.` | `handlers.py:1087` |
| `UNKNOWN_ERROR` | `Unknown error` | `handlers.py:795` |
| (unchanged modules) | progress, queue, busy, error captions | `progress_card.py` |
| (new module) | the eight progress switches (`fields=` for the caption) | `progress_fields.py` |
| (unchanged module) | preview card, format labels, file caption | `download_card.py` |

---

## 6. Edge cases

| Situation | Behaviour |
| --- | --- |
| Text without a link | Silence. The bot never chats. |
| Link rejected by `url_safety` (private IP, `.local`, credentials, non-http, too long, unresolvable) | One notice per message; the safe links in the same message are still processed. No per-link detail is shown. |
| More than five links | Only the first five, plus the "first 5 links" notice. |
| Link to an unsupported site | The **engine** fails; the bot shows `Failed to fetch info: …`. |
| Video too long / too large | Engine refusal `code=too_long` → the generic "service is overloaded — try again in ~2 min" wording (deliberately uniform, see `test_every_refusal_code_shows_the_same_friendly_text`). |
| Engine queue full (429) | Same refusal card, buttons still usable. |
| User already has a job | `code=user_busy` → "You already have a download running. Try again in ~2 min." |
| Second tap on a running card | "Already downloading — wait for the file." alert, no second job, `answer()` called once. |
| Tap on a stale card (>10 min, or after a restart) | "Session expired. Please send the link again." |
| Tap on a legacy `fmt:`/`qty:` button whose session is still alive | `video`/`back` re-render the keyboard; anything else downloads (unknown ids are forwarded to the engine). |
| Thumbnail missing or unsendable | Text card, same caption/buttons. |
| Card deleted or made uneditable mid-job | `handlers.stages.StageTracker` sees the `gone` classification, posts the stage again and forgets the dead message; the download is never lost. |
| Engine unreachable during polling | "Download service temporarily unavailable." + `download_error`; card unlocked. |
| Engine returns a broken status | Non-dict `progress` is treated as empty; missing numbers are simply not drawn. |
| Every progress field switched off in the panel | The caption is the stage line ("Downloading…" / "Скачиваю…"), never blank. |
| Panel unreachable while a download runs | The built-in default (title + bar); the caption is never blocked on the dashboard. |
| Engine says done but the file is gone | "File not found after download." + `download_error`; card unlocked. |
| Telegram upload fails twice | "Failed to upload file to Telegram." + `download_error`, local file deleted. |
| Nothing happens for 15 minutes | "Download timed out." + `download_error`, card unlocked. |
| `/cancel` | Not implemented (help text only). |
| `/mp3` with no link | Usage line. |
| `/mp3` replying to a message with a link | Uses the replied message's text or caption. |

---

## 7. Contracts that must not change

* **Engine**: `POST /api/info {url}`, `POST /api/download {url, format,
  format_id?, title, uploader?, height?, user_id?, size_bytes?}`,
  `GET /api/status/<job_id>`, header `X-Internal-Token` (from
  `koyura_client`), 429 → `KoyuraBusy(code, retry_after)`.
* **Dashboard events** (`event_client`, fire-and-forget, 2 s timeout):
  `download_start` (job_id, user_id, username, chat_id, url, platform, format,
  quality, title), `progress` (job_id, percent, speed, eta, downloaded_bytes,
  total_bytes), `download_done` (job_id, file_size_bytes, duration_seconds,
  filename), `download_error` (job_id, error_message).
* **`callback_data`**: exactly the values above, so buttons in old chats work.
* **Command names and `setMyCommands` metadata**: unchanged (the descriptions now
  come from the catalogue's `menu.*` keys, still published in English).
* **Panel internal API**: `POST /api/events` is unchanged; this release adds
  `GET /api/bot-settings` (read-only, `X-Internal-Token`, body
  `{"languages": [...], "default_language": "en", "progress": {...}}`) for the
  bot to read the enabled languages and the progress switches. Adding the
  `progress` object changes no existing field.

---

## 8. Parity evidence

* `docs/bot-behavior.md` (this file) is the checklist; the rewrite is reviewed
  against it section by section.
* The pre-rewrite suite (`bot/tests`) is kept and re-pointed at the new modules:
  `test_download_flow.py`, `test_queue_states.py` still assert the same
  captions, events, filenames, queue texts, refusal texts, throttle behaviour
  and card fallbacks.
* New tests cover every command, every `callback_data` scheme and action, the
  repeat-tap guard, the stale-button path, queue refusal, engine failures, and
  the handler registration (command names, handler types, callback pattern).
* All four suites (`bot`, `dashboard`, `koyura`, `.github/tests`) are green.

---

## 9. Progress fields (what the live message shows)

The progress card used to print everything the engine knew: the title, the bar,
`62%`, `30.8 MB / 30.8 MB`, `58.9 MB/s`, `ETA 0:42` and `0:13 elapsed`. That is
too much for a chat message, so each line is now a switch the operator sets in
Admin → **Progress fields**. The default is the title and the bar and nothing
else.

### The eight switches

Stored in the dashboard's `settings` table, one row per key, values
`true`/`false`:

| Key | Draws | Default |
| --- | --- | --- |
| `bot_progress.show_title` | the video's title, bold, first line | **on** |
| `bot_progress.show_bar` | the 12-cell bar `▓▓▓▓▓▓▓░░░░░` | **on** |
| `bot_progress.show_percent` | `62%` | off |
| `bot_progress.show_size` | `30.8 MB / 30.8 MB` | off |
| `bot_progress.show_speed` | `58.9 MB/s` | off |
| `bot_progress.show_eta` | `ETA 0:42` / `осталось 0:42` | off |
| `bot_progress.show_elapsed` | `0:13 elapsed` / `0:13 прошло` | off |
| `bot_progress.show_quality` | `1440p` | off |

Rules the tests pin (`bot/tests/test_progress_fields.py`):

* the bar is **12 cells** whether or not the percentage is on, so the caption
  never re-wraps between edits;
* with the bar off and the percentage on, the number stands alone on the facts
  line instead of vanishing;
* with **everything** off the caption is the stage line ("Downloading…" /
  "Скачиваю…") — a progress message is never blank;
* an unknown size draws no `show_size`, `show_percent` or `show_eta` line at all
  (the same rule as before: never a made-up denominator);
* a stage with nothing to measure (starting / processing / uploading) keeps its
  stage line, and a facts line with no bar or percentage keeps it too;
* the titles are not translated — the panel is English-only, the card follows
  the chat language.

The rendered shapes, for the same 30.8 MB download:

```text
default (title + bar)          percent + speed + eta           everything on
*Artemis II to the Moon*       *Artemis II to the Moon*        *Artemis II to the Moon*
▓▓▓▓▓▓▓░░░░░                   ▓▓▓▓▓▓▓░░░░░ 62%                ▓▓▓▓▓▓▓░░░░░ 62%
                               3.2 MB/s · ETA 0:42             1080p · 19.1 MB / 30.8 MB ·
                                                              3.2 MB/s · ETA 0:42 · 0:13 elapsed
```

### Where the setting comes from

`GET /api/bot-settings` (the same route the languages use) grew one object:

```json
{"languages": ["en", "ru"], "default_language": "en",
 "progress": {"show_title": true, "show_bar": true, "show_percent": false,
              "show_size": false, "show_speed": false, "show_eta": false,
              "show_elapsed": false, "show_quality": false}}
```

`bot/bot_settings.py` reads it with the languages (one request, cached for
`TTL_SECONDS` = 45 s) and `bot/progress_fields.py` turns the object into a set of
switches. Panel down, 401, bad JSON, or no `progress` key at all (an older
panel) → the built-in default, title + bar. An unreadable value for one key is
that key's default, never an error. `handlers.download_flow.poll_until_finished`
reads the set **once** per job, before the poll loop, so a mid-download change
lands on the next download rather than switching mid-caption.

### The panel section

`dashboard/templates/_admin_progress.html` is a partial and `admin.html` holds
exactly one line — `{% include "_admin_progress.html" %}` — right after the
`Bot languages` card. That is deliberate: another branch is restructuring
`/admin`, and a separate file plus a single include keeps the two changes from
conflicting. The markup reuses the existing `card` / `limits-grid` /
`limits-cell` / `language-choice` classes, so it needs no new CSS. The form
(`POST /admin/progress`) carries the double-submit CSRF token, stores the eight
rows, and redirects to `/admin?progress_saved=1`; the bot picks the change up
within 45 s, no restart.

## 10. Languages (English / Russian)

### Catalogues

`bot/i18n.py` holds every word the bot can say:

```python
CATALOGS = {"en": EN, "ru": RU}      # same keys, same {placeholders}
t("intake.too_many", lang, limit=5)   # "Only the first 5 links..."
escape_md(text)                       # the bot's only MarkdownV2 escaper
```

Rules the tests enforce (`bot/tests/test_i18n.py`):

* every key exists in **both** catalogues, with a non-empty value;
* the `{placeholders}` of a key are identical across languages;
* catalogues hold **plain text** — escaping happens once, at the send site;
* the "searching" stage is `🔍` in both languages.

Two things are deliberately not translated: the `EVENT_*` error strings stored in
the dashboard (the panel is English-only) and the protocol values
(`best`/`720`/`video`/platform names).

### Which language a chat gets

`handlers/language.current(user_id)` is the only place that answers this:
a saved choice **if the operator still offers it**, otherwise the panel's
default. A language switched in `/settings` changes the very next message.

The choice is per user and survives a restart: `bot/state_store.py` keeps it in
`user_prefs.language` (plus `user_prefs.pending_format` for the MP3/MP4 mode),
added by an additive, replay-safe `ALTER TABLE … ADD COLUMN` on every start.

### Where the setting comes from

The panel owns it — Admin → **Bot languages** — stored as two rows in the same
`settings` table the download limits use:

```text
bot_settings.languages         "en,ru"        (comma separated, ordered)
bot_settings.default_language  "en"
```

The bot reads `GET /api/bot-settings` (`X-Internal-Token`) and caches the answer
for `bot_settings.TTL_SECONDS` (45 s). Panel down, 401, bad JSON, or an empty
language list → the built-in defaults (`en,ru`, default `en`). Validation in the
panel: at least one language stays enabled, and the default must be one of them.

### First contact

| Situation | What the user gets |
| --- | --- |
| >1 language enabled, no saved choice | One message: "Choose the language of this chat." with `[English] [Русский]` (`lang:en`, `lang:ru`). The tap **replaces** that screen with the welcome in the chosen language. |
| exactly one language enabled | The welcome immediately, in that language — no screen. |
| a choice is already saved | The welcome immediately, in that language. The picker is never shown twice. |

### The welcome screen

```text
Koyura Bot — a quiet downloader.

Send a video or audio link and the file arrives in this chat.

Supported platforms: YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ more.
```

Below it, an inline keyboard instead of the old command list, two per row:

```text
[ Help ]      [ Platforms ]
[ Settings ]  [ Stats ]
[ MP3 ]       [ MP4 ]
```

`welcome:help|platforms|settings|stats` replace the welcome with the same screen
`/help`, `/platforms`, `/settings`, `/stats` produce (post the new message, then
delete the old one — the same `StageTracker` rule as a download).

`welcome:mp3` / `welcome:mp4` **arm a mode** instead of downloading: the answer
says `Mode: MP3 — send the link`, and the next link the user sends downloads in
that format without the picker, then the mode clears. If the welcome message is
itself a reply to a link, the download starts immediately. `/settings` shows the
armed mode, and `/mp3`/`/mp4` clear it (the user just said what they want).

### Back to the welcome

Every sub-screen — `Help`, `Platforms`, `Settings`, `Stats` and the MP3/MP4
confirmation — carries one more button, `Back` / `Назад` (`nav:home`). It works
the same whether the screen was opened from the welcome keyboard or with the
plain command (`/help`, `/platforms`, `/settings`, `/stats`).

Back is the **one** transition that edits instead of replacing: the message that
carries the button becomes the welcome again (`editMessageText` with the welcome
text and the `Help / Platforms / Settings / Stats / MP3 / MP4` keyboard). No new
message is posted, so the chat cannot collect a second copy of the start screen,
and the user does not have to send `/start`. The exception is a message Telegram
refuses to edit at all (deleted, older than 48 h): `StageTracker.edit` classifies
it and only then falls back to a fresh message.

Back also clears an armed MP3/MP4 mode, so a user who tapped `MP3` by mistake
gets out by pressing Back rather than by sending a link they did not mean to
convert. The settings screen keeps its Back after a language switch, and the
label follows the language (`Back` / `Назад`).

### `callback_data`

| Scheme | Origin | Shape |
| --- | --- | --- |
| `lang:<code>` | first-contact picker | `lang:en`, `lang:ru` |
| `lang:<code>:s` | the `/settings` language buttons | `lang:ru:s` (stay on settings) |
| `welcome:<action>` | the welcome keyboard | `welcome:help` … `welcome:mp4` |
| `nav:<action>` | the Back button of every sub-screen | `nav:home` |

The four patterns are separate handlers, so the card router's contract
(`^(dl|fmt|qty):` and its four-field payload) is unchanged.
