// Participation switch: "does this clip ship in the final video".
//
// Deliberately styled as a state toggle (layer-visibility pattern), not as a
// destructive red action button: excluding is reversible, files are kept, and
// the freed time flows to the neighbouring clip. The switch communicates that
// this is a property of the card, not a deletion.

interface FinalToggleProps {
  on: boolean
  onToggle: () => void
  disabled?: boolean
  title?: string
  size?: 'sm' | 'md'
}

export function FinalToggle({ on, onToggle, disabled, title, size = 'sm' }: FinalToggleProps) {
  const trackSize = size === 'md' ? 'h-5 w-9' : 'h-4 w-7'
  const thumbSize = size === 'md' ? 'h-4 w-4' : 'h-3 w-3'
  const thumbShift = size === 'md' ? (on ? 'translate-x-[18px]' : 'translate-x-0.5') : (on ? 'translate-x-[14px]' : 'translate-x-0.5')
  const textSize = size === 'md' ? 'text-sm' : 'text-xs'
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      title={title}
      onClick={(event) => {
        event.stopPropagation()
        onToggle()
      }}
      className={`inline-flex min-h-9 items-center gap-2 rounded-lg px-1.5 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45 ${on ? 'text-emerald-300' : 'text-slate-400'}`}
    >
      <span aria-hidden="true" className={`relative inline-flex shrink-0 items-center rounded-full transition-colors ${trackSize} ${on ? 'bg-emerald-500' : 'bg-slate-600'}`}>
        <span className={`inline-block transform rounded-full bg-white shadow transition-transform ${thumbSize} ${thumbShift}`} />
      </span>
      <span className={`font-semibold ${textSize}`}>{on ? 'В финале' : 'Вне финала'}</span>
    </button>
  )
}
