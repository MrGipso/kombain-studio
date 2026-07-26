import { useState, useEffect, useRef } from 'react'
import { Outlet, NavLink, useParams, useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import type { PipelineStageState, ProjectInfo } from '../api/types'
import ThemeToggle from '../components/ThemeToggle'

const NAV_ITEMS = [
  { to: '', label: 'Обзор', marker: '00' },
  { to: 'script', label: 'Сценарий', marker: '01' },
  { to: 'voice', label: 'Озвучка', marker: '02' },
  { to: 'transcription', label: 'Транскрипция', marker: '03' },
  { to: 'scenes', label: 'Сцены', marker: '04' },
  { to: 'shots', label: 'Кадры', marker: '05' },
  { to: 'board', label: 'Материалы', marker: '06' },
  { to: 'timeline', label: 'Монтаж', marker: '07' },
  { to: 'music', label: 'Музыка', marker: '08' },
  { to: 'sounds', label: 'Звуки', marker: '09' },
  { to: 'preview', label: 'Экспорт', marker: '10' },
] as const

const PIPELINE_STAGES = [
  ['transcript', 'Транскрипция'],
  ['scenes', 'Сцены'],
  ['shots', 'Кадры'],
  ['assets', 'Материалы'],
  ['validation', 'Проверка'],
  ['export', 'Экспорт'],
] as const

const STAGE_APPEARANCE = {
  pending: { label: 'ожидает', dot: 'bg-slate-700', text: 'text-slate-500' },
  running: { label: 'выполняется', dot: 'bg-sky-400 motion-safe:animate-pulse', text: 'text-sky-300' },
  completed: { label: 'готово', dot: 'bg-emerald-400', text: 'text-slate-300' },
  stale: { label: 'нужно обновить', dot: 'bg-amber-400', text: 'text-amber-300' },
  failed: { label: 'ошибка', dot: 'bg-rose-400', text: 'text-rose-300' },
} as const

type DisplayStageStatus = keyof typeof STAGE_APPEARANCE

function displayStageStatus(status: string | undefined): DisplayStageStatus {
  return status === 'running' || status === 'completed' || status === 'stale' || status === 'failed'
    ? status
    : 'pending'
}

export default function ProjectLayout() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const [project, setProject] = useState<ProjectInfo | null>(null)
  const [steps, setSteps] = useState<Record<string, boolean>>({})
  const [stageStates, setStageStates] = useState<Record<string, PipelineStageState>>({})
  // App-level lock: which pipeline step is running (shared across all pages of
  // this project) so two pages can't start conflicting jobs on the same project.
  const [busy, setBusy] = useState<string | null>(null)
  const serverBusyRef = useRef(false)

  useEffect(() => {
    if (!projectId) return
    api.getProject(projectId).then((d) => setProject(d.project)).catch(() => navigate('/'))
    api.getProjectState(projectId).then((s) => {
      setSteps(s.steps)
      setStageStates(s.stage_states || {})
    }).catch(() => {})
  }, [projectId, navigate])

  // Reconnect the shell to a server-side job after navigation or a full page
  // reload. Page-local SSE remains responsible for detailed live logs.
  useEffect(() => {
    if (!projectId) return
    let active = true
    const refreshJob = () => {
      api.getProjectJobs(projectId, 1).then((state) => {
        if (!active) return
        if (state.running) {
          serverBusyRef.current = true
          setBusy(state.running)
        } else if (serverBusyRef.current) {
          serverBusyRef.current = false
          setBusy(null)
        }
      }).catch(() => {})
      api.getProjectState(projectId).then((state) => {
        if (!active) return
        setSteps(state.steps)
        setStageStates(state.stage_states || {})
      }).catch(() => {})
    }
    refreshJob()
    const timer = window.setInterval(refreshJob, 2500)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [projectId])

  if (!project) {
    return (
      <div role="status" aria-live="polite" className="flex min-h-screen items-center justify-center bg-slate-950 px-6 text-slate-400">
        <div className="flex items-center gap-3 rounded-xl border border-slate-800 bg-slate-900 px-4 py-3 shadow-xl shadow-black/20">
          <span aria-hidden="true" className="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-500 text-[11px] font-black text-white">КС</span>
          <span>
            <span className="block text-sm font-semibold text-slate-200">Открываем проект</span>
            <span className="mt-0.5 block text-xs text-slate-500">Загружаем состояние рабочего процесса…</span>
          </span>
        </div>
      </div>
    )
  }

  const pipelineStages = PIPELINE_STAGES.map(([key, label]) => {
    const status = displayStageStatus(stageStates[key]?.status || (steps[key] ? 'completed' : 'pending'))
    return { key, label, status, error: stageStates[key]?.error }
  })
  const completedStages = pipelineStages.filter(({ status }) => status === 'completed').length
  const failedStages = pipelineStages.filter(({ status }) => status === 'failed').length
  const staleStages = pipelineStages.filter(({ status }) => status === 'stale').length
  const progressPercent = Math.round((completedStages / pipelineStages.length) * 100)
  const progressSummary = failedStages > 0
    ? 'Есть этапы с ошибками'
    : staleStages > 0
      ? 'Некоторые этапы нужно обновить'
      : completedStages === pipelineStages.length
        ? 'Все этапы завершены'
        : 'Проект в работе'
  const progressBarColor = failedStages > 0 ? 'bg-rose-500' : staleStages > 0 ? 'bg-amber-400' : 'bg-indigo-500'
  const normalizedPreset = project.preset.trim().toLocaleLowerCase()
  const normalizedLanguage = project.language.trim().toLocaleLowerCase()
  const presetLabel = ({ universal: 'Универсальный', documentary: 'Документальный', tutorial: 'Обучающий' } as Record<string, string>)[normalizedPreset]
    || project.preset
    || 'Без шаблона'
  const languageLabel = normalizedLanguage === 'ru'
    ? 'Русский'
    : normalizedLanguage === 'en'
      ? 'Английский'
      : normalizedLanguage === 'auto' || !normalizedLanguage
        ? 'Авто'
        : project.language

  return (
    <div className="flex h-screen overflow-hidden bg-slate-950 text-slate-100">
      {/* Sidebar */}
      <aside
        aria-label={`Комбайн Studio. Навигация проекта «${project.name}»`}
        className="flex w-16 shrink-0 flex-col border-r border-slate-800/90 bg-slate-900 md:w-64"
      >
        <div className="border-b border-slate-800 px-2 py-3 md:px-4 md:py-4">
          <div className="flex items-center justify-center gap-3 md:justify-start">
            <div aria-hidden="true" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-indigo-500 text-[11px] font-black tracking-tight text-white shadow-lg shadow-indigo-950/50">
              КС
            </div>
            <div className="hidden min-w-0 md:block">
              <p className="truncate text-sm font-bold tracking-tight text-white">Комбайн Studio</p>
              <p className="text-[10px] uppercase tracking-[0.16em] text-slate-500">Видеопроизводство</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => navigate('/')}
            aria-label="Все проекты"
            title="Вернуться ко всем проектам"
            className="mt-3 flex h-9 w-full cursor-pointer items-center justify-center gap-2 rounded-lg border border-slate-800 bg-slate-950/40 text-sm font-medium text-slate-400 transition-colors hover:border-slate-700 hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 md:justify-start md:px-3"
          >
            <span aria-hidden="true" className="text-base leading-none">←</span>
            <span className="hidden md:inline">Все проекты</span>
          </button>

          <div className="mt-3 hidden rounded-xl border border-slate-800 bg-slate-950/50 px-3 py-2.5 md:block">
            <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-600">Текущий проект</p>
            <h2 className="mt-1 truncate text-sm font-semibold text-white" title={project.name}>{project.name}</h2>
            <p className="mt-1 truncate text-[11px] text-slate-500" title={`Шаблон: ${presetLabel}. Язык: ${languageLabel}`}>
              {presetLabel} <span aria-hidden="true">·</span> {languageLabel}
            </p>
          </div>
        </div>

        <nav aria-label="Разделы проекта" className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-2">
          <p className="mb-1 hidden px-3 pt-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600 md:block">Рабочий процесс</p>
          {NAV_ITEMS.map(({ to, label, marker }) => (
            <NavLink
              key={to}
              to={to}
              end={to === ''}
              aria-label={label}
              title={`Открыть раздел «${label}»`}
              className={({ isActive }) =>
              `group flex items-center justify-center gap-3 rounded-lg px-2 py-2 text-sm transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 md:justify-start md:px-3 ${
                  isActive
                    ? 'nav-item-active bg-indigo-600 font-semibold text-white shadow-sm'
                    : 'text-slate-400 hover:bg-slate-800/80 hover:text-white'
                }`
              }
            >
              <span aria-hidden="true" className="flex h-6 w-7 shrink-0 items-center justify-center rounded-md bg-slate-950/60 font-mono text-[10px] text-slate-500 transition-colors group-hover:text-slate-300">{marker}</span>
              <span className="hidden truncate md:inline">{label}</span>
            </NavLink>
          ))}
        </nav>

        {/* Pipeline progress + Settings at bottom */}
        <div className="border-t border-slate-800 bg-slate-900/95">
          {busy && (
            <div
              role="status"
              aria-live="polite"
              aria-label={`Выполняется задача: ${busy}`}
              title={`Выполняется: ${busy}`}
              className="mx-2 mt-2 flex min-h-10 items-center justify-center gap-2.5 rounded-lg border border-sky-800/70 bg-sky-950/40 px-2 py-2 text-sky-300 md:mx-3 md:justify-start md:px-3"
            >
              <span aria-hidden="true" className="relative flex h-2.5 w-2.5 shrink-0">
                <span className="absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-60 motion-safe:animate-ping" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-sky-400" />
              </span>
              <span className="hidden min-w-0 md:block">
                <span className="block text-[9px] font-semibold uppercase tracking-[0.14em] text-sky-500">Выполняется</span>
                <span className="block truncate text-xs font-medium text-sky-200">{busy}</span>
              </span>
            </div>
          )}
          <div className="hidden px-4 py-3 md:block">
            <div className="flex items-end justify-between gap-3">
              <div>
                <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600">Готовность цикла</p>
                <p className="mt-0.5 text-xs text-slate-400">{progressSummary}</p>
              </div>
              <span className="font-mono text-xs font-semibold text-slate-300">{progressPercent}%</span>
            </div>
            <div
              role="progressbar"
              aria-label="Готовность проекта"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progressPercent}
              aria-valuetext={`${completedStages} из ${pipelineStages.length} этапов завершено`}
              className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800"
            >
              <div className={`h-full rounded-full transition-[width] duration-500 ${progressBarColor}`} style={{ width: `${progressPercent}%` }} />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5">
              {pipelineStages.map(({ key, label, status, error }) => {
                const appearance = STAGE_APPEARANCE[status]
                const statusText = `${label}: ${appearance.label}${error ? `. ${error}` : ''}`
                return (
                  <div key={key} aria-label={statusText} title={statusText} className="flex min-w-0 items-center gap-1.5 text-[11px]">
                    <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 rounded-full ${appearance.dot}`} />
                    <span className={`truncate ${appearance.text}`}>{label}</span>
                  </div>
                )
              })}
            </div>
          </div>
          <div className="px-2 py-2 md:hidden">
            <div
              role="progressbar"
              aria-label="Готовность проекта"
              aria-valuemin={0}
              aria-valuemax={pipelineStages.length}
              aria-valuenow={completedStages}
              aria-valuetext={`${completedStages} из ${pipelineStages.length} этапов завершено`}
              title={`${progressSummary}: ${completedStages} из ${pipelineStages.length}`}
              className="mx-auto flex h-8 w-10 items-center justify-center rounded-lg border border-slate-800 bg-slate-950/60 font-mono text-[10px] font-semibold text-slate-400"
            >
              {completedStages}/{pipelineStages.length}
            </div>
          </div>
          <div className="border-t border-slate-800 px-2 py-2">
            <ThemeToggle compact />
          </div>
          <NavLink
            to="/settings"
            aria-label="Настройки приложения"
            title="Настройки приложения"
            className={({ isActive }) =>
              `flex items-center justify-center gap-3 border-t border-slate-800 px-3 py-3 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-500 md:justify-start ${
                isActive
                  ? 'bg-indigo-500/15 font-semibold text-indigo-300'
                  : 'text-slate-500 hover:bg-slate-800 hover:text-white'
              }`
            }
          >
            <span aria-hidden="true" className="w-7 text-center text-base text-slate-500">⚙</span>
            <span className="hidden md:inline">Настройки</span>
          </NavLink>
        </div>
      </aside>

      {/* Main content */}
      <main aria-label={`Рабочая область проекта «${project.name}»`} className="min-w-0 flex-1 overflow-auto bg-slate-950">
        <Outlet context={{ projectId, project, steps, setSteps, stageStates, setStageStates, busy, setBusy }} />
      </main>
    </div>
  )
}
