import type { ReactNode } from 'react'
import type { DiagnosticResult, SetupServiceProbe, SetupToolCandidates } from '../../api/client'

export type DiagTarget = 'lm' | 'embed' | 'comfy' | 'ffmpeg' | 'whisper' | 'omnivoice' | 'woosh'
export type DiagState = DiagnosticResult & { loading?: boolean }

export const inputClass =
  'min-h-10 w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20'

export function StatusBadge({ state, label }: { state?: DiagState; label?: string }) {
  if (!state) {
    return (
      <div className="rounded-lg border border-slate-700 bg-slate-950/30 px-3 py-2 text-sm text-slate-500">
        {label ? `${label}: ` : ''}ещё не проверено
      </div>
    )
  }
  if (state.loading) {
    return (
      <div role="status" className="rounded-lg border border-slate-700 bg-slate-950/30 px-3 py-2 text-sm text-slate-400">
        {label ? `${label}: ` : ''}проверка…
      </div>
    )
  }
  const tone = state.ok ? 'bg-emerald-950/30 text-emerald-300' : 'bg-rose-950/35 text-rose-300'
  return (
    <div role="status" className={`rounded-lg border border-slate-700 px-3 py-2 text-sm ${tone}`}>
      <span className="font-medium">{state.ok ? '✓' : '✕'} {label ? `${label}: ` : ''}{state.message}</span>
      {state.detail && <span className="mt-1 block break-all font-mono text-xs text-slate-400">{state.detail}</span>}
    </div>
  )
}

export function Field({ id, label, hint, children }: { id: string; label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-xs font-medium text-slate-400">{label}</label>
      {children}
      {hint && <p className="mt-1.5 text-xs leading-5 text-slate-500">{hint}</p>}
    </div>
  )
}

/** Text input for a path plus one-click chips for every auto-discovered candidate. */
export function PathField({
  id, label, hint, value, onChange, found, placeholder,
}: {
  id: string
  label: string
  hint?: ReactNode
  value: string
  onChange: (value: string) => void
  found?: SetupToolCandidates
  placeholder?: string
}) {
  const candidates = found?.candidates ?? []
  return (
    <Field id={id} label={label} hint={hint}>
      <input
        id={id}
        type="text"
        spellCheck={false}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={inputClass}
      />
      {found && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          {candidates.length > 0 ? (
            <>
              <span className="text-slate-500">Найдено:</span>
              {candidates.map((path) => (
                <button
                  key={path}
                  type="button"
                  onClick={() => onChange(path)}
                  aria-pressed={path === value}
                  className={`max-w-full cursor-pointer truncate rounded-md border px-2 py-1 font-mono transition-colors ${
                    path === value
                      ? 'border-indigo-500 bg-indigo-500/15 text-indigo-200'
                      : 'border-slate-700 bg-slate-900 text-slate-300 hover:border-slate-500'
                  }`}
                  title={path}
                >
                  {path}
                </button>
              ))}
            </>
          ) : (
            <span className="text-amber-300">Автопоиск ничего не нашёл — укажите путь вручную.</span>
          )}
        </div>
      )}
    </Field>
  )
}

/** Reachability of one service URL with a button to adopt it. */
export function ProbeRow({ probe, active, onUse }: { probe: SetupServiceProbe; active: boolean; onUse: () => void }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-700 bg-slate-950/30 px-3 py-2">
      <div className="min-w-0">
        <p className="truncate font-mono text-sm text-slate-200">{probe.url}</p>
        <p className={`text-xs ${probe.reachable ? 'text-emerald-300' : 'text-slate-500'}`}>
          {probe.reachable
            ? `Отвечает${probe.models.length ? ` · ${probe.models.length} ${pluralModels(probe.models.length)}` : ''}`
            : probe.detail.includes('ключ') ? probe.detail : 'Не отвечает'}
        </p>
      </div>
      {active ? (
        <span className="text-xs font-medium text-indigo-300">Используется</span>
      ) : (
        <button
          type="button"
          onClick={onUse}
          disabled={!probe.reachable}
          className="min-h-9 cursor-pointer rounded-lg border border-slate-600 bg-slate-800 px-3 text-xs font-medium text-slate-200 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Использовать
        </button>
      )}
    </div>
  )
}

function pluralModels(n: number) {
  const mod10 = n % 10
  const mod100 = n % 100
  if (mod10 === 1 && mod100 !== 11) return 'модель'
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'модели'
  return 'моделей'
}

export function Command({ children }: { children: string }) {
  return <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[11px] text-slate-200">{children}</code>
}
