import { useState, useEffect } from 'react'
import { Outlet, NavLink, useParams, useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import type { ProjectInfo } from '../api/types'

const NAV_ITEMS = [
  { to: '', label: 'Обзор', icon: '01' },
  { to: 'transcription', label: 'Транскрипция', icon: '02' },
  { to: 'scenes', label: 'Сцены', icon: '03' },
  { to: 'shots', label: 'Кадры', icon: '04' },
  { to: 'timeline', label: 'Timeline', icon: '05' },
  { to: 'board', label: 'Asset Board', icon: '06' },
  { to: 'preview', label: 'Preview', icon: '07' },
]

export default function ProjectLayout() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const [project, setProject] = useState<ProjectInfo | null>(null)
  const [steps, setSteps] = useState<Record<string, boolean>>({})

  useEffect(() => {
    if (!projectId) return
    api.getProject(projectId).then((d) => setProject(d.project)).catch(() => navigate('/'))
    api.getProjectState(projectId).then((s) => setSteps(s.steps)).catch(() => {})
  }, [projectId, navigate])

  if (!project) return null

  const stepKeys: [string, string][] = [
    ['transcript', 'Транскрипция'],
    ['transcript_segments', 'Анализ сцен'],
    ['scenes', 'Сцены'],
    ['shot_list', 'Шоты'],
    ['asset_board', 'Доска кадров'],
  ]

  return (
    <div className="h-screen flex bg-slate-950">
      {/* Sidebar */}
      <aside className="w-60 bg-slate-900 border-r border-slate-800 flex flex-col shrink-0">
        <div className="p-4 border-b border-slate-800">
          <button
            onClick={() => navigate('/')}
            className="text-indigo-400 hover:text-indigo-300 font-semibold text-sm cursor-pointer"
          >
            Все проекты
          </button>
          <h2 className="text-white font-bold mt-1 truncate">{project.name}</h2>
          <p className="text-xs text-slate-500">{project.preset} · {project.language}</p>
        </div>

        <nav className="flex-1 p-2 space-y-1">
          {NAV_ITEMS.map(({ to, label, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === ''}
              className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors ${
                  isActive
                    ? 'bg-indigo-600/20 text-indigo-400 font-semibold'
                    : 'text-slate-400 hover:text-white hover:bg-slate-800'
                }`
              }
            >
              <span className="w-6 text-[11px] text-slate-500 font-mono">{icon}</span>
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        {/* Pipeline progress + Settings at bottom */}
        <div className="border-t border-slate-800">
          <div className="p-3">
            <p className="text-xs text-slate-500 uppercase tracking-wider mb-2">Прогресс</p>
            <div className="space-y-1">
              {stepKeys.map(([key, label]) => (
                <div key={key} className="flex items-center gap-2 text-xs">
                  <span className={`w-3 h-3 rounded-full shrink-0 ${steps[key] ? 'bg-green-500' : 'bg-slate-700'}`} />
                  <span className={steps[key] ? 'text-slate-300' : 'text-slate-600'}>{label}</span>
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
            <span>Настройки</span>
          </NavLink>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        <Outlet context={{ projectId, project, steps, setSteps }} />
      </main>
    </div>
  )
}
