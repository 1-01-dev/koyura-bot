"""Every word the bot says, in every language it speaks.

The bot has two audiences — an English one and a Russian one — and the text is a
product decision, so it lives in **catalogues**, not in the modules that send it:

    t("welcome.body", lang)                a finished sentence
    t("intake.too_many", lang, limit=5)    a sentence with a hole in it

Three rules keep the translation honest:

* **a key exists in every language, or the tests fail.** ``en`` is the source of
  truth (it is the language the owner writes in); ``ru`` must carry the same
  keys and the same ``{placeholders}``.
* **catalogues hold plain text, never Telegram markup.** Escaping happens once,
  in :func:`escape_md`, at the moment a string is sent with
  ``parse_mode="MarkdownV2"``. Substitution runs first, so a title containing
  ``_`` or ``.`` is escaped exactly like the prose around it — there is no
  second, subtly different escaper anywhere in the bot.
* **the language is chosen per user, never per process.** A rejection sent to
  one chat must not depend on who else is talking to the bot.

The set of enabled languages and the default come from the dashboard
(``GET /api/bot-settings``); this module only knows what a catalogue is. See
``bot_settings`` for the fetch and ``handlers.language`` for the choice.
"""
from __future__ import annotations

from string import Formatter

# The languages this module can render. Order is the order the picker shows.
LANGUAGES: tuple[str, ...] = ("en", "ru")

# Used when nothing else is known: a user with no saved choice on a bot whose
# panel cannot be reached still gets an answer.
DEFAULT_LANGUAGE = "en"

# ---------------------------------------------------------------------------
# Catalogues
# ---------------------------------------------------------------------------


EN: dict[str, str] = {
    # -- the language picker and its buttons ------------------------------
    "language.prompt": "Choose the language of this chat.",
    "language.en": "English",
    "language.ru": "Русский",
    "language.unknown": "That language is not enabled on this bot.",
    "language.saved": "Language: {language}",
    # -- the welcome screen (owner's wording, verbatim) --------------------
    "welcome.title": "Koyura Bot — a quiet downloader.",
    "welcome.body": "Send a video or audio link and the file arrives in this chat.",
    "welcome.platforms": (
        "Supported platforms: YouTube, TikTok, Instagram, Twitter, Reddit, "
        "and 1000+ more."
    ),
    # -- welcome keyboard --------------------------------------------------
    "button.help": "Help",
    "button.platforms": "Platforms",
    "button.settings": "Settings",
    "button.stats": "Stats",
    "button.mp3": "MP3",
    "button.mp4": "MP4",
    # -- /help -------------------------------------------------------------
    "help.title": "How to use Koyura Bot:",
    "help.step1": "1. Send a link (YouTube, TikTok, etc.)",
    "help.step2": "2. Pick a format (MP4 or MP3)",
    "help.step3": "3. Pick quality",
    "help.step4": "4. The file arrives",
    "help.shortcuts": "Shortcuts:",
    "help.mp3": "/mp3 <link> - Direct MP3 download",
    "help.mp4": "/mp4 <link> - Best quality MP4",
    "help.best": "/best <link> - Best available quality",
    "help.preferences": "Preferences:",
    "help.setquality": "/setquality <best/720/480> - Default quality",
    "help.setformat": "/setformat <video/audio> - Default format",
    "help.settings": "/settings - View your preferences",
    "help.other": "Other:",
    "help.platforms": "/platforms - Supported sites",
    "help.stats": "/stats - Bot stats",
    "help.cancel": "/cancel - Cancel (coming soon)",
    "help.footer": "Send several links at once and each one is handled in turn.",
    # -- /platforms --------------------------------------------------------
    "platforms.header": "Supported platforms:",
    "platforms.more": "and 1000+ more via yt-dlp",
    # -- /stats ------------------------------------------------------------
    "stats.title": "Koyura Bot Stats:",
    "stats.uptime": "Uptime",
    "stats.uptime_value": "{hours}h {minutes}m {seconds}s",
    "stats.downloads": "Downloads",
    "stats.errors": "Errors",
    "stats.cached": "Cached files",
    "stats.disk": "Disk usage",
    "stats.sessions": "Active sessions",
    "stats.line": "  {label}: {value}",
    # -- /settings ---------------------------------------------------------
    "settings.title": "Your preferences:",
    "settings.quality": "Default quality",
    "settings.format": "Default format",
    "settings.language": "Language",
    "settings.mode": "Next link",
    "settings.mode_none": "not set",
    "settings.change": "Change:",
    "settings.setquality": "/setquality <best/720/480>",
    "settings.setformat": "/setformat <video/audio>",
    "settings.language_hint": "Tap a language to switch.",
    "format.video": "video",
    "format.audio": "audio",
    # -- /setquality, /setformat -------------------------------------------
    "quality.usage": "Usage: /setquality <best/1080/720/480/360>",
    "quality.invalid": "Invalid quality. Options: {options}",
    "quality.saved": "Default quality set to: {quality}",
    "format.usage": "Usage: /setformat <video/audio>",
    "format.invalid": "Invalid format. Options: {options}",
    "format.saved": "Default format set to: {format}",
    # -- /mp3, /mp4, /best -------------------------------------------------
    "mp3.usage": "Usage: /mp3 <link>\nOr reply to a message containing a link.",
    "mp4.usage": "Usage: /mp4 <link>\nOr reply to a message containing a link.",
    "mp3.started": "Downloading MP3...",
    "mp4.started": "Downloading MP4 (best quality)...",
    # -- the MP3/MP4 mode armed from the welcome keyboard ------------------
    "mode.armed": "Mode: {mode}\nSend the link — it downloads as {mode}.",
    "mode.mp3": "MP3",
    "mode.mp4": "MP4",
    "mode.cleared": "The mode is off. Next link: not set.",
    # -- intake and the preview card -----------------------------------------
    "stage.searching": "🔍",
    "intake.unsafe": (
        "I can only download from public http(s) links. "
        "That link was skipped because it is not allowed."
    ),
    "intake.too_many": "Only the first {limit} links of a message are processed.",
    "card.render_failed": "Could not render this link. Try /mp4 or /mp3 instead.",
    "card.untitled": "Untitled",
    "card.download": "Download",
    "card.duration": "Duration",
    "card.followers": "Followers",
    "card.category": "Category",
    "card.uploaded": "Uploaded",
    "card.views": "views",
    "card.likes": "likes",
    "card.comments": "comments",
    "card.label_value": "{label}: {value}",
    "card.via": "via Koyura",
    "card.best_quality": "Best quality",
    # -- engine answers ----------------------------------------------------
    "engine.info_failed": "Failed to fetch info: {error}",
    "engine.error": "Error: {error}",
    "engine.down": "Download service temporarily unavailable.",
    # -- download stages ---------------------------------------------------
    "stage.starting": "Starting…",
    "stage.downloading": "Downloading…",
    "stage.processing": "Processing…",
    "stage.uploading": "Uploading…",
    "download.session_expired": "Session expired. Please send the link again.",
    "download.already": "Already downloading — wait for the file.",
    "download.unexpected": "Download service returned an unexpected answer.",
    "download.unknown_error": "Unknown error",
    "download.file_missing": "File not found after download.",
    "download.upload_failed": "Failed to upload file to Telegram.",
    "download.timed_out": "Download timed out.",
    "download.failed": "Download failed",
    "download.retry_hint": "Tap a format to try again.",
    "download.busy_user": "You already have a download running. Try again in {when}.",
    "download.busy_service": "The service is overloaded right now — try again in {when}.",
    "download.wait_default": "~2 min",
    # -- queue -------------------------------------------------------------
    "queue.hint": "You can leave this chat — the file arrives here.",
    "queue.head": "In queue",
    "queue.head_number": "In queue №{position}",
    "queue.estimated": "Estimated wait",
    "queue.wait_seconds": "~{seconds} s",
    "queue.wait_minutes": "~{minutes} min",
    # -- progress details --------------------------------------------------
    "progress.eta": "ETA {eta}",
    "progress.elapsed": "{elapsed} elapsed",
    # -- Telegram's own command menu (one language for the whole bot) -------
    "menu.start": "Start Koyura",
    "menu.help": "Help and commands",
    "menu.mp3": "Download as MP3",
    "menu.mp4": "Download best quality MP4",
    "menu.best": "Best available quality",
    "menu.platforms": "Supported platforms",
    "menu.settings": "Your preferences",
    "menu.setquality": "Set default quality",
    "menu.setformat": "Set default format",
    "menu.stats": "Koyura statistics",
}


RU: dict[str, str] = {
    # -- the language picker and its buttons ------------------------------
    "language.prompt": "Выберите язык этого чата.",
    "language.en": "English",
    "language.ru": "Русский",
    "language.unknown": "Этот язык не включён на этом боте.",
    "language.saved": "Язык: {language}",
    # -- the welcome screen ------------------------------------------------
    "welcome.title": "Koyura Bot — тихий загрузчик.",
    "welcome.body": "Пришлите ссылку на видео или аудио — файл придёт в этот чат.",
    "welcome.platforms": (
        "Поддерживаемые платформы: YouTube, TikTok, Instagram, Twitter, Reddit "
        "и ещё 1000+."
    ),
    # -- welcome keyboard --------------------------------------------------
    "button.help": "Помощь",
    "button.platforms": "Платформы",
    "button.settings": "Настройки",
    "button.stats": "Статистика",
    "button.mp3": "MP3",
    "button.mp4": "MP4",
    # -- /help -------------------------------------------------------------
    "help.title": "Как пользоваться Koyura Bot:",
    "help.step1": "1. Пришлите ссылку (YouTube, TikTok и т.д.)",
    "help.step2": "2. Выберите формат (MP4 или MP3)",
    "help.step3": "3. Выберите качество",
    "help.step4": "4. Файл придёт в этот чат",
    "help.shortcuts": "Быстрые команды:",
    "help.mp3": "/mp3 <ссылка> - сразу MP3",
    "help.mp4": "/mp4 <ссылка> - MP4 лучшего качества",
    "help.best": "/best <ссылка> - лучшее доступное качество",
    "help.preferences": "Настройки:",
    "help.setquality": "/setquality <best/720/480> - качество по умолчанию",
    "help.setformat": "/setformat <video/audio> - формат по умолчанию",
    "help.settings": "/settings - ваши настройки",
    "help.other": "Другое:",
    "help.platforms": "/platforms - поддерживаемые сайты",
    "help.stats": "/stats - статистика бота",
    "help.cancel": "/cancel - отмена (скоро)",
    "help.footer": "Можно прислать несколько ссылок сразу — бот обработает их по очереди.",
    # -- /platforms --------------------------------------------------------
    "platforms.header": "Поддерживаемые платформы:",
    "platforms.more": "и ещё 1000+ сайтов через yt-dlp",
    # -- /stats ------------------------------------------------------------
    "stats.title": "Статистика Koyura Bot:",
    "stats.uptime": "Аптайм",
    "stats.uptime_value": "{hours} ч {minutes} мин {seconds} с",
    "stats.downloads": "Загрузок",
    "stats.errors": "Ошибок",
    "stats.cached": "Файлов в кэше",
    "stats.disk": "Занято на диске",
    "stats.sessions": "Активных сессий",
    "stats.line": "  {label}: {value}",
    # -- /settings ---------------------------------------------------------
    "settings.title": "Ваши настройки:",
    "settings.quality": "Качество по умолчанию",
    "settings.format": "Формат по умолчанию",
    "settings.language": "Язык",
    "settings.mode": "Следующая ссылка",
    "settings.mode_none": "не задан",
    "settings.change": "Изменить:",
    "settings.setquality": "/setquality <best/720/480>",
    "settings.setformat": "/setformat <video/audio>",
    "settings.language_hint": "Нажмите язык, чтобы переключить.",
    "format.video": "видео",
    "format.audio": "аудио",
    # -- /setquality, /setformat -------------------------------------------
    "quality.usage": "Использование: /setquality <best/1080/720/480/360>",
    "quality.invalid": "Неизвестное качество. Варианты: {options}",
    "quality.saved": "Качество по умолчанию: {quality}",
    "format.usage": "Использование: /setformat <video/audio>",
    "format.invalid": "Неизвестный формат. Варианты: {options}",
    "format.saved": "Формат по умолчанию: {format}",
    # -- /mp3, /mp4, /best -------------------------------------------------
    "mp3.usage": "Использование: /mp3 <ссылка>\nИли ответьте на сообщение со ссылкой.",
    "mp4.usage": "Использование: /mp4 <ссылка>\nИли ответьте на сообщение со ссылкой.",
    "mp3.started": "Скачиваю MP3...",
    "mp4.started": "Скачиваю MP4 (лучшее качество)...",
    # -- the MP3/MP4 mode armed from the welcome keyboard ------------------
    "mode.armed": "Режим: {mode}\nПришлите ссылку — файл скачается как {mode}.",
    "mode.mp3": "MP3",
    "mode.mp4": "MP4",
    "mode.cleared": "Режим выключен. Следующая ссылка: не задан.",
    # -- intake and the preview card -----------------------------------------
    "stage.searching": "🔍",
    "intake.unsafe": (
        "Я качаю только с публичных ссылок http(s). "
        "Эта ссылка пропущена: такой адрес запрещён."
    ),
    "intake.too_many": "Обрабатываются только первые {limit} ссылок из сообщения.",
    "card.render_failed": "Не удалось показать эту ссылку. Попробуйте /mp4 или /mp3.",
    "card.untitled": "Без названия",
    "card.download": "Загрузка",
    "card.duration": "Длительность",
    "card.followers": "Подписчиков",
    "card.category": "Категория",
    "card.uploaded": "Загружено",
    "card.views": "просмотров",
    "card.likes": "лайков",
    "card.comments": "комментариев",
    "card.label_value": "{label}: {value}",
    "card.via": "через Koyura",
    "card.best_quality": "Лучшее качество",
    # -- engine answers ----------------------------------------------------
    "engine.info_failed": "Не удалось получить данные: {error}",
    "engine.error": "Ошибка: {error}",
    "engine.down": "Сервис загрузки временно недоступен.",
    # -- download stages ---------------------------------------------------
    "stage.starting": "Запускаю…",
    "stage.downloading": "Скачиваю…",
    "stage.processing": "Обрабатываю…",
    "stage.uploading": "Отправляю…",
    "download.session_expired": "Сессия истекла. Пришлите ссылку заново.",
    "download.already": "Уже скачивается — дождитесь файла.",
    "download.unexpected": "Сервис загрузки ответил неожиданно.",
    "download.unknown_error": "Неизвестная ошибка",
    "download.file_missing": "Файл не найден после загрузки.",
    "download.upload_failed": "Не удалось отправить файл в Telegram.",
    "download.timed_out": "Загрузка превысила лимит времени.",
    "download.failed": "Загрузка не удалась",
    "download.retry_hint": "Нажмите формат, чтобы попробовать снова.",
    "download.busy_user": "У вас уже идёт загрузка. Повторите через {when}.",
    "download.busy_service": "Сервис перегружен — повторите через {when}.",
    "download.wait_default": "~2 мин",
    # -- queue -------------------------------------------------------------
    "queue.hint": "Чат можно закрыть — файл придёт сюда.",
    "queue.head": "В очереди",
    "queue.head_number": "В очереди №{position}",
    "queue.estimated": "Ожидание примерно",
    "queue.wait_seconds": "~{seconds} с",
    "queue.wait_minutes": "~{minutes} мин",
    # -- progress details --------------------------------------------------
    "progress.eta": "осталось {eta}",
    "progress.elapsed": "{elapsed} прошло",
    # -- Telegram's own command menu (one language for the whole bot) -------
    "menu.start": "Запустить Koyura",
    "menu.help": "Помощь и команды",
    "menu.mp3": "Скачать как MP3",
    "menu.mp4": "Скачать MP4 лучшего качества",
    "menu.best": "Лучшее доступное качество",
    "menu.platforms": "Поддерживаемые платформы",
    "menu.settings": "Ваши настройки",
    "menu.setquality": "Качество по умолчанию",
    "menu.setformat": "Формат по умолчанию",
    "menu.stats": "Статистика Koyura",
}


CATALOGS: dict[str, dict[str, str]] = {"en": EN, "ru": RU}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

_FORMATTER = Formatter()


def normalise(lang: object) -> str:
    """The catalogue name for ``lang``: known language, else the default."""
    name = str(lang or "").strip().lower().replace("-", "_")
    if name in CATALOGS:
        return name
    # "en_US", "ru_RU" and friends: the language part is what we can render.
    base = name.split("_", 1)[0]
    return base if base in CATALOGS else DEFAULT_LANGUAGE


def t(key: str, lang: object = None, /, **values: object) -> str:
    """One catalogued line, with ``{placeholders}`` filled in.

    The lookup is deliberately forgiving: a language we do not know falls back to
    ``en``, a key that is missing everywhere falls back to the key itself. A bot
    that answers in the wrong language is a bug worth seeing; a bot that raises
    ``KeyError`` mid-download is worse.
    """
    catalogue = CATALOGS[normalise(lang)]
    template = catalogue.get(key)
    if template is None:
        template = CATALOGS[DEFAULT_LANGUAGE].get(key, key)
    if not values:
        return template
    return template.format(**{name: str(value) for name, value in values.items()})


def has(key: str, lang: object = None) -> bool:
    """Whether ``key`` exists in ``lang``'s catalogue (used by the tests)."""
    return key in CATALOGS[normalise(lang)]


def placeholders(key: str, lang: object = None) -> set[str]:
    """The ``{name}`` holes of one entry, for the cross-language check."""
    return {
        field
        for _, field, _, _ in _FORMATTER.parse(t(key, lang))
        if field
    }


def keys(lang: object = None) -> list[str]:
    """Every key of one catalogue (sorted, so a diff of the two reads easily)."""
    return sorted(CATALOGS[normalise(lang)])


# ---------------------------------------------------------------------------
# MarkdownV2
# ---------------------------------------------------------------------------

# Characters Telegram requires to be escaped in MarkdownV2 outside an entity.
MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def escape_md(text: object) -> str:
    """Escape MarkdownV2 specials.

    This is the **only** escaper in the bot: catalogues stay plain text,
    substituted values are escaped with the prose that surrounds them, and every
    ``parse_mode="MarkdownV2"`` call site goes through here (directly, or via
    ``handlers.stages`` which sends the already-escaped string).
    """
    return "".join(f"\\{char}" if char in MD_SPECIAL else char for char in str(text))


__all__ = [
    "CATALOGS",
    "DEFAULT_LANGUAGE",
    "EN",
    "LANGUAGES",
    "MD_SPECIAL",
    "RU",
    "escape_md",
    "has",
    "keys",
    "normalise",
    "placeholders",
    "t",
]
