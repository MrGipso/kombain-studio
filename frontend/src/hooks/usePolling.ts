import { useEffect } from 'react'

type PollingOptions = {
  enabled?: boolean
  // Run once as soon as polling starts instead of waiting a full interval.
  immediate?: boolean
}

// A request stuck longer than this no longer blocks the next tick.
const STUCK_AFTER_MS = 30_000

/**
 * Run `task` every `intervalMs` while `enabled`.
 *
 * - Ticks are skipped while the tab is hidden; one runs as soon as it is shown.
 * - Runs never overlap: a tick is skipped while the previous one is pending.
 * - `isCurrent()` turns false once this subscription ends (unmount, project
 *   switch, `enabled` off), so late responses can be dropped.
 *
 * `task` must be memoized (useCallback): a new identity restarts polling.
 */
export function usePolling(
  task: (isCurrent: () => boolean) => unknown,
  intervalMs: number,
  { enabled = true, immediate = false }: PollingOptions = {},
) {
  useEffect(() => {
    if (!enabled) return
    let current = true
    let startedAt: number | null = null
    const isCurrent = () => current

    const tick = async () => {
      if (!current || document.hidden) return
      if (startedAt !== null && Date.now() - startedAt < STUCK_AFTER_MS) return
      const runStartedAt = Date.now()
      startedAt = runStartedAt
      try {
        await task(isCurrent)
      } catch {
        // Each task reports its own errors; polling simply continues.
      } finally {
        if (startedAt === runStartedAt) startedAt = null
      }
    }
    const onVisibilityChange = () => {
      if (!document.hidden) void tick()
    }

    if (immediate) void tick()
    const timer = window.setInterval(() => void tick(), intervalMs)
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      current = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [task, intervalMs, enabled, immediate])
}
