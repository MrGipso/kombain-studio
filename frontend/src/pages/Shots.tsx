import { useState, useEffect } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useSSE } from '../hooks/useSSE'
import type { Shot, AssetCard } from '../api/types'

interface Context {
  projectId: string
}

export default function Shots() {
  const { projectId } = useOutletContext<Context>()
  const { startStream } = useSSE()
  const [shots, setShots] = useState<Shot[]>([])
  const [selected, setSelected] = useState<Shot | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const [hasScenes, setHasScenes] = useState(false)

  useEffect(() => {
    api.getShots(projectId).then((d) => {
      const s = d as Shot[]
      setShots(s)
      if (s.length > 0 && !selected) setSelected(s[0])
    }).catch(() => {})
    api.getScenes(projectId).then((d) => {
      setHasScenes(Array.isArray(d) && d.length > 0)
    }).catch(() => {})
  }, [projectId])

  const handleGenerate = () => {
    setRunning(true)
    setShots([])
    setSelected(null)
    setLogs((prev) => [...prev, '⏳ Генерация шот-листа...'])

    startStream(api.startShotGeneration(projectId), {
      onLog: (msg) => setLogs((prev) => [...prev, msg]),
      onDone: (data) => {
        const s = data.shots as Shot[]
        const c = data.cards as AssetCard[]
        setShots(s)
        if (s.length > 0) setSelected(s[0])
        setLogs((prev) => [...prev, `✅ Шотов: ${s.length}, карточек: ${c.length}`])
        setRunning(false)
      },
      onError: (msg) => {
        setLogs((prev) => [...prev, `❌ ${msg}`])
        setRunning(false)
      },
    })
  }

  const handleRebuildBoard = async () => {
    try {
      setLogs((prev) => [...prev, '⏳ Пересборка доски кадров...'])
      const cards = await api.getAssetBoard(projectId)
      const cardsWithStatus = cards.map((c) => ({ ...c, status: 'planned' }))
      await api.saveAssetBoard(projectId, cardsWithStatus as unknown as AssetCard[])
      setLogs((prev) => [...prev, `✅ Доска пересобрана: ${cards.length} карточек`])
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка: ${(e as Error).message}`])
    }
  }

  const shotFields: { key: keyof Shot; label: string; multiline?: boolean }[] = [
    { key: 'visual_type', label: 'Тип' },
    { key: 'start', label: 'Начало' },
    { key: 'end', label: 'Конец' },
    { key: 'image_model', label: 'Image model' },
    { key: 'video_model', label: 'Video model' },
    { key: 'motion', label: 'Motion' },
    { key: 'priority', label: 'Priority' },
    { key: 'fallback', label: 'Fallback' },
    { key: 'screen_text', label: 'Текст на экране', multiline: true },
    { key: 'description', label: 'Описание', multiline: true },
    { key: 'asset_query', label: 'Asset query', multiline: true },
    { key: 'image_prompt', label: 'Image prompt', multiline: true },
    { key: 'video_prompt', label: 'Video prompt', multiline: true },
  ]

  return (
    <div className="p-8 space-y-6">
      <h1 className="text-2xl font-bold text-white">Шот-лист</h1>

      <div className="flex gap-3">
        <button
          onClick={handleGenerate}
          disabled={running}
          className="flex-1 py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
        >
          {running ? '⏳ Генерация...' : '🎬 Сгенерировать шот-лист'}
        </button>
        <button
          onClick={handleRebuildBoard}
          disabled={shots.length === 0}
          className="py-3 px-6 bg-emerald-700 hover:bg-emerald-600 text-white font-semibold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
        >
          📋 Пересобрать доску
        </button>
      </div>

      {!hasScenes && !running && (
        <div className="bg-amber-900/30 border border-amber-700/50 rounded-xl p-4 text-amber-300 text-sm">
          ⚠️ Сначала выполните анализ сцен.
        </div>
      )}

      {running && (
        <div className="flex items-center gap-3 px-4 py-3 bg-indigo-900/40 border border-indigo-700/50 rounded-lg">
          <span className="w-3 h-3 rounded-full bg-indigo-400 animate-ping" />
          <span className="text-indigo-300 text-sm font-semibold">Генерация кадров... ({shots.length})</span>
          <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
            <div className="h-full w-1/3 bg-indigo-500 rounded-full animate-pulse" />
          </div>
        </div>
      )}

      {shots.length > 0 && (
        <div className="grid grid-cols-[1fr_400px] gap-6">
          {/* Shot table */}
          <div className="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
            <div className="overflow-auto max-h-[65vh]">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-slate-900">
                  <tr className="text-slate-500 text-xs uppercase border-b border-slate-800">
                    <th className="text-left px-3 py-2 w-12">#</th>
                    <th className="text-left px-3 py-2 w-14">Сцена</th>
                    <th className="text-left px-3 py-2 w-36">Время</th>
                    <th className="text-left px-3 py-2 w-28">Тип</th>
                    <th className="text-left px-3 py-2">Модель</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {shots.map((shot) => (
                    <tr
                      key={shot.index}
                      onClick={() => setSelected(shot)}
                      className={`cursor-pointer transition-colors ${
                        selected?.index === shot.index
                          ? 'bg-indigo-600/20'
                          : 'hover:bg-slate-800/50'
                      }`}
                    >
                      <td className="px-3 py-2 text-slate-400 font-mono text-xs">{shot.index}</td>
                      <td className="px-3 py-2 text-slate-500">{shot.scene_index}</td>
                      <td className="px-3 py-2 text-slate-300 font-mono text-xs">{shot.start} – {shot.end}</td>
                      <td className="px-3 py-2">
                        <span className="text-xs bg-slate-700 px-2 py-0.5 rounded text-slate-300">{shot.visual_type}</span>
                      </td>
                      <td className="px-3 py-2 text-slate-500 text-xs">{shot.image_model || shot.video_model || '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Shot detail */}
          {selected && (
            <div className="bg-slate-900 rounded-xl p-5 border border-slate-800 space-y-3 overflow-auto max-h-[65vh]">
              <h2 className="text-lg font-semibold text-white">Кадр {selected.index} <span className="text-sm text-slate-500 font-normal">(Сцена {selected.scene_index})</span></h2>
              <div className="space-y-2.5">
                {shotFields.map(({ key, label, multiline }) => (
                  <div key={key}>
                    <label className="block text-xs text-slate-500 mb-0.5">{label}</label>
                    {multiline ? (
                      <div className="text-sm text-slate-300 bg-slate-800/50 rounded px-3 py-2 whitespace-pre-wrap min-h-[2.5rem]">
                        {(selected[key] as string) || '—'}
                      </div>
                    ) : (
                      <div className="text-sm text-slate-300">{((selected[key] as string) || '—').toString()}</div>
                    )}
                  </div>
                ))}
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
