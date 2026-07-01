import { useEffect, useRef, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import type { TimelineMedia } from '../api/types'
import { useSSE } from '../hooks/useSSE'
import { useToast } from '../components/Toast'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

export default function Preview() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const { startStream } = useSSE()
  const toast = useToast()
  const [media, setMedia] = useState<TimelineMedia | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [exporting, setExporting] = useState(false)
  const [selectedUrl, setSelectedUrl] = useState('')
  const logEndRef = useRef<HTMLDivElement>(null)
  const lockedElsewhere = Boolean(busy) && !exporting

  const load = async () => {
    const next = await api.getTimelineMedia(projectId).catch(() => null)
    setMedia(next)
    if (next?.export_url) setSelectedUrl(next.export_url)
    else if (next?.items[0]?.url) setSelectedUrl(next.items[0].url)
  }

  useEffect(() => {
    load()
  }, [projectId])

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  const handleExport = () => {
    if (exporting || busy) { if (busy && !exporting) toast.error('Дождитесь завершения текущей задачи'); return }
    setExporting(true)
    setBusy('Экспорт')
    setLogs((prev) => [...prev.slice(-120), 'Экспорт: старт'])

    startStream(api.startExport(projectId), {
      onLog: (msg) => setLogs((prev) => [...prev.slice(-180), msg]),
      onDone: async (data) => {
        setLogs((prev) => [...prev.slice(-180), `Экспорт: готово ${data.path || ''}`])
        toast.success('Экспорт MP4 готов')
        setExporting(false)
        setBusy(null)
        await load()
      },
      onError: (msg) => {
        setLogs((prev) => [...prev.slice(-180), `Экспорт: ${msg}`])
        toast.error(`Экспорт: ${msg}`)
        setExporting(false)
        setBusy(null)
      },
    })
  }

  const selectedIsVideo = selectedUrl.toLowerCase().match(/\.(mp4|webm|mov)(\?|$)/)
  const selectedIsImage = selectedUrl.toLowerCase().match(/\.(png|jpg|jpeg|webp)(\?|$)/)

  return (
    <div className="p-6 space-y-5 max-w-[1600px]">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-white">Preview & Export</h1>
          <p className="text-sm text-slate-500 mt-1">Финальное видео (со звуком, по таймлайну) · {media?.items.length || 0} клипов с медиа</p>
        </div>
        <div className="flex gap-2">
          <Link to="../timeline" className="px-4 py-2 rounded-lg bg-slate-800 border border-slate-700 text-sm text-slate-100">
            Timeline
          </Link>
          <button onClick={handleExport} disabled={exporting || lockedElsewhere} title="Финальная сборка через ffmpeg: кадры в финале + озвучка + оверлеи" className="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45">
            {exporting ? 'Собираю…' : 'Собрать финал (MP4)'}
          </button>
          {media?.export_url && (
            <a href={api.getDownloadUrl(projectId)} download className="px-4 py-2 rounded-lg bg-emerald-700 text-white text-sm font-semibold">
              Download
            </a>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(620px,1fr)_420px] gap-5">
        <section className="rounded-lg bg-slate-900 border border-slate-800 overflow-hidden">
          <div className="aspect-video bg-black flex items-center justify-center">
            {selectedUrl && selectedIsVideo && <video key={selectedUrl} src={selectedUrl} controls className="h-full w-full object-contain" />}
            {selectedUrl && selectedIsImage && <img src={selectedUrl} alt="" className="h-full w-full object-contain" />}
            {!selectedUrl && <div className="text-sm text-slate-700">Нет файла для предпросмотра</div>}
          </div>
          <div className="border-t border-slate-800 p-4">
            <div className="text-sm font-semibold text-white">{media?.export_url && selectedUrl === media.export_url ? 'Final export' : 'Clip preview'}</div>
            <div className="mt-1 truncate text-xs text-slate-500">{selectedUrl || '-'}</div>
          </div>
        </section>

        <aside className="space-y-5">
          <section className="rounded-lg bg-slate-900 border border-slate-800 p-4">
            <h2 className="text-base font-semibold text-white mb-3">Project media</h2>
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {media?.export_url && (
                <button onClick={() => setSelectedUrl(media.export_url)} className={`w-full rounded-lg border px-3 py-2 text-left ${selectedUrl === media.export_url ? 'border-indigo-500 bg-indigo-950/30' : 'border-slate-800 bg-slate-950/50'}`}>
                  <div className="text-sm text-white">Final MP4</div>
                  <div className="truncate text-xs text-slate-500">{media.export_path}</div>
                </button>
              )}
              {media?.items.map((item) => (
                <button
                  key={`${item.card_index}-${item.path}`}
                  onClick={() => setSelectedUrl(item.url)}
                  className={`w-full rounded-lg border px-3 py-2 text-left ${selectedUrl === item.url ? 'border-indigo-500 bg-indigo-950/30' : 'border-slate-800 bg-slate-950/50 hover:border-slate-700'}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm text-white">Shot {item.shot_index}</span>
                    <span className="text-xs text-slate-500">{item.media_type}</span>
                  </div>
                  <div className="truncate text-xs text-slate-500 mt-1">{item.title || item.path}</div>
                </button>
              ))}
              {!media?.items.length && !media?.export_url && <div className="py-10 text-center text-sm text-slate-600">Файлы появятся после генерации ассетов.</div>}
            </div>
          </section>

          <section className="rounded-lg bg-slate-900 border border-slate-800 p-4">
            <h2 className="text-base font-semibold text-white mb-3">Voice track</h2>
            {media?.voice_url ? (
              <>
                <audio src={media.voice_url} controls className="w-full" />
                <div className="mt-2 truncate text-xs text-slate-500">{media.voice_path}</div>
              </>
            ) : (
              <div className="text-sm text-slate-600">Озвучка не найдена.</div>
            )}
          </section>
        </aside>
      </div>

      <section className="rounded-lg bg-slate-900 border border-slate-800">
        <div className="px-4 py-3 border-b border-slate-800">
          <h2 className="text-base font-semibold text-white">Export log</h2>
        </div>
        <div className="h-64 overflow-y-auto p-4 font-mono text-xs text-slate-400 space-y-1">
          {logs.map((line, index) => <div key={`${line}-${index}`}>{line}</div>)}
          {!logs.length && <div className="text-slate-700">Нет активного экспорта.</div>}
          <div ref={logEndRef} />
        </div>
      </section>
    </div>
  )
}

