import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import type { AssetCard, ProjectInfo } from '../api/types'
import { useSSE } from '../hooks/useSSE'
import { useToast } from '../components/Toast'
import { hasMedia, isExcluded, isIncluded } from '../lib/cards'

interface Context {
  projectId: string
  project: ProjectInfo
  steps: Record<string, boolean>
  setSteps: (steps: Record<string, boolean>) => void
  busy: string | null
  setBusy: (busy: string | null) => void
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

const RUN_LABELS: Record<RunKey, string> = {
  transcription: 'Транскрипция',
  scenes: 'Анализ сцен',
  shots: 'Шот-лист',
  export: 'Экспорт',
}

// Dependency gating: a step can only run if its prerequisites already exist.
function canRun(key: RunKey, steps: Record<string, boolean>): boolean {
  if (key === 'transcription') return true
  if (key === 'scenes') return Boolean(steps.transcript)
  if (key === 'shots') return Boolean(steps.scenes)
  if (key === 'export') return Boolean(steps.asset_board)
  return false
}

export default function Dashboard() {
  const { projectId, project, steps, setSteps, busy, setBusy } = useOutletContext<Context>()
  const { startStream, stopStream } = useSSE()
  const toast = useToast()
  const [cards, setCards] = useState<AssetCard[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const [running, setRunning] = useState<RunKey | null>(null)
  const [progress, setProgress] = useState<number | null>(null)
  const logEndRef = useRef<HTMLDivElement>(null)
  // Locked by a job started on another page of this project.
  const lockedElsewhere = Boolean(busy) && !running

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
    const withMedia = cards.filter(hasMedia).length
    const included = cards.filter(isIncluded).length
    const excluded = cards.filter(isExcluded).length
    const working = cards.filter((c) => c.status === 'generating').length
    const review = cards.filter((c) => c.status === 'needs_review').length
    return { total: cards.length, withMedia, included, excluded, working, review }
  }, [cards])

  const nextRun = useMemo<RunKey | null>(() => {
    if (!steps.transcript) return 'transcription'
    if (!steps.scenes) return 'scenes'
    if (!steps.shot_list || !steps.asset_board) return 'shots'
    return null
  }, [steps])

  const runStep = (key: RunKey) => {
    if (running || busy) return
    if (!canRun(key, steps)) {
      toast.error(`${RUN_LABELS[key]}: сначала выполните предыдущий шаг`)
      return
    }
    const endpoints: Record<RunKey, string> = {
      transcription: api.startTranscription(projectId),
      scenes: api.startSceneAnalysis(projectId),
      shots: api.startShotGeneration(projectId),
      export: api.startExport(projectId),
    }

    setRunning(key)
    setBusy(RUN_LABELS[key])
    setProgress(key === 'shots' ? 0 : null)
    setLogs((prev) => [...prev.slice(-120), `${RUN_LABELS[key]}: старт`])
    startStream(endpoints[key], {
      onLog: (msg) => setLogs((prev) => [...prev.slice(-160), msg]),
      onProgress: (_step, value) => {
        if (typeof value === 'number') setProgress(value)
      },
      onEvent: (event, data) => {
        if (event === 'scene_shots') {
          const shots = Array.isArray(data.shots) ? data.shots.length : 0
          setLogs((prev) => [...prev.slice(-160), `Получены шоты по сцене: ${shots}`])
        }
      },
      onDone: async () => {
        setLogs((prev) => [...prev.slice(-160), `${RUN_LABELS[key]}: готово`])
        toast.success(`${RUN_LABELS[key]}: готово`)
        setRunning(null)
        setBusy(null)
        setProgress(null)
        await refresh()
      },
      onError: (msg) => {
        setLogs((prev) => [...prev.slice(-160), `${RUN_LABELS[key]}: ${msg}`])
        toast.error(`${RUN_LABELS[key]}: ${msg}`)
        setRunning(null)
        setBusy(null)
        setProgress(null)
      },
    })
  }

  const validate = async () => {
    if (running || busy) return
    setLogs((prev) => [...prev.slice(-120), 'QA: старт'])
    try {
      const report = await api.validateProject(projectId)
      const errors = report.summary.counts.error || 0
      const warnings = report.summary.counts.warning || 0
      setLogs((prev) => [...prev.slice(-160), `QA: ${report.summary.status}, errors=${errors}, warnings=${warnings}`])
      if (errors) toast.error(`QA: ${errors} ошибок, ${warnings} предупреждений`)
      else toast.success(`QA: ок (${warnings} предупреждений)`)
      await refresh()
    } catch (e) {
      setLogs((prev) => [...prev.slice(-160), `QA: ${(e as Error).message}`])
      toast.error(`QA: ${(e as Error).message}`)
    }
  }

  // Note: this only detaches the client view. The server task keeps running —
  // there is no server-side cancellation for whisper/LM jobs.
  const stop = () => {
    stopStream()
    setRunning(null)
    setBusy(null)
    setProgress(null)
    setLogs((prev) => [...prev.slice(-160), 'Просмотр отключён (задача продолжает выполняться на сервере)'])
    toast.info('Просмотр отключён. Задача продолжается на сервере.')
  }

  const completion = cards.length ? Math.round((counts.included / cards.length) * 100) : 0
  const pct = progress === null ? null : Math.round(progress * 100)
  const pctLabel = pct === null ? '' : pct + '%'
  const pctWidth = (pct ?? 0) + '%'

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
            disabled={!nextRun || Boolean(running) || lockedElsewhere}
            className="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45"
          >
            {nextRun ? `Продолжить: ${RUN_LABELS[nextRun]}` : 'Пайплайн собран'}
          </button>
          <button
            onClick={validate}
            disabled={Boolean(running) || lockedElsewhere}
            className="px-4 py-2 rounded-lg bg-slate-800 text-slate-200 text-sm font-semibold border border-slate-700 disabled:opacity-45"
          >
            Проверить QA
          </button>
          {running && (
            <button onClick={stop} className="px-4 py-2 rounded-lg bg-red-950 text-red-200 text-sm font-semibold border border-red-800">
              Отключить просмотр
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 xl:grid-cols-6 gap-3">
        {[
          ['Всего', counts.total],
          ['С медиа', counts.withMedia],
          ['В финале', counts.included],
          ['Исключено', counts.excluded],
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
                  disabled={Boolean(running) || lockedElsewhere || (step.run ? !canRun(step.run, steps) : !steps.asset_board)}
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

          {running && (
            <div className="mt-4">
              <div className="mb-1 flex items-center justify-between text-xs text-slate-400">
                <span>{RUN_LABELS[running]}… {pctLabel}</span>
                {progress === null && <span className="text-slate-600">выполняется</span>}
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-slate-950">
                {progress !== null ? (
                  <div className="h-full bg-sky-500 transition-all" style={{ width: pctWidth }} />
                ) : (
                  <div className="h-full w-1/3 animate-[indeterminate_1.2s_ease-in-out_infinite] rounded-full bg-sky-500" />
                )}
              </div>
            </div>
          )}
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
            {(['transcription', 'scenes', 'shots', 'export'] as RunKey[]).map((key) => {
              const allowed = canRun(key, steps)
              return (
                <button
                  key={key}
                  onClick={() => runStep(key)}
                  disabled={Boolean(running) || lockedElsewhere || !allowed}
                  title={allowed ? '' : 'Сначала выполните предыдущий шаг'}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 text-xs text-slate-200 border border-slate-700 disabled:opacity-45"
                >
                  {key === 'transcription' ? 'Transcribe' : key === 'scenes' ? 'Scenes' : key === 'shots' ? 'Shots' : 'Export'}
                </button>
              )
            })}
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

