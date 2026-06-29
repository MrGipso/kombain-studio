import { useEffect, useRef, useCallback } from 'react'

interface SSEHandlers {
  onLog?: (msg: string) => void
  onDone?: (data: Record<string, unknown>) => void
  onError?: (msg: string) => void
  onEvent?: (event: string, data: Record<string, unknown>) => void
}

export function useSSE() {
  const controllerRef = useRef<AbortController | null>(null)

  const startStream = useCallback((url: string, handlers: SSEHandlers) => {
    if (controllerRef.current) {
      controllerRef.current.abort()
    }
    const controller = new AbortController()
    controllerRef.current = controller

    fetch(`/api${url}`, { method: 'POST', signal: controller.signal }).then(async (res) => {
      if (!res.ok || !res.body) return
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const parsed = JSON.parse(line.slice(6))
              const ev = parsed.event || 'message'
              const data = parsed.data || parsed

              if (handlers.onEvent) handlers.onEvent(ev, data)
              if (ev === 'log' && handlers.onLog) handlers.onLog(data.message as string)
              if (ev === 'done' && handlers.onDone) handlers.onDone(data as Record<string, unknown>)
              if (ev === 'error' && handlers.onError) handlers.onError(data.message as string)
            } catch {
              // skip
            }
          }
        }
      }
    }).catch(() => {})

    return controller
  }, [])

  const stopStream = useCallback(() => {
    if (controllerRef.current) {
      controllerRef.current.abort()
      controllerRef.current = null
    }
  }, [])

  useEffect(() => {
    return () => {
      if (controllerRef.current) {
        controllerRef.current.abort()
      }
    }
  }, [])

  return { startStream, stopStream }
}
