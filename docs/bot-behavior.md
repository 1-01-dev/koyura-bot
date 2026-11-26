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
| `/start` | Start Koyura | `cmd_start` (`:62`) | Static welcome text, plain (no `parse_mode`). |
| `/help` | Help and commands | `cmd_help` (`:82`) | Static how-to, sent as **MarkdownV2** (the source is pre-escaped). |
| `/mp3` | Download as MP3 | `cmd_mp3` (`:213`) | Extracts links from the command or the replied-to message, validates them, starts one direct audio job per link (no picker). |
| `/mp4` | Download best quality MP4 | `cmd_mp4` (`:229`) | Same, video, no format id → engine's best. |
| `/best` | Best available quality | `cmd_best` (`:245`) | Alias: calls `cmd_mp4` verbatim. |
| `/platforms` | Supported platforms | `cmd_platforms` (`:109`) | Static list, one indented line per platform. |
| `/settings` | Your preferences | `cmd_settings` (`:146`) | Reads `state_store.get_prefs(user.id)` and prints quality + format. |
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
   quality`, `via Koyura`.
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

---

## 5. User-visible texts

All of them live in `bot/handlers/texts.py` after the rewrite. Pre-rewrite
locations:

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
* **Command names and `setMyCommands` metadata**: unchanged.

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
