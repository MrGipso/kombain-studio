import { useEffect, useRef, useCallback } from 'react'

type CloseReason = 'completed' | 'aborted' | 'closed'

interface SSEHandlers {
  onLog?: (msg: string) => void
  onProgress?: (step: string, progress: number) => void
  onDone?: (data: Record<string, unknown>) => void
  onError?: (msg: string) => void
  onEvent?: (event: string, data: Record<string, unknown>) => void
  // Fires exactly once when the stream closes. `aborted` means the client stopped
  // watching; the server task may still be running, so callers should not treat
  // it as completion.
  onClose?: (reason: CloseReason) => void
}

export function useSSE() {
  const controllerRef = useRef<AbortController | null>(null)

  const startStream = useCallback((url: string, handlers: SSEHandlers) => {
    if (controllerRef.current) {
      controllerRef.current.abort()
    }
    const controller = new AbortController()
    controllerRef.current = controller

    let closed = false
    let terminalEvent = false
    const close = (reason?: CloseReason) => {
      if (closed) return
      closed = true
      if (controllerRef.current === controller) controllerRef.current = null
      handlers.onClose?.(reason || (terminalEvent ? 'completed' : 'closed'))
    }

    fetch(`/api${url}`, { method: 'POST', signal: controller.signal }).then(async (res) => {
      if (!res.ok || !res.body) {
        terminalEvent = true
        const body = await res.text().catch(() => '')
        let message = body || `Stream failed with HTTP ${res.status}`
        try {
          const parsed = JSON.parse(body)
          if (typeof parsed.detail === 'string') message = parsed.detail
        } catch {
          // Plain-text response.
        }
        handlers.onError?.(message)
        close('completed')
        return
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      try {
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
                if (ev === 'progress' && handlers.onProgress) handlers.onProgress(data.step as string, data.progress as number)
                if (ev === 'done') {
                  terminalEvent = true
                  if (handlers.onDone) handlers.onDone(data as Record<string, unknown>)
                }
                if (ev === 'error') {
                  terminalEvent = true
                  if (handlers.onError) handlers.onError(data.message as string)
                }
              } catch {
                // skip malformed SSE payloads
              }
            }
          }
        }
      } finally {
        close(controller.signal.aborted && !terminalEvent ? 'aborted' : undefined)
      }
    }).catch((err) => {
      if (controller.signal.aborted || err?.name === 'AbortError') {
        close('aborted')
        return
      }
      terminalEvent = true
      handlers.onError?.('Stream connection lost')
      close('completed')
    })

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
