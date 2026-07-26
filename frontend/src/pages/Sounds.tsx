import { useState, useEffect, useMemo, useRef } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import type { MusicState, SfxState, SfxItem } from '../api/types'

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

const tcToSec = (tc: string) => {
  const parts = String(tc || '').split(':').map(Number)
  if (parts.some((n) => Number.isNaN(n))) return 0
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
  if (parts.length === 2) return parts[0] * 60 + parts[1]
  return parts[0] || 0
}

// Ambience groups: consecutive cards sharing one non-empty query play one
// continuous bed — mirror the grouping the export uses, so the page shows what
// will actually be mixed. Cards WITHOUT a query are deliberately not turned
// into groups: sound is now spot work, so most cards are silent, and listing
// each of them produced hundreds of empty rows. They are summarised instead.
function groupAmbience(items: SfxItem[]): Group[] {
  const groups: Group[] = []
  for (const item of items) {
    const query = (item.sfx_query || '').trim()
    if (!query) continue
    const previous = groups[groups.length - 1]
    const contiguous = previous
      && previous.query === query
      && previous.items[previous.items.length - 1].card_index === item.card_index - 1
    if (contiguous) previous.items.push(item)
    else groups.push({ key: `g${item.card_index}`, query, items: [item] })
  }
  return groups
}

export default function Sounds() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const toast = useToast()
  const [state, setState] = useState<SfxState>(EMPTY)
  const [music, setMusic] = useState<MusicState | null>(null)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [accentEdits, setAccentEdits] = useState<Record<number, string>>({})
  const [addingTo, setAddingTo] = useState<'accent' | 'ambience' | null>(null)
  const [addCard, setAddCard] = useState('')
  const [working, setWorking] = useState<string | null>(null) // group key or 'all'
  const [progress, setProgress] = useState<{ done: number; total: number; note: string } | null>(null)
  const cancelRef = useRef(false)
  const [cancelRequested, setCancelRequested] = useState(false)
  const fileInputs = useRef<Record<string, HTMLInputElement | null>>({})
  const lockedElsewhere = Boolean(busy) && !working

  const reload = () =>
    api.getSfx(projectId).then((d) => setState((s) => ({ ...s, ...d }))).catch(() => {})

  useEffect(() => {
    api.getSfx(projectId)
      .then((d) => setState({ ...EMPTY, ...d }))
      .catch((e) => toast.error(`Не удалось загрузить звуки: ${(e as Error).message}`))
    api.getMusic(projectId).then(setMusic).catch(() => setMusic(null))
  }, [projectId, toast])

  const ambience = useMemo(() => groupAmbience(state.items), [state.items])
  const accents = useMemo(
    () => state.items.filter((item) => (item.sfx_accent_query || '').trim()),
    [state.items],
  )
  const indices = (g: Group) => g.items.map((i) => i.card_index)
  const groupUrl = (g: Group) => g.items.find((i) => i.url)?.url || ''
  const missingAmbience = ambience.filter((g) => !groupUrl(g))
  const missingAccents = accents.filter((item) => !item.accent_url)
  const missingTotal = missingAmbience.length + missingAccents.length
  const silentCards = state.items.length - new Set([
    ...ambience.flatMap(indices),
    ...accents.map((item) => item.card_index),
  ]).size

  // A music bed running under the whole video masks sustained ambience: only
  // short transients cut through. Saying so here prevents pointless renders.
  const timelineEnd = useMemo(
    () => state.items.reduce((max, item) => Math.max(max, tcToSec(item.end)), 0),
    [state.items],
  )
  const musicCoversAll = Boolean(
    music?.music_enabled && music.music_duration > 0 && timelineEnd > 0
    && music.music_duration >= timelineEnd * 0.9,
  )

  const canGenerate = state.sfx_enabled && state.woosh_online

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

  // One queue for both layers, cancellable at the item boundary like the
  // asset board's batch runs: a stuck provider must not trap the page.
  const generateAllMissing = async () => {
    if (busy && !working) { toast.error('Дождитесь завершения текущей задачи'); return }
    if (missingTotal === 0) { toast.info('Все звуки уже на месте'); return }
    cancelRef.current = false
    setCancelRequested(false)
    setWorking('all')
    setBusy('Звуки')
    const jobs: Array<{ label: string; run: () => Promise<{ items: SfxItem[] }> }> = [
      ...missingAccents.map((item) => ({
        label: `акцент · кадр ${item.shot_index}`,
        run: () => api.generateSfx(projectId, [item.card_index], false, 'accent'),
      })),
      ...missingAmbience.map((g) => ({
        label: `фон · ${g.query}`,
        run: () => api.generateSfx(projectId, indices(g), false),
      })),
    ]
    let ok = 0
    let failed = 0
    try {
      for (let i = 0; i < jobs.length; i++) {
        if (cancelRef.current) break
        setProgress({ done: i, total: jobs.length, note: jobs[i].label })
        try {
          const d = await jobs[i].run()
          setState((s) => ({ ...s, items: d.items }))
          ok++
        } catch {
          failed++
        }
      }
      if (cancelRef.current) toast.info(`Остановлено: готово ${ok} из ${jobs.length}`)
      else if (failed) toast.error(`Готово ${ok}, не удалось ${failed} — проблемные помечены ниже`)
      else toast.success(`Готово: ${ok}`)
    } finally {
      setProgress(null)
      setWorking(null)
      setCancelRequested(false)
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

  const saveAccentQuery = async (it: SfxItem) => {
    const next = (accentEdits[it.card_index] ?? it.sfx_accent_query).trim()
    if (next === (it.sfx_accent_query || '')) return
    try {
      const d = await api.setSfxQuery(projectId, [it.card_index], next, 'accent')
      setState((s) => ({ ...s, items: d.items }))
      setAccentEdits((m) => { const c = { ...m }; delete c[it.card_index]; return c })
    } catch (e) {
      toast.error(`Акцент: ${(e as Error).message}`)
    }
  }

  const generateAccent = async (it: SfxItem, fresh: boolean) => {
    if (busy && !working) { toast.error('Дождитесь завершения текущей задачи'); return }
    setWorking(`acc-${it.card_index}`)
    setBusy('Звуки')
    try {
      const d = await api.generateSfx(projectId, [it.card_index], fresh, 'accent')
      setState((s) => ({ ...s, items: d.items }))
      toast.success(fresh ? 'Новый вариант акцента готов' : 'Акцент сгенерирован')
    } catch (e) {
      toast.error(`Акцент: ${(e as Error).message}`)
    } finally {
      setWorking(null)
      setBusy(null)
    }
  }

  const uploadAccentFile = async (it: SfxItem, file: File) => {
    setWorking(`acc-${it.card_index}`)
    try {
      const d = await api.uploadSfx(projectId, [it.card_index], file, 'accent')
      setState((s) => ({ ...s, items: d.items }))
      toast.success('Файл подключён как акцент')
    } catch (e) {
      toast.error(`Загрузка: ${(e as Error).message}`)
    } finally {
      setWorking(null)
    }
  }

  const removeAccent = async (it: SfxItem) => {
    setAccentEdits((m) => { const c = { ...m }; delete c[it.card_index]; return c })
    try {
      const d = await api.setSfxQuery(projectId, [it.card_index], '', 'accent')
      setState((s) => ({ ...s, items: d.items }))
    } catch (e) {
      toast.error(`Акцент: ${(e as Error).message}`)
    }
  }

  // Adding sound to a specific shot: the planner now leaves most cards silent
  // on purpose, so filling a gap by hand has to be a first-class action.
  const addSoundTo = async () => {
    const shot = parseInt(addCard, 10)
    const item = state.items.find((entry) => entry.shot_index === shot)
    if (!item) { toast.error(`Кадр #${addCard} не найден`); return }
    const layer = addingTo === 'ambience' ? undefined : 'accent'
    try {
      const d = await api.setSfxQuery(projectId, [item.card_index], 'new sound', layer)
      setState((s) => ({ ...s, items: d.items }))
      if (addingTo === 'accent') setAccentEdits((m) => ({ ...m, [item.card_index]: 'new sound' }))
      else setEdits((m) => ({ ...m, [`g${item.card_index}`]: 'new sound' }))
      toast.info(`Кадр #${shot}: впишите описание звука по-английски`)
      setAddingTo(null)
      setAddCard('')
    } catch (e) {
      toast.error(`Добавление: ${(e as Error).message}`)
    }
  }

  const pauseOthers = (current: HTMLAudioElement) => {
    document.querySelectorAll('audio').forEach((el) => {
      if (el !== current) (el as HTMLAudioElement).pause()
    })
  }

  const percent = progress ? Math.round((progress.done / Math.max(1, progress.total)) * 100) : 0

  return (
    <div className="p-8 space-y-6 max-w-[1400px]">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Звуки</h1>
          <p className="mt-1 text-sm text-slate-500">
            Точечный звук: короткие акценты в ключевых моментах и редкий фон там, где среда сама по себе важна.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {state.woosh_online
            ? <span className="rounded bg-emerald-600/20 px-2.5 py-1 text-xs text-emerald-300">Сервер звуков доступен</span>
            : <span className="rounded bg-amber-600/20 px-2.5 py-1 text-xs text-amber-300">Сервер звуков не запущен</span>}
        </div>
      </div>

      {lockedElsewhere && (
        <div className="rounded-xl border border-sky-700/50 bg-sky-900/30 p-4 text-sm text-sky-300">
          Идёт другая задача ({busy}). Дождитесь завершения.
        </div>
      )}

      {/* Layer controls */}
      <div className="space-y-4 rounded-xl border border-slate-800 bg-slate-900 p-5">
        <div className="flex flex-wrap items-center gap-6">
          <label className="flex cursor-pointer items-center gap-3">
            <input
              type="checkbox" checked={state.sfx_enabled}
              onChange={(e) => saveGlobal({ sfx_enabled: e.target.checked })}
              className="h-5 w-5 cursor-pointer accent-indigo-500"
            />
            <span className="text-sm text-slate-200">Добавлять звуковой слой в экспорт</span>
          </label>
          <div className="flex min-w-[260px] flex-1 items-center gap-3">
            <span className="whitespace-nowrap text-sm text-slate-200">Громкость слоя</span>
            <input
              type="range" min={-40} max={-10} step={1} value={state.sfx_volume_db}
              onChange={(e) => setState((s) => ({ ...s, sfx_volume_db: Number(e.target.value) }))}
              onMouseUp={() => saveGlobal({ sfx_volume_db: state.sfx_volume_db })}
              className="flex-1 cursor-pointer accent-indigo-500"
            />
            <span className="w-14 text-sm text-slate-400">{Math.round(state.sfx_volume_db)} dB</span>
          </div>
          {working === 'all' ? (
            <button
              onClick={() => { cancelRef.current = true; setCancelRequested(true) }}
              disabled={cancelRequested}
              className="cursor-pointer rounded-lg border border-rose-800 bg-rose-950 px-4 py-2 text-sm font-bold text-rose-200 hover:bg-rose-900 disabled:opacity-50"
            >
              {cancelRequested ? 'Останавливаю…' : 'Остановить'}
            </button>
          ) : (
            <button
              onClick={generateAllMissing}
              disabled={Boolean(working) || lockedElsewhere || !canGenerate || missingTotal === 0}
              title={!state.sfx_enabled ? 'Сначала включите звуковой слой' : !state.woosh_online ? 'Сервер звуков не запущен' : ''}
              className="cursor-pointer rounded-lg bg-indigo-600 px-4 py-2 text-sm font-bold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50"
            >
              Создать недостающие ({missingTotal})
            </button>
          )}
        </div>

        {progress && (
          <div role="status" aria-live="polite" className="rounded-lg border border-teal-800/60 bg-teal-950/40 px-3 py-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="truncate font-semibold text-teal-200">{progress.done} из {progress.total} · {progress.note}</span>
              <span className="font-mono text-xs text-slate-400">{percent}%</span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800">
              <div className="h-full rounded-full bg-teal-500 transition-[width]" style={{ width: `${percent}%` }} />
            </div>
            {cancelRequested && <p className="mt-1 text-xs text-rose-300">Остановка после текущего звука…</p>}
          </div>
        )}

        {!state.sfx_enabled && (
          <p className="rounded-lg border border-slate-700 bg-slate-950/50 p-3 text-xs leading-relaxed text-slate-400">
            Звуковой слой выключен — экспорт соберётся без него, и это нормальный режим по умолчанию.
            Включите галочку выше, если хотите добавить в ролик акценты и атмосферу.
          </p>
        )}

        {state.sfx_enabled && !state.woosh_online && (
          <p className="rounded-lg border border-amber-800/60 bg-amber-950/25 p-3 text-xs leading-relaxed text-amber-200">
            Слой включён, но генератор звука не отвечает — запустите <span className="font-mono">start_woosh.bat</span> (или
            <span className="font-mono"> start_moss_sfx.bat</span>) и обновите страницу.
            Экспорт при этом не заблокируется: он просто соберётся с тем звуком, который уже есть.
          </p>
        )}

        {musicCoversAll && (
          <p className="rounded-lg border border-indigo-800/60 bg-indigo-950/25 p-3 text-xs leading-relaxed text-indigo-200">
            Музыка играет весь ролик. Под ней ровная фоновая атмосфера почти не слышна — пробиваются только
            короткие звуки. Опирайтесь на акценты, а фон добавляйте лишь там, где среда действительно важна.
          </p>
        )}
      </div>

      {state.items.length === 0 ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900 p-8 text-center text-sm text-slate-600">
          Пока нет кадров. Соберите план кадров на шаге «Кадры» — вместе с ним появятся и звуковые запросы.
        </div>
      ) : (
        <>
          {/* Accents: the layer the viewer actually hears */}
          <section className="rounded-xl border border-slate-800 bg-slate-900">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-5 py-3">
              <div>
                <h2 className="text-base font-semibold text-white">Звуковые моменты</h2>
                <p className="mt-0.5 text-xs text-slate-500">Короткие события в начале кадра — играют один раз поверх всего</p>
              </div>
              <div className="flex items-center gap-2">
                <span className="rounded-full bg-slate-800 px-2.5 py-1 text-xs text-slate-400">{accents.length}</span>
                <button
                  onClick={() => setAddingTo(addingTo === 'accent' ? null : 'accent')}
                  className="cursor-pointer rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-700"
                >
                  ＋ добавить к кадру
                </button>
              </div>
            </div>

            {addingTo === 'accent' && (
              <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 bg-slate-950/40 px-5 py-3">
                <span className="text-xs text-slate-400">Номер кадра:</span>
                <input
                  value={addCard} onChange={(e) => setAddCard(e.target.value)} inputMode="numeric"
                  onKeyDown={(e) => { if (e.key === 'Enter') addSoundTo() }}
                  className="w-24 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-white outline-none focus:border-indigo-500"
                />
                <button onClick={addSoundTo} disabled={!addCard.trim()} className="cursor-pointer rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-45">Добавить</button>
                <button onClick={() => { setAddingTo(null); setAddCard('') }} className="cursor-pointer rounded-lg px-3 py-1.5 text-xs text-slate-400 hover:text-white">Отмена</button>
              </div>
            )}

            {accents.length === 0 ? (
              <div className="px-5 py-8 text-center text-sm text-slate-600">
                Звуковых моментов нет. Планировщик добавляет их только там, где в кадре есть конкретное действие
                со своим звуком — можно добавить вручную кнопкой выше.
              </div>
            ) : (
              <div className="divide-y divide-slate-800/70">
                {accents.map((it) => {
                  const accWorking = working === `acc-${it.card_index}`
                  const accKey = `acc-${it.card_index}`
                  const value = accentEdits[it.card_index] ?? it.sfx_accent_query
                  return (
                    <div key={accKey} className="flex flex-wrap items-start gap-4 px-5 py-3">
                      <div className="w-40 shrink-0 pt-1.5">
                        <span className="rounded bg-fuchsia-600/15 px-2 py-0.5 text-xs text-fuchsia-300/90">кадр {it.shot_index}</span>
                        <div className="mt-1 font-mono text-xs text-slate-600">{it.start}</div>
                      </div>
                      <div className="min-w-[240px] flex-1">
                        <input
                          value={value}
                          placeholder="звук-событие по-английски: phone vibration buzz, door knock…"
                          onChange={(e) => setAccentEdits((m) => ({ ...m, [it.card_index]: e.target.value }))}
                          onBlur={() => saveAccentQuery(it)}
                          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                          className="w-full rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-1.5 font-mono text-sm text-slate-200 outline-none focus:border-fuchsia-500"
                        />
                        {it.accent_url && (
                          <audio controls src={it.accent_url} className="mt-2 h-8 w-full" onPlay={(e) => pauseOthers(e.currentTarget)} />
                        )}
                        {!it.accent_url && <div className="mt-1.5 text-xs text-amber-400/70">звук ещё не создан</div>}
                      </div>
                      <div className="flex w-40 shrink-0 flex-col gap-1.5">
                        <button
                          onClick={() => generateAccent(it, Boolean(it.accent_url))}
                          disabled={accWorking || Boolean(working) || lockedElsewhere || !it.sfx_accent_query || !canGenerate}
                          className="cursor-pointer rounded bg-fuchsia-700/70 px-3 py-1.5 text-xs text-white hover:bg-fuchsia-600 disabled:opacity-40"
                        >
                          {accWorking ? 'Создаю…' : it.accent_url ? 'Создать заново' : 'Создать'}
                        </button>
                        <button onClick={() => fileInputs.current[accKey]?.click()} disabled={accWorking || Boolean(working)} className="cursor-pointer rounded bg-slate-800 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-40">
                          Свой файл…
                        </button>
                        <input
                          ref={(el) => { fileInputs.current[accKey] = el }}
                          type="file" accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.opus" className="hidden"
                          onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadAccentFile(it, f); e.target.value = '' }}
                        />
                        <button onClick={() => removeAccent(it)} disabled={accWorking || Boolean(working)} className="cursor-pointer rounded px-3 py-1.5 text-xs text-slate-500 hover:text-red-400 disabled:opacity-40">
                          Убрать
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </section>

          {/* Ambience: rare, deliberate, and capped by the planner */}
          <section className="rounded-xl border border-slate-800 bg-slate-900">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-5 py-3">
              <div>
                <h2 className="text-base font-semibold text-white">Фоновая атмосфера</h2>
                <p className="mt-0.5 text-xs text-slate-500">Непрерывная среда сцены — только там, где она сама по себе несёт смысл</p>
              </div>
              <div className="flex items-center gap-2">
                <span className="rounded-full bg-slate-800 px-2.5 py-1 text-xs text-slate-400">{ambience.length}</span>
                <button
                  onClick={() => setAddingTo(addingTo === 'ambience' ? null : 'ambience')}
                  className="cursor-pointer rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-700"
                >
                  ＋ добавить к кадру
                </button>
              </div>
            </div>

            {addingTo === 'ambience' && (
              <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 bg-slate-950/40 px-5 py-3">
                <span className="text-xs text-slate-400">Номер кадра:</span>
                <input
                  value={addCard} onChange={(e) => setAddCard(e.target.value)} inputMode="numeric"
                  onKeyDown={(e) => { if (e.key === 'Enter') addSoundTo() }}
                  className="w-24 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-white outline-none focus:border-indigo-500"
                />
                <button onClick={addSoundTo} disabled={!addCard.trim()} className="cursor-pointer rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-45">Добавить</button>
                <button onClick={() => { setAddingTo(null); setAddCard('') }} className="cursor-pointer rounded-lg px-3 py-1.5 text-xs text-slate-400 hover:text-white">Отмена</button>
              </div>
            )}

            {ambience.length === 0 ? (
              <div className="px-5 py-8 text-center text-sm text-slate-600">
                Фоновой атмосферы нет — и чаще всего это правильно: под голосом и музыкой она не слышна.
                Добавьте её для сцены с выраженной средой: шторм, море, оживлённая улица, цех.
              </div>
            ) : (
              <div className="divide-y divide-slate-800/70">
                {ambience.map((g) => {
                  const url = groupUrl(g)
                  const editing = edits[g.key] ?? g.query
                  const isWorking = working === g.key
                  const first = g.items[0]
                  const last = g.items[g.items.length - 1]
                  const shots = g.items.length === 1
                    ? `кадр ${first.shot_index}`
                    : `кадры ${first.shot_index}–${last.shot_index}`
                  return (
                    <div key={g.key} className="flex flex-wrap items-start gap-4 px-5 py-3">
                      <div className="w-40 shrink-0">
                        <div className="text-sm text-slate-200">{shots}</div>
                        <div className="font-mono text-xs text-slate-600">{first.start} – {last.end}</div>
                        <div className="truncate text-xs text-slate-600" title={first.scene_title}>{first.scene_title}</div>
                      </div>
                      <div className="min-w-[240px] flex-1">
                        <input
                          value={editing}
                          placeholder="фоновый звук по-английски: server room fan hum, rain on window…"
                          onChange={(e) => setEdits((m) => ({ ...m, [g.key]: e.target.value }))}
                          onBlur={() => saveQuery(g)}
                          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                          className="w-full rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-2 font-mono text-sm text-slate-200 outline-none focus:border-indigo-500"
                        />
                        {url && <audio controls src={url} className="mt-2 h-9 w-full" onPlay={(e) => pauseOthers(e.currentTarget)} />}
                        {!url && <div className="mt-2 text-xs text-amber-400/80">звук ещё не создан</div>}
                      </div>
                      <div className="flex w-40 shrink-0 flex-col gap-1.5">
                        <button
                          onClick={() => generate(g, Boolean(url))}
                          disabled={isWorking || Boolean(working) || lockedElsewhere || !g.query || !canGenerate}
                          className="cursor-pointer rounded bg-indigo-600/80 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-40"
                        >
                          {isWorking ? 'Создаю…' : url ? 'Создать заново' : 'Создать'}
                        </button>
                        <button onClick={() => fileInputs.current[g.key]?.click()} disabled={isWorking || Boolean(working)} className="cursor-pointer rounded bg-slate-800 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-40">
                          Свой файл…
                        </button>
                        <input
                          ref={(el) => { fileInputs.current[g.key] = el }}
                          type="file" accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.opus" className="hidden"
                          onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadFile(g, f); e.target.value = '' }}
                        />
                        <button onClick={() => disableGroup(g)} disabled={isWorking || Boolean(working)} className="cursor-pointer rounded px-3 py-1.5 text-xs text-slate-500 hover:text-red-400 disabled:opacity-40">
                          Убрать
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </section>

          {silentCards > 0 && (
            <p className="px-1 text-xs text-slate-600">
              Остальные {silentCards} кадр(ов) идут без собственного звука — под голосом и музыкой это нормально,
              тишину закрывает ровный комнатный тон.
            </p>
          )}
        </>
      )}
    </div>
  )
}
