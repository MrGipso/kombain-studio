# Комбайн Studio — заметки для разработки

## Архитектура (веб-версия)

```
frontend/  React 19 + TS + Vite  → собирается в backend/static (vite build)
backend/   FastAPI
  routers/   HTTP API по ресурсам (projects, script, scenes, shots, assets,
             sfx, music, export, media, settings, diagnostics)
  services/  jobs (durable-реестр задач, SQLite, локи проект+ресурс),
             runlog (JSONL-журнал строк лога в <проект>/logs/),
             sse_manager, pipeline_orch (оркестрация этапов),
             material_engine (unattended retry/checkpoint/fallback)
core/      чистая логика без UI: project (стейт этапов, атомарная запись),
           board, scenes, shot_planning/shots, transcribe, lm,
           comfy/comfy_generation, script_align, style_presets, stock,
           reference, kenburns, export(+profiles), media_qc, sfx,
           asset_variants, validation, production (план/preflight), models
launch.py  нативное окно над тем же FastAPI/React runtime
main.py    совместимый алиас launch.py
```

**Источники правды проекта** — версионируемые stage-артефакты (`00_script.*`,
transcript, scenes, shots, `06_asset_board.json`, timeline), `.project.json`
для метаданных и `.pipeline_manifest.json` для freshness/DAG-состояния.
Asset Board остаётся монтажным источником выбранных медиа, а
`.asset_variants.json` — неизменяемой историей их происхождения.

Mutable runtime state веб-запуска хранится вне исходников, по умолчанию в
`%LOCALAPPDATA%\PipelineStudio`; пути задаются `PIPELINE_SETTINGS_PATH` и
`PIPELINE_JOBS_DB`. Web runtime держит межпроцессные instance-lock для settings
directory и канонического `output_base`: второй server/worker с теми же
проектами не запускается, пока нет отдельной distributed queue/lease реализации.
При смене `output_base` через Settings новый lock захватывается до сохранения
настройки, а прежний освобождается только после успешной записи; неудачная смена
откатывает новый lock и сохраняет защиту исходного каталога.

## Конвейер

Готовый сценарий или рерайт (yt-dlp/SRT/VTT) → выбор готового голоса или расшифровка собственного референса → пакетная озвучка OmniVoice → транскрипция (whisper.cpp) → сцены (авторские биты
выравниваются как главы, затем режутся на монтажные сцены по реальным
таймкодам; фолбэк — эмбеддинг-сегментация) → шот-лист (LLM предлагает
визуальные концепты, детерминированный scheduler назначает финальные интервалы)
→ Asset Board
(сток/KB/AI/reference, фазовая очередь) → звук (музыка ACE-Step, SFX Woosh)
→ QC (vision-QA, media_qc, слабые места на Preview) → экспорт (профили,
версионирование, immutable Master manifest с provenance/rights/edit contract).

Границы сцен имеют детерминированный authority chain: валидно выровненные
авторские биты → проверенные embedding-векторы → constrained dynamic
partitioning. Оптимизатор сохраняет непрерывность и полный порядок сегментов,
сначала минимизирует нарушения `seg_min_seconds`/`seg_max_seconds`, затем
приближает `seg_target_scenes` и выбирает сильные смысловые границы. Любой
LLM-fallback проходит финальную проверку exact coverage; `voice_text` и
таймкоды никогда не принимаются из ответа модели. Творческие поля запрашиваются
отдельно через JSON Schema пакетами до 8 сцен/12 000 символов с проверкой
индексов и письменности заголовков. Embedding-модель выгружается до загрузки
chat-модели, чтобы обе модели не конкурировали за VRAM.

## Проверки перед коммитом

```
check.bat        # lockfile + reproducible SBOM + pytest + lint + production build
```
Фронтенд пересобрать: `cd frontend && npm run build` (кладёт в backend/static).
Генератор `tools/generate_sbom.py` работает полностью офлайн и атомарно пишет
CycloneDX JSON из `pyproject.toml`, `uv.lock` и `frontend/package-lock.json`.
Подробный release-процесс описан в `docs/RELEASE.md`.

### Локальный integration preflight

Перед рабочей сессией с генерацией и перед релизом запустите пассивный smoke
для обязательных локальных зависимостей:

```powershell
.\.venv\Scripts\python.exe tools\integration_smoke.py --targets ffmpeg whisper comfy --require ffmpeg whisper comfy
```

Поддерживаемые targets: `ffmpeg`, `whisper`, `comfy`, `lm`, `embed`, `woosh`.
По умолчанию работает пассивный режим. Проверки выполняются параллельно с
путями и URL из локальных настроек, но не запускают сервисы, не отправляют
inference-запросы, не вызывают JIT-загрузку моделей LM Studio, не изменяют
проекты и не ставят генерацию в очередь. Пассивные проверки `lm` и `embed`
читают `/v1/models`: они подтверждают доступность сервера и наличие настроенных
моделей среди загруженных, но не проверяют генерацию. Для `comfy` утилита читает
`/object_info` и проверяет совместимость типов нод всех пяти поставляемых
workflows, не создавая render jobs.

Активная проверка chat и embeddings включается только явно:

```powershell
.\.venv\Scripts\python.exe tools\integration_smoke.py --targets lm embed --require lm embed --active
```

`--active` отправляет минимальные запросы в `/chat/completions` и `/embeddings`.
LM Studio при этом может JIT-загрузить модели, занять значительный объём VRAM и
конфликтовать с ComfyUI. Сначала освободите VRAM, не запускайте активный smoke
параллельно с render job и после проверки проконтролируйте резидентные модели,
если освобождение GPU-памяти критично. Короткий TTL уменьшает время ожидания
автовыгрузки, но не превращает активную проверку в пассивную.

Параметр `--output artifacts\integration-smoke.json` атомарно сохраняет JSON,
который также выводится в stdout. Код завершения `0` означает, что все targets
из `--require` доступны; ошибки необязательных targets при этом информационные.
Код `1` означает недоступность хотя бы одного обязательного target, а `2` —
ошибку аргументов командной строки.

### Golden E2E

Windows acceptance от реального HTTP API до Master запускается отдельно от
быстрых тестов:

```powershell
.\.venv\Scripts\python.exe tools\golden_e2e.py
```

Harness использует отдельные settings/output/jobs/instance lock, синтезирует
короткий WAV через System.Speech и сохраняет evidence в `.pytest-tmp/golden-*`.
LM/Comfy/stock/Woosh не нужны: LM отвечает детерминированным offline stub, а
production heuristic fallback проходит сцены и шоты. Пользовательские проекты
и настройки не изменяются.

## Legacy-интерфейсы удалены

Tk-монолит `content_pipeline_gui.py`, PySide-интерфейс (`ui/`,
`content_pipeline_qt.py`, extra `desktop`, его `core.pipeline` и локализации
`core.locale` + `presets/*/locale/`), слой `plugins/` с `core.registry`, а также
`video_joiner_gui.py` и `schema_generator.py` удалены. Их логика живёт в `core/*` (`lm`, `scenes`, `shot_planning`/`shots`, `board`,
`comfy`/`comfy_generation`, `transcribe`), единственный интерфейс — FastAPI/React
в браузере или в окне `launch.py`.

Subprocess architecture-тест поднимает весь `backend.main` с запретом
`tkinter` и `PySide6`; тесты `core.scenes` и `core.shot_planning` дополнительно
запрещают импорт `backend`.

Правило: новый production-код обращается только к `core/*`; `core` не
импортирует `backend` и UI-тулкиты.

## Границы web deployment

Текущий режим — локальный single-user сервер на `127.0.0.1`. TrustedHost,
origin guard, ограниченные media/upload paths и optimistic concurrency защищают
локальный UI, но не заменяют authentication/RBAC. Для LAN/SaaS нужны отдельные
workers, межпроцессная очередь/локи, object storage, auth, quotas и audit policy.

То же ограничение действует для LM Studio. В RC его OpenAI-compatible endpoint
должен слушать только `127.0.0.1`, а приложение — использовать
`http://127.0.0.1:1234/v1`. CORS и sensitive request/prompt logging должны быть
выключены. Endpoint нельзя публиковать в LAN, overlay-сети или Internet без
отдельного аутентифицирующего proxy и сетевого allowlist.

## Известные договорённости

- Python ≥3.12 (поддерживаемый и тестируемый baseline; CI — 3.12 и 3.13).
- Все негативы стилей идут только в LTX-проход (Krea зануляет негатив).
- Долгие задачи: `jobs.project_job(project_id, имя, resource=...)` +
  `jobs.checking_logger(runlog.tee(...), cancel_event)` — лок, отмена и
  журнал в одном месте.
