import { useState, useEffect } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useSSE } from '../hooks/useSSE'
import type { Scene } from '../api/types'

interface Context {
  projectId: string
}

export default function Scenes() {
  const { projectId } = useOutletContext<Context>()
  const { startStream } = useSSE()
  const [scenes, setScenes] = useState<Scene[]>([])
  const [selected, setSelected] = useState<Scene | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const [hasSegments, setHasSegments] = useState(false)

  useEffect(() => {
    api.getScenes(projectId).then((d) => {
      const s = d as Scene[]
      setScenes(s)
      if (s.length > 0 && !selected) setSelected(s[0])
    }).catch(() => {})
    api.getProject(projectId).then((d) => {
      setHasSegments('segments' in d && Array.isArray(d.segments) && d.segments.length > 0)
    }).catch(() => {})
  }, [projectId])

  const handleAnalyze = () => {
    setRunning(true)
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
        setRunning(false)
      },
      onError: (msg) => {
        setLogs((prev) => [...prev, `❌ ${msg}`])
        setRunning(false)
      },
    })
  }

  const handleSave = async () => {
    if (!selected) return
    try {
      await api.updateScene(projectId, selected.index, selected as unknown as Record<string, unknown>)
      setLogs((prev) => [...prev, `✅ Сцена ${selected.index} сохранена`])
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка: ${(e as Error).message}`])
    }
  }

  const handleExport = async () => {
    if (scenes.length === 0) {
      setLogs((prev) => [...prev, '⚠️ Нет сцен для экспорта'])
      return
    }
    try {
      const res = await api.exportScenes(projectId)
      setLogs((prev) => [...prev, `📦 Экспортировано ${res.count} сцен`])
      setLogs((prev) => [...prev, `   CSV: ${res.csv}`])
      setLogs((prev) => [...prev, `   MD:  ${res.md}`])
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка экспорта: ${(e as Error).message}`])
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

      <div className="flex gap-3">
        <button
          onClick={handleAnalyze}
          disabled={running}
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

      {!hasSegments && !running && (
        <div className="bg-amber-900/30 border border-amber-700/50 rounded-xl p-4 text-amber-300 text-sm">
          ⚠️ Сначала выполните транскрипцию, чтобы получить сегменты для анализа сцен.
        </div>
      )}

      {running && (
        <div className="flex items-center gap-3 px-4 py-3 bg-indigo-900/40 border border-indigo-700/50 rounded-lg">
          <span className="w-3 h-3 rounded-full bg-indigo-400 animate-ping" />
          <span className="text-indigo-300 text-sm font-semibold">Анализ сцен... ({scenes.length} найдено)</span>
          <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
            <div className="h-full w-1/3 bg-indigo-500 rounded-full animate-pulse" />
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
