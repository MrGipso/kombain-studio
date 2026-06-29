import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import type { AssetCard, ProjectInfo } from '../api/types'
import { useSSE } from '../hooks/useSSE'

interface Context {
  projectId: string
  project: ProjectInfo
  steps: Record<string, boolean>
  setSteps: (steps: Record<string, boolean>) => void
}

type RunKey = 'transcription' | 'scenes' | 'shots' | 'export'

const STEP_FLOW = [
  { key: 'transcript', label: 'Транскрипт', run: 'transcription' as RunKey },
  { key: 'scenes', label: 'Сцены', run: 'scenes' as RunKey },
  { key: 'shot_list', label: 'Шоты', run: 'shots' as RunKey },
  { key: 'asset_board', label: 'Доска', run: 'shots' as RunKey },
  { key: 'validation_report', label: 'QA', run: null },
]

const STATUS_LABELS: Record<string, string> = {
  planned: 'План',
  image_ready: 'Image',
  generating: 'В работе',
  generated: 'Готово',
  needs_review: 'Проверить',
  approved: 'Утверждено',
  rejected: 'Отклонено',
  skipped: 'Пропущено',
}

export default function Dashboard() {
  const { projectId, project, steps, setSteps } = useOutletContext<Context>()
  const { startStream, stopStream } = useSSE()
  const [cards, setCards] = useState<AssetCard[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const [running, setRunning] = useState<RunKey | null>(null)
  const logEndRef = useRef<HTMLDivElement>(null)

  const refresh = async () => {
    const [board, state] = await Promise.all([
      api.getAssetBoard(projectId).catch(() => [] as AssetCard[]),
      api.getProjectState(projectId).catch(() => null),
    ])
    setCards(board)
    if (state) setSteps(state.steps)
  }

  useEffect(() => {
    refresh()
  }, [projectId])

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  const counts = useMemo(() => {
    const generated = cards.filter((c) => c.status === 'generated' || c.status === 'approved' || c.approved).length
    const approved = cards.filter((c) => c.approved || c.status === 'approved').length
    const working = cards.filter((c) => c.status === 'generating').length
    const review = cards.filter((c) => c.status === 'needs_review' || c.status === 'rejected').length
    const withMedia = cards.filter((c) => c.image_output || c.video_output || c.remotion_output).length
    return { total: cards.length, generated, approved, working, review, withMedia }
  }, [cards])

  const nextRun = useMemo<RunKey | null>(() => {
    if (!steps.transcript) return 'transcription'
    if (!steps.scenes) return 'scenes'
    if (!steps.shot_list || !steps.asset_board) return 'shots'
    return null
  }, [steps])

  const runStep = (key: RunKey) => {
    if (running) return
    const endpoints: Record<RunKey, string> = {
      transcription: api.startTranscription(projectId),
      scenes: api.startSceneAnalysis(projectId),
      shots: api.startShotGeneration(projectId),
      export: api.startExport(projectId),
    }
    const labels: Record<RunKey, string> = {
      transcription: 'Транскрипция',
      scenes: 'Анализ сцен',
      shots: 'Шот-лист',
      export: 'Экспорт',
    }

    setRunning(key)
    setLogs((prev) => [...prev.slice(-120), `${labels[key]}: старт`])
    startStream(endpoints[key], {
      onLog: (msg) => setLogs((prev) => [...prev.slice(-160), msg]),
      onEvent: (event, data) => {
        if (event === 'scene_shots') {
          const shots = Array.isArray(data.shots) ? data.shots.length : 0
          setLogs((prev) => [...prev.slice(-160), `Получены шоты по сцене: ${shots}`])
        }
      },
      onDone: async () => {
        setLogs((prev) => [...prev.slice(-160), `${labels[key]}: готово`])
        setRunning(null)
        await refresh()
      },
      onError: (msg) => {
        setLogs((prev) => [...prev.slice(-160), `${labels[key]}: ${msg}`])
        setRunning(null)
      },
    })
  }

  const validate = async () => {
    if (running) return
    setLogs((prev) => [...prev.slice(-120), 'QA: старт'])
    try {
      const report = await api.validateProject(projectId)
      setLogs((prev) => [
        ...prev.slice(-160),
        `QA: ${report.summary.status}, errors=${report.summary.counts.error || 0}, warnings=${report.summary.counts.warning || 0}`,
      ])
      await refresh()
    } catch (e) {
      setLogs((prev) => [...prev.slice(-160), `QA: ${(e as Error).message}`])
    }
  }

  const stop = () => {
    stopStream()
    setRunning(null)
    setLogs((prev) => [...prev.slice(-160), 'Остановлено на клиенте'])
  }

  const completion = cards.length ? Math.round((counts.generated / cards.length) * 100) : 0

  return (
    <div className="p-6 space-y-5 max-w-[1500px]">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-white">{project.name}</h1>
          <p className="text-sm text-slate-500 mt-1">{project.preset} / {project.language}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => nextRun && runStep(nextRun)}
            disabled={!nextRun || Boolean(running)}
            className="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45"
          >
            {nextRun ? 'Продолжить пайплайн' : 'Пайплайн собран'}
          </button>
          <button
            onClick={validate}
            disabled={Boolean(running)}
            className="px-4 py-2 rounded-lg bg-slate-800 text-slate-200 text-sm font-semibold border border-slate-700 disabled:opacity-45"
          >
            Проверить QA
          </button>
          {running && (
            <button onClick={stop} className="px-4 py-2 rounded-lg bg-red-950 text-red-200 text-sm font-semibold border border-red-800">
              Стоп
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 xl:grid-cols-6 gap-3">
        {[
          ['Всего', counts.total],
          ['С медиа', counts.withMedia],
          ['Готово', counts.generated],
          ['Утверждено', counts.approved],
          ['В работе', counts.working],
          ['Проверить', counts.review],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg bg-slate-900 border border-slate-800 p-4">
            <div className="text-3xl font-semibold text-white">{value}</div>
            <div className="text-xs text-slate-500 mt-1">{label}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[1.15fr_0.85fr] gap-5">
        <section className="rounded-lg bg-slate-900 border border-slate-800 p-5">
          <div className="flex items-center justify-between gap-4 mb-5">
            <h2 className="text-base font-semibold text-white">Пайплайн</h2>
            <div className="text-sm text-slate-400">{completion}% ассетов готово</div>
          </div>
          <div className="grid grid-cols-5 gap-2">
            {STEP_FLOW.map((step, index) => {
              const done = Boolean(steps[step.key])
              const active = step.run && running === step.run
              return (
                <button
                  key={step.key}
                  onClick={() => step.run ? runStep(step.run) : validate()}
                  disabled={Boolean(running) || (!step.run && !steps.asset_board)}
                  className={`min-h-28 rounded-lg border p-3 text-left transition-colors disabled:opacity-45 ${
                    done ? 'border-emerald-800 bg-emerald-950/35' : active ? 'border-sky-700 bg-sky-950/35' : 'border-slate-800 bg-slate-950/40 hover:border-slate-600'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-slate-500">{String(index + 1).padStart(2, '0')}</span>
                    <span className={`h-2.5 w-2.5 rounded-full ${done ? 'bg-emerald-400' : active ? 'bg-sky-400' : 'bg-slate-700'}`} />
                  </div>
                  <div className="text-sm font-semibold text-white mt-6">{step.label}</div>
                  <div className="text-xs text-slate-500 mt-1">{active ? 'Идет выполнение' : done ? 'Готово' : 'Ожидает'}</div>
                </button>
              )
            })}
          </div>

          <div className="mt-5 h-2 rounded-full bg-slate-950 overflow-hidden">
            <div className="h-full bg-emerald-500" style={{ width: `${completion}%` }} />
          </div>
        </section>

        <section className="rounded-lg bg-slate-900 border border-slate-800 p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-base font-semibold text-white">Статусы доски</h2>
            <Link to="board" className="text-sm text-indigo-300 hover:text-indigo-200">Открыть</Link>
          </div>
          <div className="space-y-2">
            {Object.entries(
              cards.reduce<Record<string, number>>((acc, card) => {
                acc[card.status || 'planned'] = (acc[card.status || 'planned'] || 0) + 1
                return acc
              }, {}),
            ).map(([status, count]) => (
              <div key={status} className="flex items-center gap-3">
                <div className="w-28 text-xs text-slate-400">{STATUS_LABELS[status] || status}</div>
                <div className="h-2 flex-1 rounded-full bg-slate-950 overflow-hidden">
                  <div className="h-full bg-indigo-500" style={{ width: `${cards.length ? (count / cards.length) * 100 : 0}%` }} />
                </div>
                <div className="w-8 text-right text-xs text-slate-500">{count}</div>
              </div>
            ))}
            {!cards.length && <div className="text-sm text-slate-600">Доска появится после генерации шот-листа.</div>}
          </div>
        </section>
      </div>

      <section className="rounded-lg bg-slate-900 border border-slate-800">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-800">
          <h2 className="text-base font-semibold text-white">Live progress</h2>
          <div className="flex gap-2">
            {(['transcription', 'scenes', 'shots', 'export'] as RunKey[]).map((key) => (
              <button
                key={key}
                onClick={() => runStep(key)}
                disabled={Boolean(running)}
                className="px-3 py-1.5 rounded-lg bg-slate-800 text-xs text-slate-200 border border-slate-700 disabled:opacity-45"
              >
                {key === 'transcription' ? 'Transcribe' : key === 'scenes' ? 'Scenes' : key === 'shots' ? 'Shots' : 'Export'}
              </button>
            ))}
          </div>
        </div>
        <div className="h-56 overflow-y-auto p-4 font-mono text-xs text-slate-400 space-y-1">
          {logs.map((line, index) => <div key={`${line}-${index}`}>{line}</div>)}
          {!logs.length && <div className="text-slate-700">Нет активных задач.</div>}
          <div ref={logEndRef} />
        </div>
      </section>
    </div>
  )
}
