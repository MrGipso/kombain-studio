import { useState, useRef, useEffect } from 'react'
import { useOutletContext, useSearchParams } from 'react-router-dom'
import * as api from '../api/client'
import { useSSE } from '../hooks/useSSE'
import { useToast } from '../components/toast-context'
import type { TranscriptSegment, TranscriptionSource } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

export default function Transcription() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const [searchParams] = useSearchParams()
  const requestedSource = searchParams.get('source')
  const { startStream } = useSSE()
  const toast = useToast()
  const [logs, setLogs] = useState<string[]>([])
  const [transcript, setTranscript] = useState('')
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [running, setRunning] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [source, setSource] = useState<TranscriptionSource | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const logEndRef = useRef<HTMLDivElement>(null)
  const lockedElsewhere = Boolean(busy) && !running

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  useEffect(() => {
    api.getProject(projectId).then((d) => {
      if (d.transcript) {
        setTranscript(d.transcript)
        setSegments((d.segments || []) as TranscriptSegment[])
        setLogs((prev) => [...prev, `✅ Загружен сохранённый результат (${d.transcript.length} символов, ${d.segments?.length || 0} сегментов)`])
      }
    }).catch(() => {})
    api.getTranscriptionSource(projectId).then((selected) => {
      setSource(selected)
      if (selected.available && requestedSource === 'narration') {
        setLogs((prev) => [...prev, `✅ Озвучка ${selected.filename} принята из раздела «Сценарий»`])
      }
    }).catch(() => setSource(null))
  }, [projectId, requestedSource])

  useEffect(() => {
    if (!running) { setElapsed(0); return }
    const id = setInterval(() => setElapsed((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [running])

  const fmtTime = (s: number) => {
    const m = Math.floor(s / 60)
    const sec = s % 60
    return `${m}:${sec.toString().padStart(2, '0')}`
  }

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) { toast.error('Сначала выберите аудиофайл'); return }
    try {
      const result = await api.uploadAudio(projectId, file)
      const selected = await api.getTranscriptionSource(projectId)
      setSource(selected)
      setLogs((prev) => [...prev, `✅ Загружено: ${result.filename}`])
      toast.success(`Загружено: ${result.filename}`)
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка загрузки: ${(e as Error).message}`])
      toast.error(`Загрузка: ${(e as Error).message}`)
    }
  }

  const startTranscriptionStream = () => {
    setRunning(true)
    setBusy('Транскрипция')
    setLogs((prev) => [...prev, '⏳ Запуск транскрипции...'])

    startStream(api.startTranscription(projectId), {
      onLog: (msg) => setLogs((prev) => [...prev, msg]),
      onDone: (data) => {
        setTranscript(data.text as string)
        setSegments(data.segments as TranscriptSegment[])
        const txtLen = typeof data.text === 'string' ? data.text.length : 0
        setLogs((prev) => [...prev, `✅ Транскрипция завершена: ${txtLen} символов`])
        toast.success('Транскрипция завершена')
        setRunning(false)
        setBusy(null)
      },
      onError: (msg) => {
        setLogs((prev) => [...prev, `❌ ${msg}`])
        toast.error(`Транскрипция: ${msg}`)
        setRunning(false)
        setBusy(null)
      },
    })
  }

  const handleTranscribe = async () => {
    if (busy) { toast.error('Дождитесь завершения текущей задачи'); return }
    const file = fileRef.current?.files?.[0]
    if (!file) {
      toast.error('Сначала выберите аудиофайл (или используйте «Запустить на загруженном»)')
      return
    }

    try {
      const result = await api.uploadAudio(projectId, file)
      const selected = await api.getTranscriptionSource(projectId)
      setSource(selected)
      setLogs((prev) => [...prev, `✅ Загружено: ${result.filename}`])
      fileRef.current!.value = ''
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка загрузки: ${(e as Error).message}`])
      toast.error(`Загрузка: ${(e as Error).message}`)
      return
    }

    startTranscriptionStream()
  }

  // Re-run transcription on audio already saved in the project (voice/) without
  // re-selecting a file — useful after a page reload.
  const handleTranscribeExisting = () => {
    if (busy) { toast.error('Дождитесь завершения текущей задачи'); return }
    startTranscriptionStream()
  }

  return (
    <div className="max-w-[1100px] space-y-6 p-4 sm:p-6 xl:p-8">
      <h1 className="text-2xl font-bold text-white">Транскрипция</h1>

      {source?.available && (
        <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-emerald-700">Выбранный источник</p>
              <h2 className="mt-1 font-semibold text-emerald-300">{source.filename}</h2>
              <p className="mt-1 text-xs text-slate-500">{source.origin === 'omnivoice' ? 'Готовая озвучка OmniVoice из раздела «Сценарий»' : 'Загруженный аудиофайл'}</p>
            </div>
            <span className="rounded-lg border border-emerald-500/30 px-3 py-1.5 text-xs font-semibold text-emerald-700">Готово к транскрипции</span>
          </div>
          <audio controls preload="metadata" src={source.url} className="mt-4 w-full" />
        </div>
      )}

      {/* Upload */}
      <div className="bg-slate-900 rounded-xl p-6 border border-slate-800 space-y-4">
        <h2 className="text-lg font-semibold text-white">Загрузить аудио</h2>
        <div className="flex flex-col gap-3 sm:flex-row">
          <input
            ref={fileRef}
            type="file"
            accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.opus"
            className="flex-1 px-3 py-2 bg-slate-800 border border-slate-600 rounded-lg text-white text-sm file:mr-3 file:py-1 file:px-3 file:rounded-lg file:border-0 file:bg-indigo-600 file:text-white file:text-sm file:cursor-pointer"
          />
          <button
            onClick={handleUpload}
            disabled={running || lockedElsewhere}
            className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg disabled:opacity-50 cursor-pointer"
          >
            Загрузить
          </button>
        </div>

        {lockedElsewhere && (
          <div className="px-4 py-3 bg-sky-900/30 border border-sky-700/50 rounded-lg text-sky-300 text-sm">
            ⏳ Идёт другая задача ({busy}). Дождитесь завершения.
          </div>
        )}

        {running && (
          <div className="flex items-center gap-3 px-4 py-3 bg-indigo-900/40 border border-indigo-700/50 rounded-lg">
            <span className="w-3 h-3 rounded-full bg-indigo-400 animate-ping" />
            <span className="text-indigo-300 text-sm font-semibold shrink-0">Транскрибирование… {fmtTime(elapsed)}</span>
            <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
              <div className="h-full w-1/3 bg-indigo-500 rounded-full animate-[indeterminate_1.2s_ease-in-out_infinite]" />
            </div>
          </div>
        )}

        <div className="flex flex-col gap-3 sm:flex-row">
          <button
            onClick={handleTranscribe}
            disabled={running || lockedElsewhere}
            className="flex-1 py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
          >
            {running ? 'Идёт транскрипция...' : 'Загрузить и транскрибировать'}
          </button>
          <button
            onClick={handleTranscribeExisting}
            disabled={running || lockedElsewhere || !source?.available}
            title="Использовать аудио, уже сохранённое в проекте"
            className="py-3 px-5 bg-slate-700 hover:bg-slate-600 text-white font-semibold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
          >
            Транскрибировать выбранную дорожку
          </button>
        </div>
      </div>

      {/* Transcript */}
      {transcript && (
        <div className="bg-slate-900 rounded-xl p-6 border border-slate-800">
          <h2 className="text-lg font-semibold text-white mb-3">Текст транскрипции</h2>
          <pre className="text-slate-300 text-sm whitespace-pre-wrap font-sans max-h-60 overflow-y-auto">
            {transcript}
          </pre>
        </div>
      )}

      {/* Segments */}
      {segments.length > 0 && (
        <div className="bg-slate-900 rounded-xl p-6 border border-slate-800">
          <h2 className="text-lg font-semibold text-white mb-3">Сегменты ({segments.length})</h2>
          <div className="max-h-40 overflow-y-auto space-y-1">
            {segments.map((s, i) => (
              <div key={i} className="text-sm text-slate-400 flex gap-3">
                <span className="text-slate-600 shrink-0 w-16">{s.start}–{s.end}</span>
                <span>{s.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Logs */}
      <div className="bg-slate-900/50 rounded-xl p-4 border border-slate-800">
        <h3 className="text-sm text-slate-500 mb-2">Логи</h3>
        <div className="max-h-60 overflow-y-auto text-xs text-slate-500 space-y-0.5 font-mono">
          {logs.map((l, i) => (
            <div key={i}>{l}</div>
          ))}
          <div ref={logEndRef} />
          {logs.length === 0 && <div className="text-slate-700">Ожидание действий...</div>}
        </div>
      </div>
    </div>
  )
}

