"""The catalogue of bot messages the panel may edit.

One entry per user-visible message: its id, the panel group, whether it is sent
as a text or as a media caption (that is what decides Telegram's 4096/1024
limit), the placeholders it offers with a short Russian explanation and a
realistic example, and the **built-in default template** in every language.

The bot has the same registry (``bot/message_kinds.py``) because the services
cannot import each other, and the defaults are generated from ``bot/i18n.py``.
``.github/tests/test_message_template_contract.py`` keeps the two copies
honest: the same ids, the same placeholders, the same default text.

Do not edit a default by hand to "fix" a message: change ``bot/i18n.py`` and
regenerate this file, or the contract test fails.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Telegram's own limits, in characters, per kind of message.
LIMITS: dict[str, int] = {"text": 4096, "caption": 1024}

#: The budget the renderer enforces (a little under the hard limit).
BUDGETS: dict[str, int] = {"text": 4000, "caption": 1000}

LANGUAGES: tuple[str, ...] = ('en', 'ru')

@dataclass(frozen=True)
class Placeholder:
    """One ``@name``: what it substitutes, in the owner's words, and an example."""

    name: str
    summary: str
    example: str

    @property
    def token(self) -> str:
        return f"@{self.name}"



CATEGORIES: tuple[str, ...] = (
    "onboarding",
    "navigation",
    "download",
    "errors",
    "privileged",
    "buttons",
)

CATEGORY_LABELS: dict[str, str] = {
    'onboarding': 'Первое знакомство и справка',
    'navigation': 'Навигация и настройки',
    'download': 'Загрузка',
    'errors': 'Ошибки и отказы',
    'privileged': 'Статистика',
    'buttons': 'Тексты кнопок',
}

class Kind:
    """One user-visible message and everything the panel needs to edit it.

    Declared as a plain class with ``__slots__`` so the table above stays a
    literal (the generator writes it) while the properties stay cheap.
    """

    __slots__ = ("id", "category", "media", "title", "i18n_keys", "placeholders", "templates")

    def __init__(
        self,
        id: str,
        category: str,
        media: str,
        title: str,
        i18n_keys: tuple[str, ...],
        placeholders: tuple[str, ...],
        templates: dict[str, str],
    ) -> None:
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "media", media)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "i18n_keys", i18n_keys)
        object.__setattr__(self, "placeholders", placeholders)
        object.__setattr__(self, "templates", templates)

    def __setattr__(self, name: str, value: object) -> None:  # pragma: no cover
        raise AttributeError("Kind is read-only")

    def template(self, lang: object) -> str:
        """The built-in body in ``lang`` (the default language when unknown)."""
        name = str(lang or "").strip().lower().replace("-", "_")
        if name in self.templates:
            return self.templates[name]
        base = name.split("_", 1)[0]
        return self.templates.get(base, self.templates[LANGUAGES[0]])

    @property
    def limit(self) -> int:
        """Telegram's hard limit for this kind."""
        return LIMITS.get(self.media, LIMITS["text"])

    @property
    def budget(self) -> int:
        """What the renderer caps the text at."""
        return BUDGETS.get(self.media, BUDGETS["text"])

    def placeholder_objects(self) -> list[Placeholder]:
        return [PLACEHOLDERS[name] for name in self.placeholders]

    def samples(self) -> dict[str, str]:
        """Realistic test values, one per placeholder, for the preview."""
        return {name: PLACEHOLDERS[name].example for name in self.placeholders}


PLACEHOLDERS: dict[str, "Placeholder"] = {
    'languages': Placeholder('languages', 'Список включённых языков через запятую', 'English, Русский'),
    'platforms': Placeholder('platforms', 'Список поддерживаемых сервисов', 'YouTube, TikTok, Instagram, Twitter/X, Reddit'),
    'version': Placeholder('version', 'Версия бота', '1.0'),
    'count': Placeholder('count', 'Сколько сервисов в списке', '16'),
    'language': Placeholder('language', 'Язык этого чата', 'English'),
    'mode': Placeholder('mode', 'Формат, который включён кнопкой', 'MP3'),
    'quality': Placeholder('quality', 'Выбранное качество', '1080p'),
    'format': Placeholder('format', 'Формат по умолчанию', 'video'),
    'options': Placeholder('options', 'Допустимые значения команды', 'best, 1080, 720, 480, 360'),
    'title': Placeholder('title', 'Название видео или файла', 'Как приготовить пасту карбонара'),
    'uploader': Placeholder('uploader', 'Имя автора или канала', 'Кухня на каждый день'),
    'channel': Placeholder('channel', 'Канал и @ник одной строкой', 'Кухня на каждый день · @kitchen'),
    'followers': Placeholder('followers', 'Число подписчиков', '1,3M'),
    'duration': Placeholder('duration', 'Длительность файла', '12:34'),
    'views': Placeholder('views', 'Число просмотров', '110,6K'),
    'likes': Placeholder('likes', 'Число лайков', '8,4K'),
    'comments': Placeholder('comments', 'Число комментариев', '312'),
    'category': Placeholder('category', 'Категория ролика', 'Образование'),
    'uploaded': Placeholder('uploaded', 'Дата публикации', '2024-11-10'),
    'source': Placeholder('source', 'Сервис или ссылка, откуда файл', 'youtube.com'),
    'url': Placeholder('url', 'Полная ссылка на видео', 'https://youtube.com/watch?v=abc123'),
    'meta': Placeholder('meta', 'Готовая строка метаданных карточки', 'Кухня на каждый день · @kitchen\nДлительность: 12:34'),
    'stage': Placeholder('stage', 'Что сейчас происходит', 'Скачиваю…'),
    'bar': Placeholder('bar', 'Полоса прогресса, 12 ячеек', '▓▓▓▓▓▓▓░░░░░'),
    'percent': Placeholder('percent', 'Процент загрузки', '62%'),
    'facts': Placeholder('facts', 'Готовая строка фактов через ·', '62% · 3,2 MB/s · 0:42'),
    'size': Placeholder('size', 'Скачано и общий размер', '12,4 MB / 30,8 MB'),
    'speed': Placeholder('speed', 'Скорость загрузки', '3,2 MB/s'),
    'eta': Placeholder('eta', 'Сколько осталось', '0:42'),
    'elapsed': Placeholder('elapsed', 'Сколько уже прошло', '0:13'),
    'position': Placeholder('position', 'Место в очереди', 'В очереди №3 · ~45 с'),
    'wait': Placeholder('wait', 'Оценка ожидания', '~45 с'),
    'hint': Placeholder('hint', 'Подсказка про очередь', 'Чат можно закрыть — файл придёт сюда.'),
    'question': Placeholder('question', 'Вопрос про кодек', 'Отправить 2160p (AV1) файлом или конвертировать?'),
    'codec': Placeholder('codec', 'Название кодека', 'AV1'),
    'note': Placeholder('note', 'Пояснение под вопросом', 'AV1/VP9 может не проигрываться на iPhone.'),
    'limit': Placeholder('limit', 'Лимит отправки Telegram', '2000 МБ'),
    'skipped': Placeholder('skipped', 'Формат, который не влез в лимит', '2160p (2,3 GB)'),
    'error': Placeholder('error', 'Текст ошибки', 'Download timed out'),
    'retry_hint': Placeholder('retry_hint', 'Подсказка, как повторить', 'Нажмите формат, чтобы попробовать снова.'),
    'when': Placeholder('when', 'Когда можно повторить', '~2 мин'),
    'uptime': Placeholder('uptime', 'Время работы процесса', '4 ч 12 мин 9 с'),
    'downloads': Placeholder('downloads', 'Сколько загрузок сделано', '128'),
    'errors': Placeholder('errors', 'Сколько было ошибок', '3'),
    'cached': Placeholder('cached', 'Сколько файлов лежит на диске', '7'),
    'disk': Placeholder('disk', 'Сколько занято на диске', '1240.5 MB'),
    'sessions': Placeholder('sessions', 'Сколько активных сессий', '2'),
}

KINDS: tuple["Kind", ...] = (
    Kind(
        id='language.prompt',
        category='onboarding',
        media='text',
        title='Экран выбора языка',
        i18n_keys=('language.prompt',),
        placeholders=('languages',),
        templates={
            'en': 'Choose the language of this chat.',
            'ru': 'Выберите язык этого чата.',
        },
    ),
    Kind(
        id='welcome',
        category='onboarding',
        media='text',
        title='Приветствие (/start)',
        i18n_keys=('welcome.title', 'welcome.body', 'welcome.platforms'),
        placeholders=('languages', 'platforms', 'version'),
        templates={
            'en': 'Koyura Bot — a quiet downloader.\n\nSend a video or audio link and the file arrives in this chat.\n\nSupported platforms: YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ more.',
            'ru': 'Koyura Bot — тихий загрузчик.\n\nПришлите ссылку на видео или аудио — файл придёт в этот чат.\n\nПоддерживаемые платформы: YouTube, TikTok, Instagram, Twitter, Reddit и ещё 1000+.',
        },
    ),
    Kind(
        id='help',
        category='onboarding',
        media='text',
        title='Справка (/help)',
        i18n_keys=('help.title', 'help.step1', 'help.step2', 'help.step3', 'help.step4', 'help.shortcuts', 'help.mp3', 'help.mp4', 'help.best', 'help.preferences', 'help.setquality', 'help.setformat', 'help.settings', 'help.other', 'help.platforms', 'help.stats', 'help.cancel', 'help.footer'),
        placeholders=('platforms', 'version'),
        templates={
            'en': '*How to use Koyura Bot:*\n\n1. Send a link (YouTube, TikTok, etc.)\n2. Pick a format (MP4 or MP3)\n3. Pick quality\n4. The file arrives\n\n*Shortcuts:*\n\n/mp3 <link> - Direct MP3 download\n/mp4 <link> - Best quality MP4\n/best <link> - Best available quality\n\n*Preferences:*\n\n/setquality <best/720/480> - Default quality\n/setformat <video/audio> - Default format\n/settings - View your preferences\n\n*Other:*\n\n/platforms - Supported sites\n/stats - Bot stats\n/cancel - Cancel (coming soon)\n\nSend several links at once and each one is handled in turn.',
            'ru': '*Как пользоваться Koyura Bot:*\n\n1. Пришлите ссылку (YouTube, TikTok и т.д.)\n2. Выберите формат (MP4 или MP3)\n3. Выберите качество\n4. Файл придёт в этот чат\n\n*Быстрые команды:*\n\n/mp3 <ссылка> - сразу MP3\n/mp4 <ссылка> - MP4 лучшего качества\n/best <ссылка> - лучшее доступное качество\n\n*Настройки:*\n\n/setquality <best/720/480> - качество по умолчанию\n/setformat <video/audio> - формат по умолчанию\n/settings - ваши настройки\n\n*Другое:*\n\n/platforms - поддерживаемые сайты\n/stats - статистика бота\n/cancel - отмена (скоро)\n\nМожно прислать несколько ссылок сразу — бот обработает их по очереди.',
        },
    ),
    Kind(
        id='platforms',
        category='onboarding',
        media='text',
        title='Поддерживаемые платформы (/platforms)',
        i18n_keys=('platforms.header', 'platforms.more'),
        placeholders=('platforms', 'count'),
        templates={
            'en': 'Supported platforms:\n\n@platforms\n  and 1000+ more via yt-dlp',
            'ru': 'Поддерживаемые платформы:\n\n@platforms\n  и ещё 1000+ сайтов через yt-dlp',
        },
    ),
    Kind(
        id='language.unknown',
        category='onboarding',
        media='text',
        title='Язык выключен (всплывающее сообщение)',
        i18n_keys=('language.unknown',),
        placeholders=(),
        templates={
            'en': 'That language is not enabled on this bot.',
            'ru': 'Этот язык не включён на этом боте.',
        },
    ),
    Kind(
        id='mode.armed',
        category='navigation',
        media='text',
        title='Режим MP3/MP4 включён',
        i18n_keys=('mode.armed',),
        placeholders=('mode',),
        templates={
            'en': 'Mode: @mode\nSend the link — it downloads as @mode.',
            'ru': 'Режим: @mode\nПришлите ссылку — файл скачается как @mode.',
        },
    ),
    Kind(
        id='settings',
        category='navigation',
        media='text',
        title='Настройки (/settings)',
        i18n_keys=('settings.title', 'settings.quality', 'settings.format', 'settings.language', 'settings.mode', 'settings.mode_none', 'settings.change', 'settings.setquality', 'settings.setformat', 'settings.language_hint'),
        placeholders=('quality', 'format', 'language', 'mode'),
        templates={
            'en': 'Your preferences:\n\n  Default quality: @quality\n  Default format: @format\n  Language: @language\n  Next link: @mode\n\nChange:\n  /setquality <best/720/480>\n  /setformat <video/audio>\n\nTap a language to switch.',
            'ru': 'Ваши настройки:\n\n  Качество по умолчанию: @quality\n  Формат по умолчанию: @format\n  Язык: @language\n  Следующая ссылка: @mode\n\nИзменить:\n  /setquality <best/720/480>\n  /setformat <video/audio>\n\nНажмите язык, чтобы переключить.',
        },
    ),
    Kind(
        id='quality.usage',
        category='navigation',
        media='text',
        title='Как пользоваться /setquality',
        i18n_keys=('quality.usage',),
        placeholders=(),
        templates={
            'en': 'Usage: /setquality <best/1080/720/480/360>',
            'ru': 'Использование: /setquality <best/1080/720/480/360>',
        },
    ),
    Kind(
        id='quality.invalid',
        category='navigation',
        media='text',
        title='Неизвестное качество',
        i18n_keys=('quality.invalid',),
        placeholders=('options',),
        templates={
            'en': 'Invalid quality. Options: @options',
            'ru': 'Неизвестное качество. Варианты: @options',
        },
    ),
    Kind(
        id='quality.saved',
        category='navigation',
        media='text',
        title='Качество сохранено',
        i18n_keys=('quality.saved',),
        placeholders=('quality',),
        templates={
            'en': 'Default quality set to: @quality',
            'ru': 'Качество по умолчанию: @quality',
        },
    ),
    Kind(
        id='format.usage',
        category='navigation',
        media='text',
        title='Как пользоваться /setformat',
        i18n_keys=('format.usage',),
        placeholders=(),
        templates={
            'en': 'Usage: /setformat <video/audio>',
            'ru': 'Использование: /setformat <video/audio>',
        },
    ),
    Kind(
        id='format.invalid',
        category='navigation',
        media='text',
        title='Неизвестный формат',
        i18n_keys=('format.invalid',),
        placeholders=('options',),
        templates={
            'en': 'Invalid format. Options: @options',
            'ru': 'Неизвестный формат. Варианты: @options',
        },
    ),
    Kind(
        id='format.saved',
        category='navigation',
        media='text',
        title='Формат сохранён',
        i18n_keys=('format.saved',),
        placeholders=('format',),
        templates={
            'en': 'Default format set to: @format',
            'ru': 'Формат по умолчанию: @format',
        },
    ),
    Kind(
        id='mp3.usage',
        category='navigation',
        media='text',
        title='Как пользоваться /mp3',
        i18n_keys=('mp3.usage',),
        placeholders=(),
        templates={
            'en': 'Usage: /mp3 <link>\nOr reply to a message containing a link.',
            'ru': 'Использование: /mp3 <ссылка>\nИли ответьте на сообщение со ссылкой.',
        },
    ),
    Kind(
        id='mp4.usage',
        category='navigation',
        media='text',
        title='Как пользоваться /mp4',
        i18n_keys=('mp4.usage',),
        placeholders=(),
        templates={
            'en': 'Usage: /mp4 <link>\nOr reply to a message containing a link.',
            'ru': 'Использование: /mp4 <ссылка>\nИли ответьте на сообщение со ссылкой.',
        },
    ),
    Kind(
        id='mp3.started',
        category='navigation',
        media='text',
        title='MP3 скачивается',
        i18n_keys=('mp3.started',),
        placeholders=(),
        templates={
            'en': 'Downloading MP3...',
            'ru': 'Скачиваю MP3...',
        },
    ),
    Kind(
        id='mp4.started',
        category='navigation',
        media='text',
        title='MP4 скачивается',
        i18n_keys=('mp4.started',),
        placeholders=(),
        templates={
            'en': 'Downloading MP4 (best quality)...',
            'ru': 'Скачиваю MP4 (лучшее качество)...',
        },
    ),
    Kind(
        id='stage.searching',
        category='download',
        media='text',
        title='Поиск информации о ссылке',
        i18n_keys=('stage.searching',),
        placeholders=(),
        templates={
            'en': '🔍',
            'ru': '🔍',
        },
    ),
    Kind(
        id='stage.starting',
        category='download',
        media='text',
        title='Начало загрузки',
        i18n_keys=('stage.starting',),
        placeholders=(),
        templates={
            'en': 'Starting…',
            'ru': 'Запускаю…',
        },
    ),
    Kind(
        id='card',
        category='download',
        media='caption',
        title='Карточка превью ссылки',
        i18n_keys=('card.untitled', 'card.duration', 'card.followers', 'card.category', 'card.uploaded', 'card.views', 'card.likes', 'card.comments', 'card.via'),
        placeholders=('title', 'uploader', 'channel', 'followers', 'duration', 'views', 'likes', 'comments', 'category', 'uploaded', 'source', 'url', 'meta'),
        templates={
            'en': '*@title*\n\n@channel\nDuration: @duration\nFollowers: @followers\n@views views · @likes likes · @comments comments\nCategory: @category\nUploaded: @uploaded',
            'ru': '*@title*\n\n@channel\nДлительность: @duration\nПодписчиков: @followers\n@views просмотров · @likes лайков · @comments комментариев\nКатегория: @category\nЗагружено: @uploaded',
        },
    ),
    Kind(
        id='progress',
        category='download',
        media='caption',
        title='Прогресс загрузки',
        i18n_keys=('stage.starting', 'stage.downloading', 'stage.processing', 'stage.uploading', 'stage.converting', 'progress.eta', 'progress.elapsed'),
        placeholders=('title', 'stage', 'quality', 'bar', 'percent', 'size', 'speed', 'eta', 'elapsed', 'facts'),
        templates={
            'en': '*@title*\n@stage\n@bar',
            'ru': '*@title*\n@stage\n@bar',
        },
    ),
    Kind(
        id='queued',
        category='download',
        media='caption',
        title='Задача в очереди',
        i18n_keys=('queue.hint', 'queue.head', 'queue.head_number', 'queue.estimated'),
        placeholders=('title', 'position', 'wait', 'hint'),
        templates={
            'en': '*@title*\n@position\n@hint',
            'ru': '*@title*\n@position\n@hint',
        },
    ),
    Kind(
        id='done',
        category='download',
        media='caption',
        title='Подпись к готовому файлу',
        i18n_keys=('card.download', 'card.via', 'transcode.document', 'limit.quality_selected'),
        placeholders=('title', 'quality', 'size', 'duration', 'source'),
        templates={
            'en': '*@title*\n@quality · @size · @duration\n@source',
            'ru': '*@title*\n@quality · @size · @duration\n@source',
        },
    ),
    Kind(
        id='transcode.ask',
        category='download',
        media='caption',
        title='Вопрос о конвертации кодека',
        i18n_keys=('transcode.ask', 'transcode.note'),
        placeholders=('title', 'question', 'quality', 'codec', 'note'),
        templates={
            'en': '*@title*\n@question\n@note',
            'ru': '*@title*\n@question\n@note',
        },
    ),
    Kind(
        id='limit.all_too_big',
        category='download',
        media='text',
        title='Все форматы больше лимита',
        i18n_keys=('limit.all_too_big',),
        placeholders=('limit',),
        templates={
            'en': 'All available formats are over the send limit (@limit). Try another link or MP3.',
            'ru': 'Все доступные форматы больше лимита отправки (@limit). Попробуйте другую ссылку или MP3.',
        },
    ),
    Kind(
        id='limit.video_too_big',
        category='download',
        media='text',
        title='Видео больше лимита',
        i18n_keys=('limit.video_too_big',),
        placeholders=('limit',),
        templates={
            'en': 'This video is too large to send (limit @limit).',
            'ru': 'Это видео слишком большое для отправки (лимит @limit).',
        },
    ),
    Kind(
        id='limit.audio_too_big',
        category='download',
        media='text',
        title='Аудио больше лимита',
        i18n_keys=('limit.audio_too_big',),
        placeholders=('limit',),
        templates={
            'en': 'This audio is too large to send (limit @limit).',
            'ru': 'Это аудио слишком большое для отправки (лимит @limit).',
        },
    ),
    Kind(
        id='transcode.document',
        category='download',
        media='text',
        title='Файл с несовместимым кодеком',
        i18n_keys=('transcode.document',),
        placeholders=(),
        templates={
            'en': 'Sent as a file: this codec plays as a video only on some phones.',
            'ru': 'Отправлено файлом: этот кодек проигрывается как видео не на всех телефонах.',
        },
    ),
    Kind(
        id='limit.quality_selected',
        category='download',
        media='text',
        title='Качество снижено под лимит',
        i18n_keys=('limit.quality_selected',),
        placeholders=('quality', 'skipped'),
        templates={
            'en': 'Selected @quality: @skipped does not fit the send limit',
            'ru': 'Выбрано @quality: @skipped не влезает в лимит',
        },
    ),
    Kind(
        id='error',
        category='errors',
        media='caption',
        title='Ошибка загрузки',
        i18n_keys=('download.failed', 'download.unknown_error', 'download.retry_hint'),
        placeholders=('title', 'error', 'retry_hint', 'url', 'limit'),
        templates={
            'en': '*@title*\n@error\n@retry_hint',
            'ru': '*@title*\n@error\n@retry_hint',
        },
    ),
    Kind(
        id='download.session_expired',
        category='errors',
        media='text',
        title='Сессия истекла',
        i18n_keys=('download.session_expired',),
        placeholders=(),
        templates={
            'en': 'Session expired. Please send the link again.',
            'ru': 'Сессия истекла. Пришлите ссылку заново.',
        },
    ),
    Kind(
        id='download.already',
        category='errors',
        media='text',
        title='Уже скачивается',
        i18n_keys=('download.already',),
        placeholders=(),
        templates={
            'en': 'Already downloading — wait for the file.',
            'ru': 'Уже скачивается — дождитесь файла.',
        },
    ),
    Kind(
        id='download.unexpected',
        category='errors',
        media='text',
        title='Неожиданный ответ сервиса',
        i18n_keys=('download.unexpected',),
        placeholders=(),
        templates={
            'en': 'Download service returned an unexpected answer.',
            'ru': 'Сервис загрузки ответил неожиданно.',
        },
    ),
    Kind(
        id='download.unknown_error',
        category='errors',
        media='text',
        title='Неизвестная ошибка',
        i18n_keys=('download.unknown_error',),
        placeholders=(),
        templates={
            'en': 'Unknown error',
            'ru': 'Неизвестная ошибка',
        },
    ),
    Kind(
        id='download.file_missing',
        category='errors',
        media='text',
        title='Файл не найден',
        i18n_keys=('download.file_missing',),
        placeholders=(),
        templates={
            'en': 'File not found after download.',
            'ru': 'Файл не найден после загрузки.',
        },
    ),
    Kind(
        id='download.upload_failed',
        category='errors',
        media='text',
        title='Не удалось отправить файл',
        i18n_keys=('download.upload_failed',),
        placeholders=(),
        templates={
            'en': 'Failed to upload file to Telegram.',
            'ru': 'Не удалось отправить файл в Telegram.',
        },
    ),
    Kind(
        id='download.timed_out',
        category='errors',
        media='text',
        title='Загрузка превысила лимит времени',
        i18n_keys=('download.timed_out',),
        placeholders=(),
        templates={
            'en': 'Download timed out.',
            'ru': 'Загрузка превысила лимит времени.',
        },
    ),
    Kind(
        id='download.busy_user',
        category='errors',
        media='text',
        title='У пользователя уже идёт загрузка',
        i18n_keys=('download.busy_user',),
        placeholders=('when',),
        templates={
            'en': 'You already have a download running. Try again in @when.',
            'ru': 'У вас уже идёт загрузка. Повторите через @when.',
        },
    ),
    Kind(
        id='download.busy_service',
        category='errors',
        media='text',
        title='Сервис перегружен',
        i18n_keys=('download.busy_service',),
        placeholders=('when',),
        templates={
            'en': 'The service is overloaded right now — try again in @when.',
            'ru': 'Сервис перегружен — повторите через @when.',
        },
    ),
    Kind(
        id='engine.down',
        category='errors',
        media='text',
        title='Движок недоступен',
        i18n_keys=('engine.down',),
        placeholders=(),
        templates={
            'en': 'Download service temporarily unavailable.',
            'ru': 'Сервис загрузки временно недоступен.',
        },
    ),
    Kind(
        id='engine.info_failed',
        category='errors',
        media='text',
        title='Не удалось получить данные ссылки',
        i18n_keys=('engine.info_failed',),
        placeholders=('error',),
        templates={
            'en': 'Failed to fetch info: @error',
            'ru': 'Не удалось получить данные: @error',
        },
    ),
    Kind(
        id='engine.error',
        category='errors',
        media='text',
        title='Ошибка движка',
        i18n_keys=('engine.error',),
        placeholders=('error',),
        templates={
            'en': 'Error: @error',
            'ru': 'Ошибка: @error',
        },
    ),
    Kind(
        id='card.render_failed',
        category='errors',
        media='text',
        title='Не удалось показать карточку',
        i18n_keys=('card.render_failed',),
        placeholders=(),
        templates={
            'en': 'Could not render this link. Try /mp4 or /mp3 instead.',
            'ru': 'Не удалось показать эту ссылку. Попробуйте /mp4 или /mp3.',
        },
    ),
    Kind(
        id='intake.unsafe',
        category='errors',
        media='text',
        title='Ссылка запрещена',
        i18n_keys=('intake.unsafe',),
        placeholders=(),
        templates={
            'en': 'I can only download from public http(s) links. That link was skipped because it is not allowed.',
            'ru': 'Я качаю только с публичных ссылок http(s). Эта ссылка пропущена: такой адрес запрещён.',
        },
    ),
    Kind(
        id='intake.too_many',
        category='errors',
        media='text',
        title='Слишком много ссылок в сообщении',
        i18n_keys=('intake.too_many',),
        placeholders=('limit',),
        templates={
            'en': 'Only the first @limit links of a message are processed.',
            'ru': 'Обрабатываются только первые @limit ссылок из сообщения.',
        },
    ),
    Kind(
        id='stats',
        category='privileged',
        media='text',
        title='Статистика (/stats)',
        i18n_keys=('stats.title', 'stats.uptime', 'stats.downloads', 'stats.errors', 'stats.cached', 'stats.disk', 'stats.sessions'),
        placeholders=('uptime', 'downloads', 'errors', 'cached', 'disk', 'sessions', 'version'),
        templates={
            'en': 'Koyura Bot Stats:\n\n  Uptime: @uptime\n  Downloads: @downloads\n  Errors: @errors\n  Cached files: @cached\n  Disk usage: @disk\n  Active sessions: @sessions',
            'ru': 'Статистика Koyura Bot:\n\n  Аптайм: @uptime\n  Загрузок: @downloads\n  Ошибок: @errors\n  Файлов в кэше: @cached\n  Занято на диске: @disk\n  Активных сессий: @sessions',
        },
    ),
    Kind(
        id='nav.back',
        category='buttons',
        media='text',
        title='Кнопка «Назад»',
        i18n_keys=('nav.back',),
        placeholders=(),
        templates={
            'en': 'Back',
            'ru': 'Назад',
        },
    ),
    Kind(
        id='button.help',
        category='buttons',
        media='text',
        title='Кнопка «Помощь»',
        i18n_keys=('button.help',),
        placeholders=(),
        templates={
            'en': 'Help',
            'ru': 'Помощь',
        },
    ),
    Kind(
        id='button.platforms',
        category='buttons',
        media='text',
        title='Кнопка «Платформы»',
        i18n_keys=('button.platforms',),
        placeholders=(),
        templates={
            'en': 'Platforms',
            'ru': 'Платформы',
        },
    ),
    Kind(
        id='button.settings',
        category='buttons',
        media='text',
        title='Кнопка «Настройки»',
        i18n_keys=('button.settings',),
        placeholders=(),
        templates={
            'en': 'Settings',
            'ru': 'Настройки',
        },
    ),
    Kind(
        id='button.stats',
        category='buttons',
        media='text',
        title='Кнопка «Статистика»',
        i18n_keys=('button.stats',),
        placeholders=(),
        templates={
            'en': 'Stats',
            'ru': 'Статистика',
        },
    ),
    Kind(
        id='button.mp3',
        category='buttons',
        media='text',
        title='Кнопка «MP3»',
        i18n_keys=('button.mp3',),
        placeholders=(),
        templates={
            'en': 'MP3',
            'ru': 'MP3',
        },
    ),
    Kind(
        id='button.mp4',
        category='buttons',
        media='text',
        title='Кнопка «MP4»',
        i18n_keys=('button.mp4',),
        placeholders=(),
        templates={
            'en': 'MP4',
            'ru': 'MP4',
        },
    ),
    Kind(
        id='card.best_quality',
        category='buttons',
        media='text',
        title='Кнопка «Лучшее качество»',
        i18n_keys=('card.best_quality',),
        placeholders=(),
        templates={
            'en': 'Best quality',
            'ru': 'Лучшее качество',
        },
    ),
    Kind(
        id='transcode.send_as_file',
        category='buttons',
        media='text',
        title='Кнопка «Отправить файлом»',
        i18n_keys=('transcode.send_as_file',),
        placeholders=(),
        templates={
            'en': 'Send as file',
            'ru': 'Отправить файлом',
        },
    ),
    Kind(
        id='transcode.convert',
        category='buttons',
        media='text',
        title='Кнопка «Конвертировать»',
        i18n_keys=('transcode.convert', 'transcode.convert_no_eta'),
        placeholders=('eta',),
        templates={
            'en': 'Convert (@eta)',
            'ru': 'Конвертировать (@eta)',
        },
    ),
)

BY_ID: dict[str, "Kind"] = {kind.id: kind for kind in KINDS}


def get(kind_id: str) -> Kind | None:
    return BY_ID.get(str(kind_id))


def categories() -> list[dict[str, Any]]:
    """The panel's group list: every category with the kinds it holds."""
    grouped: list[dict[str, object]] = []
    for name in CATEGORIES:
        members = [kind for kind in KINDS if kind.category == name]
        if members:
            grouped.append(
                {"id": name, "label": CATEGORY_LABELS.get(name, name), "kinds": members}
            )
    return grouped


def samples(kind_id: str) -> dict[str, str]:
    kind = get(kind_id)
    return kind.samples() if kind is not None else {}


__all__ = [
    "BUDGETS",
    "BY_ID",
    "CATEGORIES",
    "CATEGORY_LABELS",
    "KINDS",
    "LIMITS",
    "LANGUAGES",
    "PLACEHOLDERS",
    "Kind",
    "Placeholder",
    "categories",
    "get",
    "samples",
]


# ---------------------------------------------------------------------------
# The operator documentation
# ---------------------------------------------------------------------------

_HEADER = """# Message templates

<!-- Generated from `dashboard/message_catalog.py`; regenerate with
     `python -c "import message_catalog; open('../docs/message-templates.md', 'w').write(message_catalog.document())"`
     The documentation test fails when this file and the catalogue disagree. -->

Every message the bot sends is described in one catalogue: a stable `id`, the
panel group it belongs to, whether it is a text message or a media caption (that
is what decides Telegram's 4096/1024 limit), the placeholders it offers, and the
built-in default in every language.

The defaults are built from `bot/i18n.py` and are **read-only**: they cannot be
broken from the panel. A custom body written in Admin → **Message templates** is
stored in the dashboard's `message_templates` table and handed to the bot in
`GET /api/bot-settings` (cached for 45 seconds), together with the languages and
the progress switches. One custom body is active per `(kind, language)` pair; the
rest stay stored and inactive. Deleting the active one, or using **Reset to
default**, makes the bot render the built-in text again.

## How rendering works

* `@name` is a placeholder. The **value** is escaped for MarkdownV2, the text
  around it is not — so `*@title*` is bold and a title containing `&`, `_` or `.`
  cannot break the message.
* A line whose placeholders are all empty disappears, label and all. That is how
  the preview card drops the fields the engine did not report.
* Balanced `*bold*`, `_italic_`, `~strike~`, `||spoiler||` and `` `code` `` are
  kept. A body with an unpaired marker is sent as literal text instead of being
  rejected by Telegram; `\\*` writes a literal asterisk.
* An **unknown placeholder** is a validation error: the panel refuses to save
  the body.
* A body that is empty, unavailable, over the limit or impossible to render is
  replaced by the built-in default. The bot never sends an empty message.
* The renderer caps a text at 4000 characters and a caption at 1000, a little
  under Telegram's own limits, and cuts the tail with `…`.

"""

_RULES = """## Placeholders

The same name means the same thing everywhere, so a custom body can be written by
copying a default and moving a piece around.

| Плейсхолдер | Что подставляет | Пример |
| --- | --- | --- |
"""


def document() -> str:
    """The full `docs/message-templates.md`, generated from this catalogue."""
    lines = [_HEADER]
    lines.append(_RULES)
    for name in sorted(PLACEHOLDERS):
        placeholder = PLACEHOLDERS[name]
        example = placeholder.example.replace("|", "\\|").replace("\n", " / ")
        lines.append(
            f"| `@{name}` | {placeholder.summary} | `{example}` |\n"
        )
    lines.append("\n## The catalogue\n\n")
    lines.append(
        "| Категория | Сообщение | `id` | Отправка | Лимит | Плейсхолдеры |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
    )
    for kind in KINDS:
        tokens = ", ".join(f"`@{name}`" for name in kind.placeholders) or "—"
        lines.append(
            f"| {CATEGORY_LABELS.get(kind.category, kind.category)} "
            f"| {kind.title} | `{kind.id}` | {kind.media} | {kind.limit} | {tokens} |\n"
        )
    lines.append("\n## Default templates\n\n")
    lines.append(
        "These are the texts the bot sends today. They are read-only; a custom\n"
        "template starts as a copy of them.\n"
    )
    for kind in KINDS:
        lines.append(
            f"\n### {kind.title} (`{kind.id}`)\n\n"
            f"`{kind.media}` · limit {kind.limit} · i18n: "
            + ", ".join(f"`{key}`" for key in kind.i18n_keys)
            + "\n"
        )
        for lang in LANGUAGES:
            lines.append(f"\n#### {lang}\n\n```text\n{kind.template(lang)}\n```\n")
    lines.append(
        "\n## When something goes wrong\n\n"
        "* **Unknown placeholder** — the panel refuses to save and names the\n"
        "  placeholder and the ones this message offers.\n"
        "* **Empty body** — same: a template has to say something.\n"
        "* **Too long** — saved, with a warning in the editor; the bot cuts the\n"
        "  tail with `…`.\n"
        "* **Broken MarkdownV2** — the message goes out as literal text.\n"
        "* **The panel is down** — the bot keeps its last settings for a while and\n"
        "  then renders the built-in defaults; nothing is lost and no message is\n"
        "  skipped.\n"
        "* **An empty value** — the placeholder becomes an empty string and its\n"
        "  line disappears.\n"
    )
    return "".join(lines)


__all__ += ["document"]
