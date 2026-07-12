import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { ToastContext, type ToastApi, type ToastKind } from './toast-context'

interface Toast {
  id: number
  kind: ToastKind
  message: string
}

let nextId = 1

const KIND_STYLES: Record<ToastKind, string> = {
  success: 'border-emerald-700 bg-emerald-950/90 text-emerald-200',
  error: 'border-red-700 bg-red-950/90 text-red-200',
  info: 'border-slate-700 bg-slate-900/95 text-slate-200',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const remove = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const toast = useCallback((message: string, kind: ToastKind = 'info') => {
    const id = nextId++
    setToasts((prev) => [...prev, { id, kind, message }])
    const ttl = kind === 'error' ? 7000 : 4000
    setTimeout(() => remove(id), ttl)
  }, [remove])

  const success = useCallback((message: string) => toast(message, 'success'), [toast])
  const error = useCallback((message: string) => toast(message, 'error'), [toast])
  const info = useCallback((message: string) => toast(message, 'info'), [toast])
  const api = useMemo<ToastApi>(() => ({ toast, success, error, info }), [toast, success, error, info])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div aria-live="polite" aria-relevant="additions" className="fixed bottom-4 right-4 z-50 flex max-w-sm flex-col gap-2">
        {toasts.map((t) => (
          <button
            key={t.id}
            type="button"
            role={t.kind === 'error' ? 'alert' : 'status'}
            aria-label={`${t.kind === 'error' ? 'Ошибка' : 'Уведомление'}: ${t.message}. Закрыть`}
            onClick={() => remove(t.id)}
            className={`text-left rounded-lg border px-4 py-3 text-sm shadow-lg backdrop-blur cursor-pointer ${KIND_STYLES[t.kind]}`}
          >
            {t.message}
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
