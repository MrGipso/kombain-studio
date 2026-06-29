import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import type { Settings as SettingsType } from '../api/types'

export default function Settings() {
  const navigate = useNavigate()
  const [settings, setSettings] = useState<SettingsType | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.getSettings().then(setSettings).catch(() => {})
  }, [])

  const handleSave = async () => {
    if (!settings) return
    setSaving(true)
    try {
      await api.updateSettings(settings)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      alert('Error saving settings: ' + (e as Error).message)
    }
    setSaving(false)
  }

  if (!settings) return null

  const fields: { key: keyof SettingsType; label: string; type?: string }[] = [
    { key: 'lm_url', label: 'LM Studio URL' },
    { key: 'lm_model', label: 'LM Studio Model' },
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
      </div>
    </div>
  )
}
