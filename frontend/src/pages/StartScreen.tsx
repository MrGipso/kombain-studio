import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import type { ProjectInfo, Preset } from '../api/types'

export default function StartScreen() {
  const navigate = useNavigate()
  const [projects, setProjects] = useState<ProjectInfo[]>([])
  const [presets, setPresets] = useState<Preset[]>([])
  const [showNew, setShowNew] = useState(false)
  const [newName, setNewName] = useState('')
  const [newPreset, setNewPreset] = useState('documentary')
  const [newLang, setNewLang] = useState('ru')

  useEffect(() => {
    api.getProjects().then(setProjects).catch(() => {})
    api.getPresets().then(setPresets).catch(() => {})
  }, [])

  const handleCreate = async () => {
    if (!newName.trim()) return
    try {
      const p = await api.createProject(newName.trim(), newPreset, newLang)
      const slug = p.dir.split(/[\\/]/).pop() || p.name
      navigate(`/project/${encodeURIComponent(slug)}`)
    } catch (e) {
      alert('Failed to create project: ' + (e as Error).message)
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 flex flex-col items-center justify-center p-8">
      <div className="max-w-3xl w-full">
        <div className="text-center mb-12">
          <h1 className="text-5xl font-bold text-white mb-3">Pipeline Studio</h1>
          <p className="text-slate-400 text-lg">Полуавтоматическое создание YouTube-документалок</p>
        </div>

        {/* New project button */}
        {!showNew && (
          <button
            onClick={() => setShowNew(true)}
            className="w-full py-4 px-6 mb-8 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-xl text-lg transition-colors cursor-pointer"
          >
            + Новый проект
          </button>
        )}

        {/* New project form */}
        {showNew && (
          <div className="bg-slate-900 rounded-xl p-6 mb-8 border border-slate-700">
            <h2 className="text-xl font-bold text-white mb-4">Создать проект</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm text-slate-400 mb-1">Название</label>
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-indigo-500"
                  placeholder="Моё видео"
                  onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
                />
              </div>
              <div className="flex gap-4">
                <div className="flex-1">
                  <label className="block text-sm text-slate-400 mb-1">Пресет</label>
                  <select
                    value={newPreset}
                    onChange={(e) => setNewPreset(e.target.value)}
                    className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-indigo-500"
                  >
                    {presets.map((p) => (
                      <option key={p.name} value={p.name}>{p.name}</option>
                    ))}
                    {presets.length === 0 && <option value="documentary">documentary</option>}
                  </select>
                </div>
                <div className="w-24">
                  <label className="block text-sm text-slate-400 mb-1">Язык</label>
                  <select
                    value={newLang}
                    onChange={(e) => setNewLang(e.target.value)}
                    className="w-full px-3 py-2 bg-slate-800 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-indigo-500"
                  >
                    <option value="ru">RU</option>
                    <option value="en">EN</option>
                  </select>
                </div>
              </div>
              <div className="flex gap-3">
                <button
                  onClick={handleCreate}
                  className="flex-1 py-2 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg transition-colors cursor-pointer"
                >
                  Создать
                </button>
                <button
                  onClick={() => setShowNew(false)}
                  className="py-2 px-6 bg-slate-700 hover:bg-slate-600 text-white rounded-lg transition-colors cursor-pointer"
                >
                  Отмена
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Project list */}
        <div className="space-y-2">
          <h2 className="text-sm text-slate-500 uppercase tracking-wider mb-3">Проекты</h2>
          {projects.length === 0 && (
            <p className="text-slate-600 text-center py-8">Нет проектов. Создайте первый!</p>
          )}
          {projects.map((p) => {
            const slug = p.dir.split(/[\\/]/).pop() || p.name
            return (
            <button
              key={p.dir}
              onClick={() => navigate(`/project/${encodeURIComponent(slug)}`)}
              className="w-full text-left px-5 py-4 bg-slate-900 hover:bg-slate-800 rounded-xl border border-slate-800 transition-colors cursor-pointer group"
            >
              <div className="flex items-center justify-between">
                <div>
                  <span className="text-white font-semibold text-lg">{p.name}</span>
                  <span className="ml-3 text-xs text-slate-500 uppercase">{p.language}</span>
                </div>
                <span className="text-slate-600 group-hover:text-slate-400 text-sm">{p.preset}</span>
              </div>
            </button>
            );
          })}
        </div>

        <div className="mt-8 text-center">
          <button
            onClick={() => navigate('/settings')}
            className="text-slate-600 hover:text-slate-400 text-sm transition-colors cursor-pointer"
          >
            ⚙ Настройки
          </button>
        </div>
      </div>
    </div>
  )
}
