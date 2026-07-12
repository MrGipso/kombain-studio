import { useState, useEffect } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useSSE } from '../hooks/useSSE'
import { useToast } from '../components/toast-context'
import type { Scene, ProjectDetail, VideoStyle } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

export default function Scenes() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const { startStream } = useSSE()
  const toast = useToast()
  const [scenes, setScenes] = useState<Scene[]>([])
  const [selected, setSelected] = useState<Scene | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const [hasSegments, setHasSegments] = useState(false)
  const [styles, setStyles] = useState<VideoStyle[]>([])
  const [videoStyle, setVideoStyle] = useState('knitted')
  const [styleAnchor, setStyleAnchor] = useState('')
  const [charDesc, setCharDesc] = useState('')
  const [segMin, setSegMin] = useState(20)
  const [segMax, setSegMax] = useState(90)
  const [segSensitivity, setSegSensitivity] = useState(0.6)
  const [segTarget, setSegTarget] = useState(0)
  const [saving, setSaving] = useState(false)
  const lockedElsewhere = Boolean(busy) && !running
  const selectedStyle = styles.find((s) => s.id === videoStyle) || styles[0]

  useEffect(() => {
    api.getVideoStyles().then((items) => {
      setStyles(items)
      setVideoStyle((current) => (items.length && !items.some((item) => item.id === current) ? items[0].id : current))
    }).catch(() => {})
    api.getScenes(projectId).then((d) => {
      const s = d as Scene[]
      setScenes(s)
      setSelected((current) => current || (s.length > 0 ? s[0] : null))
    }).catch(() => {})
    api.getProject(projectId).then((d) => {
      const proj = d as ProjectDetail
      setHasSegments('segments' in proj && Array.isArray(proj.segments) && proj.segments.length > 0)
      setVideoStyle(proj.video_style || 'knitted')
      setStyleAnchor(proj.style_anchor || '')
      setCharDesc(proj.character_description || '')
      setSegMin(proj.seg_min_seconds ?? 20)
      setSegMax(proj.seg_max_seconds ?? 90)
      setSegSensitivity(proj.seg_sensitivity ?? 0.6)
      setSegTarget(proj.seg_target_scenes ?? 0)
    }).catch(() => {})
  }, [projectId])

  const handleSaveSettings = async () => {
    setSaving(true)
    try {
      await api.updateProjectSettings(projectId, {
        video_style: videoStyle,
        style_anchor: styleAnchor,
        character_description: charDesc,
        seg_min_seconds: segMin,
        seg_max_seconds: segMax,
        seg_sensitivity: segSensitivity,
        seg_target_scenes: segTarget,
      })
      setLogs((prev) => [...prev, '✅ Стиль и персонаж сохранены'])
      toast.success('Настройки стиля сохранены')
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка: ${(e as Error).message}`])
      toast.error(`Сохранение: ${(e as Error).message}`)
    }
    setSaving(false)
  }

  const handleAnalyze = () => {
    if (busy) { toast.error('Дождитесь завершения текущей задачи'); return }
    setRunning(true)
    setBusy('Анализ сцен')
    setScenes([])
    setSelected(null)
    setLogs((prev) => [...prev, '⏳ Анализ сцен...'])

    startStream(api.startSceneAnalysis(projectId), {
      onLog: (msg) => setLogs((prev) => [...prev, msg]),
      onDone: (data) => {
        const s = data.scenes as Scene[]
        setScenes(s)
        if (s.length > 0) setSelected(s[0])
        setLogs((prev) => [...prev, `✅ Сцен найдено: ${s.length}`])
        toast.success(`Найдено сцен: ${s.length}`)
        setRunning(false)
        setBusy(null)
      },
      onError: (msg) => {
        setLogs((prev) => [...prev, `❌ ${msg}`])
        toast.error(`Анализ сцен: ${msg}`)
        setRunning(false)
        setBusy(null)
      },
    })
  }

  const handleSave = async () => {
    if (!selected) return
    try {
      await api.updateScene(projectId, selected.index, selected as unknown as Record<string, unknown>)
      setLogs((prev) => [...prev, `✅ Сцена ${selected.index} сохранена`])
      toast.success(`Сцена ${selected.index} сохранена`)
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка: ${(e as Error).message}`])
      toast.error(`Сохранение: ${(e as Error).message}`)
    }
  }

  const handleExport = async () => {
    if (scenes.length === 0) {
      toast.error('Нет сцен для экспорта')
      return
    }
    try {
      const res = await api.exportScenes(projectId)
      setLogs((prev) => [...prev, `📦 Экспортировано ${res.count} сцен`])
      setLogs((prev) => [...prev, `   CSV: ${res.csv}`])
      setLogs((prev) => [...prev, `   MD:  ${res.md}`])
      toast.success(`Экспортировано ${res.count} сцен (CSV + MD)`)
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка экспорта: ${(e as Error).message}`])
      toast.error(`Экспорт: ${(e as Error).message}`)
    }
  }

  const fields: { key: keyof Scene; label: string; multiline?: boolean }[] = [
    { key: 'title', label: 'Название' },
    { key: 'start', label: 'Начало' },
    { key: 'end', label: 'Конец' },
    { key: 'voice_text', label: 'Текст', multiline: true },
    { key: 'visual_direction', label: 'Визуальное направление', multiline: true },
    { key: 'screen_text', label: 'Текст на экране', multiline: true },
    { key: 'music_note', label: 'Музыка', multiline: true },
    { key: 'retention_note', label: 'Заметка по удержанию', multiline: true },
  ]

  return (
    <div className="p-8 space-y-6">
      <h1 className="text-2xl font-bold text-white">Сцены</h1>

      {/* Video style settings */}
      <div className="bg-slate-900 rounded-xl p-5 border border-slate-800 space-y-4">
        <h2 className="text-sm text-slate-500 uppercase tracking-wider">Визуальный стиль всего видео</h2>
        <div className="grid grid-cols-[260px_1fr] gap-4">
          <div>
            <label className="block text-xs text-slate-500 mb-1">Стиль видео</label>
            <select
              value={videoStyle}
              onChange={(e) => setVideoStyle(e.target.value)}
              className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500"
            >
              {styles.map((style) => (
                <option key={style.id} value={style.id}>{style.name}</option>
              ))}
              {!styles.length && <option value="knitted">Вязанный</option>}
            </select>
          </div>
          <div className="rounded-lg bg-slate-950/50 border border-slate-800 px-4 py-3">
            <div className="text-sm font-semibold text-white">{selectedStyle?.name || 'Вязанный'}</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">
              {selectedStyle?.description || 'Вязаная stop-motion диорама с amigurumi-персонажами и handmade-декорациями.'}
            </p>
          </div>
        </div>

        {selectedStyle && (
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-slate-500 mb-1">Style prompt (добавляется в каждый Krea 2 кадр)</label>
              <textarea
                value={selectedStyle.base_prompt}
                readOnly
                className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded text-slate-300 text-xs focus:outline-none h-24 resize-none"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-500 mb-1">Правила разбиения сцен в этом стиле</label>
              <div className="h-24 overflow-y-auto rounded bg-slate-950 border border-slate-700 px-3 py-2 text-xs text-slate-400 space-y-1">
                {selectedStyle.shot_planning_rules.map((rule) => <div key={rule}>- {rule}</div>)}
              </div>
            </div>
          </div>
        )}

        <div>
          <label className="block text-xs text-slate-500 mb-1">Описание персонажа (одинаковый во всех кадрах)</label>
          <input
            value={charDesc}
            onChange={(e) => setCharDesc(e.target.value)}
            placeholder="Главный герой: мужчина 35 лет, короткая стрижка, борода, кожаная куртка"
            className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500"
          />
        </div>

        <div>
          <label className="block text-xs text-slate-500 mb-1">Дополнительные заметки к стилю (не LoRA, только текст prompt)</label>
          <textarea
            value={styleAnchor}
            onChange={(e) => setStyleAnchor(e.target.value)}
            placeholder="Например: warmer palette, more cave-like backgrounds, bigger yarn stitches"
            className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500 h-16 resize-none"
          />
        </div>

        <div className="border-t border-slate-800 pt-4">
          <div className="text-xs text-slate-500 uppercase tracking-wider mb-2">Разбиение на сцены (эмбеддинги)</div>
          <div className="grid grid-cols-[1fr_110px_110px_140px] gap-4 items-end">
            <div>
              <label className="block text-xs text-slate-500 mb-1">Чувствительность границ: {segSensitivity.toFixed(2)} {segTarget > 0 && '(игнор., задано число сцен)'}</label>
              <input
                type="range" min={0} max={1} step={0.05}
                value={segSensitivity}
                onChange={(e) => setSegSensitivity(Number(e.target.value))}
                className="w-full accent-indigo-500"
              />
              <div className="flex justify-between text-[10px] text-slate-600"><span>меньше сцен</span><span>больше сцен</span></div>
            </div>
            <div>
              <label className="block text-xs text-slate-500 mb-1">Мин. сек.</label>
              <input type="number" min={2} value={segMin} onChange={(e) => setSegMin(Number(e.target.value) || 0)} className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono focus:outline-none focus:border-indigo-500" />
            </div>
            <div>
              <label className="block text-xs text-slate-500 mb-1">Макс. сек.</label>
              <input type="number" min={5} value={segMax} onChange={(e) => setSegMax(Number(e.target.value) || 0)} className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono focus:outline-none focus:border-indigo-500" />
            </div>
            <div>
              <label className="block text-xs text-slate-500 mb-1">Число сцен (0=авто)</label>
              <input type="number" min={0} value={segTarget} onChange={(e) => setSegTarget(Number(e.target.value) || 0)} className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm font-mono focus:outline-none focus:border-indigo-500" />
            </div>
          </div>
          <p className="text-[11px] text-slate-600 mt-1">Применяется при следующем «Анализировать сцены». Если задать число сцен, чувствительность игнорируется (берутся N сильнейших границ).</p>
        </div>

        <button
          onClick={handleSaveSettings}
          disabled={saving}
          className="py-1.5 px-4 bg-emerald-700 hover:bg-emerald-600 text-white rounded-lg text-sm font-semibold disabled:opacity-50 cursor-pointer"
        >
          {saving ? '⏳...' : '💾 Сохранить настройки стиля'}
        </button>
      </div>

      <div className="flex gap-3">
        <button
          onClick={handleAnalyze}
          disabled={running || lockedElsewhere || !hasSegments}
          className="flex-1 py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
        >
          {running ? '⏳ Анализ...' : '📝 Анализировать сцены (LM Studio)'}
        </button>
        <button
          onClick={handleExport}
          disabled={scenes.length === 0}
          className="py-3 px-6 bg-slate-700 hover:bg-slate-600 text-white font-semibold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
        >
          📦 Экспорт
        </button>
      </div>

      {lockedElsewhere && (
        <div className="bg-sky-900/30 border border-sky-700/50 rounded-xl p-4 text-sky-300 text-sm">
          ⏳ Идёт другая задача ({busy}). Дождитесь завершения.
        </div>
      )}

      {!hasSegments && !running && (
        <div className="bg-amber-900/30 border border-amber-700/50 rounded-xl p-4 text-amber-300 text-sm">
          ⚠️ Сначала выполните транскрипцию, чтобы получить сегменты для анализа сцен.
        </div>
      )}

      {running && (
        <div className="flex items-center gap-3 px-4 py-3 bg-indigo-900/40 border border-indigo-700/50 rounded-lg">
          <span className="w-3 h-3 rounded-full bg-indigo-400 animate-ping" />
          <span className="text-indigo-300 text-sm font-semibold shrink-0">Анализ сцен… ({scenes.length} найдено)</span>
          <button
            onClick={() => api.cancelJob(projectId).then(() => toast.info('Отмена — остановится на ближайшем шаге')).catch(() => {})}
            className="shrink-0 text-xs px-3 py-1 rounded bg-rose-800 hover:bg-rose-700 text-white cursor-pointer"
          >
            Отменить
          </button>
          <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
            <div className="h-full w-1/3 bg-indigo-500 rounded-full animate-[indeterminate_1.2s_ease-in-out_infinite]" />
          </div>
        </div>
      )}

      {scenes.length > 0 && (
        <div className="grid grid-cols-[320px_1fr] gap-6">
          {/* Scene list */}
          <div className="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
            <div className="divide-y divide-slate-800 max-h-[65vh] overflow-y-auto">
              {scenes.map((s) => (
                <button
                  key={s.index}
                  onClick={() => setSelected(s)}
                  className={`w-full text-left px-4 py-3 transition-colors cursor-pointer ${
                    selected?.index === s.index ? 'bg-indigo-600/20 border-l-2 border-indigo-500' : 'border-l-2 border-transparent hover:bg-slate-800'
                  }`}
                >
                  <div className="text-white font-medium text-sm">Сцена {s.index}</div>
                  <div className="text-xs text-slate-500 mt-0.5">{s.start} – {s.end}</div>
                  <p className="text-sm text-slate-400 truncate mt-0.5">{s.title}</p>
                </button>
              ))}
            </div>
          </div>

          {/* Scene detail */}
          {selected && (
            <div className="bg-slate-900 rounded-xl p-5 border border-slate-800 space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-lg font-semibold text-white">Сцена {selected.index}</h2>
                <span className="text-xs text-slate-500">{selected.start} – {selected.end}</span>
              </div>
              <div className="space-y-3">
                {fields.map(({ key, label, multiline }) => (
                  <div key={key}>
                    <label className="block text-xs text-slate-500 mb-1">{label}</label>
                    {multiline ? (
                      <textarea
                        value={(selected[key] as string) || ''}
                        onChange={(e) => setSelected({ ...selected, [key]: e.target.value })}
                        className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500 h-20 resize-none"
                      />
                    ) : (
                      <input
                        value={(selected[key] as string) || ''}
                        onChange={(e) => setSelected({ ...selected, [key]: e.target.value })}
                        className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded text-white text-sm focus:outline-none focus:border-indigo-500"
                      />
                    )}
                  </div>
                ))}
                <button
                  onClick={handleSave}
                  className="py-2 px-6 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-sm font-semibold cursor-pointer"
                >
                  💾 Сохранить
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Logs */}
      <div className="bg-slate-900/50 rounded-xl p-4 border border-slate-800">
        <h3 className="text-sm text-slate-500 mb-2">Логи</h3>
        <div className="max-h-32 overflow-y-auto text-xs text-slate-500 space-y-0.5 font-mono">
          {logs.map((l, i) => (<div key={i}>{l}</div>))}
          {logs.length === 0 && <div className="text-slate-700">Ожидание...</div>}
        </div>
      </div>
    </div>
  )
}

