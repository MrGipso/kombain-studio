import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'

type ToastKind = 'success' | 'error' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  message: string
}

interface ToastApi {
  toast: (message: string, kind?: ToastKind) => void
  success: (message: string) => void
  error: (message: string) => void
  info: (message: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

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

  const api: ToastApi = {
    toast,
    success: (m) => toast(m, 'success'),
    error: (m) => toast(m, 'error'),
    info: (m) => toast(m, 'info'),
  }

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
        {toasts.map((t) => (
          <button
            key={t.id}
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

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) {
    // Fallback so the app never crashes if a component renders outside the provider.
    return {
      toast: (m) => console.log(m),
      success: (m) => console.log(m),
      error: (m) => console.error(m),
      info: (m) => console.log(m),
    }
  }
  return ctx
}
