import { useState } from 'react'

type Theme = 'light' | 'dark'

const THEME_STORAGE_KEY = 'pipeline-studio-theme'

function currentTheme(): Theme {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
}

function applyTheme(theme: Theme) {
  const root = document.documentElement
  root.dataset.theme = theme
  root.classList.toggle('dark', theme === 'dark')
  root.classList.toggle('light', theme === 'light')
  root.style.colorScheme = theme
  localStorage.setItem(THEME_STORAGE_KEY, theme)

  document
    .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute('content', theme === 'light' ? '#f8fafc' : '#020617')
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-4 w-4">
      <circle cx="12" cy="12" r="3.5" stroke="currentColor" strokeWidth="1.8" />
      <path d="M12 2.5v2M12 19.5v2M4.5 12h-2M21.5 12h-2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="h-4 w-4">
      <path d="M20.2 15.1A8.2 8.2 0 0 1 8.9 3.8 8.3 8.3 0 1 0 20.2 15Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  )
}

export default function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const [theme, setTheme] = useState<Theme>(currentTheme)
  const nextTheme = theme === 'light' ? 'dark' : 'light'
  const nextLabel = nextTheme === 'light' ? 'светлую' : 'тёмную'

  const toggleTheme = () => {
    applyTheme(nextTheme)
    setTheme(nextTheme)
  }

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={`Включить ${nextLabel} тему`}
      title={`Включить ${nextLabel} тему`}
      className={`theme-toggle inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border text-sm font-medium transition-colors focus-visible:outline-none ${compact ? 'w-full px-3 py-2 md:justify-start' : 'w-10 px-0 py-2'}`}
    >
      <span aria-hidden="true" className="flex h-5 w-5 items-center justify-center">
        {theme === 'light' ? <SunIcon /> : <MoonIcon />}
      </span>
      {compact && <span className="hidden md:inline">Тема: {theme === 'light' ? 'светлая' : 'тёмная'}</span>}
    </button>
  )
}
