# Свои ComfyUI workflow: контракт маркеров ENGINE_*

Любой из workflow движка — Krea, Ideogram, LTX, LTX draft, ACE-Step и
canonical reference — можно заменить своим, в том числе собранным на облачных
API-нодах ComfyUI. Код править не нужно: укажите путь к файлу в настройках
(`comfy_krea_workflow`, `comfy_ideogram_workflow`, `comfy_ltx_workflow`,
`comfy_ltx_draft_workflow`, `comfy_ace_workflow`, `comfy_reference_workflow`)
и отметьте в нём узлы, которые движок заполняет.

Реализация — `core/comfy_contract.py`. Подстановку выполняет
`core/comfy_generation.py`, проверку — `core/comfy.py`.

## Как подготовить файл

1. Соберите workflow в ComfyUI и экспортируйте его в **API-формате**
   (*Workflow → Export (API)*). Обычный UI-формат с `nodes`/`links` не подходит.
2. Переименуйте нужные узлы (*Title* / двойной клик по заголовку), добавив в
   заголовок маркер. В JSON это поле `_meta.title`.
3. Запустите проверку «ComfyUI» в диагностике. Она читает `/object_info`,
   проверяет типы узлов и контракт маркеров и ничего не ставит в очередь.
   Ту же проверку для нужных workflow выполняет preflight кнопки «Материал».

## Синтаксис маркеров

- Маркер можно поставить в любом месте заголовка, рядом с обычным текстом:
  `ENGINE_PROMPT | User prompt` или `Positive · ENGINE_PROMPT`. Регистр не важен.
- В одном заголовке может быть несколько маркеров. Это нужно для облачных
  нод, которые принимают сразу промпт, негатив и длительность:
  `Kling i2v ENGINE_PROMPT ENGINE_NEGATIVE ENGINE_DURATION`.
- По умолчанию движок пишет в первый вход из списка кандидатов роли (см.
  таблицы ниже). Вход можно указать явно: `ENGINE_DURATION:duration_seconds`,
  `ENGINE_PROMPT:prompt_text`.
- Вход должен быть **значением**, а не соединением. Если `text` у
  `CLIPTextEncode` подключён к другому узлу, ставьте маркер на узел-источник
  (например, `PrimitiveStringMultiline`).
- Тип значения проверяется: в текстовые роли пишется строка, в числовые — число.
  Combo-входы (например, длительность `"5"`/`"10"` у части облачных нод) движок
  заполнить не может, такие узлы не отмечайте.
- Две роли не могут указывать на один и тот же вход.
- Неизвестные маркеры (опечатки вроде `ENGINE_PROMT`) и маркеры, которые этот
  workflow не использует, попадают в предупреждения отчёта и в текст ошибки.

## Маркеры или старые id

- Если в workflow есть хотя бы один маркер его контракта (кроме `ENGINE_SEED`),
  он разбирается **только по маркерам**: обязательные роли должны быть
  отмечены, необязательные без маркера пропускаются, id узлов не используются.
- Если маркеров нет, движок берёт id узлов бандл-workflow (столбец «id-фолбэк»).
  Так продолжают работать старые копии файлов из `workflows/` и уже
  сохранённые пути. Если таких id в файле нет или там не те входы, проверка
  сообщит, каких узлов не хватает и какие маркеры нужно проставить.
- Для canonical reference фолбэка на id нет: его контракт всегда был маркерным.

Бандл-workflow в `workflows/` уже размечены. Их payload для ComfyUI совпадает
с прежним по всем `inputs`, кроме случайного сида. Отличаются только заголовки
`_meta.title`, которые ComfyUI при исполнении не читает.

## Роли по workflow

Столбец «обяз.» показывает, обязателен ли маркер. Без необязательного маркера
движок оставляет значение из файла.

### Krea, Ideogram (картинки)

| Маркер | Обяз. | Что пишет движок | Входы-кандидаты | id-фолбэк Krea / Ideogram |
|---|---|---|---|---|
| `ENGINE_PROMPT` | да | промпт кадра | `text`, `value`, `prompt`, `prompt_text` | `30:19.value` / `98:24.text` |
| `ENGINE_OUTPUT` | да | `content_pipeline/shot_NNN_krea2` или `_ideogram4` | `filename_prefix` | `29` / `158` |
| `ENGINE_SEED` | нет | новый сид | `seed`, `noise_seed`, `sampling_mode.seed` | — |

### LTX и LTX draft (видео из картинки)

| Маркер | Обяз. | Что пишет движок | Входы-кандидаты | id-фолбэк |
|---|---|---|---|---|
| `ENGINE_INPUT_IMAGE` | да | имя загруженной в ComfyUI картинки кадра | `image` | `269.image` |
| `ENGINE_PROMPT` | да | видеопромпт | `text`, `value`, `prompt`, `prompt_text` | `267:266.value` |
| `ENGINE_NEGATIVE` | нет | дописывает негатив видеостиля через запятую | `negative_prompt`, `negative`, `text`, `value`, `prompt` | `267:247.text` |
| `ENGINE_DURATION` | нет | длительность клипа, 2–15 с (дробное число) | `duration`, `duration_seconds`, `seconds`, `value` | `267:290.value` |
| `ENGINE_WIDTH` | нет | ширина из настройки «LTX: разрешение» | `width`, `value` | `267:257.value` |
| `ENGINE_HEIGHT` | нет | высота из той же настройки | `height`, `value` | `267:258.value` |
| `ENGINE_OUTPUT` | да | `content_pipeline/video/shot_NNN_ltx2_3` | `filename_prefix` | `273` |
| `ENGINE_SEED` | нет | новый сид | `seed`, `noise_seed`, `sampling_mode.seed` | — |

`ENGINE_NEGATIVE`, `ENGINE_DURATION`, `ENGINE_WIDTH` и `ENGINE_HEIGHT` можно
поставить на несколько узлов, значение запишется в каждый. Без
`ENGINE_DURATION` длина клипа определяется самим workflow. Если у облачной
ноды диапазон длительности уже 2–15 с, ComfyUI отклонит значение вне диапазона.
В таком случае не ставьте маркер или ограничьте значение узлом-калькулятором.

### ACE-Step (музыка)

| Маркер | Обяз. | Что пишет движок | Входы-кандидаты | id-фолбэк |
|---|---|---|---|---|
| `ENGINE_PROMPT` | да | теги стиля / описание музыки | `tags`, `text`, `value`, `prompt`, `prompt_text` | `94.tags` |
| `ENGINE_LYRICS` | нет | текст песни, по умолчанию `[instrumental]` | `lyrics`, `text`, `value` | `94.lyrics` |
| `ENGINE_BPM` | нет | темп, если он задан | `bpm`, `value` | `94.bpm` |
| `ENGINE_DURATION` | нет | длина трека в секундах, 15–120 | `duration`, `seconds`, `duration_seconds`, `value` | `94.duration`, `98.seconds` |
| `ENGINE_OUTPUT` | да | `content_pipeline/music/track` | `filename_prefix` | `107` |
| `ENGINE_SEED` | нет | новый сид | `seed`, `noise_seed`, `sampling_mode.seed` | — |

В бандл-файле узел `94` несёт сразу четыре маркера:
`ENGINE_PROMPT ENGINE_LYRICS ENGINE_BPM ENGINE_DURATION`.

Для неразмеченных файлов движок, как и раньше, ставит `steps = 8` узлу `3`
(турбо-чекпойнт ACE-Step). Размеченному workflow движок доверяет: шаги
сэмплера берутся из файла.

### Canonical reference

| Маркер | Обяз. | Что пишет движок | Входы-кандидаты |
|---|---|---|---|
| `ENGINE_REFERENCE_IMAGE` | да | загруженный канонический кадр | `image` |
| `ENGINE_PROMPT` | да | промпт кадра | `text`, `value`, `prompt`, `prompt_text` |
| `ENGINE_OUTPUT` | да | `content_pipeline/shot_NNN_reference` | `filename_prefix` |
| `ENGINE_SEED` | нет | новый сид | `seed`, `noise_seed`, `sampling_mode.seed` |

Подробнее о режимах reference — в [CONTEXT_STORYBOARD.md](CONTEXT_STORYBOARD.md).

## Сиды

Если в workflow нет `ENGINE_SEED`, перед каждым рендером новый сид получают
все входы `seed`, `noise_seed` и `sampling_mode.seed`, как и раньше. Если хотя
бы один узел отмечен `ENGINE_SEED`, меняются только отмеченные узлы, остальные
сиды остаются как в файле. `ENGINE_SEED` работает и в неразмеченной копии
бандл-workflow: режим id-фолбэка он не выключает.

Сиды генерируются в диапазоне ниже 2³¹. Часть облачных API-нод объявляет
32-битный сид и отклоняет большие значения.

## Выходы

`ENGINE_OUTPUT` задаёт только `filename_prefix`. Сам файл движок забирает из
истории ComfyUI: последний результат нужного типа. Для картинок это `images`,
для видео — `videos`, `gifs` или `images`, для музыки — `audio`. Поэтому
отмечайте сохраняющий узел (`SaveImage`, `SaveVideo`, `SaveAudioMP3` и т.п.).

## Облачные API-ноды: что важно знать

- Контракт маркеров одинаков для локальных и облачных нод. Облачная нода
  обычно принимает промпт, негатив и длительность сама, поэтому маркеры
  ставятся в её заголовок вместе. Входная картинка остаётся узлом `LoadImage`
  с `ENGINE_INPUT_IMAGE`, а результат — узлом `SaveImage`/`SaveVideo`.
- **Ограничение: авторизация.** Движок отправляет workflow через HTTP
  `POST /prompt`. При таком запуске partner/API-ноды ComfyUI требуют ключ
  аккаунта comfy.org в `extra_data.api_key_comfy_org`. Браузерный интерфейс
  ComfyUI подставляет ключ сам, а движок пока его не передаёт. Пока это не
  добавлено, облачные ноды при рендере из движка завершатся ошибкой
  авторизации («Sign In Required»). Проверка контракта и диагностика при этом
  проходят: они ничего не запускают.
