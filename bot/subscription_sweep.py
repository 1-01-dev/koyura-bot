"""The nightly re-check of who is still subscribed.

The gate is passed once, with a ``getChatMember`` call per channel, and the
answer is trusted for a day (``state_store.SUBSCRIPTION_PASS_TTL_SECONDS``).
That trust is what makes the daily sweep necessary rather than optional: a user
who subscribed a month ago and left last week still looks subscribed to the bot
until something asks Telegram again.

This loop is that something. Once a day it takes the users the panel reports as
active (its ``GET /api/subscriptions/active-users`` list — ids only), asks
``getChatMember`` for each of them on each enabled channel, and reports a
``left`` for the channels they are no longer in. The panel records a
transition only when it is a change, so re-running the sweep cannot inflate the
unsubscribe count.

Three properties keep the sweep cheap enough to run on a small box:

* **only active users** — a user who never passed the gate is not on the list,
  so the sweep does not grow with the number of people who merely opened the
  bot;
* **sequentially, with a small pause** — Telegram rate-limits ``getChatMember``
  per bot, and a burst of thousands would be throttled into a pile of failed
  checks that read as "left";
* **a bounded batch** — ``SUBSCRIPTION_SWEEP_LIMIT`` caps one run, so a
  deployment with many users spreads the work over nights instead of hammering
  the API once.

A check that *fails* (a network error, a ``RetryAfter``) changes nothing: only a
verdict Telegram actually returned can record a ``left``.
"""
from __future__ import annotations

import asyncio
import logging
import os

import event_client
import state_store
import subscriptions

logger = logging.getLogger(__name__)

#: How often the sweep runs. A day, because that is how long one check is
#: trusted; a shorter interval would only repeat work.
SWEEP_INTERVAL_SECONDS = int(
    os.environ.get("SUBSCRIPTION_SWEEP_INTERVAL_SECONDS", str(24 * 60 * 60))
)

#: How many users one run may check. Keeps a big deployment inside Telegram's
#: patience and a small one on the same code path.
SWEEP_LIMIT = int(os.environ.get("SUBSCRIPTION_SWEEP_LIMIT", "200"))

#: The pause between two users. ``getChatMember`` is a cheap call but it is
#: still ~30 requests/second at best; 50 ms keeps a run polite.
SWEEP_DELAY_SECONDS = float(os.environ.get("SUBSCRIPTION_SWEEP_DELAY_SECONDS", "0.05"))

#: How long to wait before the first run. A bot that just started has more
#: urgent things to do (and the panel may not be up yet).
SWEEP_START_DELAY_SECONDS = float(
    os.environ.get("SUBSCRIPTION_SWEEP_START_DELAY_SECONDS", "300")
)


async def subscription_sweep_loop(bot) -> None:
    """Re-check every active user once a day, forever.

    ``bot`` is the running ``telegram.Bot`` — the sweep needs ``get_chat_member``
    and nothing else, so it takes the object rather than the application.
    """
    logger.info(
        "Subscription sweep started: interval=%ds limit=%d",
        SWEEP_INTERVAL_SECONDS,
        SWEEP_LIMIT,
    )
    await asyncio.sleep(SWEEP_START_DELAY_SECONDS)
    while True:
        try:
            await sweep_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Subscription sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


async def sweep_once(bot) -> dict[str, int]:
    """One pass over the active users; returns the counters it logged.

    Answers ``{"checked": n, "left": n, "failed": n}`` so a test (and an operator
    reading the log) can see what a run did without parsing sentences.
    """
    gate = await subscriptions.get()
    if not gate.active():
        # Nothing to check: the switch is off or no channel is verifiable. The
        # sweep stays quiet rather than asking the panel every day for nothing.
        return {"checked": 0, "left": 0, "failed": 0}

    user_ids = await _active_users()
    if not user_ids:
        return {"checked": 0, "left": 0, "failed": 0}

    checked = 0
    left_total = 0
    failed = 0
    for user_id in user_ids[:SWEEP_LIMIT]:
        checked += 1
        gone: list[int] = []
        for channel in gate.channels:
            verdict = await _status(bot, channel, user_id)
            if verdict is None:
                failed += 1
                continue
            if not subscriptions.is_subscribed(verdict):
                gone.append(int(channel.id))
        if gone:
            written = 0
            for channel_id in gone:
                await event_client.send_subscription_event(
                    user_id=user_id, channel_id=channel_id, state="left"
                )
                written += 1
            if written:
                # The cached "this user passed the gate" must not outlive the
                # verdict that says otherwise.
                state_store.set_subscription_passed(user_id, False)
                left_total += written
        await asyncio.sleep(SWEEP_DELAY_SECONDS)

    if checked:
        logger.info(
            "Subscription sweep: checked %d user(s), %d left, %d check(s) failed",
            checked,
            left_total,
            failed,
        )
    return {"checked": checked, "left": left_total, "failed": failed}


async def _active_users() -> list[int]:
    """The ids the panel considers subscribed on every enabled channel."""
    return await event_client.fetch_active_subscribers()


async def _status(bot, channel: subscriptions.Channel, user_id: int):
    """``get_chat_member`` for one pair, or ``None`` when Telegram did not answer."""
    target = channel.handle or (
        f"@{channel.username}" if channel.username else channel.link
    )
    if not target:
        return None
    try:
        member = await bot.get_chat_member(chat_id=target, user_id=int(user_id))
    except Exception as exc:  # noqa: BLE001 - a failed check changes nothing
        logger.debug("Sweep check failed for user %s on %s: %s", user_id, channel.id, exc)
        return None
    return getattr(member, "status", None)


__all__ = [
    "SWEEP_DELAY_SECONDS",
    "SWEEP_INTERVAL_SECONDS",
    "SWEEP_LIMIT",
    "SWEEP_START_DELAY_SECONDS",
    "subscription_sweep_loop",
    "sweep_once",
]
