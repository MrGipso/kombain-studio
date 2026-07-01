import { useState, useEffect } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useSSE } from '../hooks/useSSE'
import { useToast } from '../components/Toast'
import type { MusicState } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

const EMPTY: MusicState = {
  music_tags: '',
  music_bpm: 90,
  music_duration: 90,
  music_enabled: true,
  music_volume_db: -18,
  music_duck: true,
  music_selected: '',
  tracks: [],
}

export default function Music() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const { startStream } = useSSE()
  const toast = useToast()
  const [music, setMusic] = useState<MusicState>(EMPTY)
  const [running, setRunning] = useState(false)
  const [briefing, setBriefing] = useState(false)
  const [logs, setLogs] = useState<string[]>([])
  const lockedElsewhere = Boolean(busy) && !running

  const reload = () =>
    api.getMusic(projectId).then((d) => setMusic((m) => ({ ...m, ...d }))).catch(() => {})

  useEffect(() => {
    api.getMusic(projectId)
      .then((d) => setMusic({ ...EMPTY, ...d }))
      .catch((e) => toast.error(`Не удалось загрузить музыку: ${(e as Error).message}`))
  }, [projectId])

  const patch = (p: Partial<MusicState>) => setMusic((m) => ({ ...m, ...p }))

  const save = async (next?: Partial<MusicState>) => {
    try {
      const saved = await api.updateMusicSettings(projectId, { ...music, ...next })
      setMusic((m) => ({ ...m, ...saved }))
    } catch (e) {
      toast.error(`Сохранение: ${(e as Error).message}`)
    }
  }

  const handleBrief = async () => {
    setBriefing(true)
    try {
      const d = await api.generateMusicBrief(projectId)
      setMusic((m) => ({ ...m, ...d }))
      toast.success('Бриф музыки собран из сцен')
    } catch (e) {
      toast.error(`Бриф: ${(e as Error).message}`)
    } finally {
      setBriefing(false)
    }
  }

  const handleGenerate = async () => {
    if (busy) { toast.error('Дождитесь завершения текущей задачи'); return }
    await save()
    setRunning(true)
    setBusy('Музыка')
    setLogs((prev) => [...prev, '⏳ Генерация музыки...'])
    startStream(api.startMusicGeneration(projectId), {
      onLog: (msg) => setLogs((prev) => [...prev, msg]),
      onDone: () => {
        setLogs((prev) => [...prev, '✅ Музыка готова'])
        toast.success('Музыка сгенерирована')
        reload()
        setRunning(false)
        setBusy(null)
      },
      onError: (msg) => {
        setLogs((prev) => [...prev, `❌ ${msg}`])
        toast.error(`Музыка: ${msg}`)
        setRunning(false)
        setBusy(null)
      },
      onClose: () => { setRunning(false); setBusy(null) },
    })
  }

  const handleSelect = async (filename: string) => {
    try {
      const d = await api.selectMusicTrack(projectId, filename)
      setMusic((m) => ({ ...m, ...d }))
    } catch (e) {
      toast.error(`Выбор трека: ${(e as Error).message}`)
    }
  }

  const handleDelete = async (filename: string) => {
    if (!window.confirm('Удалить этот трек?')) return
    try {
      const d = await api.deleteMusicTrack(projectId, filename)
      setMusic((m) => ({ ...m, ...d }))
      toast.success('Трек удалён')
    } catch (e) {
      toast.error(`Удаление: ${(e as Error).message}`)
    }
  }

  const [trimEdits, setTrimEdits] = useState<Record<string, { start: number; end: number }>>({})
  const trimVal = (t: { filename: string; trim_start?: number; trim_end?: number }) =>
    trimEdits[t.filename] ?? { start: t.trim_start ?? 0, end: t.trim_end ?? 0 }
  const updateTrim = (t: { filename: string; trim_start?: number; trim_end?: number }, field: 'start' | 'end', value: number) => {
    const cur = trimVal(t)
    setTrimEdits((m) => ({ ...m, [t.filename]: { ...cur, [field]: Math.max(0, value) } }))
  }
  const saveTrim = async (t: { filename: string; trim_start?: number; trim_end?: number }) => {
    const { start, end } = trimVal(t)
    try {
      const d = await api.saveMusicTrim(projectId, t.filename, start, end)
      setMusic((m) => ({ ...m, ...d }))
    } catch (e) {
      toast.error(`Обрезка: ${(e as Error).message}`)
    }
  }
  // Player behaviour: only one track plays at a time. When one starts, pause the rest.
  const pauseOthers = (current: HTMLAudioElement) => {
    document.querySelectorAll('audio').forEach((el) => {
      if (el !== current) (el as HTMLAudioElement).pause()
    })
  }

  const previewRegion = (filename: string, start: number, end: number) => {
    const el = document.getElementById(`audio-${filename}`) as HTMLAudioElement | null
    if (!el) return
    el.currentTime = start
    el.play()
    if (end > start) {
      const stop = () => {
        if (el.currentTime >= end) { el.pause(); el.removeEventListener('timeupdate', stop) }
      }
      el.addEventListener('timeupdate', stop)
    }
  }

  const tracks = music.tracks || []

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Музыка</h1>
        <span className="text-xs text-indigo-300 bg-indigo-600/20 px-2.5 py-1 rounded">проектный аудио-слой</span>
      </div>

      {lockedElsewhere && (
        <div className="bg-sky-900/30 border border-sky-700/50 rounded-xl p-4 text-sky-300 text-sm">
          ⏳ Идёт другая задача ({busy}). Дождитесь завершения.
        </div>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)_380px] gap-6 items-start">
        {/* Left: controls */}
        <div className="space-y-6">
          <div className="bg-slate-900 rounded-xl border border-slate-800 p-5 space-y-4">
            {/* Brief */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <label className="text-sm text-slate-400">Теги — стиль, настроение, инструменты</label>
                <button
                  onClick={handleBrief}
                  disabled={briefing || running || lockedElsewhere}
                  className="text-xs px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-50 cursor-pointer"
                >
                  {briefing ? 'Собираю...' : '✨ Собрать из сцен'}
                </button>
              </div>
              <textarea
                value={music.music_tags}
                onChange={(e) => patch({ music_tags: e.target.value })}
                onBlur={() => save()}
                placeholder="cinematic ambient documentary, warm strings, soft piano, calm, instrumental, no vocals"
                className="w-full min-h-[72px] bg-slate-800/60 text-slate-200 text-sm rounded-lg p-3 border border-slate-700 focus:border-indigo-500 outline-none resize-y"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-sm text-slate-400 mb-1.5">Темп, bpm</label>
                <input
                  type="number" value={music.music_bpm}
                  onChange={(e) => patch({ music_bpm: Number(e.target.value) })}
                  onBlur={() => save()}
                  className="w-full bg-slate-800/60 text-slate-200 rounded-lg px-3 py-2 border border-slate-700 focus:border-indigo-500 outline-none"
                />
              </div>
              <div>
                <label className="block text-sm text-slate-400 mb-1.5">Длина бэда, сек</label>
                <input
                  type="number" value={music.music_duration}
                  onChange={(e) => patch({ music_duration: Number(e.target.value) })}
                  onBlur={() => save()}
                  className="w-full bg-slate-800/60 text-slate-200 rounded-lg px-3 py-2 border border-slate-700 focus:border-indigo-500 outline-none"
                />
              </div>
            </div>

            <button
              onClick={handleGenerate}
              disabled={running || lockedElsewhere}
              className="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
            >
              {running ? '⏳ Генерация...' : '🎵 Сгенерировать музыку'}
            </button>

            {/* Export mix settings */}
            <div className="border-t border-slate-800 pt-4 space-y-4">
              <label className="flex items-center justify-between cursor-pointer">
                <span className="text-sm text-slate-200">Добавить в финальный экспорт</span>
                <input
                  type="checkbox" checked={music.music_enabled}
                  onChange={(e) => { patch({ music_enabled: e.target.checked }); save({ music_enabled: e.target.checked }) }}
                  className="w-5 h-5 accent-indigo-500 cursor-pointer"
                />
              </label>

              <div>
                <div className="flex items-center justify-between text-sm mb-1.5">
                  <span className="text-slate-200">Громкость музыки</span>
                  <span className="text-slate-400">{Math.round(music.music_volume_db)} dB</span>
                </div>
                <input
                  type="range" min={-30} max={0} step={1} value={music.music_volume_db}
                  onChange={(e) => patch({ music_volume_db: Number(e.target.value) })}
                  onMouseUp={() => save()}
                  className="w-full accent-indigo-500 cursor-pointer"
                />
              </div>

              <label className="flex items-center justify-between cursor-pointer">
                <div className="flex flex-col">
                  <span className="text-sm text-slate-200">Приглушать под голос</span>
                  <span className="text-xs text-slate-500">музыка тише, когда говорит диктор (дакинг)</span>
                </div>
                <input
                  type="checkbox" checked={music.music_duck}
                  onChange={(e) => { patch({ music_duck: e.target.checked }); save({ music_duck: e.target.checked }) }}
                  className="w-5 h-5 accent-indigo-500 cursor-pointer"
                />
              </label>
            </div>

            <p className="text-xs text-slate-500 pt-1 leading-relaxed">
              Выбранный трек зацикливается до длины видео и подмешивается под голос на экспорте.
            </p>
          </div>

          {/* Logs */}
          {logs.length > 0 && (
            <div className="bg-slate-900/50 rounded-xl p-4 border border-slate-800">
              <h3 className="text-sm text-slate-500 mb-2">Логи</h3>
              <div className="max-h-32 overflow-y-auto text-xs text-slate-500 space-y-0.5 font-mono">
                {logs.map((l, i) => (<div key={i}>{l}</div>))}
              </div>
            </div>
          )}
        </div>

        {/* Right: library */}
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-5">
          <div className="text-sm text-slate-400 mb-3">
            Библиотека {tracks.length > 0 ? `· выбери трек для видео (${tracks.length})` : ''}
          </div>
          {tracks.length === 0 ? (
            <div className="text-sm text-slate-600 py-8 text-center">
              Пока нет треков.<br />Сгенерируй музыку слева.
            </div>
          ) : (
            <div className="space-y-2 max-h-[70vh] overflow-y-auto pr-1">
              {tracks.map((t) => {
                const active = t.filename === music.music_selected
                const tr = trimVal(t)
                return (
                  <div
                    key={t.filename}
                    className={`rounded-lg border p-3 ${active ? 'border-indigo-500 bg-indigo-600/10' : 'border-slate-700 bg-slate-800/40'}`}
                  >
                    <div className="flex items-center gap-3">
                      <input
                        type="radio" name="active-track" checked={active}
                        onChange={() => handleSelect(t.filename)}
                        className="accent-indigo-500 cursor-pointer w-4 h-4"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-slate-200 truncate">{t.tags || t.filename}</div>
                        <div className="text-xs text-slate-500">
                          {t.bpm ? `${t.bpm} bpm · ` : ''}{t.duration ? `${t.duration}s · ` : ''}
                          {t.created ? new Date(t.created * 1000).toLocaleString() : ''}
                          {active ? ' · активный' : ''}
                        </div>
                      </div>
                      <button
                        onClick={() => handleDelete(t.filename)}
                        className="text-xs text-slate-500 hover:text-red-400 px-2 py-1 cursor-pointer"
                      >
                        Удалить
                      </button>
                    </div>
                    <audio
                      id={`audio-${t.filename}`} controls src={t.url} className="w-full mt-2"
                      onPlay={(e) => pauseOthers(e.currentTarget)}
                    />
                    <div className="flex items-center gap-2 mt-2 flex-wrap">
                      <span className="text-xs text-slate-500">Участок:</span>
                      <input
                        type="number" min={0} step={1} value={Math.round(tr.start)}
                        onChange={(e) => updateTrim(t, 'start', Number(e.target.value))}
                        onBlur={() => saveTrim(t)}
                        className="w-14 bg-slate-800/60 text-slate-200 text-xs rounded px-2 py-1 border border-slate-700 focus:border-indigo-500 outline-none"
                      />
                      <span className="text-xs text-slate-500">–</span>
                      <input
                        type="number" min={0} step={1} value={Math.round(tr.end)}
                        onChange={(e) => updateTrim(t, 'end', Number(e.target.value))}
                        onBlur={() => saveTrim(t)}
                        className="w-14 bg-slate-800/60 text-slate-200 text-xs rounded px-2 py-1 border border-slate-700 focus:border-indigo-500 outline-none"
                      />
                      <span className="text-xs text-slate-500">сек</span>
                      <button
                        onClick={() => previewRegion(t.filename, tr.start, tr.end)}
                        className="text-xs px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 cursor-pointer"
                      >
                        ▶ участок
                      </button>
                      {tr.end > tr.start && (
                        <span className="text-xs text-indigo-300">→ {Math.round(tr.end - tr.start)}с</span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
