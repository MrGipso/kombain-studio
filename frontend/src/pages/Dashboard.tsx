import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import type { AssetCard, JobRecord, ProjectInfo, RunLogRecord } from '../api/types'
import { useSSE } from '../hooks/useSSE'
import { useToast } from '../components/toast-context'
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
type IconName =
  | 'activity'
  | 'arrow'
  | 'check'
  | 'clock'
  | 'eye'
  | 'film'
  | 'history'
  | 'layers'
  | 'minus'
  | 'review'
  | 'terminal'
  | 'work'

const STEP_FLOW = [
  { key: 'transcript', label: 'Транскрипция', run: 'transcription' as RunKey },
  { key: 'scenes', label: 'Сцены', run: 'scenes' as RunKey },
  { key: 'shot_list', label: 'План кадров', run: 'shots' as RunKey },
  { key: 'asset_board', label: 'Материалы', run: 'shots' as RunKey },
  { key: 'validation_report', label: 'Проверка', run: null },
]

const STATUS_LABELS: Record<string, string> = {
  planned: 'Запланировано',
  image_ready: 'Изображение готово',
  generating: 'Создаётся',
  generated: 'Готово',
  needs_review: 'Нужна проверка',
  approved: 'Утверждено',
  rejected: 'Отклонено',
  skipped: 'Пропущено',
}

const RUN_LABELS: Record<RunKey, string> = {
  transcription: 'Транскрипция',
  scenes: 'Анализ сцен',
  shots: 'План кадров',
  export: 'Черновой экспорт',
}

const NEXT_ACTION_COPY: Record<RunKey, { title: string; description: string }> = {
  transcription: {
    title: 'Подготовить транскрипцию',
    description: 'Распознайте загруженную голосовую дорожку — это основа всех следующих этапов.',
  },
  scenes: {
    title: 'Разделить материал на сцены',
    description: 'Соберите смысловую структуру ролика на основе готовой транскрипции.',
  },
  shots: {
    title: 'Собрать план кадров',
    description: 'Создайте кадры и доску материалов, чтобы перейти к производству и монтажу.',
  },
  export: {
    title: 'Собрать черновую версию',
    description: 'Экспортируйте текущий монтаж для просмотра и редакторской проверки.',
  },
}

const JOB_STATUS_LABELS: Record<string, string> = {
  running: 'Выполняется',
  succeeded: 'Завершено',
  failed: 'Ошибка',
  cancelled: 'Отменено',
  interrupted: 'Прервано',
}

const RESOURCE_LABELS: Record<string, string> = {
  whisper: 'Распознавание',
  llm: 'Языковая модель',
  comfy: 'Генерация',
  ffmpeg: 'Кодирование',
  project: 'Проект',
}

const EVENT_LABELS: Record<string, string> = {
  log: 'Сообщение',
  start: 'Запуск',
  started: 'Запуск',
  done: 'Завершено',
  completed: 'Завершено',
  error: 'Ошибка',
  progress: 'Прогресс',
  cancelled: 'Отменено',
}

const LEVEL_LABELS: Record<string, string> = {
  info: 'Информация',
  warning: 'Предупреждение',
  error: 'Ошибка',
}

// Dependency gating: a step can only run if its prerequisites already exist.
function canRun(key: RunKey, steps: Record<string, boolean>): boolean {
  if (key === 'transcription') return true
  if (key === 'scenes') return Boolean(steps.transcript)
  if (key === 'shots') return Boolean(steps.scenes)
  if (key === 'export') return Boolean(steps.asset_board)
  return false
}

function Icon({ name, className = 'h-5 w-5' }: { name: IconName; className?: string }) {
  let content: ReactNode
  switch (name) {
    case 'activity':
      content = <path d="M3 12h4l2.2-6 4.2 12 2.1-6H21" />
      break
    case 'arrow':
      content = <><path d="M5 12h14" /><path d="m13 6 6 6-6 6" /></>
      break
    case 'check':
      content = <path d="m5 12 4 4L19 6" />
      break
    case 'clock':
      content = <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>
      break
    case 'eye':
      content = <><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" /><circle cx="12" cy="12" r="2.5" /></>
      break
    case 'film':
      content = <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M7 5v14M17 5v14M3 9h4M17 9h4M3 15h4M17 15h4" /></>
      break
    case 'history':
      content = <><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5M12 7v5l3 2" /></>
      break
    case 'layers':
      content = <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></>
      break
    case 'minus':
      content = <path d="M5 12h14" />
      break
    case 'review':
      content = <><path d="M9 3h6l1 2h3v16H5V5h3l1-2Z" /><path d="M9 12h6M9 16h4" /></>
      break
    case 'terminal':
      content = <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m7 9 3 3-3 3M13 15h4" /></>
      break
    case 'work':
      content = <><path d="M4 7h16v12H4z" /><path d="M9 7V4h6v3M4 12h16" /></>
      break
  }
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {content}
    </svg>
  )
}

function formatProjectLanguage(language: string): string {
  const normalized = (language || '').toLowerCase()
  if (normalized === 'ru') return 'Русский'
  if (normalized === 'en') return 'Английский'
  if (normalized === 'uk' || normalized === 'ua') return 'Украинский'
  if (normalized === 'auto') return 'Определяется автоматически'
  return 'Другой язык'
}

function formatPreset(preset: string): string {
  const normalized = (preset || '').toLowerCase()
  if (normalized === 'tutorial') return 'Обучающий ролик'
  if (normalized === 'documentary') return 'Документальный ролик'
  if (normalized === 'universal') return 'Универсальный шаблон'
  return 'Пользовательский шаблон'
}

function formatTime(value: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function localizeJobName(value: string): string {
  return (value || 'Задача')
    .replace('Asset Board', 'доски материалов')
    .replace('SFX', 'звуковых эффектов')
    .replace('Draft export', 'Черновой экспорт')
    .replace('Master export', 'Финальный экспорт')
    .replace('Delivery 1080p', 'Версия 1080p')
    .replace('Preview 720p', 'Предпросмотр 720p')
    .replace('Master 1080p', 'Финальная версия 1080p')
}

function localizeJobMessage(job: JobRecord): string {
  const message = job.error || job.message
  if (!message || message === 'Completed') return JOB_STATUS_LABELS[job.status] || 'Состояние обновлено'
  if (message === 'Failed') return 'Задача завершилась с ошибкой'
  if (message === 'Cancelled') return 'Задача отменена'
  return message
}

function statusBarClass(status: string): string {
  if (status === 'generated' || status === 'approved' || status === 'image_ready') return 'bg-emerald-500'
  if (status === 'generating') return 'bg-sky-500'
  if (status === 'needs_review') return 'bg-amber-500'
  if (status === 'rejected' || status === 'skipped') return 'bg-slate-600'
  return 'bg-indigo-500'
}

export default function Dashboard() {
  const { projectId, project, steps, setSteps, busy, setBusy } = useOutletContext<Context>()
  const { startStream } = useSSE()
  const toast = useToast()
  const [cards, setCards] = useState<AssetCard[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const [running, setRunning] = useState<RunKey | null>(null)
  const [progress, setProgress] = useState<number | null>(null)
  const [jobItems, setJobItems] = useState<JobRecord[]>([])
  const [runLogItems, setRunLogItems] = useState<RunLogRecord[]>([])
  const [initialLoaded, setInitialLoaded] = useState(false)
  const logContainerRef = useRef<HTMLDivElement>(null)
  // Locked by a job started on another page of this project.
  const lockedElsewhere = Boolean(busy) && !running

  const refresh = useCallback(async () => {
    const [board, state, jobState, runLogState] = await Promise.all([
      api.getAssetBoard(projectId).catch(() => [] as AssetCard[]),
      api.getProjectState(projectId).catch(() => null),
      api.getProjectJobs(projectId, 8).catch(() => null),
      api.getProjectRunLogs(projectId, 40).catch(() => null),
    ])
    setCards(board)
    if (state) setSteps(state.steps)
    if (jobState) setJobItems(jobState.items)
    if (runLogState) setRunLogItems(runLogState.items)
    setInitialLoaded(true)
  }, [projectId, setSteps])

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    const timer = window.setInterval(() => {
      // Keep the durable job history independent from diagnostic log reads:
      // a damaged/locked JSONL file must not stop the existing job refresh.
      api.getProjectJobs(projectId, 8).then((state) => setJobItems(state.items)).catch(() => {})
      api.getProjectRunLogs(projectId, 40).then((state) => setRunLogItems(state.items)).catch(() => {})
    }, 2500)
    return () => window.clearInterval(timer)
  }, [projectId])

  useEffect(() => {
    if (!logs.length || !logContainerRef.current) return
    logContainerRef.current.scrollTo({
      top: logContainerRef.current.scrollHeight,
      behavior: 'smooth',
    })
  }, [logs])

  const counts = useMemo(() => {
    const withMedia = cards.filter(hasMedia).length
    const included = cards.filter(isIncluded).length
    const excluded = cards.filter(isExcluded).length
    const working = cards.filter((card) => card.status === 'generating').length
    const review = cards.filter((card) => card.status === 'needs_review').length
    return { total: cards.length, withMedia, included, excluded, working, review }
  }, [cards])

  const statusSummary = useMemo(() => {
    const grouped = cards.reduce<Record<string, number>>((acc, card) => {
      const status = card.status || 'planned'
      acc[status] = (acc[status] || 0) + 1
      return acc
    }, {})
    return Object.entries(grouped).sort(([, left], [, right]) => right - left)
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
      toast.error(`${RUN_LABELS[key]}: сначала выполните предыдущий этап`)
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
    setLogs((previous) => [...previous.slice(-120), `${RUN_LABELS[key]}: запуск`])
    startStream(endpoints[key], {
      onLog: (message) => setLogs((previous) => [...previous.slice(-160), message]),
      onProgress: (_step, value) => {
        if (typeof value === 'number') setProgress(value)
      },
      onEvent: (event, data) => {
        if (event === 'scene_shots') {
          const shots = Array.isArray(data.shots) ? data.shots.length : 0
          setLogs((previous) => [...previous.slice(-160), `Получены кадры для сцены: ${shots}`])
        }
      },
      onDone: async () => {
        setLogs((previous) => [...previous.slice(-160), `${RUN_LABELS[key]}: завершено`])
        toast.success(`${RUN_LABELS[key]}: завершено`)
        setRunning(null)
        setBusy(null)
        setProgress(null)
        await refresh()
      },
      onError: (message) => {
        setLogs((previous) => [...previous.slice(-160), `${RUN_LABELS[key]}: ${message}`])
        toast.error(`${RUN_LABELS[key]}: ${message}`)
        setRunning(null)
        setBusy(null)
        setProgress(null)
      },
    })
  }

  const validate = async () => {
    if (running || busy) return
    setLogs((previous) => [...previous.slice(-120), 'Проверка качества: запуск'])
    try {
      const report = await api.validateProject(projectId)
      const errors = report.summary.counts.error || 0
      const warnings = report.summary.counts.warning || 0
      setLogs((previous) => [
        ...previous.slice(-160),
        `Проверка качества: ошибок — ${errors}, предупреждений — ${warnings}`,
      ])
      if (errors) toast.error(`Проверка качества: ${errors} ошибок, ${warnings} предупреждений`)
      else toast.success(`Проверка пройдена (${warnings} предупреждений)`)
      await refresh()
    } catch (error) {
      setLogs((previous) => [...previous.slice(-160), `Проверка качества: ${(error as Error).message}`])
      toast.error(`Проверка качества: ${(error as Error).message}`)
    }
  }

  const cancel = async () => {
    try {
      const result = await api.cancelJob(projectId)
      toast.info(result.cancelled ? `Запрошена отмена: ${result.cancelled}` : 'Активная задача не найдена')
    } catch (error) {
      toast.error((error as Error).message)
    }
  }

  const materialCompletion = cards.length ? Math.round((counts.included / cards.length) * 100) : 0
  const completedStages = STEP_FLOW.filter((step) => Boolean(steps[step.key])).length
  const pipelineCompletion = Math.round((completedStages / STEP_FLOW.length) * 100)
  const pct = progress === null ? null : Math.round(progress * 100)
  const activeTask = running ? RUN_LABELS[running] : busy ? localizeJobName(busy) : null
  const nextCopy = nextRun ? NEXT_ACTION_COPY[nextRun] : null

  const projectStatus = activeTask
    ? {
        label: 'Задача выполняется',
        title: activeTask,
        description: 'Можно покинуть эту страницу: состояние задачи сохранится и восстановится автоматически.',
        classes: 'border-sky-800/70 bg-sky-950/30 text-sky-300',
        dot: 'bg-sky-400 animate-pulse',
      }
    : nextCopy
      ? {
          label: completedStages ? 'Производство продолжается' : 'Проект готов к запуску',
          title: nextCopy.title,
          description: nextCopy.description,
          classes: 'border-indigo-800/70 bg-indigo-950/25 text-indigo-300',
          dot: 'bg-indigo-400',
        }
      : !steps.validation_report
        ? {
            label: 'Материалы собраны',
            title: 'Провести проверку качества',
            description: 'Проверьте таймкоды, медиа и готовность кадров перед просмотром итоговой версии.',
            classes: 'border-amber-800/70 bg-amber-950/25 text-amber-300',
            dot: 'bg-amber-400',
          }
        : {
            label: 'Пайплайн завершён',
            title: 'Перейти к просмотру и экспорту',
            description: 'Основные этапы готовы. Проверьте монтаж и соберите нужную версию ролика.',
            classes: 'border-emerald-800/70 bg-emerald-950/25 text-emerald-300',
            dot: 'bg-emerald-400',
          }

  const kpis: Array<{
    label: string
    value: number | string
    note: string
    icon: IconName
    iconClass: string
  }> = [
    { label: 'Кадров в плане', value: counts.total, note: 'всего', icon: 'layers', iconClass: 'text-indigo-300' },
    { label: 'С материалами', value: counts.withMedia, note: 'файлы назначены', icon: 'film', iconClass: 'text-sky-300' },
    { label: 'В монтаже', value: counts.included, note: 'войдут в экспорт', icon: 'check', iconClass: 'text-emerald-300' },
    { label: 'Нужна проверка', value: counts.review, note: 'требуют решения', icon: 'review', iconClass: 'text-amber-300' },
    { label: 'Создаётся', value: counts.working, note: 'прямо сейчас', icon: 'activity', iconClass: 'text-sky-300' },
    { label: 'Исключено', value: counts.excluded, note: 'не войдут в монтаж', icon: 'minus', iconClass: 'text-slate-400' },
  ]

  return (
    <div className="mx-auto max-w-[1500px] space-y-6 p-4 sm:p-6 lg:p-8">
      <header className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-medium uppercase tracking-[0.18em] text-slate-500">Рабочая область проекта</p>
            <h1 className="mt-1 truncate text-2xl font-semibold tracking-tight text-white sm:text-3xl">{project.name}</h1>
            <p className="mt-1 text-sm text-slate-500">
              {formatPreset(project.preset)} <span aria-hidden="true">·</span> {formatProjectLanguage(project.language)}
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs text-slate-500" aria-label={`До проверки завершено этапов: ${completedStages} из ${STEP_FLOW.length}`}>
            <span className="font-medium text-slate-300">{completedStages}/{STEP_FLOW.length}</span>
            <span>до проверки</span>
          </div>
        </div>

        <section
          aria-labelledby="project-next-action"
          className={`overflow-hidden rounded-2xl border p-5 shadow-lg shadow-black/10 sm:p-6 ${projectStatus.classes}`}
        >
          <div className="flex flex-col gap-5 lg:flex-row lg:items-center lg:justify-between">
            <div className="min-w-0 max-w-3xl">
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em]">
                <span className={`h-2.5 w-2.5 rounded-full ${projectStatus.dot}`} aria-hidden="true" />
                <span role="status" aria-live="polite">{projectStatus.label}</span>
              </div>
              <h2 id="project-next-action" className="mt-3 text-xl font-semibold text-white sm:text-2xl">{projectStatus.title}</h2>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-300/90">{projectStatus.description}</p>
              {activeTask && (
                <div className="mt-4 max-w-xl" aria-live="polite">
                  <div className="mb-1.5 flex items-center justify-between text-xs text-slate-400">
                    <span>{pct === null ? 'Выполнение без точной оценки' : `Выполнено ${pct}%`}</span>
                    <span>{activeTask}</span>
                  </div>
                  <div
                    className="h-2 overflow-hidden rounded-full bg-slate-950/80"
                    role="progressbar"
                    aria-label={`Прогресс задачи: ${activeTask}`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={pct ?? undefined}
                  >
                    {progress !== null ? (
                      <div className="h-full rounded-full bg-sky-400 transition-all" style={{ width: `${pct}%` }} />
                    ) : (
                      <div className="h-full w-1/3 animate-[indeterminate_1.2s_ease-in-out_infinite] rounded-full bg-sky-400" />
                    )}
                  </div>
                </div>
              )}
            </div>

            <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row lg:justify-end">
              {activeTask ? (
                <button
                  type="button"
                  disabled
                  aria-label={`Выполняется задача: ${activeTask}`}
                  className="inline-flex min-h-11 cursor-not-allowed items-center justify-center gap-2 rounded-lg border border-sky-700/70 bg-sky-950/60 px-4 py-2.5 text-sm font-semibold text-sky-200 opacity-80"
                >
                  <Icon name="activity" className="h-4 w-4" />
                  Задача выполняется
                </button>
              ) : nextRun ? (
                <button
                  type="button"
                  onClick={() => runStep(nextRun)}
                  disabled={Boolean(running) || lockedElsewhere}
                  className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-300 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  Продолжить: {RUN_LABELS[nextRun]}
                  <Icon name="arrow" className="h-4 w-4" />
                </button>
              ) : !steps.validation_report ? (
                <button
                  type="button"
                  onClick={validate}
                  disabled={Boolean(running) || lockedElsewhere}
                  className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-amber-500 px-4 py-2.5 text-sm font-semibold text-slate-950 transition-colors hover:bg-amber-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-300 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  <Icon name="review" className="h-4 w-4" />
                  Проверить качество
                </button>
              ) : (
                <Link
                  to="preview"
                  className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-emerald-500 px-4 py-2.5 text-sm font-semibold text-slate-950 transition-colors hover:bg-emerald-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-300"
                >
                  Открыть просмотр
                  <Icon name="arrow" className="h-4 w-4" />
                </Link>
              )}

              {(nextRun || steps.validation_report) && (
                <button
                  type="button"
                  onClick={validate}
                  disabled={Boolean(running) || lockedElsewhere || !steps.asset_board}
                  className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg border border-slate-600/80 bg-slate-900/70 px-4 py-2.5 text-sm font-semibold text-slate-200 transition-colors hover:border-slate-500 hover:bg-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-400 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  <Icon name="review" className="h-4 w-4" />
                  {steps.validation_report ? 'Проверить повторно' : 'Проверить качество'}
                </button>
              )}

              {(running || busy) && (
                <button
                  type="button"
                  onClick={cancel}
                  className="inline-flex min-h-11 items-center justify-center rounded-lg border border-red-800 bg-red-950/70 px-4 py-2.5 text-sm font-semibold text-red-200 transition-colors hover:bg-red-900/70 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500"
                >
                  Отменить задачу
                </button>
              )}
            </div>
          </div>
        </section>
      </header>

      <section aria-label="Ключевые показатели проекта" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {kpis.map((item) => (
          <div key={item.label} className="rounded-xl border border-slate-800 bg-slate-900/80 p-3.5 sm:p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="text-2xl font-semibold tabular-nums text-white sm:text-3xl">{item.value}</div>
              <Icon name={item.icon} className={`h-5 w-5 ${item.iconClass}`} />
            </div>
            <div className="mt-2 text-xs font-medium text-slate-300">{item.label}</div>
            <div className="mt-0.5 hidden text-[11px] text-slate-600 sm:block">{item.note}</div>
          </div>
        ))}
      </section>

      <section aria-labelledby="pipeline-heading" className="rounded-2xl border border-slate-800 bg-slate-900 p-4 sm:p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">Производственный маршрут</p>
            <h2 id="pipeline-heading" className="mt-1 text-lg font-semibold text-white">Этапы проекта</h2>
            <p className="mt-1 text-sm text-slate-500">Запускайте этапы последовательно — результат каждого становится основой следующего.</p>
          </div>
          <div className="shrink-0 text-sm text-slate-400">
            <span className="font-semibold tabular-nums text-white">{pipelineCompletion}%</span> до проверки
          </div>
        </div>

        <div
          className="mt-4 h-1.5 overflow-hidden rounded-full bg-slate-950"
          role="progressbar"
          aria-label="Готовность этапов проекта"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pipelineCompletion}
        >
          <div className="h-full rounded-full bg-indigo-500 transition-all" style={{ width: `${pipelineCompletion}%` }} />
        </div>

        <ol className="mt-5 grid grid-cols-1 gap-2 sm:grid-cols-5">
          {STEP_FLOW.map((step, index) => {
            const done = Boolean(steps[step.key])
            const active = Boolean(step.run && running === step.run)
            const available = step.run ? canRun(step.run, steps) : Boolean(steps.asset_board)
            const stateLabel = active ? 'Выполняется' : done ? 'Завершено' : available ? 'Можно запустить' : 'Ожидает предыдущий этап'
            return (
              <li key={step.key} className="min-w-0">
                <button
                  type="button"
                  onClick={() => step.run ? runStep(step.run) : validate()}
                  disabled={Boolean(running) || lockedElsewhere || !available}
                  aria-label={`${step.label}: ${stateLabel}`}
                  aria-current={active ? 'step' : undefined}
                  className={`group flex min-h-20 w-full items-center gap-3 rounded-xl border p-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-32 sm:flex-col sm:items-start sm:justify-between ${
                    active
                      ? 'border-sky-700 bg-sky-950/30'
                      : done
                        ? 'border-emerald-800/70 bg-emerald-950/25 hover:bg-emerald-950/40'
                        : available
                          ? 'border-slate-700 bg-slate-950/40 hover:border-indigo-700 hover:bg-slate-950/70'
                          : 'border-slate-800 bg-slate-950/20'
                  }`}
                >
                  <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full border text-xs font-semibold tabular-nums ${
                    active
                      ? 'border-sky-700 bg-sky-950 text-sky-300'
                      : done
                        ? 'border-emerald-700 bg-emerald-950 text-emerald-300'
                        : 'border-slate-700 bg-slate-900 text-slate-500'
                  }`}>
                    {active
                      ? <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-sky-400" aria-hidden="true" />
                      : done
                        ? <Icon name="check" className="h-4 w-4" />
                        : String(index + 1).padStart(2, '0')}
                  </span>
                  <span className="min-w-0 sm:w-full">
                    <span className="block truncate text-sm font-semibold text-slate-100">{step.label}</span>
                    <span className={`mt-0.5 block text-xs ${active ? 'text-sky-300' : done ? 'text-emerald-400' : 'text-slate-500'}`}>{stateLabel}</span>
                  </span>
                </button>
              </li>
            )
          })}
        </ol>
      </section>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.2fr)_minmax(22rem,0.8fr)]">
        <section aria-labelledby="materials-heading" className="overflow-hidden rounded-2xl border border-slate-800 bg-slate-900">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-800 px-4 py-4 sm:px-5">
            <div>
              <p className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">Производство</p>
              <h2 id="materials-heading" className="mt-1 text-base font-semibold text-white">Материалы для монтажа</h2>
              <p className="mt-1 text-sm text-slate-500">Состояние кадров на доске и их готовность к экспорту.</p>
            </div>
            <Link
              to="board"
              className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-semibold text-indigo-300 transition-colors hover:bg-indigo-950/40 hover:text-indigo-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400"
            >
              Открыть доску
              <Icon name="arrow" className="h-4 w-4" />
            </Link>
          </div>

          <div className="p-4 sm:p-5">
            {!initialLoaded ? (
              <div role="status" className="space-y-3" aria-label="Загрузка материалов">
                {[1, 2, 3].map((item) => <div key={item} className="h-8 animate-pulse rounded-lg bg-slate-800/70" />)}
              </div>
            ) : cards.length ? (
              <>
                <div className="space-y-3">
                  {statusSummary.map(([status, count]) => (
                    <div key={status} className="grid grid-cols-[minmax(8rem,12rem)_minmax(0,1fr)_2.5rem] items-center gap-3">
                      <div className="truncate text-xs font-medium text-slate-300" title={STATUS_LABELS[status] || 'Другой статус'}>
                        {STATUS_LABELS[status] || 'Другой статус'}
                      </div>
                      <div
                        className="h-2 overflow-hidden rounded-full bg-slate-950"
                        role="progressbar"
                        aria-label={`${STATUS_LABELS[status] || 'Другой статус'}: ${count}`}
                        aria-valuemin={0}
                        aria-valuemax={cards.length}
                        aria-valuenow={count}
                      >
                        <div className={`h-full rounded-full ${statusBarClass(status)}`} style={{ width: `${(count / cards.length) * 100}%` }} />
                      </div>
                      <div className="text-right text-xs font-semibold tabular-nums text-slate-400">{count}</div>
                    </div>
                  ))}
                </div>
                <div className="mt-5 rounded-xl border border-slate-800 bg-slate-950/45 p-4">
                  <div className="flex items-center justify-between gap-3 text-sm">
                    <span className="text-slate-400">Готовность материалов к монтажу</span>
                    <span className="font-semibold tabular-nums text-white">{materialCompletion}%</span>
                  </div>
                  <div
                    className="mt-2 h-2 overflow-hidden rounded-full bg-slate-900"
                    role="progressbar"
                    aria-label="Готовность материалов к монтажу"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={materialCompletion}
                  >
                    <div className="h-full rounded-full bg-emerald-500 transition-all" style={{ width: `${materialCompletion}%` }} />
                  </div>
                  <p className="mt-2 text-xs text-slate-600">{counts.included} из {counts.total} кадров имеют медиа и не исключены из монтажа.</p>
                </div>
              </>
            ) : (
              <div className="flex min-h-52 flex-col items-center justify-center rounded-xl border border-dashed border-slate-700 bg-slate-950/30 px-6 py-8 text-center">
                <span className="flex h-12 w-12 items-center justify-center rounded-full bg-slate-800 text-slate-400">
                  <Icon name="layers" className="h-6 w-6" />
                </span>
                <h3 className="mt-4 text-sm font-semibold text-slate-200">Материалы ещё не запланированы</h3>
                <p className="mt-1 max-w-sm text-sm leading-6 text-slate-500">После создания плана кадров здесь появятся изображения, видео и их статусы.</p>
                <Link to="shots" className="mt-4 text-sm font-semibold text-indigo-300 hover:text-indigo-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400">
                  Перейти к плану кадров
                </Link>
              </div>
            )}
          </div>
        </section>

        <section aria-labelledby="jobs-heading" className="overflow-hidden rounded-2xl border border-slate-800 bg-slate-900">
          <div className="border-b border-slate-800 px-4 py-4 sm:px-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">Очередь</p>
                <h2 id="jobs-heading" className="mt-1 text-base font-semibold text-white">Задачи проекта</h2>
              </div>
              <Icon name="work" className="h-5 w-5 text-slate-500" />
            </div>
            <p className="mt-1 text-xs text-slate-600">История сохраняется после перезапуска приложения.</p>
          </div>
          <div className="max-h-[28rem] divide-y divide-slate-800/70 overflow-y-auto">
            {jobItems.slice(0, 6).map((job) => {
              const jobProgress = Math.round((job.progress || 0) * 100)
              const statusLabel = JOB_STATUS_LABELS[job.status] || 'Неизвестное состояние'
              return (
                <article key={job.id} className="px-4 py-3.5 sm:px-5">
                  <div className="flex items-start gap-3">
                    <span className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${
                      job.status === 'running'
                        ? 'animate-pulse bg-sky-400'
                        : job.status === 'succeeded'
                          ? 'bg-emerald-500'
                          : job.status === 'failed' || job.status === 'interrupted'
                            ? 'bg-red-500'
                            : 'bg-amber-500'
                    }`} aria-hidden="true" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-start justify-between gap-3">
                        <h3 className="truncate text-sm font-medium text-slate-200" title={localizeJobName(job.name)}>{localizeJobName(job.name)}</h3>
                        <span className="shrink-0 text-[11px] font-medium text-slate-500">{statusLabel}</span>
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-600">
                        <span>{RESOURCE_LABELS[job.resource || 'project'] || 'Системная задача'}</span>
                        <span aria-hidden="true">·</span>
                        <time dateTime={job.updated_at}>{formatTime(job.updated_at)}</time>
                      </div>
                      <p className="mt-1.5 truncate text-xs text-slate-500" title={localizeJobMessage(job)}>{localizeJobMessage(job)}</p>
                      {(job.status === 'running' || jobProgress > 0) && (
                        <div className="mt-2 flex items-center gap-2">
                          <div
                            className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-950"
                            role="progressbar"
                            aria-label={`Прогресс задачи ${localizeJobName(job.name)}`}
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-valuenow={jobProgress}
                          >
                            <div className={`h-full rounded-full ${job.status === 'running' ? 'bg-sky-500' : 'bg-emerald-600'}`} style={{ width: `${jobProgress}%` }} />
                          </div>
                          <span className="w-9 text-right font-mono text-[11px] text-slate-600">{jobProgress}%</span>
                        </div>
                      )}
                    </div>
                  </div>
                </article>
              )
            })}
            {initialLoaded && !jobItems.length && (
              <div className="flex min-h-52 flex-col items-center justify-center px-6 py-8 text-center">
                <span className="flex h-11 w-11 items-center justify-center rounded-full bg-slate-800 text-slate-500">
                  <Icon name="clock" className="h-5 w-5" />
                </span>
                <h3 className="mt-3 text-sm font-semibold text-slate-300">Задач пока не было</h3>
                <p className="mt-1 text-xs leading-5 text-slate-600">Запущенные этапы появятся здесь вместе с прогрессом и результатом.</p>
              </div>
            )}
            {!initialLoaded && <div className="h-52 animate-pulse bg-slate-900" aria-label="Загрузка задач" role="status" />}
          </div>
        </section>
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <section aria-labelledby="history-heading" className="overflow-hidden rounded-2xl border border-slate-800 bg-slate-900">
          <div className="flex items-center justify-between gap-3 border-b border-slate-800 px-4 py-4 sm:px-5">
            <div>
              <p className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">Аудит</p>
              <h2 id="history-heading" className="mt-1 text-base font-semibold text-white">История запусков</h2>
            </div>
            <Icon name="history" className="h-5 w-5 text-slate-500" />
          </div>
          <div className="max-h-80 divide-y divide-slate-800/70 overflow-y-auto">
            {runLogItems.slice(0, 12).map((record, index) => {
              const eventLabel = EVENT_LABELS[record.event] || 'Событие'
              const levelLabel = LEVEL_LABELS[record.level] || 'Информация'
              return (
                <article key={`${record.run_id}-${record.ts}-${index}`} className="px-4 py-3 sm:px-5">
                  <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-3 gap-y-1 sm:grid-cols-[6rem_9rem_minmax(7rem,auto)_minmax(0,1fr)] sm:items-center">
                    <time className="font-mono text-[11px] text-slate-600" dateTime={record.ts} title={record.ts}>{formatTime(record.ts)}</time>
                    <span className="truncate text-xs font-medium text-slate-300" title={localizeJobName(record.job || record.run_id)}>
                      {record.job ? localizeJobName(record.job) : record.run_id ? `Запуск ${record.run_id.slice(0, 8)}` : 'Запуск'}
                    </span>
                    <span className={`col-start-2 text-[11px] font-medium sm:col-start-auto ${
                      record.level === 'error'
                        ? 'text-red-400'
                        : record.level === 'warning'
                          ? 'text-amber-400'
                          : 'text-sky-400'
                    }`} title={`${eventLabel} · ${levelLabel}`}>
                      {eventLabel} · {levelLabel}
                    </span>
                    <span className="col-span-2 truncate text-xs text-slate-500 sm:col-span-1" title={record.msg}>{record.msg}</span>
                  </div>
                </article>
              )
            })}
            {initialLoaded && !runLogItems.length && (
              <div className="flex min-h-56 flex-col items-center justify-center px-6 py-8 text-center">
                <span className="flex h-11 w-11 items-center justify-center rounded-full bg-slate-800 text-slate-500">
                  <Icon name="history" className="h-5 w-5" />
                </span>
                <h3 className="mt-3 text-sm font-semibold text-slate-300">История пока пуста</h3>
                <p className="mt-1 max-w-sm text-xs leading-5 text-slate-600">После запуска этапов здесь появятся время, результат и диагностические сообщения.</p>
              </div>
            )}
            {!initialLoaded && <div className="h-56 animate-pulse bg-slate-900" aria-label="Загрузка истории" role="status" />}
          </div>
        </section>

        <section aria-labelledby="live-heading" className="overflow-hidden rounded-2xl border border-slate-800 bg-slate-900">
          <div className="border-b border-slate-800 px-4 py-4 sm:px-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">Текущий сеанс</p>
                <h2 id="live-heading" className="mt-1 text-base font-semibold text-white">Ход выполнения</h2>
              </div>
              <div className="flex flex-wrap justify-end gap-1.5" aria-label="Быстрый запуск этапов">
                {(['transcription', 'scenes', 'shots', 'export'] as RunKey[]).map((key) => {
                  const allowed = canRun(key, steps)
                  return (
                    <button
                      type="button"
                      key={key}
                      onClick={() => runStep(key)}
                      disabled={Boolean(running) || lockedElsewhere || !allowed}
                      title={allowed ? `Запустить: ${RUN_LABELS[key]}` : 'Сначала завершите предыдущий этап'}
                      className="rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-slate-600 hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {RUN_LABELS[key]}
                    </button>
                  )
                })}
              </div>
            </div>
          </div>
          <div
            ref={logContainerRef}
            className="h-64 overflow-y-auto bg-slate-950/45 p-4 font-mono text-xs leading-5 text-slate-400 sm:p-5"
            role="log"
            aria-live="polite"
            aria-relevant="additions"
            aria-label="Сообщения текущего запуска"
          >
            {logs.map((line, index) => <div key={`${line}-${index}`} className="break-words">{line}</div>)}
            {!logs.length && (
              <div className="flex h-full min-h-40 flex-col items-center justify-center text-center font-sans">
                <span className="flex h-11 w-11 items-center justify-center rounded-full bg-slate-900 text-slate-600">
                  <Icon name="terminal" className="h-5 w-5" />
                </span>
                <p className="mt-3 text-sm font-medium text-slate-400">Сейчас ничего не выполняется</p>
                <p className="mt-1 text-xs text-slate-600">Запустите следующий этап — сообщения появятся здесь в реальном времени.</p>
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
