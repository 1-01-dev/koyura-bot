"""The mandatory-subscription gate: the screen, the buttons and the check.

A user who has not subscribed to every enabled channel sees one screen instead
of the bot doing anything else:

    One more step:
    To use the bot, subscribe to these channels:
    • Koyura · news
    • Koyura · chat
    Tap a channel to open it, then tap "Check subscription".
    [Open channel] [Open channel] [Check subscription]

Four decisions live here, and nowhere else:

* **whether there is a gate at all** — :func:`active` asks ``subscriptions``,
  which answers "off when the master switch is off" and "off when no channel is
  enabled". Callers never repeat that rule;
* **what a passed gate means** — :func:`passed` is the one place that turns
  ``getChatMember`` answers into "let the user in", so the download path and the
  gate screen cannot disagree;
* **what happens when Telegram cannot be asked** — the check reports a *failure*
  and the user is told "try again later"; a network problem must never read as
  "you are not subscribed" and must never let the user through either;
* **what is recorded** — every verdict is posted to the panel
  (``POST /api/subscriptions/events``), which writes it only when it is a
  change, so the unsubscribe statistic counts transitions and not observations.

The screen is a *stage* like every other: the tap replaces the message it sits
on, so the chat does not collect a copy of the gate per attempt.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

import event_client
import i18n
import state_store
import subscriptions

from . import keyboards, language, stages, texts

logger = logging.getLogger(__name__)

PARSE_MODE = "MarkdownV2"

#: The prefix of the gate's own buttons. ``sub:check`` is the check,
#: ``sub:open:<channel id>`` is a channel link rendered as a callback (the link
#: itself is in the button URL, so a tap never needs a round trip).
CHECK_PREFIX = "sub"


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

async def current() -> subscriptions.Gate:
    """The gate as it stands right now (cached by ``subscriptions``)."""
    return await subscriptions.get()


async def active() -> subscriptions.Gate | None:
    """The gate when it is in force, else ``None``.

    The single question every caller asks: "must this user subscribe before I do
    anything?". It answers ``None`` when the switch is off **or** when no channel
    could actually be verified — a bot that blocked everyone behind a list it
    cannot check would be unusable.
    """
    gate = await current()
    return gate if gate.active() else None


def missing_names(channels, lang: object = None) -> str:
    """``@one, @two`` — the channels the user is still not in."""
    return ", ".join(channel.label() for channel in channels)


def gate_text(gate: subscriptions.Gate, lang: object = None) -> str:
    """The gate screen: the heading, the list and the instruction."""
    rows = "\n".join(f"  • {channel.label()}" for channel in gate.channels)
    default = i18n.escape_md(
        "\n\n".join(
            (
                f"*{i18n.t('subscription.title', lang)}*",
                i18n.t("subscription.body", lang, channels=rows),
                i18n.t("subscription.hint", lang),
            )
        )
    )
    # The channel list is built here, not in the catalogue, so the template's
    # ``@channels`` value is the already-assembled list.
    return texts.render_template(
        "subscription.gate", lang, {"channels": rows}, default
    )


def missing_text(channels, lang: object = None) -> str:
    """The polite refusal: still not subscribed, and to what."""
    names = missing_names(channels, lang)
    default = i18n.escape_md(
        "\n\n".join(
            (
                f"*{i18n.t('subscription.title', lang)}*",
                i18n.t("subscription.missing", lang, missing=names),
                i18n.t("subscription.hint", lang),
            )
        )
    )
    return texts.render_template(
        "subscription.missing", lang, {"missing": names}, default
    )


def error_text(lang: object = None) -> str:
    """The "could not check" sentence (a custom body wins when it renders)."""
    return texts.md("subscription.error", lang)


# ---------------------------------------------------------------------------
# The Telegram check
# ---------------------------------------------------------------------------

async def passed(bot, user_id: int, gate: subscriptions.Gate) -> tuple[bool, bool]:
    """``(subscribed, failed)`` for one user against every channel of the gate.

    ``subscribed`` is ``True`` only when **every** channel reports a subscribed
    status. ``failed`` is ``True`` when Telegram could not answer for at least
    one channel — the caller then says "try again later" instead of "you are not
    subscribed", because a network error is not a refusal.

    Each verdict is reported to the panel, so the history exists whether the user
    is let in or not. The report is fire-and-forget: the panel never blocks a
    reply.
    """
    if not gate.channels:
        return True, False

    subscribed = True
    failed = False
    missing: list[subscriptions.Channel] = []
    for channel in gate.channels:
        verdict = await _member_status(bot, channel, user_id)
        if verdict is None:
            failed = True
            subscribed = False
            missing.append(channel)
            continue
        joined = subscriptions.is_subscribed(verdict)
        await _report(user_id, channel, joined)
        if not joined:
            subscribed = False
            missing.append(channel)
    return subscribed, failed


async def _member_status(bot, channel: subscriptions.Channel, user_id: int):
    """``get_chat_member`` for one channel, or ``None`` when it did not answer."""
    target = channel.handle or f"@{channel.username}"
    try:
        member = await bot.get_chat_member(chat_id=target, user_id=int(user_id))
    except Exception as exc:  # noqa: BLE001 - any Telegram failure is "ask later"
        logger.debug("getChatMember for %s failed: %s", channel.id, exc)
        return None
    return getattr(member, "status", None)


async def _report(user_id: int, channel: subscriptions.Channel, joined: bool) -> None:
    """Tell the panel one verdict (the panel decides whether it is a change)."""
    await event_client.send_subscription_event(
        user_id=int(user_id),
        channel_id=int(channel.id),
        state="subscribed" if joined else "left",
    )


# ---------------------------------------------------------------------------
# The screen and its buttons
# ---------------------------------------------------------------------------

async def show(tracker, gate: subscriptions.Gate, lang: object = None) -> None:
    """Render the gate into the stage the tap sits on."""
    await tracker.replace_text(
        gate_text(gate, lang), reply_markup=keyboards.subscription(gate, lang)
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def on_subscription_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route ``sub:check`` and ``sub:open:<id>`` taps.

    ``sub:open:*`` needs no work at all: its URL is on the button, and Telegram
    opens it without telling the bot. The handler still answers the query so the
    button does not spin, which is why the tap still arrives here.
    """
    query = update.callback_query
    if query is None:
        return
    action = keyboards.parse_subscription(query.data)
    if action is None:
        await query.answer()
        return

    user = update.effective_user
    if user is None:
        await query.answer()
        return

    lang = await language.current(user.id)
    gate = await active()
    if gate is None:
        # The owner turned the gate off while the message was on screen (or
        # deleted the last channel): there is nothing to check, so the tap
        # simply rewrites the screen into the welcome one.
        await query.answer()
        tracker = _tracker(update, context, query)
        if tracker is not None:
            await tracker.edit(
                texts.welcome_text(lang), reply_markup=keyboards.welcome(lang)
            )
        return

    if action != "check":
        # A channel button: the URL is on the button, nothing to do.
        await query.answer()
        return

    subscribed, failed = await passed(context.bot, user.id, gate)
    if subscribed:
        await query.answer()
        # Remember the pass, so the very next link is not checked again: the
        # daily sweep is what re-verifies it.
        state_store.set_subscription_passed(user.id, True)
        tracker = _tracker(update, context, query)
        if tracker is not None:
            await tracker.replace_text(
                texts.welcome_text(lang), reply_markup=keyboards.welcome(lang)
            )
        return

    if failed:
        await query.answer(texts.plain("subscription.error", lang), show_alert=True)
        # The screen stays, with the same buttons: the user retries it.
        return

    # Still not subscribed: say which channels, keep the same screen.
    missing = [
        channel
        for channel in gate.channels
        if not await _subscribed_now(context.bot, channel, user.id)
    ]
    spoken = missing or list(gate.channels)
    await query.answer(
        texts.plain(
            "subscription.missing", lang, missing=missing_names(spoken, lang)
        ),
        show_alert=True,
    )
    tracker = _tracker(update, context, query)
    if tracker is not None:
        await tracker.edit(
            missing_text(spoken, lang),
            reply_markup=keyboards.subscription(gate, lang, missing_only=spoken),
        )


async def _subscribed_now(bot, channel: subscriptions.Channel, user_id: int) -> bool:
    """A second, quiet look at one channel (the screen names what is missing)."""
    verdict = await _member_status(bot, channel, user_id)
    return bool(verdict is not None and subscriptions.is_subscribed(verdict))


def _tracker(update: Update, context: ContextTypes.DEFAULT_TYPE, query):
    """The stage tracker for the message the tapped button sits on."""
    message = getattr(query, "message", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        chat = update.effective_chat
        chat_id = getattr(chat, "id", None)
    if chat_id is None:
        logger.debug("Cannot replace the gate: the tapped message has no chat")
        return None
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    return stages.StageTracker(
        context.bot,
        chat_id,
        getattr(message, "message_id", None),
        photo=bool(getattr(message, "photo", None)),
    )


async def remind(message, user_id: int, lang: object = None) -> bool:
    """Answer an ordinary action with the gate instead of doing it.

    Returns ``True`` when the gate stopped the action (so the caller returns),
    ``False`` when there is no gate or the user has passed it. This is the one
    function the download path calls: it is deliberately not "check the gate"
    plus "remember the answer", because a subscription can lapse at any time and
    the check is what Telegram is authoritative about.
    """
    gate = await active()
    if gate is None:
        return False
    if state_store.get_subscription_passed(user_id):
        # A user who passed earlier today is not re-asked for every link; the
        # daily check in ``cleanup`` re-verifies them in the background.
        return False
    subscribed, _failed = await passed(message.get_bot(), user_id, gate)
    if subscribed:
        state_store.set_subscription_passed(user_id, True)
        return False
    await message.reply_text(
        gate_text(gate, lang),
        parse_mode=PARSE_MODE,
        reply_markup=keyboards.subscription(gate, lang),
    )
    return True


__all__ = [
    "CHECK_PREFIX",
    "active",
    "current",
    "error_text",
    "gate_text",
    "missing_names",
    "missing_text",
    "on_subscription_button",
    "passed",
    "remind",
    "show",
]
