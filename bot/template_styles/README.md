# Preset message styles

Four complete voices for the bot, one file each. The panel lists them as a row
of buttons in **Admin → Bot settings → Message templates**; applying one writes
its bodies for every message kind in both languages in a single transaction.

| File | Button | Voice |
| --- | --- | --- |
| `01-quiet-premium.yaml` | Quiet premium | Calm and restrained: short lines, no exclamation marks. |
| `02-terminal.yaml` | Terminal | Mono, imperative, machine-like: a status line per event. |
| `03-media-studio.yaml` | Media studio | Editorial: the asset, its source and its specs, then the action. |
| `04-friendly.yaml` | Friendly | Warm and conversational, with the user addressed as a person. |

## Where they came from

The owner wrote them as a product decision and handed them over as YAML. They are
committed here so a style goes through review like code and the owner can diff two
voices; the panel only reads them, it never writes them back.

## What a file holds

Category → message kind → the text, exactly the shape
`dashboard/message_catalog.py` describes:

```yaml
_about: 'Koyura message templates - Quiet Premium. ...'   # a note for humans
onboarding:
  welcome:
    media: text
    placeholders: [languages, platforms, version]
    en: 'Koyura Bot — a quiet downloader. ...'
    ru: 'Koyura Bot — тихий загрузчик. ...'
```

`_about` and the per-kind `builtin_en` / `builtin_ru` / `customised` fields
document the file for a human and are **not** imported. Only `en` and `ru` are
read; `media` and `placeholders` are documentation too — the catalogue, not the
file, decides the media type and which `@placeholders` a kind offers.

## The rules the panel enforces

Each body is validated before it is written, with the same rules as a body typed
by hand. A body that breaks one is **skipped and named** in the panel's notice
("applied 54 of 56, skipped: …"), never silently dropped:

* only placeholders the kind actually offers (`@title`, `@url`, …);
* no more than Telegram's limit for the kind — 4096 characters for a text
  message, 1024 for a media caption;
* no empty body, and both `en` and `ru` present.

A kind the catalogue does not know is ignored (the bot has no such message) and
reported separately.

## Editing one

Change the `en`/`ru` text and nothing else, then apply the style in the panel.
Never hand-edit a stored template in SQLite to match: apply the style. If you add
a kind to the catalogue, add its two bodies here too, or the style simply leaves
that one message on the default.

`.yamllint` in this directory relaxes the two rules that fight a long
`\n`-separated body (line length and list indentation). Everything else stays
strict.
