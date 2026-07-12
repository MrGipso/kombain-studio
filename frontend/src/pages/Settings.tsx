import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import type { Settings as SettingsType } from '../api/types'

type DiagTarget = 'lm' | 'embed' | 'comfy' | 'ffmpeg' | 'whisper' | 'woosh'
const SECRET_FIELDS = new Set<keyof SettingsType>(['pexels_api_key', 'pixabay_api_key'])
const DIAG_TARGETS: { key: DiagTarget; label: string }[] = [
  { key: 'lm', label: 'LM Studio' },
  { key: 'embed', label: 'Embeddings' },
  { key: 'comfy', label: 'ComfyUI' },
  { key: 'ffmpeg', label: 'FFmpeg' },
  { key: 'whisper', label: 'Whisper' },
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
    { key: 'lm_url', label: 'LM Studio URL' },
    { key: 'lm_model', label: 'LM Studio Model' },
    { key: 'lm_embed_model', label: 'LM Studio Embedding Model (сегментация сцен)' },
    { key: 'lm_context', label: 'Context Length' },
    { key: 'comfy_url', label: 'ComfyUI URL' },
    { key: 'comfy_krea_workflow', label: 'Krea Workflow Path' },
    { key: 'comfy_ideogram_workflow', label: 'Ideogram Workflow Path' },
    { key: 'comfy_ltx_workflow', label: 'LTX Workflow Path' },
    { key: 'comfy_ltx_draft_workflow', label: 'LTX Draft Workflow Path (пусто = workflows/LTX23_GGUF_i2v_draft_api.json)' },
    { key: 'comfy_ace_workflow', label: 'ACE-Step Workflow Path (музыка)' },
    { key: 'whisper_cli', label: 'Whisper CLI Path' },
    { key: 'whisper_model', label: 'Whisper Model Path' },
    { key: 'ffmpeg', label: 'FFmpeg Path' },
    { key: 'whisper_language', label: 'Whisper Language' },
    { key: 'output_base', label: 'Output Base Path' },
    { key: 'image_model_preference', label: 'Image Model Preference' },
    { key: 'pexels_api_key', label: 'Pexels API Key (стоки)' },
    { key: 'pixabay_api_key', label: 'Pixabay API Key (стоки)' },
    { key: 'lm_vision_model', label: 'LM Studio Vision Model (оценка стока)' },
  ]

  const stockNumbers: { key: keyof SettingsType; label: string }[] = [
    { key: 'stock_score_threshold', label: 'Сток: порог «беру сразу» (0..1)' },
    { key: 'stock_floor', label: 'Сток: минимальный порог (0..1)' },
    { key: 'stock_retry_queries', label: 'Сток: перефраз-повторы' },
    { key: 'stock_max_seconds', label: 'Сток: потолок длины (0 = под слот)' },
    { key: 'stock_candidates', label: 'Сток: кандидатов на кадр' },
  ]

  const secretIsSet = (key: keyof SettingsType) => {
    if (key === 'pexels_api_key') return !!settings.pexels_api_key_set
    if (key === 'pixabay_api_key') return !!settings.pixabay_api_key_set
    return false
  }

  return (
    <div className="min-h-screen bg-slate-950 p-8">
      <div className="max-w-2xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-white">Настройки</h1>
          <button
            onClick={() => navigate(-1)}
            className="text-sm text-slate-500 hover:text-white transition-colors cursor-pointer"
          >
            ← Назад
          </button>
        </div>

        <div className="bg-slate-900 rounded-xl border border-slate-800 p-6 space-y-4">
          {fields.map(({ key, label }) => {
            const isSecret = SECRET_FIELDS.has(key)
            const isSet = isSecret && secretIsSet(key)
            return (
            <div key={key}>
              <label className="block text-xs text-slate-500 mb-1">
                {label}{isSet ? ' · задан, оставьте пустым чтобы не менять' : ''}
              </label>
              <input
                type={isSecret ? 'password' : 'text'}
                value={(settings[key] as string) || ''}
                placeholder={isSet ? 'Новый ключ или пусто без изменений' : ''}
                onChange={(e) => setSettings({ ...settings, [key]: e.target.value })}
                className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500 font-mono"
              />
            </div>
            )
          })}

          <div>
            <label className="block text-xs text-slate-500 mb-1">LTX: разрешение генерации (экспорт масштабирует под выбранный delivery-профиль)</label>
            <select
              value={settings.ltx_resolution || '1920x1088'}
              onChange={(e) => setSettings({ ...settings, ltx_resolution: e.target.value })}
              className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500"
            >
              <option value="1920x1088">1920×1088 — Full HD, максимум качества</option>
              <option value="1280x704">1280×704 — ~720p, заметно быстрее (LTX требует кратность 32, поэтому не ровно 720)</option>
            </select>
          </div>

          <div>
            <label className="block text-xs text-slate-500 mb-1">LTX: черновой режим (draft ~2-3x быстрее, чуть мягче картинка)</label>
            <select
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
            <label className="text-sm text-slate-400">Auto-unload LM model</label>
            <input
              type="checkbox"
              checked={settings.lm_auto_unload}
              onChange={(e) => setSettings({ ...settings, lm_auto_unload: e.target.checked })}
              className="w-4 h-4"
            />
          </div>

          <div>
            <label className="block text-xs text-slate-500 mb-1">Unload delay (sec, 0 = immediate)</label>
            <input
              value={settings.lm_unload_delay}
              onChange={(e) => setSettings({ ...settings, lm_unload_delay: e.target.value })}
              className="w-32 px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono"
            />
          </div>

          <div className="grid grid-cols-3 gap-3 pt-1">
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

          {/* Authenticity: export finishing + SFX layer */}
          <div className="border-t border-slate-800 pt-4 space-y-4">
            <h2 className="text-sm text-slate-400 uppercase tracking-wider">Экспорт и звук</h2>
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
            <div className="flex flex-wrap items-center gap-6">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={settings.ai_frame_qa}
                  onChange={(e) => setSettings({ ...settings, ai_frame_qa: e.target.checked })}
                  className="w-4 h-4"
                />
                <span className="text-sm text-slate-300">Vision-QA сгенерированных кадров (нужна Vision Model; ниже порога → needs_review)</span>
              </label>
            </div>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label className="block text-xs text-slate-500 mb-1">Зерно (0 = выкл, 4-10 норм)</label>
                <input
                  value={String(settings.export_grain ?? '')}
                  onChange={(e) => setSettings({ ...settings, export_grain: e.target.value as unknown as number })}
                  className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono"
                />
              </div>
              <div>
                <label className="block text-xs text-slate-500 mb-1">QA-порог (0..1, 0.45 норм)</label>
                <input
                  value={String(settings.ai_qa_threshold ?? '')}
                  onChange={(e) => setSettings({ ...settings, ai_qa_threshold: e.target.value as unknown as number })}
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
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-slate-500 mb-1">Woosh API URL</label>
                <input
                  value={settings.woosh_url || ''}
                  placeholder="http://127.0.0.1:8500"
                  onChange={(e) => setSettings({ ...settings, woosh_url: e.target.value })}
                  className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono"
                />
              </div>
              <div>
                <label className="block text-xs text-slate-500 mb-1">Громкость SFX, dB (−24 норм)</label>
                <input
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
          </div>

          <button
            onClick={handleSave}
            disabled={saving}
            className="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
          >
            {saved ? '✅ Сохранено' : saving ? '⏳ Сохранение...' : '💾 Сохранить настройки'}
          </button>
        </div>

        {/* Connection diagnostics */}
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-6 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm text-slate-400 uppercase tracking-wider">Проверка подключений</h2>
            <button
              onClick={checkAll}
              className="text-xs px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-200 hover:bg-slate-700 cursor-pointer"
            >
              Проверить всё
            </button>
          </div>
          <p className="text-xs text-slate-600">Сохраняет настройки и проверяет внешние зависимости по текущим путям/URL.</p>
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
        </div>
      </div>
    </div>
  )
}
