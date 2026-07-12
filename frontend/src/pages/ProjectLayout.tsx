import { useState, useEffect, useRef } from 'react'
import { Outlet, NavLink, useParams, useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import type { PipelineStageState, ProjectInfo } from '../api/types'

const NAV_ITEMS = [
  { to: '', label: 'Обзор', icon: '01' },
  { to: 'script', label: 'Сценарий', icon: '00' },
  { to: 'transcription', label: 'Транскрипция', icon: '02' },
  { to: 'scenes', label: 'Сцены', icon: '03' },
  { to: 'shots', label: 'Кадры', icon: '04' },
  { to: 'board', label: 'Asset Board', icon: '05' },
  { to: 'timeline', label: 'Timeline', icon: '06' },
  { to: 'music', label: 'Музыка', icon: '07' },
  { to: 'sounds', label: 'Звуки', icon: '08' },
  { to: 'preview', label: 'Preview', icon: '09' },
]

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

  if (!project) return null

  const stepKeys: [string, string][] = [
    ['transcript', 'Транскрипция'],
    ['scenes', 'Сцены'],
    ['shots', 'Шоты'],
    ['assets', 'Ассеты'],
    ['validation', 'QC'],
    ['export', 'Экспорт'],
  ]

  return (
    <div className="h-screen flex bg-slate-950">
      {/* Sidebar */}
      <aside className="flex w-16 shrink-0 flex-col border-r border-slate-800 bg-slate-900 md:w-60">
        <div className="border-b border-slate-800 p-2 md:p-4">
          <button
            onClick={() => navigate('/')}
            aria-label="Все проекты"
            className="w-full cursor-pointer text-center text-sm font-semibold text-indigo-400 hover:text-indigo-300 md:text-left"
          >
            <span className="md:hidden">←</span><span className="hidden md:inline">Все проекты</span>
          </button>
          <h2 className="mt-1 hidden truncate font-bold text-white md:block">{project.name}</h2>
          <p className="hidden text-xs text-slate-500 md:block">{project.preset} · {project.language}</p>
        </div>

        <nav className="flex-1 p-2 space-y-1">
          {NAV_ITEMS.map(({ to, label, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === ''}
              aria-label={label}
              className={({ isActive }) =>
              `flex items-center justify-center gap-3 rounded-lg px-2 py-2.5 text-sm transition-colors md:justify-start md:px-3 ${
                  isActive
                    ? 'bg-indigo-600/20 text-indigo-400 font-semibold'
                    : 'text-slate-400 hover:text-white hover:bg-slate-800'
                }`
              }
            >
              <span className="w-6 text-center font-mono text-[11px] text-slate-500 md:text-left">{icon}</span>
              <span className="hidden md:inline">{label}</span>
            </NavLink>
          ))}
        </nav>

        {/* Pipeline progress + Settings at bottom */}
        <div className="border-t border-slate-800">
          {busy && (
            <div role="status" aria-live="polite" className="mx-2 mt-3 flex items-center justify-center gap-2 rounded-lg border border-sky-800 bg-sky-950/40 px-2 py-2 text-xs text-sky-300 md:mx-3 md:justify-start md:px-3">
              <span className="h-2.5 w-2.5 shrink-0 animate-ping rounded-full bg-sky-400" />
              <span className="hidden truncate md:inline">Идёт: {busy}</span>
            </div>
          )}
          <div className="hidden p-3 md:block">
            <p className="text-xs text-slate-500 uppercase tracking-wider mb-2">Прогресс</p>
            <div className="space-y-1">
              {stepKeys.map(([key, label]) => (
                <div key={key} className="flex items-center gap-2 text-xs" title={stageStates[key]?.error || stageStates[key]?.status || ''}>
                  <span className={`w-3 h-3 rounded-full shrink-0 ${
                    stageStates[key]?.status === 'completed' ? 'bg-green-500'
                      : stageStates[key]?.status === 'stale' ? 'bg-amber-500'
                        : stageStates[key]?.status === 'failed' ? 'bg-red-500'
                          : 'bg-slate-700'
                  }`} />
                  <span className={stageStates[key]?.status === 'completed' ? 'text-slate-300' : stageStates[key]?.status === 'stale' ? 'text-amber-400' : 'text-slate-600'}>
                    {label}{stageStates[key]?.status === 'stale' ? ' · устарело' : ''}
                  </span>
                </div>
              ))}
            </div>
          </div>
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              `flex items-center gap-3 px-4 py-3 text-sm transition-colors border-t border-slate-800 ${
                isActive
                  ? 'bg-indigo-600/20 text-indigo-400 font-semibold'
                  : 'text-slate-500 hover:text-white hover:bg-slate-800'
              }`
            }
          >
            <span className="w-6 text-[11px] text-slate-500 font-mono">SET</span>
            <span className="hidden md:inline">Настройки</span>
          </NavLink>
        </div>
      </aside>

      {/* Main content */}
      <main className="min-w-0 flex-1 overflow-auto">
        <Outlet context={{ projectId, project, steps, setSteps, stageStates, setStageStates, busy, setBusy }} />
      </main>
    </div>
  )
}
