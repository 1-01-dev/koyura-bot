# Message styles

Four complete voices for the bot, shipped as YAML next to it. They are the
one-click answer to "make the bot sound different" without editing 56 messages
by hand: applying a style writes every kind in both languages at once.

The generator in `docs/message-templates.md` still describes the *default* texts
and the editor; this page describes the presets on top of them.

## The four styles

| File | Button | Voice |
| --- | --- | --- |
| `01-quiet-premium.yaml` | Quiet premium | Calm and restrained: short lines, no exclamation marks. |
| `02-terminal.yaml` | Terminal | Mono, imperative, machine-like: a status line per event. |
| `03-media-studio.yaml` | Media studio | Editorial: the asset, its source and its specs, then the action. |
| `04-friendly.yaml` | Friendly | Warm and conversational, with the user addressed as a person. |

They live in `bot/template_styles/` and are read from there by the panel. The
compose file mounts that directory into the dashboard read-only
(`./bot/template_styles:/app/template_styles:ro`); a native install finds it at
`../bot/template_styles` from the dashboard's working directory.
`TEMPLATE_STYLES_PATH` overrides both when a deployment puts it elsewhere.

Each file carries the same **56 kinds** in `en` and `ru`. The catalogue has 64,
so a style leaves the eight newer `track.*` messages on their built-in text —
applying a style never blanks a message it does not describe.

## How the active style is stored

One row in the dashboard's `settings` table:

```
templates.style = 01-quiet-premium | 02-terminal | 03-media-studio | 04-friendly
                | default | custom
```

It records *which* style the operator picked; the texts themselves are ordinary
rows in `message_templates`, one active per `(kind, language)` — exactly what a
body typed by hand becomes. That is deliberate: the bot reads one shape
(`templates` in `GET /api/bot-settings`) and does not need to know that a style
exists at all.

`custom` is not a fifth file. It means the set no longer matches the preset it
came from — the operator saved, activated, duplicated, deleted or reset a single
body after applying a style. Every one-pair action sets it.

## Applying a style

`Admin → Bot settings → Message templates` shows the row of buttons above the
editor, the active one lit.

`POST /admin/templates/apply-style` (session + CSRF, like every other action):

1. reads and validates the file: known placeholders only, no empty body, under
   Telegram's limit for the kind (4096 for a text, 1024 for a caption);
2. writes the accepted bodies for every kind and both languages in **one
   transaction**, replacing each pair's previous row;
3. stores `templates.style`;
4. redirects back with a notice.

A body that fails validation is **skipped and named**: the notice says
`Applied 54 of 64 kinds from Friendly. Skipped 2: welcome (en: unknown
placeholder @nobody); …`. A skipped kind keeps whatever it had. Nothing is ever
dropped silently.

Applying a file that is missing, unreadable or unknown is a rendered error (400)
and writes nothing.

## What Default does

`Default` is the same endpoint with `style=default`. It **deletes every stored
body** so the bot renders its built-in text from `message_kinds` again, and sets
`templates.style=default`. It deletes rather than copying the built-in text into
rows: a stored copy would freeze today's wording, and an upgraded panel would
keep sending yesterday's sentences while the chat forgot what "default" means.

## The bot needs no change

It already polls `GET /api/bot-settings` (45 s cache) and renders
`templates[kind][lang]` when a body is there. Applying a style fills exactly
that object, so the next poll carries the new voice. Nothing in `bot/` reads
`templates.style`; the setting is the panel's memory of what it applied.

## Adding a style

Drop a new `NN-name.yaml` in `bot/template_styles/`, add a `StylePreset` to
`dashboard/message_styles.py`, and add both languages for every kind the
catalogue has. `dashboard/tests/test_message_styles.py` checks all four shipped
files for completeness, unknown placeholders and the limits on every run.
