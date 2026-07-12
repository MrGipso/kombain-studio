import { useState, useEffect, useMemo, useRef } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import type { SfxState, SfxItem } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

interface Group {
  key: string
  query: string
  items: SfxItem[]
}

const EMPTY: SfxState = {
  sfx_enabled: false,
  woosh_url: '',
  woosh_online: false,
  sfx_volume_db: -24,
  items: [],
}

// Consecutive cards with the same non-empty query share one sound — mirror the
// exact grouping the export bed uses, so the page shows what will be mixed.
function groupItems(items: SfxItem[]): Group[] {
  const groups: Group[] = []
  for (const it of items) {
    const q = (it.sfx_query || '').trim()
    const prev = groups[groups.length - 1]
    if (prev && prev.query === q && q !== '') {
      prev.items.push(it)
    } else {
      groups.push({ key: `g${it.card_index}`, query: q, items: [it] })
    }
  }
  return groups
}

export default function Sounds() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const toast = useToast()
  const [state, setState] = useState<SfxState>(EMPTY)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [working, setWorking] = useState<string | null>(null) // group key or 'all'
  const [batchNote, setBatchNote] = useState('')
  const fileInputs = useRef<Record<string, HTMLInputElement | null>>({})
  const lockedElsewhere = Boolean(busy) && !working

  const reload = () =>
    api.getSfx(projectId).then((d) => setState((s) => ({ ...s, ...d }))).catch(() => {})

  useEffect(() => {
    api.getSfx(projectId)
      .then((d) => setState({ ...EMPTY, ...d }))
      .catch((e) => toast.error(`Не удалось загрузить звуки: ${(e as Error).message}`))
  }, [projectId, toast])

  const groups = useMemo(() => groupItems(state.items), [state.items])
  const indices = (g: Group) => g.items.map((i) => i.card_index)
  const groupUrl = (g: Group) => g.items.find((i) => i.url)?.url || ''
  const missing = groups.filter((g) => g.query && !groupUrl(g))

  const saveGlobal = async (patch: { sfx_enabled?: boolean; sfx_volume_db?: number }) => {
    try {
      await api.updateSettings(patch)
      setState((s) => ({ ...s, ...patch }))
    } catch (e) {
      toast.error(`Настройки: ${(e as Error).message}`)
    }
  }

  const saveQuery = async (g: Group) => {
    const next = (edits[g.key] ?? g.query).trim()
    if (next === g.query) return
    try {
      const d = await api.setSfxQuery(projectId, indices(g), next)
      setState((s) => ({ ...s, items: d.items }))
      setEdits((m) => { const c = { ...m }; delete c[g.key]; return c })
    } catch (e) {
      toast.error(`Запрос: ${(e as Error).message}`)
    }
  }

  const generate = async (g: Group, fresh: boolean) => {
    if (busy && !working) { toast.error('Дождитесь завершения текущей задачи'); return }
    setWorking(g.key)
    setBusy('Звуки')
    try {
      const d = await api.generateSfx(projectId, indices(g), fresh)
      setState((s) => ({ ...s, items: d.items }))
      toast.success(fresh ? 'Новый вариант готов' : 'Звук сгенерирован')
    } catch (e) {
      toast.error(`Генерация: ${(e as Error).message}`)
    } finally {
      setWorking(null)
      setBusy(null)
    }
  }

  const generateAllMissing = async () => {
    if (busy && !working) { toast.error('Дождитесь завершения текущей задачи'); return }
    if (missing.length === 0) { toast.success('Все звуки уже на месте'); return }
    setWorking('all')
    setBusy('Звуки')
    let ok = 0
    try {
      for (let i = 0; i < missing.length; i++) {
        setBatchNote(`${i + 1} / ${missing.length}: ${missing[i].query}`)
        try {
          const d = await api.generateSfx(projectId, indices(missing[i]), false)
          setState((s) => ({ ...s, items: d.items }))
          ok++
        } catch (e) {
          toast.error(`«${missing[i].query}»: ${(e as Error).message}`)
        }
      }
      toast.success(`Готово: ${ok} из ${missing.length}`)
    } finally {
      setBatchNote('')
      setWorking(null)
      setBusy(null)
      reload()
    }
  }

  const uploadFile = async (g: Group, file: File) => {
    setWorking(g.key)
    try {
      const d = await api.uploadSfx(projectId, indices(g), file)
      setState((s) => ({ ...s, items: d.items }))
      toast.success('Файл подключён к кадрам')
    } catch (e) {
      toast.error(`Загрузка: ${(e as Error).message}`)
    } finally {
      setWorking(null)
    }
  }

  const disableGroup = async (g: Group) => {
    try {
      const d = await api.setSfxQuery(projectId, indices(g), '')
      setState((s) => ({ ...s, items: d.items }))
    } catch (e) {
      toast.error(`Отключение: ${(e as Error).message}`)
    }
  }

  const pauseOthers = (current: HTMLAudioElement) => {
    document.querySelectorAll('audio').forEach((el) => {
      if (el !== current) (el as HTMLAudioElement).pause()
    })
  }

  const wooshChip = state.woosh_online
    ? <span className="text-xs text-emerald-300 bg-emerald-600/20 px-2.5 py-1 rounded">Woosh online</span>
    : <span className="text-xs text-amber-300 bg-amber-600/20 px-2.5 py-1 rounded">Woosh offline — запустите start_woosh.bat</span>

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Звуки</h1>
        <div className="flex items-center gap-2">
          <span className="text-xs text-indigo-300 bg-indigo-600/20 px-2.5 py-1 rounded">ambient-слой кадров</span>
          {wooshChip}
        </div>
      </div>

      {lockedElsewhere && (
        <div className="bg-sky-900/30 border border-sky-700/50 rounded-xl p-4 text-sky-300 text-sm">
          ⏳ Идёт другая задача ({busy}). Дождитесь завершения.
        </div>
      )}

      {/* Layer controls */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 p-5 space-y-4">
        <div className="flex flex-wrap items-center gap-6">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox" checked={state.sfx_enabled}
              onChange={(e) => saveGlobal({ sfx_enabled: e.target.checked })}
              className="w-5 h-5 accent-indigo-500 cursor-pointer"
            />
            <span className="text-sm text-slate-200">Добавлять SFX-слой в финальный экспорт</span>
          </label>
          <div className="flex items-center gap-3 flex-1 min-w-[260px]">
            <span className="text-sm text-slate-200 whitespace-nowrap">Громкость слоя</span>
            <input
              type="range" min={-40} max={-10} step={1} value={state.sfx_volume_db}
              onChange={(e) => setState((s) => ({ ...s, sfx_volume_db: Number(e.target.value) }))}
              onMouseUp={() => saveGlobal({ sfx_volume_db: state.sfx_volume_db })}
              className="flex-1 accent-indigo-500 cursor-pointer"
            />
            <span className="text-sm text-slate-400 w-14">{Math.round(state.sfx_volume_db)} dB</span>
          </div>
          <button
            onClick={generateAllMissing}
            disabled={Boolean(working) || lockedElsewhere || !state.woosh_online || missing.length === 0}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-bold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
          >
            {working === 'all' ? `⏳ ${batchNote}` : `🔊 Сгенерировать недостающие (${missing.length})`}
          </button>
        </div>
        <p className="text-xs text-slate-500 leading-relaxed">
          Каждая группа кадров одной локации получает один непрерывный фоновый звук — он миксуется под голос при экспорте.
          Запросы придумывает LLM на шаге «Кадры»; здесь их можно поправить, послушать результат, перегенерировать или заменить своим файлом.
        </p>
      </div>

      {/* Groups */}
      {state.items.length === 0 ? (
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-8 text-center text-sm text-slate-600">
          Пока нет кадров. Сгенерируй shot list на шаге «Кадры» — появятся и звуковые запросы.
        </div>
      ) : (
        <div className="space-y-3">
          {groups.map((g) => {
            const url = groupUrl(g)
            const editing = edits[g.key] ?? g.query
            const isWorking = working === g.key
            const first = g.items[0]
            const last = g.items[g.items.length - 1]
            const shots = g.items.length === 1
              ? `кадр ${first.shot_index}`
              : `кадры ${first.shot_index}–${last.shot_index}`
            return (
              <div key={g.key} className={`rounded-xl border p-4 ${g.query ? 'border-slate-700 bg-slate-900' : 'border-slate-800 bg-slate-900/40'}`}>
                <div className="flex items-start gap-4 flex-wrap">
                  <div className="w-44 shrink-0">
                    <div className="text-sm text-slate-200">{shots}</div>
                    <div className="text-xs text-slate-500">{first.start} – {last.end}</div>
                    <div className="text-xs text-slate-600 truncate" title={first.scene_title}>{first.scene_title}</div>
                  </div>
                  <div className="flex-1 min-w-[240px]">
                    <input
                      value={editing}
                      placeholder="фоновый звук по-английски: server room fan hum, rain on window…"
                      onChange={(e) => setEdits((m) => ({ ...m, [g.key]: e.target.value }))}
                      onBlur={() => saveQuery(g)}
                      onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                      className="w-full bg-slate-800/60 text-slate-200 text-sm rounded-lg px-3 py-2 border border-slate-700 focus:border-indigo-500 outline-none font-mono"
                    />
                    {url && (
                      <audio
                        controls src={url} className="w-full mt-2 h-9"
                        onPlay={(e) => pauseOthers(e.currentTarget)}
                      />
                    )}
                    {!url && g.query && (
                      <div className="text-xs text-amber-400/80 mt-2">звук ещё не сгенерирован</div>
                    )}
                    {!g.query && (
                      <div className="text-xs text-slate-600 mt-2">без звука — впиши запрос, чтобы включить</div>
                    )}
                  </div>
                  <div className="flex flex-col gap-1.5 shrink-0 w-40">
                    <button
                      onClick={() => generate(g, Boolean(url))}
                      disabled={isWorking || Boolean(working) || lockedElsewhere || !g.query || !state.woosh_online}
                      className="text-xs px-3 py-1.5 rounded bg-indigo-600/80 hover:bg-indigo-500 text-white disabled:opacity-40 cursor-pointer"
                    >
                      {isWorking ? '⏳ Генерация…' : url ? '🎲 Перегенерировать' : '🔊 Сгенерировать'}
                    </button>
                    <button
                      onClick={() => fileInputs.current[g.key]?.click()}
                      disabled={isWorking || Boolean(working)}
                      className="text-xs px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-40 cursor-pointer"
                    >
                      📁 Свой файл…
                    </button>
                    <input
                      ref={(el) => { fileInputs.current[g.key] = el }}
                      type="file" accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.opus" className="hidden"
                      onChange={(e) => {
                        const f = e.target.files?.[0]
                        if (f) uploadFile(g, f)
                        e.target.value = ''
                      }}
                    />
                    {g.query && (
                      <button
                        onClick={() => disableGroup(g)}
                        disabled={isWorking || Boolean(working)}
                        className="text-xs px-3 py-1.5 rounded text-slate-500 hover:text-red-400 disabled:opacity-40 cursor-pointer"
                      >
                        Отключить звук
                      </button>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
