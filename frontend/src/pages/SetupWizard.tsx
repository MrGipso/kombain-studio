import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import * as api from '../api/client'
import type { SetupDetection } from '../api/client'
import type { Settings } from '../api/types'
import ThemeToggle from '../components/ThemeToggle'
import { DIAG_LABELS, REQUIRED_DIAG, SECRET_KEYS, STEPS, autofill, type StepDef, type StepId } from './setup/config'
import { ComfyStep, FfmpegStep, LmStep, OutputStep, StockStep, VoiceStep, WelcomeStep, WhisperStep, type StepProps } from './setup/steps'
import { StatusBadge, type DiagState, type DiagTarget } from './setup/ui'

const STEP_BODIES: Partial<Record<StepId, (props: StepProps) => ReactNode>> = {
  welcome: WelcomeStep,
  output: OutputStep,
  ffmpeg: FfmpegStep,
  whisper: WhisperStep,
  lm: LmStep,
  comfy: ComfyStep,
  voice: VoiceStep,
  stock: StockStep,
}

const errorText = (error: unknown) => (error instanceof Error ? error.message : String(error))

const buttonGhost =
  'min-h-10 cursor-pointer rounded-lg border border-slate-700 bg-slate-900 px-4 py-2 text-sm font-medium text-slate-300 transition-colors hover:bg-slate-800 hover:text-white disabled:cursor-not-allowed disabled:opacity-50'
const buttonPrimary =
  'min-h-10 cursor-pointer rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50'

export default function SetupWizard() {
  const navigate = useNavigate()
  const [settings, setSettings] = useState<Settings | null>(null)
  const [detection, setDetection] = useState<SetupDetection | null>(null)
  const [draft, setDraft] = useState<Partial<Settings>>({})
  const [index, setIndex] = useState(0)
  const [diag, setDiag] = useState<Partial<Record<DiagTarget, DiagState>>>({})
  const [busy, setBusy] = useState<'' | 'saving' | 'scanning' | 'finishing'>('')
  const [error, setError] = useState('')
  const [loadError, setLoadError] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([api.getSettings(), api.detectSetup()])
      .then(([loaded, found]) => {
        if (!active) return
        setSettings(loaded)
        setDetection(found)
        setDraft(autofill(loaded, found))
      })
      .catch((e) => active && setLoadError(errorText(e)))
    return () => { active = false }
  }, [])

  const value = useCallback(
    <K extends keyof Settings>(key: K): Settings[K] =>
      (key in draft ? draft[key] : settings?.[key]) as Settings[K],
    [draft, settings],
  )
  const set = useCallback(<K extends keyof Settings>(key: K, next: Settings[K]) => {
    setDraft((prev) => ({ ...prev, [key]: next }))
  }, [])

  const step = STEPS[index]

  const checksFor = (def: StepDef): DiagTarget[] =>
    def.checks.filter((target) => target !== 'woosh' || Boolean(value('sfx_enabled')))

  /** Save the draft values of `keys`; returns the fresh settings or null on error. */
  const save = async (keys: (keyof Settings)[]): Promise<Settings | null> => {
    if (!settings) return null
    const payload: Partial<Settings> = {}
    for (const key of keys) {
      if (!(key in draft)) continue
      const next = draft[key]
      if (SECRET_KEYS.has(key) && !next) continue // blank secret = keep the stored one
      if (next === settings[key]) continue
      ;(payload as Record<string, unknown>)[key] = next
    }
    if (Object.keys(payload).length === 0) return settings
    setBusy('saving')
    setError('')
    try {
      await api.updateSettings(payload)
      const fresh = await api.getSettings()
      setSettings(fresh)
      setDraft((prev) => {
        const rest = { ...prev }
        for (const key of Object.keys(payload)) delete (rest as Record<string, unknown>)[key]
        return rest
      })
      return fresh
    } catch (e) {
      setError(`Не удалось сохранить: ${errorText(e)}`)
      return null
    } finally {
      setBusy('')
    }
  }

  const runChecks = async (targets: DiagTarget[]) => {
    setDiag((prev) => {
      const next = { ...prev }
      for (const target of targets) next[target] = { target, ok: false, message: '', loading: true }
      return next
    })
    await Promise.all(targets.map(async (target) => {
      try {
        const result = await api.runDiagnostic(target)
        setDiag((prev) => ({ ...prev, [target]: result }))
      } catch (e) {
        setDiag((prev) => ({ ...prev, [target]: { target, ok: false, message: errorText(e) } }))
      }
    }))
  }

  const saveAndCheck = async () => {
    if (await save(step.keys)) await runChecks(checksFor(step))
  }

  const rescan = async () => {
    const fresh = await save(step.keys)
    if (!fresh) return
    setBusy('scanning')
    try {
      const found = await api.detectSetup()
      setDetection(found)
      const filled = autofill(fresh, found)
      setDraft((prev) => {
        const next = { ...prev }
        for (const key of step.keys) {
          if (key in filled && !(key in prev)) (next as Record<string, unknown>)[key] = filled[key]
        }
        return next
      })
    } catch (e) {
      setError(`Поиск не удался: ${errorText(e)}`)
    } finally {
      setBusy('')
    }
  }

  const goTo = async (target: number) => {
    if (target === index || busy) return
    if (!(await save(step.keys))) return
    setError('')
    setIndex(target)
    const next = STEPS[target]
    if (next.id === 'finish') void runChecks(checksFor(next))
  }

  const finish = async () => {
    setBusy('finishing')
    setError('')
    try {
      if (!settings?.setup_completed) await api.updateSettings({ setup_completed: true })
      navigate('/', { replace: true })
    } catch (e) {
      setError(`Не удалось завершить: ${errorText(e)}`)
      setBusy('')
    }
  }

  if (loadError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950 px-4">
        <div className="max-w-md rounded-2xl border border-slate-800 bg-slate-900 p-6 text-sm text-slate-300">
          <p className="font-semibold text-white">Не удалось открыть мастер настройки</p>
          <p className="mt-2 text-rose-300">{loadError}</p>
          <button type="button" onClick={() => window.location.reload()} className={`${buttonPrimary} mt-4`}>Повторить</button>
        </div>
      </div>
    )
  }
  if (!settings) {
    return (
      <div role="status" className="flex min-h-screen items-center justify-center bg-slate-950 text-sm text-slate-400">
        Ищем программы на этом компьютере…
      </div>
    )
  }

  const Body = STEP_BODIES[step.id]
  const stepProps = { settings, detection, value, set }
  const stepChecks = checksFor(step)
  const isLast = index === STEPS.length - 1
  const requiredFailing = [...REQUIRED_DIAG].filter((target) => diag[target] && !diag[target]?.loading && !diag[target]?.ok)
  const stepState = (def: StepDef): 'ok' | 'fail' | 'none' => {
    const results = checksFor(def).filter(() => def.id !== 'finish').map((target) => diag[target]).filter(Boolean) as DiagState[]
    if (!results.length || results.some((r) => r.loading)) return 'none'
    return results.every((r) => r.ok) ? 'ok' : 'fail'
  }

  return (
    <div className="min-h-screen bg-slate-950 px-4 py-6 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-5xl space-y-6">
        <header className="flex flex-col gap-4 rounded-2xl border border-slate-800 bg-slate-900/95 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-indigo-400">Первый запуск</p>
            <h1 className="mt-1 text-2xl font-bold text-white">Мастер настройки</h1>
            <p className="mt-1 text-sm text-slate-500">Шаг {index + 1} из {STEPS.length} · {step.title}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ThemeToggle />
            <button type="button" onClick={finish} disabled={Boolean(busy)} className={buttonGhost}>
              {settings.setup_completed ? 'Закрыть' : 'Пропустить мастер'}
            </button>
          </div>
        </header>

        <div className="h-1.5 overflow-hidden rounded-full bg-slate-800" aria-hidden="true">
          <div className="h-full rounded-full bg-indigo-500 transition-all" style={{ width: `${((index + 1) / STEPS.length) * 100}%` }} />
        </div>

        <div className="grid gap-6 lg:grid-cols-[230px_1fr]">
          <nav aria-label="Шаги мастера" className="hidden lg:block">
            <ol className="space-y-1">
              {STEPS.map((def, i) => {
                const state = stepState(def)
                return (
                  <li key={def.id}>
                    <button
                      type="button"
                      onClick={() => void goTo(i)}
                      aria-current={i === index ? 'step' : undefined}
                      className={`flex w-full cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                        i === index ? 'bg-indigo-500/15 text-indigo-200' : 'text-slate-400 hover:bg-slate-900 hover:text-slate-200'
                      }`}
                    >
                      <span className={`flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                        state === 'ok' ? 'bg-emerald-950/30 text-emerald-300' : state === 'fail' ? 'bg-rose-950/35 text-rose-300' : 'bg-slate-800 text-slate-400'
                      }`}>
                        {state === 'ok' ? '✓' : state === 'fail' ? '!' : i + 1}
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate">{def.title}</span>
                        {def.optional && <span className="block text-[11px] text-slate-500">по желанию</span>}
                      </span>
                    </button>
                  </li>
                )
              })}
            </ol>
          </nav>

          <main className="rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-7">
            <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-xl font-semibold text-white">{step.title}</h2>
                <p className="mt-1 text-sm text-slate-500">{step.description}</p>
              </div>
              {step.optional && (
                <span className="rounded-full border border-slate-700 px-2.5 py-1 text-xs text-slate-400">Можно пропустить</span>
              )}
            </div>

            {Body && <Body {...stepProps} />}

            {step.id === 'finish' && (
              <div className="space-y-3">
                {checksFor(step).map((target) => (
                  <div key={target} className="flex flex-col gap-1">
                    <StatusBadge state={diag[target]} label={`${DIAG_LABELS[target]}${REQUIRED_DIAG.has(target) ? ' (обязательно)' : ''}`} />
                  </div>
                ))}
                {requiredFailing.length > 0 ? (
                  <p className="rounded-lg bg-amber-950/30 px-3 py-2 text-sm text-amber-300">
                    Не прошли обязательные проверки: {requiredFailing.map((t) => DIAG_LABELS[t]).join(', ')}.
                    Можно вернуться к шагу или завершить сейчас и донастроить позже в «Настройках».
                  </p>
                ) : (
                  <p className="text-sm text-slate-400">Необязательные сервисы можно запустить позже — программа проверит их перед работой.</p>
                )}
              </div>
            )}

            {step.id !== 'finish' && stepChecks.length > 0 && stepChecks.some((target) => diag[target]) && (
              <div className="mt-6 space-y-2">
                {stepChecks.map((target) => <StatusBadge key={target} state={diag[target]} label={DIAG_LABELS[target]} />)}
              </div>
            )}

            {error && <p role="alert" className="mt-5 rounded-lg bg-rose-950/35 px-3 py-2 text-sm text-rose-300">{error}</p>}

            <div className="mt-8 flex flex-wrap items-center justify-between gap-3 border-t border-slate-800 pt-5">
              <button type="button" onClick={() => void goTo(index - 1)} disabled={index === 0 || Boolean(busy)} className={buttonGhost}>
                ← Назад
              </button>
              <div className="flex flex-wrap items-center gap-2">
                {['output', 'ffmpeg', 'whisper', 'lm', 'comfy', 'voice'].includes(step.id) && (
                  <button type="button" onClick={() => void rescan()} disabled={Boolean(busy)} className={buttonGhost}>
                    {busy === 'scanning' ? 'Ищем…' : 'Искать снова'}
                  </button>
                )}
                {step.id !== 'finish' && stepChecks.length > 0 && (
                  <button type="button" onClick={() => void saveAndCheck()} disabled={Boolean(busy)} className={buttonGhost}>
                    Проверить
                  </button>
                )}
                {step.id === 'finish' && (
                  <button type="button" onClick={() => void runChecks(checksFor(step))} disabled={Boolean(busy)} className={buttonGhost}>
                    Проверить снова
                  </button>
                )}
                {isLast ? (
                  <button type="button" onClick={() => void finish()} disabled={Boolean(busy)} className={buttonPrimary}>
                    {busy === 'finishing' ? 'Сохраняем…' : 'Завершить настройку'}
                  </button>
                ) : (
                  <button type="button" onClick={() => void goTo(index + 1)} disabled={Boolean(busy)} className={buttonPrimary}>
                    {busy === 'saving' ? 'Сохраняем…' : index === 0 ? 'Начать' : 'Далее'}
                  </button>
                )}
              </div>
            </div>
          </main>
        </div>
      </div>
    </div>
  )
}
