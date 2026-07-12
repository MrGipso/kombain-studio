import { createContext, useContext } from 'react'

export type ToastKind = 'success' | 'error' | 'info'

export interface ToastApi {
  toast: (message: string, kind?: ToastKind) => void
  success: (message: string) => void
  error: (message: string) => void
  info: (message: string) => void
}

export const ToastContext = createContext<ToastApi | null>(null)

const FALLBACK_TOAST: ToastApi = {
  toast: (message) => console.log(message),
  success: (message) => console.log(message),
  error: (message) => console.error(message),
  info: (message) => console.log(message),
}

export function useToast(): ToastApi {
  return useContext(ToastContext) || FALLBACK_TOAST
}
