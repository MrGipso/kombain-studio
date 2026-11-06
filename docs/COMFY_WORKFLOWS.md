# Свои ComfyUI workflow: контракт маркеров ENGINE_*

Любой из workflow движка — Krea, Ideogram, LTX, LTX draft, MiniMax H3, ACE-Step
и canonical reference — можно заменить своим, в том числе собранным на облачных
API-нодах ComfyUI. Код править не нужно: укажите путь к файлу в настройках
(`comfy_krea_workflow`, `comfy_ideogram_workflow`, `comfy_ltx_workflow`,
`comfy_ltx_draft_workflow`, `comfy_h3_workflow`, `comfy_ace_workflow`,
`comfy_reference_workflow`) и отметьте в нём узлы, которые движок заполняет.

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

### MiniMax H3 (видео из картинки)

Используется вместо LTX, когда в *Настройки → Генерация материалов →
Видео-движок* выбран MiniMax H3 (`video_engine = "minimax_h3"`). Тогда
диагностика и preflight проверяют этот workflow, а LTX-workflow пропускают, и
наоборот. Бандл `workflows/minimax_h3_i2v_api.json` — официальный шаблон
ComfyUI «MiniMax H3 Image to Video» в API-формате с проставленными маркерами.
Неразмеченный экспорт того же шаблона тоже подойдёт: id его узлов указаны в
столбце «id-фолбэк».

| Маркер | Обяз. | Что пишет движок | Входы-кандидаты | id-фолбэк |
|---|---|---|---|---|
| `ENGINE_INPUT_IMAGE` | да | имя загруженной в ComfyUI картинки кадра (первый кадр) | `image` | `114.image` |
| `ENGINE_PROMPT` | да | структурированный промпт H3 (см. ниже) | `text`, `value`, `prompt`, `prompt_text` | `105:104.prompt` |
| `ENGINE_FRAMES` | нет | длина клипа **в кадрах** при 24 fps на сетке 17k+5, 124–362 | `length`, `frames`, `num_frames`, `value` | `105:104.length` |
| `ENGINE_WIDTH` | нет | ширина: 1344, 768 или 1024 по пропорциям картинки | `width`, `value` | `105:104.width` |
| `ENGINE_HEIGHT` | нет | высота: 768, 1344 или 1024 | `height`, `value` | `105:104.height` |
| `ENGINE_TURBO` | нет | `true` для черновика (турбо-LoRA, 8 шагов), `false` для финала | `value`, `boolean`, `enabled` | `105:126.value` |
| `ENGINE_OUTPUT` | да | `content_pipeline/video/shot_NNN_minimax_h3` | `filename_prefix` | `92` |
| `ENGINE_SEED` | нет | новый сид | `seed`, `noise_seed`, `sampling_mode.seed` | — |

- **Длина.** Длина слота округляется вверх до сетки 17k+5: 124 кадра (≈5,2 с)
  минимум, 362 (≈15,1 с) — верх обученного диапазона ноды. Слот короче 5 с
  всё равно получает 124 кадра, монтаж обрезает лишнее.
- **Разрешение.** Open weights рендерят 768p, а H3 следует пропорциям первого
  кадра: горизонтальная картинка → 1344×768, вертикальная → 768×1344, близкая
  к квадрату → 1024×1024. Настройка «LTX: разрешение» на H3 не влияет.
- **Черновик.** Политика `ltx_draft_mode` (off/auto/all) общая для обоих
  движков. Для H3 черновик — это турбо-LoRA; «Финал» перерендеривает клип
  полными 20 шагами. Workflow без `ENGINE_TURBO` всегда рендерит со своими
  настройками и черновиков не даёт.
- **VRAM.** H3 (трансформер 33B плюс текстовый энкодер Qwen3-VL-32B) не делит
  видеопамять с Krea, LTX и ACE-Step. При переходе на H3 и обратно движок
  вызывает `POST /free`, а серия H3-клипов подряд модель не перезагружает.
- **Промпт.** Движок сам собирает формат I2VA из официального гайда MiniMax
  ([VIDEO_PROMPT_WRITING_GUIDE_base_en.md](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md)):

  ```text
  For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

  integrated_multimodal_description: [Shot 1] The video opens exactly on the first frame: <первое предложение image_prompt>. <video_prompt кадра> It is one continuous shot from start to end, ...

  overall_soundscape: The only sound is <sfx_query кадра или тихий фон места>.

  non_diegetic_music: N/A
  ```

  Один `[Shot 1]` без тайм-кода даёт один непрерывный план: без описания
  единственного плана H3 склеивает несколько. Двойные кавычки и теги `<…>`
  из текста планировщика вырезаются: в H3 текст в кавычках рисуется на
  экране, а теги — служебные токены. Негатива у H3 нет (cfg 1), поэтому
  стилевой негатив не передаётся. Звук клипа экспорт всё равно выбрасывает,
  поле `overall_soundscape` заполняется, чтобы модель не придумывала речь.
- **Правила для LLM.** При H3 планировщик кадров пишет `video_prompt` по
  правилам из того же гайда: начало действия → развитие → результат, одно
  движение камеры словами H3 (`push in`, `pan left`, `truck right`, `tilt up`,
  `arc shot`…, с `with small amplitude` / `at slow speed`), без склеек, реплик
  и экранного текста. Уже спланированные кадры при смене движка не
  перепланируются: их `video_prompt` оборачивается в формат H3 при рендере.

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
- **Авторизация: ключ comfy.org.** Движок отправляет workflow через HTTP
  `POST /prompt`. При таком запуске partner/API-ноды ComfyUI берут ключ
  аккаунта comfy.org из `extra_data.api_key_comfy_org`
  ([документация](https://docs.comfy.org/development/comfyui-server/api-key-integration)).
  Браузерный интерфейс ComfyUI подставляет ключ сам, движку его нужно задать:
  1. Создайте API-ключ в аккаунте comfy.org (на platform.comfy.org) и
     пополните баланс кредитов: облачные ноды платные.
  2. Вставьте ключ в *Настройки → Модели и сервисы → «API-ключ comfy.org»*
     (поле `comfy_api_key`) и сохраните.

  После этого движок добавляет ключ к каждому `POST /prompt`: картинки,
  reference, LTX и музыка. Без ключа payload такой же, как раньше, только
  `prompt` и `client_id`. Если ключ не задан или неверен, облачная нода
  завершится ошибкой авторизации (например, «Sign In Required»), и текст
  ошибки попадёт в ошибку задачи. Проверка контракта и диагностика ключ не
  проверяют: они ничего не ставят в очередь.
- **Где хранится ключ.** Ключ — секрет, как `lm_api_key`. `GET /settings`
  отдаёт вместо него пустую строку и флаг `comfy_api_key_set`, а пустое поле
  при сохранении оставляет прежний ключ. Ключ не входит в `settings_hash`
  вариантов, хеши стадий и манифесты экспорта, и смена ключа не делает
  стадии устаревшими. Из ошибок постановки в очередь он вырезается (`***`),
  поэтому в ошибки задач и run log не попадает. В открытом виде ключ лежит
  только в файле настроек (`content_pipeline_settings.json`).
- **Куда уходит ключ.** Только на сервер из `comfy_url`, в теле запроса
  вместе с workflow. Если ComfyUI работает на другой машине (ZeroTier,
  `--listen 0.0.0.0`), ключ идёт по сети открытым HTTP. Подключайтесь только
  к своему ComfyUI по доверенной сети.
