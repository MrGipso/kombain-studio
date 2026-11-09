import { useEffect, useState } from 'react'
import * as api from '../api/client'

const DISMISS_KEY = 'kombain.update.dismissed'

function updateCommand(status: Pick<api.UpdateStatus, 'platform'>): string {
  return status.platform === 'win32' ? 'update.bat' : './update.sh'
}

function readDismissed(): string {
  try {
    return localStorage.getItem(DISMISS_KEY) || ''
  } catch {
    return ''
  }
}

/** Plaque on the start screen when GitHub has a newer release. */
export default function UpdateBanner() {
  const [status, setStatus] = useState<api.UpdateStatus | null>(null)
  const [dismissed, setDismissed] = useState(readDismissed)

  useEffect(() => {
    let active = true
    // A failed check is silent here: the Settings page shows the reason.
    api.getUpdates().then((s) => { if (active) setStatus(s) }).catch(() => {})
    return () => { active = false }
  }, [])

  if (!status?.update_available || dismissed === status.latest) return null

  const dismiss = () => {
    try { localStorage.setItem(DISMISS_KEY, status.latest) } catch { /* private mode */ }
    setDismissed(status.latest)
  }

  return (
    <section
      aria-label="Доступно обновление"
      className="mb-6 flex flex-col gap-3 rounded-2xl border border-emerald-500/30 bg-emerald-950/30 px-5 py-4 sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="text-sm">
        <p className="font-semibold text-emerald-200">Вышла версия {status.latest}</p>
        <p className="mt-1 text-slate-400">
          У вас {status.current}.{' '}
          {status.install === 'git' ? (
            <>Закройте приложение и запустите <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-xs text-slate-200">{updateCommand(status)}</code> в папке программы.</>
          ) : (
            <>Скачайте новую версию и распакуйте в новую папку; настройки сохранятся, папку projects перенесите вручную.</>
          )}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <a
          href={status.release_url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex min-h-10 items-center rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-emerald-500"
        >
          Что нового
        </a>
        <button
          type="button"
          onClick={dismiss}
          className="min-h-10 rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-400 transition-colors hover:bg-slate-800 hover:text-white cursor-pointer"
        >
          Скрыть
        </button>
      </div>
    </section>
  )
}
