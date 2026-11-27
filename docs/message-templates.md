# Message templates

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
  rejected by Telegram; `\*` writes a literal asterisk.
* An **unknown placeholder** is a validation error: the panel refuses to save
  the body.
* A body that is empty, unavailable, over the limit or impossible to render is
  replaced by the built-in default. The bot never sends an empty message.
* The renderer caps a text at 4000 characters and a caption at 1000, a little
  under Telegram's own limits, and cuts the tail with `…`.

## Placeholders

The same name means the same thing everywhere, so a custom body can be written by
copying a default and moving a piece around.

| Плейсхолдер | Что подставляет | Пример |
| --- | --- | --- |
| `@bar` | Полоса прогресса, 12 ячеек | `▓▓▓▓▓▓▓░░░░░` |
| `@cached` | Сколько файлов лежит на диске | `7` |
| `@category` | Категория ролика | `Образование` |
| `@channel` | Канал и @ник одной строкой | `Кухня на каждый день · @kitchen` |
| `@codec` | Название кодека | `AV1` |
| `@comments` | Число комментариев | `312` |
| `@count` | Сколько сервисов в списке | `16` |
| `@disk` | Сколько занято на диске | `1240.5 MB` |
| `@downloads` | Сколько загрузок сделано | `128` |
| `@duration` | Длительность файла | `12:34` |
| `@elapsed` | Сколько уже прошло | `0:13` |
| `@error` | Текст ошибки | `Download timed out` |
| `@errors` | Сколько было ошибок | `3` |
| `@eta` | Сколько осталось | `0:42` |
| `@facts` | Готовая строка фактов через · | `62% · 3,2 MB/s · 0:42` |
| `@followers` | Число подписчиков | `1,3M` |
| `@format` | Формат по умолчанию | `video` |
| `@hint` | Подсказка про очередь | `Чат можно закрыть — файл придёт сюда.` |
| `@language` | Язык этого чата | `English` |
| `@languages` | Список включённых языков через запятую | `English, Русский` |
| `@likes` | Число лайков | `8,4K` |
| `@limit` | Лимит отправки Telegram | `2000 МБ` |
| `@meta` | Готовая строка метаданных карточки | `Кухня на каждый день · @kitchen / Длительность: 12:34` |
| `@mode` | Формат, который включён кнопкой | `MP3` |
| `@note` | Пояснение под вопросом | `AV1/VP9 может не проигрываться на iPhone.` |
| `@options` | Допустимые значения команды | `best, 1080, 720, 480, 360` |
| `@percent` | Процент загрузки | `62%` |
| `@platforms` | Список поддерживаемых сервисов | `YouTube, TikTok, Instagram, Twitter/X, Reddit` |
| `@position` | Место в очереди | `В очереди №3 · ~45 с` |
| `@quality` | Выбранное качество | `1080p` |
| `@question` | Вопрос про кодек | `Отправить 2160p (AV1) файлом или конвертировать?` |
| `@retry_hint` | Подсказка, как повторить | `Нажмите формат, чтобы попробовать снова.` |
| `@sessions` | Сколько активных сессий | `2` |
| `@size` | Скачано и общий размер | `12,4 MB / 30,8 MB` |
| `@skipped` | Формат, который не влез в лимит | `2160p (2,3 GB)` |
| `@source` | Сервис или ссылка, откуда файл | `youtube.com` |
| `@speed` | Скорость загрузки | `3,2 MB/s` |
| `@stage` | Что сейчас происходит | `Скачиваю…` |
| `@title` | Название видео или файла | `Как приготовить пасту карбонара` |
| `@uploaded` | Дата публикации | `2024-11-10` |
| `@uploader` | Имя автора или канала | `Кухня на каждый день` |
| `@uptime` | Время работы процесса | `4 ч 12 мин 9 с` |
| `@url` | Полная ссылка на видео | `https://youtube.com/watch?v=abc123` |
| `@version` | Версия бота | `1.0` |
| `@views` | Число просмотров | `110,6K` |
| `@wait` | Оценка ожидания | `~45 с` |
| `@when` | Когда можно повторить | `~2 мин` |

## The catalogue

| Категория | Сообщение | `id` | Отправка | Лимит | Плейсхолдеры |
| --- | --- | --- | --- | --- | --- |
| Первое знакомство и справка | Экран выбора языка | `language.prompt` | text | 4096 | `@languages` |
| Первое знакомство и справка | Приветствие (/start) | `welcome` | text | 4096 | `@languages`, `@platforms`, `@version` |
| Первое знакомство и справка | Справка (/help) | `help` | text | 4096 | `@platforms`, `@version` |
| Первое знакомство и справка | Поддерживаемые платформы (/platforms) | `platforms` | text | 4096 | `@platforms`, `@count` |
| Первое знакомство и справка | Язык выключен (всплывающее сообщение) | `language.unknown` | text | 4096 | — |
| Навигация и настройки | Режим MP3/MP4 включён | `mode.armed` | text | 4096 | `@mode` |
| Навигация и настройки | Настройки (/settings) | `settings` | text | 4096 | `@quality`, `@format`, `@language`, `@mode` |
| Навигация и настройки | Как пользоваться /setquality | `quality.usage` | text | 4096 | — |
| Навигация и настройки | Неизвестное качество | `quality.invalid` | text | 4096 | `@options` |
| Навигация и настройки | Качество сохранено | `quality.saved` | text | 4096 | `@quality` |
| Навигация и настройки | Как пользоваться /setformat | `format.usage` | text | 4096 | — |
| Навигация и настройки | Неизвестный формат | `format.invalid` | text | 4096 | `@options` |
| Навигация и настройки | Формат сохранён | `format.saved` | text | 4096 | `@format` |
| Навигация и настройки | Как пользоваться /mp3 | `mp3.usage` | text | 4096 | — |
| Навигация и настройки | Как пользоваться /mp4 | `mp4.usage` | text | 4096 | — |
| Навигация и настройки | MP3 скачивается | `mp3.started` | text | 4096 | — |
| Навигация и настройки | MP4 скачивается | `mp4.started` | text | 4096 | — |
| Загрузка | Поиск информации о ссылке | `stage.searching` | text | 4096 | — |
| Загрузка | Начало загрузки | `stage.starting` | text | 4096 | — |
| Загрузка | Карточка превью ссылки | `card` | caption | 1024 | `@title`, `@uploader`, `@channel`, `@followers`, `@duration`, `@views`, `@likes`, `@comments`, `@category`, `@uploaded`, `@source`, `@url`, `@meta` |
| Загрузка | Прогресс загрузки | `progress` | caption | 1024 | `@title`, `@stage`, `@quality`, `@bar`, `@percent`, `@size`, `@speed`, `@eta`, `@elapsed`, `@facts` |
| Загрузка | Задача в очереди | `queued` | caption | 1024 | `@title`, `@position`, `@wait`, `@hint` |
| Загрузка | Подпись к готовому файлу | `done` | caption | 1024 | `@title`, `@quality`, `@size`, `@duration`, `@source` |
| Загрузка | Вопрос о конвертации кодека | `transcode.ask` | caption | 1024 | `@title`, `@question`, `@quality`, `@codec`, `@note` |
| Загрузка | Все форматы больше лимита | `limit.all_too_big` | text | 4096 | `@limit` |
| Загрузка | Видео больше лимита | `limit.video_too_big` | text | 4096 | `@limit` |
| Загрузка | Аудио больше лимита | `limit.audio_too_big` | text | 4096 | `@limit` |
| Загрузка | Файл с несовместимым кодеком | `transcode.document` | text | 4096 | — |
| Загрузка | Качество снижено под лимит | `limit.quality_selected` | text | 4096 | `@quality`, `@skipped` |
| Ошибки и отказы | Ошибка загрузки | `error` | caption | 1024 | `@title`, `@error`, `@retry_hint`, `@url`, `@limit` |
| Ошибки и отказы | Сессия истекла | `download.session_expired` | text | 4096 | — |
| Ошибки и отказы | Уже скачивается | `download.already` | text | 4096 | — |
| Ошибки и отказы | Неожиданный ответ сервиса | `download.unexpected` | text | 4096 | — |
| Ошибки и отказы | Неизвестная ошибка | `download.unknown_error` | text | 4096 | — |
| Ошибки и отказы | Файл не найден | `download.file_missing` | text | 4096 | — |
| Ошибки и отказы | Не удалось отправить файл | `download.upload_failed` | text | 4096 | — |
| Ошибки и отказы | Загрузка превысила лимит времени | `download.timed_out` | text | 4096 | — |
| Ошибки и отказы | У пользователя уже идёт загрузка | `download.busy_user` | text | 4096 | `@when` |
| Ошибки и отказы | Сервис перегружен | `download.busy_service` | text | 4096 | `@when` |
| Ошибки и отказы | Движок недоступен | `engine.down` | text | 4096 | — |
| Ошибки и отказы | Не удалось получить данные ссылки | `engine.info_failed` | text | 4096 | `@error` |
| Ошибки и отказы | Ошибка движка | `engine.error` | text | 4096 | `@error` |
| Ошибки и отказы | Не удалось показать карточку | `card.render_failed` | text | 4096 | — |
| Ошибки и отказы | Ссылка запрещена | `intake.unsafe` | text | 4096 | — |
| Ошибки и отказы | Слишком много ссылок в сообщении | `intake.too_many` | text | 4096 | `@limit` |
| Статистика | Статистика (/stats) | `stats` | text | 4096 | `@uptime`, `@downloads`, `@errors`, `@cached`, `@disk`, `@sessions`, `@version` |
| Тексты кнопок | Кнопка «Назад» | `nav.back` | text | 4096 | — |
| Тексты кнопок | Кнопка «Помощь» | `button.help` | text | 4096 | — |
| Тексты кнопок | Кнопка «Платформы» | `button.platforms` | text | 4096 | — |
| Тексты кнопок | Кнопка «Настройки» | `button.settings` | text | 4096 | — |
| Тексты кнопок | Кнопка «Статистика» | `button.stats` | text | 4096 | — |
| Тексты кнопок | Кнопка «MP3» | `button.mp3` | text | 4096 | — |
| Тексты кнопок | Кнопка «MP4» | `button.mp4` | text | 4096 | — |
| Тексты кнопок | Кнопка «Лучшее качество» | `card.best_quality` | text | 4096 | — |
| Тексты кнопок | Кнопка «Отправить файлом» | `transcode.send_as_file` | text | 4096 | — |
| Тексты кнопок | Кнопка «Конвертировать» | `transcode.convert` | text | 4096 | `@eta` |

## Default templates

These are the texts the bot sends today. They are read-only; a custom
template starts as a copy of them.

### Экран выбора языка (`language.prompt`)

`text` · limit 4096 · i18n: `language.prompt`

#### en

```text
Choose the language of this chat.
```

#### ru

```text
Выберите язык этого чата.
```

### Приветствие (/start) (`welcome`)

`text` · limit 4096 · i18n: `welcome.title`, `welcome.body`, `welcome.platforms`

#### en

```text
Koyura Bot — a quiet downloader.

Send a video or audio link and the file arrives in this chat.

Supported platforms: YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ more.
```

#### ru

```text
Koyura Bot — тихий загрузчик.

Пришлите ссылку на видео или аудио — файл придёт в этот чат.

Поддерживаемые платформы: YouTube, TikTok, Instagram, Twitter, Reddit и ещё 1000+.
```

### Справка (/help) (`help`)

`text` · limit 4096 · i18n: `help.title`, `help.step1`, `help.step2`, `help.step3`, `help.step4`, `help.shortcuts`, `help.mp3`, `help.mp4`, `help.best`, `help.preferences`, `help.setquality`, `help.setformat`, `help.settings`, `help.other`, `help.platforms`, `help.stats`, `help.cancel`, `help.footer`

#### en

```text
*How to use Koyura Bot:*

1. Send a link (YouTube, TikTok, etc.)
2. Pick a format (MP4 or MP3)
3. Pick quality
4. The file arrives

*Shortcuts:*

/mp3 <link> - Direct MP3 download
/mp4 <link> - Best quality MP4
/best <link> - Best available quality

*Preferences:*

/setquality <best/720/480> - Default quality
/setformat <video/audio> - Default format
/settings - View your preferences

*Other:*

/platforms - Supported sites
/stats - Bot stats
/cancel - Cancel (coming soon)

Send several links at once and each one is handled in turn.
```

#### ru

```text
*Как пользоваться Koyura Bot:*

1. Пришлите ссылку (YouTube, TikTok и т.д.)
2. Выберите формат (MP4 или MP3)
3. Выберите качество
4. Файл придёт в этот чат

*Быстрые команды:*

/mp3 <ссылка> - сразу MP3
/mp4 <ссылка> - MP4 лучшего качества
/best <ссылка> - лучшее доступное качество

*Настройки:*

/setquality <best/720/480> - качество по умолчанию
/setformat <video/audio> - формат по умолчанию
/settings - ваши настройки

*Другое:*

/platforms - поддерживаемые сайты
/stats - статистика бота
/cancel - отмена (скоро)

Можно прислать несколько ссылок сразу — бот обработает их по очереди.
```

### Поддерживаемые платформы (/platforms) (`platforms`)

`text` · limit 4096 · i18n: `platforms.header`, `platforms.more`

#### en

```text
Supported platforms:

@platforms
  and 1000+ more via yt-dlp
```

#### ru

```text
Поддерживаемые платформы:

@platforms
  и ещё 1000+ сайтов через yt-dlp
```

### Язык выключен (всплывающее сообщение) (`language.unknown`)

`text` · limit 4096 · i18n: `language.unknown`

#### en

```text
That language is not enabled on this bot.
```

#### ru

```text
Этот язык не включён на этом боте.
```

### Режим MP3/MP4 включён (`mode.armed`)

`text` · limit 4096 · i18n: `mode.armed`

#### en

```text
Mode: @mode
Send the link — it downloads as @mode.
```

#### ru

```text
Режим: @mode
Пришлите ссылку — файл скачается как @mode.
```

### Настройки (/settings) (`settings`)

`text` · limit 4096 · i18n: `settings.title`, `settings.quality`, `settings.format`, `settings.language`, `settings.mode`, `settings.mode_none`, `settings.change`, `settings.setquality`, `settings.setformat`, `settings.language_hint`

#### en

```text
Your preferences:

  Default quality: @quality
  Default format: @format
  Language: @language
  Next link: @mode

Change:
  /setquality <best/720/480>
  /setformat <video/audio>

Tap a language to switch.
```

#### ru

```text
Ваши настройки:

  Качество по умолчанию: @quality
  Формат по умолчанию: @format
  Язык: @language
  Следующая ссылка: @mode

Изменить:
  /setquality <best/720/480>
  /setformat <video/audio>

Нажмите язык, чтобы переключить.
```

### Как пользоваться /setquality (`quality.usage`)

`text` · limit 4096 · i18n: `quality.usage`

#### en

```text
Usage: /setquality <best/1080/720/480/360>
```

#### ru

```text
Использование: /setquality <best/1080/720/480/360>
```

### Неизвестное качество (`quality.invalid`)

`text` · limit 4096 · i18n: `quality.invalid`

#### en

```text
Invalid quality. Options: @options
```

#### ru

```text
Неизвестное качество. Варианты: @options
```

### Качество сохранено (`quality.saved`)

`text` · limit 4096 · i18n: `quality.saved`

#### en

```text
Default quality set to: @quality
```

#### ru

```text
Качество по умолчанию: @quality
```

### Как пользоваться /setformat (`format.usage`)

`text` · limit 4096 · i18n: `format.usage`

#### en

```text
Usage: /setformat <video/audio>
```

#### ru

```text
Использование: /setformat <video/audio>
```

### Неизвестный формат (`format.invalid`)

`text` · limit 4096 · i18n: `format.invalid`

#### en

```text
Invalid format. Options: @options
```

#### ru

```text
Неизвестный формат. Варианты: @options
```

### Формат сохранён (`format.saved`)

`text` · limit 4096 · i18n: `format.saved`

#### en

```text
Default format set to: @format
```

#### ru

```text
Формат по умолчанию: @format
```

### Как пользоваться /mp3 (`mp3.usage`)

`text` · limit 4096 · i18n: `mp3.usage`

#### en

```text
Usage: /mp3 <link>
Or reply to a message containing a link.
```

#### ru

```text
Использование: /mp3 <ссылка>
Или ответьте на сообщение со ссылкой.
```

### Как пользоваться /mp4 (`mp4.usage`)

`text` · limit 4096 · i18n: `mp4.usage`

#### en

```text
Usage: /mp4 <link>
Or reply to a message containing a link.
```

#### ru

```text
Использование: /mp4 <ссылка>
Или ответьте на сообщение со ссылкой.
```

### MP3 скачивается (`mp3.started`)

`text` · limit 4096 · i18n: `mp3.started`

#### en

```text
Downloading MP3...
```

#### ru

```text
Скачиваю MP3...
```

### MP4 скачивается (`mp4.started`)

`text` · limit 4096 · i18n: `mp4.started`

#### en

```text
Downloading MP4 (best quality)...
```

#### ru

```text
Скачиваю MP4 (лучшее качество)...
```

### Поиск информации о ссылке (`stage.searching`)

`text` · limit 4096 · i18n: `stage.searching`

#### en

```text
🔍
```

#### ru

```text
🔍
```

### Начало загрузки (`stage.starting`)

`text` · limit 4096 · i18n: `stage.starting`

#### en

```text
Starting…
```

#### ru

```text
Запускаю…
```

### Карточка превью ссылки (`card`)

`caption` · limit 1024 · i18n: `card.untitled`, `card.duration`, `card.followers`, `card.category`, `card.uploaded`, `card.views`, `card.likes`, `card.comments`, `card.via`

#### en

```text
*@title*

@channel
Duration: @duration
Followers: @followers
@views views · @likes likes · @comments comments
Category: @category
Uploaded: @uploaded
```

#### ru

```text
*@title*

@channel
Длительность: @duration
Подписчиков: @followers
@views просмотров · @likes лайков · @comments комментариев
Категория: @category
Загружено: @uploaded
```

### Прогресс загрузки (`progress`)

`caption` · limit 1024 · i18n: `stage.starting`, `stage.downloading`, `stage.processing`, `stage.uploading`, `stage.converting`, `progress.eta`, `progress.elapsed`

#### en

```text
*@title*
@stage
@bar
```

#### ru

```text
*@title*
@stage
@bar
```

### Задача в очереди (`queued`)

`caption` · limit 1024 · i18n: `queue.hint`, `queue.head`, `queue.head_number`, `queue.estimated`

#### en

```text
*@title*
@position
@hint
```

#### ru

```text
*@title*
@position
@hint
```

### Подпись к готовому файлу (`done`)

`caption` · limit 1024 · i18n: `card.download`, `card.via`, `transcode.document`, `limit.quality_selected`

#### en

```text
*@title*
@quality · @size · @duration
@source
```

#### ru

```text
*@title*
@quality · @size · @duration
@source
```

### Вопрос о конвертации кодека (`transcode.ask`)

`caption` · limit 1024 · i18n: `transcode.ask`, `transcode.note`

#### en

```text
*@title*
@question
@note
```

#### ru

```text
*@title*
@question
@note
```

### Все форматы больше лимита (`limit.all_too_big`)

`text` · limit 4096 · i18n: `limit.all_too_big`

#### en

```text
All available formats are over the send limit (@limit). Try another link or MP3.
```

#### ru

```text
Все доступные форматы больше лимита отправки (@limit). Попробуйте другую ссылку или MP3.
```

### Видео больше лимита (`limit.video_too_big`)

`text` · limit 4096 · i18n: `limit.video_too_big`

#### en

```text
This video is too large to send (limit @limit).
```

#### ru

```text
Это видео слишком большое для отправки (лимит @limit).
```

### Аудио больше лимита (`limit.audio_too_big`)

`text` · limit 4096 · i18n: `limit.audio_too_big`

#### en

```text
This audio is too large to send (limit @limit).
```

#### ru

```text
Это аудио слишком большое для отправки (лимит @limit).
```

### Файл с несовместимым кодеком (`transcode.document`)

`text` · limit 4096 · i18n: `transcode.document`

#### en

```text
Sent as a file: this codec plays as a video only on some phones.
```

#### ru

```text
Отправлено файлом: этот кодек проигрывается как видео не на всех телефонах.
```

### Качество снижено под лимит (`limit.quality_selected`)

`text` · limit 4096 · i18n: `limit.quality_selected`

#### en

```text
Selected @quality: @skipped does not fit the send limit
```

#### ru

```text
Выбрано @quality: @skipped не влезает в лимит
```

### Ошибка загрузки (`error`)

`caption` · limit 1024 · i18n: `download.failed`, `download.unknown_error`, `download.retry_hint`

#### en

```text
*@title*
@error
@retry_hint
```

#### ru

```text
*@title*
@error
@retry_hint
```

### Сессия истекла (`download.session_expired`)

`text` · limit 4096 · i18n: `download.session_expired`

#### en

```text
Session expired. Please send the link again.
```

#### ru

```text
Сессия истекла. Пришлите ссылку заново.
```

### Уже скачивается (`download.already`)

`text` · limit 4096 · i18n: `download.already`

#### en

```text
Already downloading — wait for the file.
```

#### ru

```text
Уже скачивается — дождитесь файла.
```

### Неожиданный ответ сервиса (`download.unexpected`)

`text` · limit 4096 · i18n: `download.unexpected`

#### en

```text
Download service returned an unexpected answer.
```

#### ru

```text
Сервис загрузки ответил неожиданно.
```

### Неизвестная ошибка (`download.unknown_error`)

`text` · limit 4096 · i18n: `download.unknown_error`

#### en

```text
Unknown error
```

#### ru

```text
Неизвестная ошибка
```

### Файл не найден (`download.file_missing`)

`text` · limit 4096 · i18n: `download.file_missing`

#### en

```text
File not found after download.
```

#### ru

```text
Файл не найден после загрузки.
```

### Не удалось отправить файл (`download.upload_failed`)

`text` · limit 4096 · i18n: `download.upload_failed`

#### en

```text
Failed to upload file to Telegram.
```

#### ru

```text
Не удалось отправить файл в Telegram.
```

### Загрузка превысила лимит времени (`download.timed_out`)

`text` · limit 4096 · i18n: `download.timed_out`

#### en

```text
Download timed out.
```

#### ru

```text
Загрузка превысила лимит времени.
```

### У пользователя уже идёт загрузка (`download.busy_user`)

`text` · limit 4096 · i18n: `download.busy_user`

#### en

```text
You already have a download running. Try again in @when.
```

#### ru

```text
У вас уже идёт загрузка. Повторите через @when.
```

### Сервис перегружен (`download.busy_service`)

`text` · limit 4096 · i18n: `download.busy_service`

#### en

```text
The service is overloaded right now — try again in @when.
```

#### ru

```text
Сервис перегружен — повторите через @when.
```

### Движок недоступен (`engine.down`)

`text` · limit 4096 · i18n: `engine.down`

#### en

```text
Download service temporarily unavailable.
```

#### ru

```text
Сервис загрузки временно недоступен.
```

### Не удалось получить данные ссылки (`engine.info_failed`)

`text` · limit 4096 · i18n: `engine.info_failed`

#### en

```text
Failed to fetch info: @error
```

#### ru

```text
Не удалось получить данные: @error
```

### Ошибка движка (`engine.error`)

`text` · limit 4096 · i18n: `engine.error`

#### en

```text
Error: @error
```

#### ru

```text
Ошибка: @error
```

### Не удалось показать карточку (`card.render_failed`)

`text` · limit 4096 · i18n: `card.render_failed`

#### en

```text
Could not render this link. Try /mp4 or /mp3 instead.
```

#### ru

```text
Не удалось показать эту ссылку. Попробуйте /mp4 или /mp3.
```

### Ссылка запрещена (`intake.unsafe`)

`text` · limit 4096 · i18n: `intake.unsafe`

#### en

```text
I can only download from public http(s) links. That link was skipped because it is not allowed.
```

#### ru

```text
Я качаю только с публичных ссылок http(s). Эта ссылка пропущена: такой адрес запрещён.
```

### Слишком много ссылок в сообщении (`intake.too_many`)

`text` · limit 4096 · i18n: `intake.too_many`

#### en

```text
Only the first @limit links of a message are processed.
```

#### ru

```text
Обрабатываются только первые @limit ссылок из сообщения.
```

### Статистика (/stats) (`stats`)

`text` · limit 4096 · i18n: `stats.title`, `stats.uptime`, `stats.downloads`, `stats.errors`, `stats.cached`, `stats.disk`, `stats.sessions`

#### en

```text
Koyura Bot Stats:

  Uptime: @uptime
  Downloads: @downloads
  Errors: @errors
  Cached files: @cached
  Disk usage: @disk
  Active sessions: @sessions
```

#### ru

```text
Статистика Koyura Bot:

  Аптайм: @uptime
  Загрузок: @downloads
  Ошибок: @errors
  Файлов в кэше: @cached
  Занято на диске: @disk
  Активных сессий: @sessions
```

### Кнопка «Назад» (`nav.back`)

`text` · limit 4096 · i18n: `nav.back`

#### en

```text
Back
```

#### ru

```text
Назад
```

### Кнопка «Помощь» (`button.help`)

`text` · limit 4096 · i18n: `button.help`

#### en

```text
Help
```

#### ru

```text
Помощь
```

### Кнопка «Платформы» (`button.platforms`)

`text` · limit 4096 · i18n: `button.platforms`

#### en

```text
Platforms
```

#### ru

```text
Платформы
```

### Кнопка «Настройки» (`button.settings`)

`text` · limit 4096 · i18n: `button.settings`

#### en

```text
Settings
```

#### ru

```text
Настройки
```

### Кнопка «Статистика» (`button.stats`)

`text` · limit 4096 · i18n: `button.stats`

#### en

```text
Stats
```

#### ru

```text
Статистика
```

### Кнопка «MP3» (`button.mp3`)

`text` · limit 4096 · i18n: `button.mp3`

#### en

```text
MP3
```

#### ru

```text
MP3
```

### Кнопка «MP4» (`button.mp4`)

`text` · limit 4096 · i18n: `button.mp4`

#### en

```text
MP4
```

#### ru

```text
MP4
```

### Кнопка «Лучшее качество» (`card.best_quality`)

`text` · limit 4096 · i18n: `card.best_quality`

#### en

```text
Best quality
```

#### ru

```text
Лучшее качество
```

### Кнопка «Отправить файлом» (`transcode.send_as_file`)

`text` · limit 4096 · i18n: `transcode.send_as_file`

#### en

```text
Send as file
```

#### ru

```text
Отправить файлом
```

### Кнопка «Конвертировать» (`transcode.convert`)

`text` · limit 4096 · i18n: `transcode.convert`, `transcode.convert_no_eta`

#### en

```text
Convert (@eta)
```

#### ru

```text
Конвертировать (@eta)
```

## When something goes wrong

* **Unknown placeholder** — the panel refuses to save and names the
  placeholder and the ones this message offers.
* **Empty body** — same: a template has to say something.
* **Too long** — saved, with a warning in the editor; the bot cuts the
  tail with `…`.
* **Broken MarkdownV2** — the message goes out as literal text.
* **The panel is down** — the bot keeps its last settings for a while and
  then renders the built-in defaults; nothing is lost and no message is
  skipped.
* **An empty value** — the placeholder becomes an empty string and its
  line disappears.
