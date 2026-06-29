import { useState, useRef, useEffect } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useSSE } from '../hooks/useSSE'
import type { TranscriptSegment } from '../api/types'

interface Context {
  projectId: string
}

export default function Transcription() {
  const { projectId } = useOutletContext<Context>()
  const { startStream } = useSSE()
  const [logs, setLogs] = useState<string[]>([])
  const [transcript, setTranscript] = useState('')
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [running, setRunning] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const fileRef = useRef<HTMLInputElement>(null)
  const logEndRef = useRef<HTMLDivElement>(null)

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
  }, [projectId])

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
    if (!file) return
    try {
      const result = await api.uploadAudio(projectId, file)
      setLogs((prev) => [...prev, `✅ Загружено: ${result.filename}`])
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка загрузки: ${(e as Error).message}`])
    }
  }

  const handleTranscribe = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setLogs((prev) => [...prev, '⚠️ Сначала выберите аудиофайл'])
      return
    }

    try {
      const result = await api.uploadAudio(projectId, file)
      setLogs((prev) => [...prev, `✅ Загружено: ${result.filename}`])
      fileRef.current!.value = ''
    } catch (e) {
      setLogs((prev) => [...prev, `❌ Ошибка загрузки: ${(e as Error).message}`])
      return
    }

    setRunning(true)
    setLogs((prev) => [...prev, '⏳ Запуск транскрипции...'])

    startStream(api.startTranscription(projectId), {
      onLog: (msg) => setLogs((prev) => [...prev, msg]),
      onDone: (data) => {
        setTranscript(data.text as string)
        setSegments(data.segments as TranscriptSegment[])
        const txtLen = typeof data.text === 'string' ? data.text.length : 0
        setLogs((prev) => [...prev, `✅ Транскрипция завершена: ${txtLen} символов`])
        setRunning(false)
      },
      onError: (msg) => {
        setLogs((prev) => [...prev, `❌ ${msg}`])
        setRunning(false)
      },
    })
  }

  return (
    <div className="p-8 space-y-6">
      <h1 className="text-2xl font-bold text-white">Транскрипция</h1>

      {/* Upload */}
      <div className="bg-slate-900 rounded-xl p-6 border border-slate-800 space-y-4">
        <h2 className="text-lg font-semibold text-white">Загрузить аудио</h2>
        <div className="flex gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.opus"
            className="flex-1 px-3 py-2 bg-slate-800 border border-slate-600 rounded-lg text-white text-sm file:mr-3 file:py-1 file:px-3 file:rounded-lg file:border-0 file:bg-indigo-600 file:text-white file:text-sm file:cursor-pointer"
          />
          <button
            onClick={handleUpload}
            disabled={running}
            className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg disabled:opacity-50 cursor-pointer"
          >
            Загрузить
          </button>
        </div>

        {running && (
          <div className="flex items-center gap-3 px-4 py-3 bg-indigo-900/40 border border-indigo-700/50 rounded-lg">
            <span className="w-3 h-3 rounded-full bg-indigo-400 animate-ping" />
            <span className="text-indigo-300 text-sm font-semibold">Транскрибирование... {fmtTime(elapsed)}</span>
            <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
              <div className="h-full w-1/3 bg-indigo-500 rounded-full animate-pulse" />
            </div>
          </div>
        )}

        <button
          onClick={handleTranscribe}
          disabled={running}
          className="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
        >
          {running ? '⏳ Идёт транскрипция...' : '🎤 Запустить транскрипцию'}
        </button>
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
