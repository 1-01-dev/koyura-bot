"""Emulate three progress frames under three panel configurations.

The owner's complaint was that the live message carries too much: the title, a
bar, ``100%``, ``30.8 MB / 30.8 MB``, ``58.9 MB/s`` and ``0:13 elapsed`` in one
caption. This script prints what the same download *actually* looks like at the
start, in the middle and at the end, with the switches the admin panel now
controls:

* **default** — title + bar (what a deployment shows before anybody saves);
* **everything** — every switch on (the pre-panel caption, for comparison);
* **minimal** — every switch off (the stage line, so the message is never blank).

It reads no network and no Telegram: the three payloads are shapes the engine
reports, and the captions come from ``progress_card`` exactly as the bot sends
them.

    cd bot && python -m tests.emulate_progress_frames

The output is the table quoted in the pull request.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import progress_card as pc  # noqa: E402
import progress_fields  # noqa: E402

INFO = {"title": "Artemis II to the Moon", "duration": 635, "extractor": "youtube"}

# Three moments of one 30.8 MB download: just started, halfway, nearly merged.
FRAMES: tuple[tuple[str, dict, float], ...] = (
    ("start", {
        "percent": 0.2,
        "downloaded_bytes": 61_440,
        "total_bytes": 32_283_750,
        "speed": 2_097_152,
        "eta": 15,
    }, 2.0),
    ("middle", {
        "percent": 62.0,
        "downloaded_bytes": 20_000_000,
        "total_bytes": 32_283_750,
        "speed": 3_355_443.2,
        "eta": 42,
    }, 13.0),
    ("end", {
        "percent": 100.0,
        "downloaded_bytes": 32_283_750,
        "total_bytes": 32_283_750,
        "speed": 11_534_336,
        "eta": 0,
    }, 61.0),
)

CONFIGURATIONS: tuple[tuple[str, progress_fields.Fields], ...] = (
    ("default (title + bar)", progress_fields.defaults()),
    ("percent + speed + eta", progress_fields.Fields(values={
        **progress_fields.DEFAULTS, "show_percent": True,
        "show_speed": True, "show_eta": True,
    })),
    ("everything on", progress_fields.Fields(
        values=dict.fromkeys(progress_fields.FIELD_NAMES, True))),
)


def _plain(text: str) -> str:
    """Drop MarkdownV2 backslashes: what the user reads, not what is sent."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def main() -> None:
    for name, fields in CONFIGURATIONS:
        print(f"\n=== {name} — {fields.describe()} ===")
        for frame, progress, elapsed in FRAMES:
            caption = pc.build_progress_caption(
                INFO, progress, quality="1080p", elapsed=elapsed,
                lang="en", fields=fields,
            )
            print(f"-- {frame} ({progress['percent']}%, {elapsed:.0f}s) --")
            for line in _plain(caption).splitlines():
                print(f"   {line}")


if __name__ == "__main__":
    main()
