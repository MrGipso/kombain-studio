import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import ThemeToggle from '../components/ThemeToggle'
import type { ProjectInfo } from '../api/types'

function BrandMark() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true" className="h-8 w-8">
      <defs>
        <linearGradient id="brand-gradient" x1="4" y1="4" x2="28" y2="28" gradientUnits="userSpaceOnUse">
          <stop stopColor="var(--brand-start, #818cf8)" />
          <stop offset="1" stopColor="var(--brand-end, #a855f7)" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#brand-gradient)" />
      <path d="M9.5 8h4.2v6.1L19.1 8h5.2l-6.8 7.4 7.2 8.6h-5.3l-5.7-7.1V24H9.5V8Z" fill="white" />
    </svg>
  )
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-5 w-5">
      <path d="m20 20-4.2-4.2m1.7-5.3a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}

function SettingsIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-5 w-5">
      <path d="M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z" stroke="currentColor" strokeWidth="1.7" />
      <path d="M19.4 13.2a7.9 7.9 0 0 0 0-2.4l2-1.5-2-3.4-2.5 1a8.4 8.4 0 0 0-2-1.2L14.5 3h-5l-.4 2.7a8.4 8.4 0 0 0-2 1.2l-2.5-1-2 3.4 2 1.5a7.9 7.9 0 0 0 0 2.4l-2 1.5 2 3.4 2.5-1a8.4 8.4 0 0 0 2 1.2l.4 2.7h5l.4-2.7a8.4 8.4 0 0 0 2-1.2l2.5 1 2-3.4-2-1.5Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
    </svg>
  )
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-5 w-5">
      <path d="M5 12h14m-5-5 5 5-5 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-5 w-5">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

function projectInitials(name: string) {
  const initials = name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join('')
    .toLocaleUpperCase()
  return initials || 'КС'
}

function languageLabel(language: string) {
  const normalized = language.trim().toLocaleLowerCase()
  if (normalized === 'ru') return 'Русский'
  if (normalized === 'en') return 'Английский'
  if (!normalized || normalized === 'auto') return 'Авто'
  return language.toLocaleUpperCase()
}

function presetLabel(preset: string) {
  const normalized = preset.trim().toLocaleLowerCase()
  const labels: Record<string, string> = {
    universal: 'Универсальный',
    documentary: 'Документальный',
    tutorial: 'Обучающий',
  }
  return labels[normalized] || preset || 'Без шаблона'
}

function projectCountLabel(count: number) {
  const lastTwo = count % 100
  const last = count % 10
  if (lastTwo >= 11 && lastTwo <= 14) return `${count} проектов`
  if (last === 1) return `${count} проект`
  if (last >= 2 && last <= 4) return `${count} проекта`
  return `${count} проектов`
}

export default function StartScreen() {
  const navigate = useNavigate()
  const toast = useToast()
  const [projects, setProjects] = useState<ProjectInfo[]>([])
  const [showNew, setShowNew] = useState(false)
  const [newName, setNewName] = useState('')
  const [newLang, setNewLang] = useState('ru')
  const [query, setQuery] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [isCreating, setIsCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    let active = true
    setIsLoading(true)
    setLoadError(false)

    api.getProjects()
      .then((items) => {
        if (active) setProjects(items)
      })
      .catch((error) => {
        if (!active) return
        setLoadError(true)
        toast.error(`Не удалось загрузить проекты: ${(error as Error).message}`)
      })
      .finally(() => {
        if (active) setIsLoading(false)
      })

    return () => {
      active = false
    }
  }, [reloadToken, toast])

  // First launch: send a fresh install through the setup wizard once.
  useEffect(() => {
    let active = true
    api.getSettings()
      .then((settings) => {
        if (active && settings.setup_completed === false) navigate('/setup', { replace: true })
      })
      .catch(() => { /* the projects request reports connectivity problems */ })
    return () => { active = false }
  }, [navigate])

  const filteredProjects = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase()
    if (!normalizedQuery) return projects
    return projects.filter((project) => project.name.toLocaleLowerCase().includes(normalizedQuery))
  }, [projects, query])

  const openProject = (project: ProjectInfo) => {
    const slug = project.dir.split(/[\\/]/).pop() || project.name
    navigate(`/project/${encodeURIComponent(slug)}`)
  }

  const handleCreate = async () => {
    const name = newName.trim()
    if (!name) {
      setCreateError('Введите название проекта.')
      return
    }

    setIsCreating(true)
    setCreateError('')
    try {
      const project = await api.createProject(name, newLang)
      const slug = project.dir.split(/[\\/]/).pop() || project.name
      navigate(`/project/${encodeURIComponent(slug)}`)
    } catch (error) {
      setCreateError('Не удалось создать проект. Проверьте название и попробуйте снова.')
      toast.error(`Не удалось создать проект: ${(error as Error).message}`)
    } finally {
      setIsCreating(false)
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800/80 bg-slate-900/95">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-4 sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <BrandMark />
            <div>
              <p className="text-base font-semibold leading-tight text-white">Комбайн Studio</p>
              <p className="text-xs text-slate-500">Производство AI-видео</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <button
              type="button"
              onClick={() => navigate('/settings')}
              className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm font-medium text-slate-300 transition-colors hover:border-slate-700 hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 cursor-pointer"
            >
              <SettingsIcon />
              <span className="hidden sm:inline">Настройки</span>
              <span className="sr-only sm:hidden">Открыть настройки</span>
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-7xl px-4 py-10 sm:px-6 sm:py-14 lg:px-8">
        <section className="relative overflow-hidden rounded-2xl border border-slate-800 bg-gradient-to-br from-slate-900 via-slate-900 to-indigo-950/60 px-6 py-8 shadow-2xl shadow-black/20 sm:px-8 sm:py-10">
          <div aria-hidden="true" className="absolute -right-20 -top-24 h-64 w-64 rounded-full bg-indigo-500/10 blur-3xl" />
          <div className="relative flex flex-col items-start justify-between gap-6 lg:flex-row lg:items-center">
            <div className="max-w-2xl">
              <p className="mb-3 text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">Рабочее пространство</p>
              <h1 className="text-3xl font-bold tracking-tight text-white sm:text-4xl">Ваши видеопроекты</h1>
              <p className="mt-3 max-w-xl text-sm leading-6 text-slate-400 sm:text-base">
                Создайте новый проект или продолжите работу: от транскрибации и планирования кадров до проверки и экспорта.
              </p>
            </div>
            <button
              type="button"
              onClick={() => {
                setShowNew(true)
                setCreateError('')
              }}
              className="inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-indigo-600 px-5 py-3 text-sm font-semibold text-white shadow-lg shadow-indigo-950/40 transition-colors hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900 cursor-pointer sm:w-auto"
            >
              <PlusIcon />
              Создать проект
            </button>
          </div>
        </section>

        {showNew && (
          <section aria-labelledby="new-project-title" className="mt-6 rounded-2xl border border-indigo-500/30 bg-slate-900 p-5 shadow-xl shadow-black/20 sm:p-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 id="new-project-title" className="text-xl font-semibold text-white">Новый проект</h2>
                <p className="mt-1 text-sm text-slate-400">Укажите рабочее название и язык исходного материала.</p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setShowNew(false)
                  setCreateError('')
                }}
                aria-label="Закрыть форму создания проекта"
                className="rounded-lg p-2 text-slate-500 transition-colors hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 cursor-pointer"
              >
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-5 w-5">
                  <path d="m6 6 12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                </svg>
              </button>
            </div>

            <form
              className="mt-6 grid gap-5 lg:grid-cols-[minmax(0,1fr)_14rem_auto] lg:items-end"
              onSubmit={(event) => {
                event.preventDefault()
                void handleCreate()
              }}
            >
              <div>
                <label htmlFor="project-name" className="mb-2 block text-sm font-medium text-slate-200">Название проекта</label>
                <input
                  id="project-name"
                  value={newName}
                  onChange={(event) => {
                    setNewName(event.target.value)
                    if (createError) setCreateError('')
                  }}
                  className="min-h-11 w-full rounded-lg border border-slate-700 bg-slate-950 px-3.5 py-2.5 text-sm text-white placeholder:text-slate-600 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                  placeholder="Например, Документальный фильм"
                  autoFocus
                  autoComplete="off"
                  aria-describedby={createError ? 'project-create-error' : undefined}
                  aria-invalid={Boolean(createError)}
                  disabled={isCreating}
                />
              </div>
              <div>
                <label htmlFor="project-language" className="mb-2 block text-sm font-medium text-slate-200">Язык проекта</label>
                <select
                  id="project-language"
                  value={newLang}
                  onChange={(event) => setNewLang(event.target.value)}
                  className="min-h-11 w-full rounded-lg border border-slate-700 bg-slate-950 px-3.5 py-2.5 text-sm text-white focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                  disabled={isCreating}
                >
                  <option value="ru">Русский</option>
                  <option value="en">Английский</option>
                </select>
              </div>
              <div className="flex gap-3">
                <button
                  type="submit"
                  disabled={isCreating || !newName.trim()}
                  className="inline-flex min-h-11 flex-1 items-center justify-center rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer lg:flex-none"
                >
                  {isCreating ? 'Создание…' : 'Создать'}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setShowNew(false)
                    setCreateError('')
                  }}
                  disabled={isCreating}
                  className="min-h-11 rounded-lg border border-slate-700 px-4 py-2.5 text-sm font-medium text-slate-300 transition-colors hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
                >
                  Отмена
                </button>
              </div>
            </form>
            {createError && <p id="project-create-error" role="alert" className="mt-3 text-sm text-rose-400">{createError}</p>}
          </section>
        )}

        <section aria-labelledby="projects-title" className="mt-10">
          <div className="flex flex-col gap-4 border-b border-slate-800 pb-5 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <div className="flex items-center gap-3">
                <h2 id="projects-title" className="text-xl font-semibold text-white">Каталог проектов</h2>
                {!isLoading && !loadError && (
                  <span className="rounded-full border border-slate-800 bg-slate-900 px-2.5 py-1 text-xs font-medium text-slate-400">
                    {projectCountLabel(projects.length)}
                  </span>
                )}
              </div>
              <p className="mt-1 text-sm text-slate-500">Откройте проект, чтобы продолжить с последнего этапа.</p>
            </div>

            {!isLoading && !loadError && projects.length > 0 && (
              <div className="relative w-full sm:w-80">
                <label htmlFor="project-search" className="sr-only">Поиск проектов по названию</label>
                <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-slate-500">
                  <SearchIcon />
                </span>
                <input
                  id="project-search"
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Найти проект по названию"
                  className="min-h-11 w-full rounded-lg border border-slate-800 bg-slate-900 py-2.5 pl-10 pr-10 text-sm text-white placeholder:text-slate-600 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                />
                {query && (
                  <button
                    type="button"
                    onClick={() => setQuery('')}
                    aria-label="Очистить поиск"
                    className="absolute inset-y-0 right-1 flex items-center rounded-md px-2.5 text-slate-500 transition-colors hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 cursor-pointer"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-4 w-4">
                      <path d="m7 7 10 10M17 7 7 17" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                    </svg>
                  </button>
                )}
              </div>
            )}
          </div>

          {isLoading && (
            <div role="status" aria-live="polite" className="mt-6">
              <span className="sr-only">Загрузка проектов…</span>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {Array.from({ length: 6 }).map((_, index) => (
                  <div key={index} aria-hidden="true" className="h-44 animate-pulse rounded-xl border border-slate-800 bg-slate-900 p-5">
                    <div className="h-11 w-11 rounded-lg bg-slate-800" />
                    <div className="mt-5 h-4 w-2/3 rounded bg-slate-800" />
                    <div className="mt-3 h-3 w-1/2 rounded bg-slate-800/70" />
                  </div>
                ))}
              </div>
            </div>
          )}

          {!isLoading && loadError && (
            <div role="alert" className="mt-6 rounded-xl border border-rose-900/60 bg-rose-950/20 px-6 py-10 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-rose-500/10 text-rose-400">
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-6 w-6">
                  <path d="M12 8v5m0 3.5v.1M10.3 4.8 3.1 17.3A1.8 1.8 0 0 0 4.7 20h14.6a1.8 1.8 0 0 0 1.6-2.7L13.7 4.8a2 2 0 0 0-3.4 0Z" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </div>
              <h3 className="mt-4 text-base font-semibold text-white">Не удалось загрузить проекты</h3>
              <p className="mt-2 text-sm text-slate-400">Проверьте, запущен ли сервер, и повторите попытку.</p>
              <button
                type="button"
                onClick={() => setReloadToken((value) => value + 1)}
                className="mt-5 min-h-10 rounded-lg border border-slate-700 bg-slate-900 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 cursor-pointer"
              >
                Повторить
              </button>
            </div>
          )}

          {!isLoading && !loadError && projects.length === 0 && (
            <div className="mt-6 rounded-xl border border-dashed border-slate-700 bg-slate-900/50 px-6 py-14 text-center">
              <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl border border-slate-800 bg-slate-900 text-indigo-400">
                <PlusIcon />
              </div>
              <h3 className="mt-5 text-lg font-semibold text-white">Здесь появятся ваши проекты</h3>
              <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-slate-400">Создайте первый проект, выберите язык и начните работу с исходным материалом.</p>
              <button
                type="button"
                onClick={() => setShowNew(true)}
                className="mt-6 min-h-11 rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 cursor-pointer"
              >
                Создать первый проект
              </button>
            </div>
          )}

          {!isLoading && !loadError && projects.length > 0 && filteredProjects.length === 0 && (
            <div className="mt-6 rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-12 text-center">
              <h3 className="text-base font-semibold text-white">Проекты не найдены</h3>
              <p className="mt-2 text-sm text-slate-400">Измените запрос или очистите строку поиска.</p>
              <button
                type="button"
                onClick={() => setQuery('')}
                className="mt-4 rounded-lg px-4 py-2 text-sm font-semibold text-indigo-400 transition-colors hover:bg-indigo-500/10 hover:text-indigo-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 cursor-pointer"
              >
                Очистить поиск
              </button>
            </div>
          )}

          {!isLoading && !loadError && filteredProjects.length > 0 && (
            <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {filteredProjects.map((project) => (
                <button
                  key={project.dir}
                  type="button"
                  onClick={() => openProject(project)}
                  aria-label={`Открыть проект «${project.name}»`}
                  className="group flex min-h-44 flex-col rounded-xl border border-slate-800 bg-slate-900 p-5 text-left shadow-lg shadow-black/10 transition-all hover:-translate-y-0.5 hover:border-indigo-500/50 hover:bg-slate-800/80 hover:shadow-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 cursor-pointer"
                >
                  <div className="flex w-full items-start justify-between gap-4">
                    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500/20 to-purple-500/20 text-sm font-bold text-indigo-300 ring-1 ring-inset ring-indigo-500/20">
                      {projectInitials(project.name)}
                    </span>
                    <span className="text-slate-600 transition-colors group-hover:text-indigo-300">
                      <ArrowIcon />
                    </span>
                  </div>
                  <h3 className="mt-5 line-clamp-2 text-base font-semibold leading-6 text-white">{project.name}</h3>
                  <div className="mt-auto flex w-full items-center justify-between gap-3 pt-4 text-xs">
                    <span className="rounded-full border border-slate-700 bg-slate-950/60 px-2.5 py-1 font-medium text-slate-300">
                      {languageLabel(project.language)}
                    </span>
                    <span className="max-w-32 truncate text-slate-500" title={presetLabel(project.preset)}>
                      {presetLabel(project.preset)}
                    </span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </section>
      </main>
    </div>
  )
}
