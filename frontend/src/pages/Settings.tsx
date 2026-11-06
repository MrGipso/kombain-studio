import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import ThemeToggle from '../components/ThemeToggle'
import type { Settings as SettingsType } from '../api/types'

type DiagTarget = 'lm' | 'embed' | 'comfy' | 'ffmpeg' | 'whisper' | 'omnivoice' | 'woosh'
const SECRET_FIELDS = new Set<keyof SettingsType>(['lm_api_key', 'comfy_api_key', 'pexels_api_key', 'pixabay_api_key'])
// Fields that only mean something for one LLM provider.
const CLOUD_ONLY_FIELDS = new Set<keyof SettingsType>(['lm_api_key'])
const LMSTUDIO_ONLY_FIELDS = new Set<keyof SettingsType>(['lm_context'])
const DIAG_TARGETS: { key: DiagTarget; label: string }[] = [
  { key: 'lm', label: 'LLM' },
  { key: 'embed', label: 'Embeddings' },
  { key: 'comfy', label: 'ComfyUI' },
  { key: 'ffmpeg', label: 'FFmpeg' },
  { key: 'whisper', label: 'Whisper' },
  { key: 'omnivoice', label: 'OmniVoice' },
  { key: 'woosh', label: 'Woosh SFX' },
]

export default function Settings() {
  const navigate = useNavigate()
  const toast = useToast()
  const [settings, setSettings] = useState<SettingsType | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [diag, setDiag] = useState<Record<string, api.DiagnosticResult & { loading?: boolean }>>({})

  useEffect(() => {
    api.getSettings().then(setSettings).catch((e) => toast.error(`Не удалось загрузить настройки: ${(e as Error).message}`))
  }, [toast])

  const handleSave = async () => {
    if (!settings) return
    setSaving(true)
    try {
      await api.updateSettings(settings)
      setSaved(true)
      toast.success('Настройки сохранены')
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      toast.error('Ошибка сохранения: ' + (e as Error).message)
    }
    setSaving(false)
  }

  const checkOne = async (target: DiagTarget) => {
    setDiag((prev) => ({ ...prev, [target]: { target, ok: false, message: 'Проверка…', loading: true } }))
    try {
      const res = await api.runDiagnostic(target)
      setDiag((prev) => ({ ...prev, [target]: { ...res, loading: false } }))
    } catch (e) {
      setDiag((prev) => ({ ...prev, [target]: { target, ok: false, message: (e as Error).message, loading: false } }))
    }
  }

  const checkAll = async () => {
    // Save first so checks use current field values.
    if (settings) { try { await api.updateSettings(settings) } catch { /* ignore */ } }
    for (const t of DIAG_TARGETS) await checkOne(t.key)
  }

  if (!settings) return null

  const fields: { key: keyof SettingsType; label: string; type?: string }[] = [
    { key: 'lm_url', label: 'LLM URL (OpenAI-совместимый, …/v1)' },
    { key: 'lm_api_key', label: 'API-ключ LLM' },
    { key: 'lm_model', label: 'LLM Model' },
    { key: 'lm_embed_model', label: 'Embedding Model (сегментация сцен)' },
    { key: 'lm_context', label: 'Context Length (LM Studio)' },
    { key: 'comfy_url', label: 'ComfyUI URL' },
    { key: 'comfy_api_key', label: 'API-ключ comfy.org (облачные API-ноды ComfyUI)' },
    { key: 'comfy_install_dir', label: 'Portable ComfyUI — каталог установки' },
    { key: 'comfy_krea_workflow', label: 'Krea Workflow Path' },
    { key: 'comfy_reference_workflow', label: 'Canonical Reference Workflow Path (API JSON)' },
    { key: 'comfy_ideogram_workflow', label: 'Ideogram Workflow Path' },
    { key: 'comfy_ltx_workflow', label: 'LTX Workflow Path' },
    { key: 'comfy_ltx_draft_workflow', label: 'LTX Draft Workflow Path (пусто = workflows/LTX23_GGUF_i2v_draft_api.json)' },
    { key: 'comfy_ace_workflow', label: 'ACE-Step Workflow Path (музыка)' },
    { key: 'whisper_cli', label: 'Whisper CLI Path' },
    { key: 'whisper_model', label: 'Whisper Model Path' },
    { key: 'ffmpeg', label: 'FFmpeg Path' },
    { key: 'whisper_language', label: 'Whisper Language' },
    { key: 'omnivoice_cli', label: 'OmniVoice CLI (omnivoice-infer)' },
    { key: 'omnivoice_model', label: 'OmniVoice Model' },
    { key: 'output_base', label: 'Output Base Path' },
    { key: 'image_model_preference', label: 'Image Model Preference' },
    { key: 'pexels_api_key', label: 'Pexels API Key (стоки)' },
    { key: 'pixabay_api_key', label: 'Pixabay API Key (стоки)' },
    { key: 'lm_vision_model', label: 'Vision Model (оценка стока)' },
  ]

  const settingGroups = [
    {
      title: 'Модели и сервисы',
      description: 'Подключения к LLM (LM Studio или облачный API) и ComfyUI, а также модели, используемые в пайплайне. Ключ comfy.org нужен только workflow на облачных API-нодах и отправляется лишь на ComfyUI URL.',
      keys: ['lm_url', 'lm_api_key', 'lm_model', 'lm_embed_model', 'lm_vision_model', 'lm_context', 'comfy_url', 'comfy_api_key', 'image_model_preference'],
    },
    {
      title: 'Пути и инструменты',
      description: 'ComfyUI workflow, Whisper, FFmpeg и папка для результатов. Свой workflow (в т.ч. на облачных API-нодах) подключается по маркерам ENGINE_* в названиях узлов — см. docs/COMFY_WORKFLOWS.md.',
      keys: ['comfy_install_dir', 'comfy_krea_workflow', 'comfy_reference_workflow', 'comfy_ideogram_workflow', 'comfy_ltx_workflow', 'comfy_ltx_draft_workflow', 'comfy_ace_workflow', 'whisper_cli', 'whisper_model', 'ffmpeg', 'whisper_language', 'omnivoice_cli', 'omnivoice_model', 'output_base'],
    },
    {
      title: 'Источники стоков',
      description: 'Ключи внешних каталогов. Сохранённые секреты не отображаются повторно.',
      keys: ['pexels_api_key', 'pixabay_api_key'],
    },
  ]

  const stockNumbers: { key: keyof SettingsType; label: string }[] = [
    { key: 'stock_score_threshold', label: 'Сток: порог «беру сразу» (0..1)' },
    { key: 'stock_floor', label: 'Сток: минимальный порог (0..1)' },
    { key: 'stock_retry_queries', label: 'Сток: перефраз-повторы' },
    { key: 'stock_max_seconds', label: 'Сток: потолок длины (0 = под слот)' },
    { key: 'stock_candidates', label: 'Сток: кандидатов на кадр' },
  ]

  const secretIsSet = (key: keyof SettingsType) =>
    Boolean((settings as unknown as Record<string, unknown>)[`${String(key)}_set`])

  const cloudLm = settings.lm_provider === 'openai_compatible'
  const fieldVisible = (key: keyof SettingsType) =>
    cloudLm ? !LMSTUDIO_ONLY_FIELDS.has(key) : !CLOUD_ONLY_FIELDS.has(key)

  return (
    <div className="min-h-screen bg-slate-950 px-4 py-6 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-5xl space-y-6">
        <header className="sticky top-0 z-20 -mx-2 flex flex-col gap-4 rounded-2xl border border-slate-800 bg-slate-900/95 px-4 py-4 shadow-lg shadow-slate-950/20 backdrop-blur-xl sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-indigo-400">Системная конфигурация</p>
            <h1 className="mt-1 text-2xl font-bold text-white">Настройки</h1>
            <p className="mt-1 text-sm text-slate-500">Модели, локальные инструменты, генерация и диагностика подключений.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ThemeToggle />
            <button
              onClick={() => navigate(-1)}
              className="min-h-10 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm font-medium text-slate-400 transition-colors hover:bg-slate-800 hover:text-white cursor-pointer"
            >
              ← Назад
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="min-h-10 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
            >
              {saved ? 'Сохранено' : saving ? 'Сохранение…' : 'Сохранить'}
            </button>
          </div>
        </header>

        <div className="space-y-8 rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-7">
          {settingGroups.map((group) => (
            <section key={group.title} className="border-b border-slate-800 pb-8 last:border-b-0 last:pb-0">
              <div className="mb-5">
                <h2 className="text-base font-semibold text-white">{group.title}</h2>
                <p className="mt-1 text-sm text-slate-500">{group.description}</p>
              </div>
              {group.keys.includes('lm_url') && (
                <div className="mb-4 space-y-2">
                  <label htmlFor="settings-lm-provider" className="mb-1.5 block text-xs font-medium text-slate-500">Провайдер LLM</label>
                  <select
                    id="settings-lm-provider"
                    value={settings.lm_provider || 'lmstudio'}
                    onChange={(e) => setSettings({ ...settings, lm_provider: e.target.value as SettingsType['lm_provider'] })}
                    className="min-h-10 w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus:border-indigo-500 focus:outline-none"
                  >
                    <option value="lmstudio">LM Studio — локально, без ключа</option>
                    <option value="openai_compatible">OpenAI-совместимый API — облако или прокси, по ключу</option>
                  </select>
                  {cloudLm && (
                    <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                      Текст сценария, сцены и превью стока будут уходить провайдеру, а полный прогон — это сотни платных запросов.
                      Адрес вида https://api.openai.com/v1 или https://openrouter.ai/api/v1; ключ отправляется только на этот адрес.
                      Выгрузка моделей и Context Length в этом режиме не используются.
                    </p>
                  )}
                  {cloudLm && (
                    <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
                      <div>
                        <label htmlFor="settings-lm-cloud-daily-limit" className="mb-1.5 block text-xs font-medium text-slate-500">Лимит облачных запросов в сутки (0 = без лимита)</label>
                        <input
                          id="settings-lm-cloud-daily-limit"
                          type="number"
                          min={0}
                          step={100}
                          value={settings.lm_cloud_daily_limit ?? 0}
                          onChange={(e) => setSettings({ ...settings, lm_cloud_daily_limit: Math.max(0, Math.floor(Number(e.target.value) || 0)) })}
                          className="min-h-10 w-40 rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus:border-indigo-500 focus:outline-none"
                        />
                      </div>
                      <p className="pb-2 text-xs text-slate-500">
                        Сегодня отправлено: {settings.lm_cloud_requests_today ?? 0}. Счётчик сбрасывается в полночь и при перезапуске сервера; повторы после 429/5xx не считаются.
                      </p>
                    </div>
                  )}
                </div>
              )}
              <div className="grid gap-4 md:grid-cols-2">
                {fields.filter(({ key }) => group.keys.includes(String(key)) && fieldVisible(key)).map(({ key, label }) => {
                  const isSecret = SECRET_FIELDS.has(key)
                  const isSet = isSecret && secretIsSet(key)
                  const inputId = `settings-field-${String(key)}`
                  return (
                    <div key={key} className={String(key).includes('workflow') || key === 'output_base' || key === 'comfy_install_dir' ? 'md:col-span-2' : ''}>
                      <label htmlFor={inputId} className="mb-1.5 block text-xs font-medium text-slate-500">
                        {label}{isSet ? ' · ключ сохранён' : ''}
                      </label>
                      <input
                        id={inputId}
                        type={isSecret ? 'password' : 'text'}
                        autoComplete={isSecret ? 'off' : undefined}
                        value={(settings[key] as string) || ''}
                        placeholder={isSet ? 'Новый ключ или пусто без изменений' : ''}
                        onChange={(e) => setSettings({ ...settings, [key]: e.target.value })}
                        className="min-h-10 w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                      />
                    </div>
                  )
                })}
              </div>
            </section>
          ))}

          <section className="space-y-4 border-b border-slate-800 pb-8">
            <div>
              <h2 className="text-base font-semibold text-white">YouTube и субтитры</h2>
              <p className="mt-1 text-sm text-slate-500">Устойчивость yt-dlp при ограничениях YouTube. Cookies используются только во время получения метаданных и субтитров.</p>
            </div>
            <div className="grid gap-4 md:grid-cols-3">
              <label>
                <span className="mb-1.5 block text-xs font-medium text-slate-500">Cookies из браузера</span>
                <select
                  value={settings.youtube_cookies_browser || ''}
                  onChange={(e) => setSettings({ ...settings, youtube_cookies_browser: e.target.value as SettingsType['youtube_cookies_browser'] })}
                  className="min-h-10 w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus:border-indigo-500 focus:outline-none"
                >
                  <option value="">Не использовать</option>
                  <option value="edge">Microsoft Edge</option>
                  <option value="chrome">Google Chrome</option>
                  <option value="firefox">Mozilla Firefox</option>
                  <option value="brave">Brave</option>
                  <option value="opera">Opera</option>
                  <option value="vivaldi">Vivaldi</option>
                </select>
              </label>
              <label>
                <span className="mb-1.5 block text-xs font-medium text-slate-500">Повторы · 0–5</span>
                <input
                  type="number"
                  min={0}
                  max={5}
                  value={settings.youtube_retry_count}
                  onChange={(e) => setSettings({ ...settings, youtube_retry_count: Number(e.target.value) })}
                  className="min-h-10 w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus:border-indigo-500 focus:outline-none"
                />
              </label>
              <label>
                <span className="mb-1.5 block text-xs font-medium text-slate-500">Пауза между запросами · сек</span>
                <input
                  type="number"
                  min={0}
                  max={10}
                  step={0.5}
                  value={settings.youtube_request_delay_sec}
                  onChange={(e) => setSettings({ ...settings, youtube_request_delay_sec: Number(e.target.value) })}
                  className="min-h-10 w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus:border-indigo-500 focus:outline-none"
                />
              </label>
            </div>
            <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-slate-700 bg-slate-950/30 p-3">
              <input
                type="checkbox"
                checked={settings.youtube_fallback_original}
                onChange={(e) => setSettings({ ...settings, youtube_fallback_original: e.target.checked })}
                className="mt-0.5 h-4 w-4"
              />
              <span>
                <span className="block text-sm font-medium text-slate-300">Использовать исходную дорожку как резервную</span>
                <span className="mt-1 block text-xs leading-5 text-slate-500">Если выбранный перевод недоступен, Комбайн попробует оригинальные субтитры. При HTTP 429 дополнительные запросы не выполняются.</span>
              </span>
            </label>
            <p className="text-xs leading-5 text-amber-700">Cookies помогают при проверке «я не робот» и возрастных ограничениях, но не обходят активный лимит IP. Для браузерных cookies должна быть выполнена авторизация на YouTube.</p>
          </section>

          <section className="space-y-4 border-b border-slate-800 pb-8">
            <div>
              <h2 className="text-base font-semibold text-white">Watchdog ComfyUI</h2>
              <p className="mt-1 text-sm text-slate-500">
                Следит за API, перезапускает только настроенную portable-установку и продолжает незавершённые очереди после восстановления.
              </p>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-slate-700 bg-slate-950/30 p-3">
                <input
                  type="checkbox"
                  checked={settings.comfy_watchdog_enabled}
                  onChange={(e) => setSettings({ ...settings, comfy_watchdog_enabled: e.target.checked })}
                  className="mt-0.5 h-4 w-4"
                />
                <span>
                  <span className="block text-sm font-medium text-slate-300">Автоматически восстанавливать ComfyUI</span>
                  <span className="mt-1 block text-xs leading-5 text-slate-500">Три неудачные проверки подряд, затем ограниченный перезапуск.</span>
                </span>
              </label>
              <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-slate-700 bg-slate-950/30 p-3">
                <input
                  type="checkbox"
                  checked={settings.comfy_watchdog_sage_attention}
                  onChange={(e) => setSettings({ ...settings, comfy_watchdog_sage_attention: e.target.checked })}
                  className="mt-0.5 h-4 w-4"
                />
                <span>
                  <span className="block text-sm font-medium text-slate-300">Запускать с SageAttention</span>
                  <span className="mt-1 block text-xs leading-5 text-slate-500">Передаёт ComfyUI параметр --use-sage-attention.</span>
                </span>
              </label>
              <label className="md:col-span-2">
                <span className="mb-1.5 block text-xs font-medium text-slate-500">Адрес --listen</span>
                <input
                  type="text"
                  value={settings.comfy_watchdog_listen_address}
                  onChange={(e) => setSettings({ ...settings, comfy_watchdog_listen_address: e.target.value })}
                  placeholder="0.0.0.0"
                  className="min-h-10 w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                />
                <span className="mt-1 block text-xs leading-5 text-slate-500">Для доступа и с этого ПК, и через ZeroTier используйте 0.0.0.0; доступ с других сетей должен отсекать Windows Firewall.</span>
              </label>
              {([
                ['comfy_watchdog_poll_seconds', 'Интервал проверки, сек', 2, 60],
                ['comfy_watchdog_failure_threshold', 'Ошибок до перезапуска', 2, 12],
                ['comfy_watchdog_startup_timeout_seconds', 'Ожидание запуска, сек', 30, 900],
                ['comfy_watchdog_max_restarts', 'Максимум перезапусков', 1, 10],
                ['comfy_watchdog_restart_window_seconds', 'Окно лимита, сек', 60, 86400],
              ] as const).map(([key, label, min, max]) => (
                <label key={key}>
                  <span className="mb-1.5 block text-xs font-medium text-slate-500">{label}</span>
                  <input
                    type="number"
                    min={min}
                    max={max}
                    value={settings[key]}
                    onChange={(e) => setSettings({ ...settings, [key]: Number(e.target.value) })}
                    className="min-h-10 w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus:border-indigo-500 focus:outline-none"
                  />
                </label>
              ))}
            </div>
            <p className="text-xs leading-5 text-amber-700">
              Watchdog не чинит ошибки workflow, нехватку VRAM и несовместимые custom nodes. При повторяющемся падении он остановится на лимите, чтобы не устроить бесконечный цикл.
            </p>
          </section>

          <section className="space-y-4 border-b border-slate-800 pb-8">
            <div>
              <h2 className="text-base font-semibold text-white">Генерация материалов</h2>
              <p className="mt-1 text-sm text-slate-500">Canonical references, качество LTX, управление моделью и параметры подбора стоков.</p>
            </div>

          <div>
            <label htmlFor="settings-reference-conditioning-mode" className="block text-xs text-slate-500 mb-1">Canonical reference для связанных кадров</label>
            <select
              id="settings-reference-conditioning-mode"
              value={settings.reference_conditioning_mode || 'auto'}
              onChange={(e) => setSettings({ ...settings, reference_conditioning_mode: e.target.value as SettingsType['reference_conditioning_mode'] })}
              className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500"
            >
              <option value="off">off — только текстовый Context Bible</option>
              <option value="auto">auto — использовать reference-workflow, если он настроен</option>
              <option value="required">required — не запускать «Материал» без рабочего reference-workflow</option>
            </select>
            <p className="mt-1 text-xs leading-5 text-slate-500">Для режима auto/required API-workflow должен содержать по одному узлу с названиями ENGINE_REFERENCE_IMAGE, ENGINE_PROMPT и ENGINE_OUTPUT.</p>
          </div>

          <div>
            <label htmlFor="settings-ltx-resolution" className="block text-xs text-slate-500 mb-1">LTX: разрешение генерации (экспорт масштабирует под выбранный delivery-профиль)</label>
            <select
              id="settings-ltx-resolution"
              value={settings.ltx_resolution || '1920x1088'}
              onChange={(e) => setSettings({ ...settings, ltx_resolution: e.target.value })}
              className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500"
            >
              <option value="1920x1088">1920×1088 — Full HD, максимум качества</option>
              <option value="1280x704">1280×704 — ~720p, заметно быстрее (LTX требует кратность 32, поэтому не ровно 720)</option>
            </select>
          </div>

          <div>
            <label htmlFor="settings-ltx-draft-mode" className="block text-xs text-slate-500 mb-1">LTX: черновой режим (draft ~2-3x быстрее, чуть мягче картинка)</label>
            <select
              id="settings-ltx-draft-mode"
              value={settings.ltx_draft_mode || 'off'}
              onChange={(e) => setSettings({ ...settings, ltx_draft_mode: e.target.value })}
              className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500"
            >
              <option value="off">off — всегда полный двухпроходный рендер</option>
              <option value="auto">auto — draft для обычных кадров, полный для priority=high</option>
              <option value="all">all — всегда draft</option>
            </select>
          </div>

          <div className="flex items-center gap-3 pt-2">
            <label htmlFor="settings-lm-auto-unload" className="text-sm text-slate-400">Auto-unload LM model</label>
            <input
              id="settings-lm-auto-unload"
              type="checkbox"
              checked={settings.lm_auto_unload}
              onChange={(e) => setSettings({ ...settings, lm_auto_unload: e.target.checked })}
              className="w-4 h-4"
            />
          </div>

          <div>
            <label htmlFor="settings-lm-unload-delay" className="block text-xs text-slate-500 mb-1">Unload delay (sec, 0 = immediate)</label>
            <input
              id="settings-lm-unload-delay"
              value={settings.lm_unload_delay}
              onChange={(e) => setSettings({ ...settings, lm_unload_delay: e.target.value })}
              className="w-32 px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono"
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {stockNumbers.map(({ key, label }) => (
              <div key={key}>
                <label className="block text-xs text-slate-500 mb-1">{label}</label>
                <input
                  value={String(settings[key] ?? '')}
                  onChange={(e) => setSettings({ ...settings, [key]: e.target.value as unknown as number })}
                  className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono"
                />
              </div>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-6 pt-1">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.stock_uniquify}
                onChange={(e) => setSettings({ ...settings, stock_uniquify: e.target.checked })}
                className="w-4 h-4"
              />
              <span className="text-sm text-slate-300">Уникализация стока (грейд + кроп)</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.stock_mirror}
                onChange={(e) => setSettings({ ...settings, stock_mirror: e.target.checked })}
                className="w-4 h-4"
              />
              <span className="text-sm text-slate-300">+ зеркалить (сильнее, рискованно)</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.stock_free_sources}
                onChange={(e) => setSettings({ ...settings, stock_free_sources: e.target.checked })}
                className="w-4 h-4"
              />
              <span className="text-sm text-slate-300">Архивные источники (Wikimedia / NASA / Archive.org)</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.stock_variable_rhythm}
                onChange={(e) => setSettings({ ...settings, stock_variable_rhythm: e.target.checked })}
                className="w-4 h-4"
              />
              <span className="text-sm text-slate-300">Переменный ритм нарезки (каты по фразам)</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.kenburns_variety}
                onChange={(e) => setSettings({ ...settings, kenburns_variety: e.target.checked })}
                className="w-4 h-4"
              />
              <span className="text-sm text-slate-300">Вариативный Ken Burns (зум/пан, easing)</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.reference_enable}
                onChange={(e) => setSettings({ ...settings, reference_enable: e.target.checked })}
                className="w-4 h-4"
              />
              <span className="text-sm text-slate-300">Инсерты-источники (скрин Википедии / arXiv выезжает сбоку)</span>
            </label>
            <label className="flex items-center gap-2">
              <span className="text-sm text-slate-300">Частота источников</span>
              <select
                value={settings.reference_density}
                onChange={(e) => setSettings({ ...settings, reference_density: e.target.value })}
                disabled={!settings.reference_enable}
                className="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-sm text-slate-200 disabled:opacity-40"
              >
                <option value="off">Выкл</option>
                <option value="sparse">Редко (≤1 на 2 сцены)</option>
                <option value="normal">Обычно (~1 на сцену)</option>
              </select>
            </label>
          </div>

          </section>

          {/* Authenticity: export finishing + SFX layer */}
          <section className="space-y-4">
            <div>
              <h2 className="text-base font-semibold text-white">Экспорт, контроль качества и звук</h2>
              <p className="mt-1 text-sm text-slate-500">Финишная обработка, автоматическая проверка материалов и SFX-слой.</p>
            </div>
            <div className="flex flex-wrap items-center gap-6">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={settings.export_unify_grade}
                  onChange={(e) => setSettings({ ...settings, export_unify_grade: e.target.checked })}
                  className="w-4 h-4"
                />
                <span className="text-sm text-slate-300">Единый грейд + зерно на все кадры</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={settings.export_loudnorm}
                  onChange={(e) => setSettings({ ...settings, export_loudnorm: e.target.checked })}
                  className="w-4 h-4"
                />
                <span className="text-sm text-slate-300">Loudnorm финального микса (−14 LUFS)</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={settings.music_normalize}
                  onChange={(e) => setSettings({ ...settings, music_normalize: e.target.checked })}
                  className="w-4 h-4"
                />
                <span className="text-sm text-slate-300">Нормализация громкости музыки</span>
              </label>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <div>
                <label htmlFor="settings-export-grain" className="block text-xs text-slate-500 mb-1">Зерно (0 = выкл, 4-10 норм)</label>
                <input
                  id="settings-export-grain"
                  value={String(settings.export_grain ?? '')}
                  onChange={(e) => setSettings({ ...settings, export_grain: e.target.value as unknown as number })}
                  className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono"
                />
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-6">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={settings.sfx_enabled}
                  onChange={(e) => setSettings({ ...settings, sfx_enabled: e.target.checked })}
                  className="w-4 h-4"
                />
                <span className="text-sm text-slate-300">SFX-слой (ambient через Woosh)</span>
              </label>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label htmlFor="settings-woosh-url" className="block text-xs text-slate-500 mb-1">Woosh API URL</label>
                <input
                  id="settings-woosh-url"
                  value={settings.woosh_url || ''}
                  placeholder="http://127.0.0.1:8500"
                  onChange={(e) => setSettings({ ...settings, woosh_url: e.target.value })}
                  className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono"
                />
              </div>
              <div>
                <label htmlFor="settings-sfx-volume-db" className="block text-xs text-slate-500 mb-1">Громкость SFX, dB (−24 норм)</label>
                <input
                  id="settings-sfx-volume-db"
                  value={String(settings.sfx_volume_db ?? '')}
                  onChange={(e) => setSettings({ ...settings, sfx_volume_db: e.target.value as unknown as number })}
                  className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono"
                />
              </div>
            </div>
            <p className="text-xs text-slate-600">
              Woosh — SFX-модель Sony AI (github.com/SonyResearch/Woosh). Запустите её API-сервер отдельно
              (порт не 8000 — он занят этим приложением). Веса CC-BY-NC — только для некоммерческого использования.
            </p>
          </section>
        </div>

        {/* Connection diagnostics */}
        <section className="space-y-3 rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-7">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-base font-semibold text-white">Проверка подключений</h2>
              <p className="mt-1 text-sm text-slate-500">Проверка внешних сервисов по текущим путям и URL.</p>
            </div>
            <button
              onClick={checkAll}
              className="text-xs px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-200 hover:bg-slate-700 cursor-pointer"
            >
              Проверить всё
            </button>
          </div>
          <div className="space-y-2">
            {DIAG_TARGETS.map(({ key, label }) => {
              const r = diag[key]
              const dot = r?.loading ? 'bg-sky-400 animate-pulse' : r ? (r.ok ? 'bg-emerald-500' : 'bg-red-500') : 'bg-slate-600'
              return (
                <div key={key} className="flex items-center gap-3 rounded-lg bg-slate-950/50 border border-slate-800 px-3 py-2">
                  <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${dot}`} />
                  <div className="w-24 shrink-0 text-sm text-slate-200">{label}</div>
                  <div className="flex-1 min-w-0">
                    <div className={`text-xs truncate ${r && !r.ok && !r.loading ? 'text-red-300' : 'text-slate-400'}`}>
                      {r ? r.message : 'не проверялось'}
                    </div>
                    {r?.detail && <div className="text-[11px] text-slate-600 truncate">{r.detail}</div>}
                  </div>
                  <button
                    onClick={() => checkOne(key)}
                    className="text-xs px-2.5 py-1 rounded bg-slate-800 border border-slate-700 text-slate-300 hover:bg-slate-700 cursor-pointer shrink-0"
                  >
                    Тест
                  </button>
                </div>
              )
            })}
          </div>
        </section>
      </div>
    </div>
  )
}
