import { useEffect, useMemo, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import type { AssetCard } from '../api/types'

interface Context {
  projectId: string
}

const STATUSES = ['all', 'planned', 'image_ready', 'generating', 'generated', 'needs_review', 'approved', 'rejected', 'skipped']
const ROUTES = ['all', 'image+video', 'remotion', 'graphic']

const STATUS_COLORS: Record<string, string> = {
  planned: '#64748b',
  image_ready: '#8b5cf6',
  generating: '#0ea5e9',
  generated: '#16a34a',
  needs_review: '#f59e0b',
  approved: '#22c55e',
  rejected: '#ef4444',
  skipped: '#64748b',
}

function mediaUrl(path: string, v: number = 0) {
  return `/api/media/${encodeURIComponent(path)}${v ? `?v=${v}` : ''}`
}

function bestMedia(card: AssetCard) {
  if (card.video_output) return { type: 'video', path: card.video_output }
  if (card.remotion_output) return { type: 'video', path: card.remotion_output }
  if (card.image_output) return { type: 'image', path: card.image_output }
  return null
}

export default function AssetBoard() {
  const { projectId } = useOutletContext<Context>()
  const [cards, setCards] = useState<AssetCard[]>([])
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [filterStatus, setFilterStatus] = useState('all')
  const [filterRoute, setFilterRoute] = useState('all')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [mediaVersion, setMediaVersion] = useState(0)

  const load = async () => {
    const board = await api.getAssetBoard(projectId).catch(() => [] as AssetCard[])
    setCards(board)
    setSelectedIndex((current) => {
      if (!board.length) return null
      if (current === null || current >= board.length) return 0
      return current
    })
  }

  useEffect(() => {
    load()
  }, [projectId])

  const visibleIndexes = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return cards
      .map((card, index) => ({ card, index }))
      .filter(({ card }) => filterStatus === 'all' || card.status === filterStatus)
      .filter(({ card }) => filterRoute === 'all' || card.route === filterRoute)
      .filter(({ card }) => {
        if (!needle) return true
        return [card.description, card.image_prompt, card.video_prompt, card.screen_text, card.scene_title]
          .some((value) => value.toLowerCase().includes(needle))
      })
      .map(({ index }) => index)
  }, [cards, filterRoute, filterStatus, query])

  const selected = selectedIndex === null ? null : cards[selectedIndex]
  const checkedIndexes = [...checked].filter((index) => index >= 0 && index < cards.length)

  const counts = useMemo(() => ({
    total: cards.length,
    generated: cards.filter((c) => c.status === 'generated' || c.status === 'approved' || c.approved).length,
    approved: cards.filter((c) => c.approved || c.status === 'approved').length,
    review: cards.filter((c) => c.status === 'needs_review' || c.status === 'rejected').length,
    media: cards.filter((c) => c.image_output || c.video_output || c.remotion_output).length,
  }), [cards])

  const updateCardAt = (index: number, card: AssetCard) => {
    setCards((prev) => {
      const next = [...prev]
      next[index] = card
      return next
    })
  }

  const patchCardAt = (index: number, patch: Partial<AssetCard>) => {
    setCards((prev) => {
      if (index < 0 || index >= prev.length) return prev
      const next = [...prev]
      next[index] = { ...next[index], ...patch }
      return next
    })
  }

  const saveSelected = async () => {
    if (selectedIndex === null || !selected) return
    setBusy('save')
    try {
      const updated = await api.updateCard(projectId, selectedIndex, selected)
      updateCardAt(selectedIndex, updated)
    } finally {
      setBusy(null)
    }
  }

  const patchCard = async (index: number, patch: Partial<AssetCard>) => {
    const updated = await api.updateCard(projectId, index, patch)
    updateCardAt(index, updated)
  }

  const approve = async (index: number) => {
    setBusy(`approve-${index}`)
    try {
      await patchCard(index, { approved: true, status: 'approved' })
    } finally {
      setBusy(null)
    }
  }

  const generate = async (index: number, mode: 'route' | 'image' | 'video') => {
    setBusy(`generate-${index}`)
    try {
      const result = await api.generateCard(projectId, index, mode)
      if (result.card) {
        updateCardAt(index, result.card)
        setMediaVersion((v) => v + 1)
      }
    } catch (e) {
      alert((e as Error).message)
      await load()
    } finally {
      setBusy(null)
    }
  }

  const regeneratePrompt = async (index: number, target: 'image_prompt' | 'video_prompt') => {
    setBusy(`prompt-${target}-${index}`)
    try {
      const result = await api.regeneratePrompt(projectId, index, target)
      if (result.card) updateCardAt(index, result.card)
      patchCardAt(index, { [target]: result.prompt })
    } catch (e) {
      alert((e as Error).message)
      await load()
    } finally {
      setBusy(null)
    }
  }

  const batchGenerate = async (mode: 'route' | 'image' | 'video') => {
    const candidates = checkedIndexes.length ? checkedIndexes : visibleIndexes
    const targets = candidates.filter((index) => {
      const card = cards[index]
      if (card.status === 'approved') return false
      if (mode === 'image') return !card.image_output
      if (mode === 'video') return !card.video_output
      return true
    })
    for (const index of targets) {
      await generate(index, mode)
    }
  }

  const batchApprove = async () => {
    const targets = checkedIndexes.length ? checkedIndexes : visibleIndexes
    setBusy('batch-approve')
    try {
      for (const index of targets) {
        if (cards[index].status === 'generated' || cards[index].image_output || cards[index].video_output || cards[index].remotion_output) {
          const updated = await api.updateCard(projectId, index, { approved: true, status: 'approved' })
          updateCardAt(index, updated)
        }
      }
    } finally {
      setBusy(null)
    }
  }

  const toggleVisible = () => {
    setChecked((prev) => {
      const next = new Set(prev)
      const allVisibleChecked = visibleIndexes.every((index) => next.has(index))
      for (const index of visibleIndexes) {
        if (allVisibleChecked) next.delete(index)
        else next.add(index)
      }
      return next
    })
  }

  return (
    <div className="p-6 space-y-5 max-w-[1700px]">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-white">Asset Board</h1>
          <p className="text-sm text-slate-500 mt-1">{counts.media}/{counts.total} с файлами, {counts.approved} утверждено, {counts.review} на проверке</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={() => batchGenerate('route')} disabled={Boolean(busy)} className="px-3 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45">
            Generate route
          </button>
          <button onClick={() => batchGenerate('image')} disabled={Boolean(busy)} className="px-3 py-2 rounded-lg bg-slate-800 text-slate-100 text-sm font-semibold border border-slate-700 disabled:opacity-45">
            Image
          </button>
          <button onClick={() => batchGenerate('video')} disabled={Boolean(busy)} className="px-3 py-2 rounded-lg bg-slate-800 text-slate-100 text-sm font-semibold border border-slate-700 disabled:opacity-45">
            Video
          </button>
          <button onClick={batchApprove} disabled={Boolean(busy)} className="px-3 py-2 rounded-lg bg-emerald-700 text-white text-sm font-semibold disabled:opacity-45">
            Approve
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        {[
          ['Всего', counts.total],
          ['Медиа', counts.media],
          ['Готово', counts.generated],
          ['Утверждено', counts.approved],
          ['Проверить', counts.review],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg bg-slate-900 border border-slate-800 p-3">
            <div className="text-2xl font-semibold text-white">{value}</div>
            <div className="text-xs text-slate-500">{label}</div>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3 rounded-lg bg-slate-900 border border-slate-800 p-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Поиск"
          className="w-64 max-w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-sm text-white outline-none focus:border-indigo-500"
        />
        <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)} className="px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-sm text-white">
          {STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}
        </select>
        <select value={filterRoute} onChange={(e) => setFilterRoute(e.target.value)} className="px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-sm text-white">
          {ROUTES.map((route) => <option key={route} value={route}>{route}</option>)}
        </select>
        <button onClick={toggleVisible} className="px-3 py-2 rounded-lg bg-slate-800 border border-slate-700 text-sm text-slate-200">
          {visibleIndexes.every((index) => checked.has(index)) && visibleIndexes.length ? 'Снять выбор' : 'Выбрать видимые'}
        </button>
        <div className="ml-auto text-xs text-slate-500">{checkedIndexes.length} выбрано</div>
      </div>

      <div className="grid grid-cols-1 2xl:grid-cols-[minmax(700px,1fr)_520px] gap-5">
        <section className="rounded-lg bg-slate-900 border border-slate-800 overflow-hidden">
          <div className="grid grid-cols-[42px_74px_82px_minmax(180px,1fr)_116px_116px_150px] gap-2 px-3 py-2 border-b border-slate-800 text-xs text-slate-500">
            <div />
            <div>Shot</div>
            <div>Status</div>
            <div>Prompt</div>
            <div>Route</div>
            <div>Media</div>
            <div className="text-right">Actions</div>
          </div>
          <div className="max-h-[68vh] overflow-y-auto">
            {visibleIndexes.map((index) => {
              const card = cards[index]
              const media = bestMedia(card)
              const color = STATUS_COLORS[card.status] || '#64748b'
              return (
                <button
                  type="button"
                  key={`${card.scene_index}-${card.shot_index}-${index}`}
                  onClick={() => setSelectedIndex(index)}
                  className={`grid w-full grid-cols-[42px_74px_82px_minmax(180px,1fr)_116px_116px_150px] gap-2 px-3 py-3 text-left border-b border-slate-800/70 hover:bg-slate-800/50 ${
                    selectedIndex === index ? 'bg-indigo-950/30' : ''
                  }`}
                >
                  <div onClick={(e) => e.stopPropagation()} className="flex items-center">
                    <input
                      type="checkbox"
                      checked={checked.has(index)}
                      onChange={(e) => {
                        setChecked((prev) => {
                          const next = new Set(prev)
                          if (e.target.checked) next.add(index)
                          else next.delete(index)
                          return next
                        })
                      }}
                      className="h-4 w-4 accent-indigo-500"
                    />
                  </div>
                  <div>
                    <div className="text-sm font-mono text-white">#{card.shot_index}</div>
                    <div className="text-xs text-slate-600">Сц. {card.scene_index}</div>
                  </div>
                  <div>
                    <span className="inline-flex rounded px-2 py-1 text-[11px] font-medium" style={{ backgroundColor: `${color}22`, color }}>{card.status || 'planned'}</span>
                    {card.approved && <div className="text-[11px] text-emerald-400 mt-1">approved</div>}
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm text-slate-200 truncate">{card.description || card.screen_text || card.scene_title || 'Без описания'}</div>
                    <div className="text-xs text-slate-500 truncate mt-0.5">{card.image_prompt || card.video_prompt || card.asset_query}</div>
                  </div>
                  <div className="text-xs text-slate-400 self-center">{card.route || '-'}</div>
                  <div className="text-xs text-slate-400 self-center">{media ? media.type : 'none'}</div>
                  <div onClick={(e) => e.stopPropagation()} className="flex justify-end gap-1">
                    <button onClick={() => generate(index, 'route')} disabled={Boolean(busy)} className="px-2 py-1 rounded bg-indigo-600 text-white text-xs disabled:opacity-45">Route</button>
                    <button onClick={() => approve(index)} disabled={Boolean(busy)} className="px-2 py-1 rounded bg-emerald-700 text-white text-xs disabled:opacity-45">OK</button>
                  </div>
                </button>
              )
            })}
            {!visibleIndexes.length && <div className="p-10 text-center text-sm text-slate-600">Нет карточек под выбранные фильтры.</div>}
          </div>
        </section>

        <aside className="rounded-lg bg-slate-900 border border-slate-800 p-4 space-y-4 h-fit 2xl:sticky 2xl:top-6">
          {selected ? (
            <>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="text-lg font-semibold text-white">Shot {selected.shot_index}</h2>
                  <p className="text-xs text-slate-500">Scene {selected.scene_index} / {selected.start} - {selected.end}</p>
                </div>
                <span className="rounded px-2 py-1 text-xs" style={{ backgroundColor: `${STATUS_COLORS[selected.status] || '#64748b'}22`, color: STATUS_COLORS[selected.status] || '#94a3b8' }}>{selected.status}</span>
              </div>

              <div className="aspect-video rounded-lg bg-slate-950 border border-slate-800 overflow-hidden flex items-center justify-center">
                {bestMedia(selected)?.type === 'video' && (
                  <video key={`${bestMedia(selected)!.path}-${mediaVersion}`} src={mediaUrl(bestMedia(selected)!.path, mediaVersion)} poster={selected.image_output ? mediaUrl(selected.image_output, mediaVersion) : undefined} controls className="h-full w-full object-contain" />
                )}
                {bestMedia(selected)?.type === 'image' && (
                  <img key={`${bestMedia(selected)!.path}-${mediaVersion}`} src={mediaUrl(bestMedia(selected)!.path, mediaVersion)} alt="" className="h-full w-full object-contain" />
                )}
                {!bestMedia(selected) && <div className="text-sm text-slate-700">Файл еще не создан</div>}
              </div>

              <div className="grid grid-cols-3 gap-2">
                <button onClick={() => selectedIndex !== null && generate(selectedIndex, 'route')} disabled={Boolean(busy)} className="py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45">Route</button>
                <button onClick={() => selectedIndex !== null && generate(selectedIndex, 'image')} disabled={Boolean(busy)} className="py-2 rounded-lg bg-slate-800 text-slate-100 text-sm font-semibold border border-slate-700 disabled:opacity-45">Image</button>
                <button onClick={() => selectedIndex !== null && generate(selectedIndex, 'video')} disabled={Boolean(busy)} className="py-2 rounded-lg bg-slate-800 text-slate-100 text-sm font-semibold border border-slate-700 disabled:opacity-45">Video</button>
              </div>

              <div className="space-y-3">
                <label className="block">
                  <span className="text-xs text-slate-500">Description</span>
                  <textarea value={selected.description} onChange={(e) => updateCardAt(selectedIndex!, { ...selected, description: e.target.value })} className="mt-1 h-16 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500" />
                </label>
                <label className="block">
                  <span className="mb-1 flex items-center justify-between gap-3 text-xs text-slate-500">
                    <span>Image prompt</span>
                    <button type="button" onClick={() => selectedIndex !== null && regeneratePrompt(selectedIndex, 'image_prompt')} disabled={Boolean(busy)} className="rounded bg-slate-800 px-2 py-1 text-[11px] font-semibold text-slate-200 border border-slate-700 disabled:opacity-45">Improve</button>
                  </span>
                  <textarea value={selected.image_prompt} onChange={(e) => updateCardAt(selectedIndex!, { ...selected, image_prompt: e.target.value })} className="h-24 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500" />
                </label>
                <label className="block">
                  <span className="mb-1 flex items-center justify-between gap-3 text-xs text-slate-500">
                    <span>Video prompt</span>
                    <button type="button" onClick={() => selectedIndex !== null && regeneratePrompt(selectedIndex, 'video_prompt')} disabled={Boolean(busy)} className="rounded bg-slate-800 px-2 py-1 text-[11px] font-semibold text-slate-200 border border-slate-700 disabled:opacity-45">Improve</button>
                  </span>
                  <textarea value={selected.video_prompt} onChange={(e) => updateCardAt(selectedIndex!, { ...selected, video_prompt: e.target.value })} className="h-24 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500" />
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <label className="block">
                    <span className="text-xs text-slate-500">Start</span>
                    <input value={selected.start} onChange={(e) => updateCardAt(selectedIndex!, { ...selected, start: e.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white font-mono outline-none focus:border-indigo-500" />
                  </label>
                  <label className="block">
                    <span className="text-xs text-slate-500">End</span>
                    <input value={selected.end} onChange={(e) => updateCardAt(selectedIndex!, { ...selected, end: e.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white font-mono outline-none focus:border-indigo-500" />
                  </label>
                </div>
                <label className="block">
                  <span className="text-xs text-slate-500">Notes</span>
                  <textarea value={selected.notes} onChange={(e) => updateCardAt(selectedIndex!, { ...selected, notes: e.target.value })} className="mt-1 h-16 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500" />
                </label>
              </div>

              <div className="flex gap-2">
                <button onClick={saveSelected} disabled={Boolean(busy)} className="flex-1 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45">Сохранить</button>
                <button onClick={() => selectedIndex !== null && approve(selectedIndex)} disabled={Boolean(busy)} className="px-4 py-2 rounded-lg bg-emerald-700 text-white text-sm font-semibold disabled:opacity-45">Approve</button>
              </div>
            </>
          ) : (
            <div className="py-20 text-center text-sm text-slate-600">Выберите карточку.</div>
          )}
        </aside>
      </div>
    </div>
  )
}
