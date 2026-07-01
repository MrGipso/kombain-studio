import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/Toast'
import type { Settings as SettingsType } from '../api/types'

type DiagTarget = 'lm' | 'embed' | 'comfy' | 'ffmpeg' | 'whisper'
const DIAG_TARGETS: { key: DiagTarget; label: string }[] = [
  { key: 'lm', label: 'LM Studio' },
  { key: 'embed', label: 'Embeddings' },
  { key: 'comfy', label: 'ComfyUI' },
  { key: 'ffmpeg', label: 'FFmpeg' },
  { key: 'whisper', label: 'Whisper' },
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
  }, [])

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
    { key: 'whisper_cli', label: 'Whisper CLI Path' },
    { key: 'whisper_model', label: 'Whisper Model Path' },
    { key: 'ffmpeg', label: 'FFmpeg Path' },
    { key: 'whisper_language', label: 'Whisper Language' },
    { key: 'output_base', label: 'Output Base Path' },
    { key: 'image_model_preference', label: 'Image Model Preference' },
  ]

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
          {fields.map(({ key, label }) => (
            <div key={key}>
              <label className="block text-xs text-slate-500 mb-1">{label}</label>
              <input
                value={(settings[key] as string) || ''}
                onChange={(e) => setSettings({ ...settings, [key]: e.target.value })}
                className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500 font-mono"
              />
            </div>
          ))}

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
