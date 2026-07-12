import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useOutletContext, useSearchParams } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import { hasMedia, isExcluded, isIncluded } from '../lib/cards'
import type { AssetCard, AssetVariant, AssetVariantHistory } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

const STATUSES = ['all', 'planned', 'image_ready', 'generating', 'generated', 'needs_review', 'approved', 'rejected', 'skipped']
const ROUTES = ['all', 'image+video', 'image', 'stock', 'kenburns', 'reference']

// Manually-selectable routes in the card editor (no "all" pseudo-filter).
const CARD_ROUTES = ['image+video', 'image', 'stock', 'kenburns', 'reference']
const ROUTE_LABELS: Record<string, string> = {
  'image+video': 'Реалистичный (картинка→видео)',
  image: 'Реалистичная картинка',
  stock: 'Сток-видео',
  kenburns: 'Картинка + зум',
  reference: 'Источник (Википедия / arXiv)',
}

function routePatch(route: string): Partial<AssetCard> {
  if (route === 'stock') return { route, visual_type: 'stock_video', image_model: '', video_model: '' }
  if (route === 'kenburns') return { route, visual_type: 'still_kenburns', video_model: '' }
  if (route === 'image') return { route, visual_type: 'ai_image', video_model: '' }
  if (route === 'reference') return { route, visual_type: 'web_reference_card', image_model: '', video_model: '' }
  return { route: 'image+video', visual_type: 'ai_image_then_i2v', video_model: 'ltx2.3_i2v' }
}

const TEXT_STYLES = [
  ['subtitle', 'Subtitle'],
  ['caption', 'Caption'],
  ['title', 'Title'],
  ['stat', 'Stat'],
  ['terminal', 'Terminal'],
  ['badge', 'Badge'],
  ['warning', 'Warning'],
  ['label', 'Label'],
  ['quote', 'Quote'],
]

const TEXT_POSITIONS = [
  ['bottom', 'Bottom'],
  ['top', 'Top'],
  ['center', 'Center'],
  ['top_left', 'Top left'],
  ['top_right', 'Top right'],
  ['bottom_left', 'Bottom left'],
  ['bottom_right', 'Bottom right'],
  ['left', 'Left'],
  ['right', 'Right'],
  ['manual', 'Manual'],
]

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

function safeExternalUrl(value?: string) {
  if (!value) return ''
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : ''
  } catch {
    return ''
  }
}

function bestMedia(card: AssetCard) {
  if (card.video_output) return { type: 'video', path: card.video_output }
  if (card.image_output) return { type: 'image', path: card.image_output }
  return null
}

const VARIANT_FIELD_LABELS: Record<string, string> = {
  image_output: 'Image',
  video_output: 'Video',
  reference_output: 'Reference',
  remotion_output: 'Graphic',
  assigned_media: 'Assigned',
}

function variantFileName(path: string) {
  return path.split(/[\\/]/).pop() || path
}

function variantSize(bytes: number) {
  if (!bytes) return '0 B'
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export default function AssetBoard() {
  const { projectId, busy: projectBusy, setBusy } = useOutletContext<Context>()
  const toast = useToast()
  const [cards, setCards] = useState<AssetCard[]>([])
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [filterStatus, setFilterStatus] = useState('all')
  const [filterRoute, setFilterRoute] = useState('all')
  const [query, setQuery] = useState('')
  const [localBusy, setLocalBusyState] = useState<string | null>(null)
  const [mediaVersion, setMediaVersion] = useState(0)
  const [stockProgress, setStockProgress] = useState<{ label: string; done: number; total: number } | null>(null)
  const [cancelRequested, setCancelRequested] = useState(false)
  const [variantHistory, setVariantHistory] = useState<AssetVariantHistory | null>(null)
  const [variantsLoading, setVariantsLoading] = useState(false)
  const [selectingVariant, setSelectingVariant] = useState<string | null>(null)
  const cancelBatchRef = useRef(false)
  const variantRequestRef = useRef(0)
  const busy = localBusy || projectBusy
  const setLocalBusy = (value: string | null) => {
    setLocalBusyState(value)
    setBusy(value)
  }
  // Generation/render is blocked while a pipeline step runs on another page.

  const load = useCallback(async () => {
    const board = await api.getAssetBoard(projectId).catch((e) => {
      toast.error(`Не удалось загрузить доску: ${(e as Error).message}`)
      return [] as AssetCard[]
    })
    setCards(board)
    setSelectedIndex((current) => {
      if (!board.length) return null
      if (current === null || current >= board.length) return 0
      return current
    })
  }, [projectId, toast])

  useEffect(() => {
    load()
  }, [load])

  const loadVariants = useCallback(async (cardIndex: number) => {
    const requestId = ++variantRequestRef.current
    setVariantsLoading(true)
    try {
      const history = await api.getAssetVariants(projectId, cardIndex)
      if (requestId === variantRequestRef.current) setVariantHistory(history)
    } catch {
      if (requestId === variantRequestRef.current) setVariantHistory(null)
    } finally {
      if (requestId === variantRequestRef.current) setVariantsLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (selectedIndex === null) {
      setVariantHistory(null)
      return
    }
    void loadVariants(selectedIndex)
  }, [loadVariants, selectedIndex])

  // Deep link from the Preview QC list: /board?card=N opens that card.
  const [searchParams] = useSearchParams()
  useEffect(() => {
    const c = searchParams.get('card')
    if (c === null || !cards.length) return
    const n = parseInt(c, 10)
    if (!isNaN(n) && n >= 0 && n < cards.length) setSelectedIndex(n)
  }, [searchParams, cards.length])

  // Auto-refresh while any card is still generating, so renders that finished
  // on the server (e.g. while you were on another tab) get picked up here.
  const anyGenerating = useMemo(
    () => cards.some((c) => (c.status || '').toLowerCase() === 'generating'),
    [cards],
  )
  useEffect(() => {
    if (!anyGenerating) return
    const id = setInterval(() => {
      load()
      setMediaVersion((v) => v + 1)
    }, 4000)
    return () => clearInterval(id)
  }, [anyGenerating, load])

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
    media: cards.filter(hasMedia).length,
    included: cards.filter(isIncluded).length,
    excluded: cards.filter(isExcluded).length,
    review: cards.filter((c) => c.status === 'needs_review').length,
  }), [cards])
  const isBatchBusy = Boolean(busy?.startsWith('batch-'))

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
    setLocalBusy('save')
    try {
      const updated = await api.updateCard(projectId, selectedIndex, selected)
      updateCardAt(selectedIndex, updated)
      toast.success('Карточка сохранена')
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setLocalBusy(null)
    }
  }

  const activateVariant = async (variant: AssetVariant) => {
    if (selectedIndex === null || variant.selected || busy) return
    setSelectingVariant(variant.id)
    try {
      const result = await api.selectAssetVariant(projectId, selectedIndex, variant.id)
      updateCardAt(selectedIndex, result.card)
      setMediaVersion((value) => value + 1)
      await loadVariants(selectedIndex)
      toast.success(`${VARIANT_FIELD_LABELS[variant.output_field] || variant.output_field}: версия выбрана`)
    } catch (error) {
      toast.error(`Не удалось выбрать версию: ${(error as Error).message}`)
    } finally {
      setSelectingVariant(null)
    }
  }

  const patchCard = async (index: number, patch: Partial<AssetCard>) => {
    const updated = await api.updateCard(projectId, index, patch)
    updateCardAt(index, updated)
  }

  const saveCardForAction = async (index: number): Promise<boolean> => {
    const card = cards[index]
    if (!card) return false
    try {
      const updated = await api.updateCard(projectId, index, card)
      updateCardAt(index, updated)
      return true
    } catch (e) {
      toast.error(`Не удалось сохранить карточку перед запуском: ${(e as Error).message}`)
      return false
    }
  }

  // Toggle a card in/out of the final video (replaces the old approve gate).
  const toggleExclude = async (index: number) => {
    const card = cards[index]
    setLocalBusy(`exclude-${index}`)
    try {
      if (isExcluded(card)) {
        await patchCard(index, { status: hasMedia(card) ? 'generated' : 'planned' })
      } else {
        await patchCard(index, { status: 'skipped' })
      }
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setLocalBusy(null)
    }
  }

  const runGenerateCard = async (index: number, mode: 'route' | 'image' | 'video', skipStock = false, forceFull = false): Promise<AssetCard | null> => {
    patchCardAt(index, { status: 'generating' }) // optimistic, so the UI reflects work immediately
    try {
      const result = await api.generateCard(projectId, index, cards[index].card_id, mode, skipStock, forceFull)
      if (result.card) {
        updateCardAt(index, result.card)
        if (selectedIndex === index) void loadVariants(index)
        setMediaVersion((v) => v + 1)
        return result.card
      }
      return null
    } catch (e) {
      toast.error(`Кадр #${cards[index]?.shot_index ?? index}: ${(e as Error).message}`)
      await load()
      return null
    }
  }

  const generate = async (index: number, mode: 'route' | 'image' | 'video', skipStock = false, forceFull = false) => {
    setLocalBusy(`generate-${index}`)
    try {
      if (!(await saveCardForAction(index))) return
      await runGenerateCard(index, mode, skipStock, forceFull)
    } finally {
      setLocalBusy(null)
    }
  }

  // Finalize a drafted clip: re-render its LTX video with the full workflow.
  const finalize = async (index: number) => {
    setLocalBusy(`finalize-${index}`)
    try {
      if (!(await saveCardForAction(index))) return
      await runGenerateCard(index, 'video', false, true)
      toast.success('Кадр отрендерен в финальном качестве')
    } finally {
      setLocalBusy(null)
    }
  }

  // Batch: re-render every drafted clip (checked, or all visible) at full quality.
  const batchFinalize = async () => {
    const candidates = checkedIndexes.length ? checkedIndexes : visibleIndexes
    const targets = candidates.filter((index) => {
      const card = cards[index]
      return !isExcluded(card) && Boolean(card.ltx_draft) && Boolean(card.video_output)
    })
    if (!targets.length) { toast.error('Нет черновых кадров для финализации'); return }
    startBatch('batch-finalize', 'Финал', targets.length)
    let ok = 0
    let fail = 0
    let done = 0
    for (const index of targets) {
      if (cancelBatchRef.current) break
      if (await runGenerateCard(index, 'video', false, true)) ok++
      else fail++
      done++
      setStockProgress({ label: 'Финал', done, total: targets.length })
    }
    const stopped = cancelBatchRef.current
    finishBatch()
    if (stopped) toast.info(`Финал: остановлено после ${done} из ${targets.length}`)
    else toast.success(`Финал: готово ${ok}, ошибок ${fail}`)
  }

  const runPickStock = async (index: number, showToast = true): Promise<AssetCard | null> => {
    patchCardAt(index, { status: 'generating' })
    try {
      const result = await api.resolveStock(projectId, index, cards[index].card_id)
      if (result.ok && result.card) {
        updateCardAt(index, result.card)
        if (selectedIndex === index) void loadVariants(index)
        setMediaVersion((v) => v + 1)
        if (showToast) toast.success(`Сток подобран${result.source ? ` (${result.source})` : ''}`)
        return result.card
      } else {
        await load()
        if (showToast) toast.error(result.message || 'Подходящий сток не найден — можно отправить кадр в AI')
        return null
      }
    } catch (e) {
      if (showToast) toast.error(`Сток #${cards[index]?.shot_index ?? index}: ${(e as Error).message}`)
      await load()
      return null
    }
  }

  const pickStock = async (index: number) => {
    setLocalBusy(`stock-${index}`)
    try {
      if (!(await saveCardForAction(index))) return
      await runPickStock(index)
    } finally {
      setLocalBusy(null)
    }
  }

  const startBatch = (busyKey: string, label: string, total: number) => {
    cancelBatchRef.current = false
    setCancelRequested(false)
    setLocalBusy(busyKey)
    setStockProgress({ label, done: 0, total })
  }

  const finishBatch = () => {
    cancelBatchRef.current = false
    setCancelRequested(false)
    setStockProgress(null)
    setLocalBusy(null)
  }

  const requestCancelBatch = () => {
    cancelBatchRef.current = true
    setCancelRequested(true)
    toast.info('Останавливаю очередь после текущей карточки')
  }

  const regeneratePrompt = async (index: number, target: 'image_prompt' | 'video_prompt') => {
    setLocalBusy(`prompt-${target}-${index}`)
    try {
      const result = await api.regeneratePrompt(projectId, index, cards[index].card_id, target)
      if (result.card) updateCardAt(index, result.card)
      patchCardAt(index, { [target]: result.prompt })
      toast.success('Промпт обновлён')
    } catch (e) {
      toast.error((e as Error).message)
      await load()
    } finally {
      setLocalBusy(null)
    }
  }

  const batchGenerate = async (mode: 'route' | 'image' | 'video') => {
    const candidates = checkedIndexes.length ? checkedIndexes : visibleIndexes
    if (mode !== 'route') {
      const targets = candidates.filter((index) => {
        const card = cards[index]
        if (isExcluded(card)) return false
        if (mode === 'image') return !card.image_output
        return !card.video_output
      })
      if (!targets.length) { toast.error('Нет карточек для генерации'); return }
      const label = mode === 'image' ? 'Генерация картинок' : 'Генерация видео'
      startBatch(`batch-${mode}`, label, targets.length)
      let ok = 0
      let fail = 0
      let done = 0
      for (const index of targets) {
        if (cancelBatchRef.current) break
        if (await runGenerateCard(index, mode)) ok++
        else fail++
        done++
        setStockProgress({ label, done, total: targets.length })
      }
      const stopped = cancelBatchRef.current
      finishBatch()
      if (stopped) toast.info(`${label}: остановлено после ${done} из ${targets.length}`)
      else toast.success(`${label}: готово ${ok}, ошибок ${fail}`)
      return
    }

    // «Собрать» — phased queue grouped by MODEL, so LM Studio / Krea / LTX are
    // each loaded once for the whole batch instead of swapping in VRAM per card:
    //   1) all stock lookups (vision model);  a miss re-routes the card to Ken
    //      Burns and defers it to the image phase — no Krea load mid-phase;
    //   2) all Krea stills (Ken Burns + sources for image-to-video);
    //   3) all Ken Burns zooms (pure ffmpeg, no models);
    //   4) all LTX image-to-video renders.
    // `live` mirrors the freshest card state; React `cards` is stale in-loop.
    const live: Record<number, AssetCard> = {}
    candidates.forEach((i) => { live[i] = cards[i] })
    const eligible = candidates.filter((i) => live[i] && !isExcluded(live[i]) && !live[i].video_output)
    if (!eligible.length) { toast.error('Нет карточек для генерации'); return }
    const vt = (i: number) => (live[i].visual_type || '').toLowerCase()

    startBatch('batch-route', 'Очередь', eligible.length)
    let ok = 0
    let fail = 0
    let stopped = false

    const runPhase = async (label: string, targets: number[], step: (i: number) => Promise<AssetCard | null>) => {
      if (!targets.length) return
      setStockProgress({ label, done: 0, total: targets.length })
      let done = 0
      for (const i of targets) {
        if (cancelBatchRef.current) { stopped = true; return }
        const card = await step(i)
        if (card) { live[i] = card; ok++ } else fail++
        done++
        setStockProgress({ label, done, total: targets.length })
      }
    }

    // 0) Reference inserts (Wikipedia / arXiv screenshots — no VRAM model, so
    //    run them first; a miss re-routes the card to Ken Burns on the backend).
    if (!stopped) {
      await runPhase(
        'Очередь: источники (скрины)',
        eligible.filter((i) => vt(i) === 'web_reference_card' && !live[i].video_output),
        (i) => runGenerateCard(i, 'route'),
      )
    }

    // 1) Stock (vision model stays loaded for the whole run)
    if (!stopped) await runPhase(
      'Очередь 1/4: сток',
      eligible.filter((i) => vt(i) === 'stock_video'),
      async (i) => {
        const card = await runPickStock(i, false)
        if (card) return card
        // No stock found — defer to the Krea phase as a Ken Burns still.
        try {
          const updated = await api.updateCard(projectId, i, {
            visual_type: 'still_kenburns',
            route: 'kenburns',
            notes: `${live[i].notes ? live[i].notes + '\n' : ''}Сток не найден → Ken Burns`,
          })
          updateCardAt(i, updated)
          live[i] = updated
          return updated
        } catch {
          return null
        }
      },
    )

    // 2) All Krea stills in one go (Ken Burns + i2v sources)
    if (!stopped) {
      await runPhase(
        'Очередь 2/4: картинки (Krea)',
        eligible.filter((i) => ['still_kenburns', 'ai_image_then_i2v'].includes(vt(i)) && !live[i].image_output && !live[i].video_output),
        (i) => runGenerateCard(i, 'image'),
      )
    }

    // 3) Ken Burns zooms (ffmpeg only — image already exists, route skips Krea)
    if (!stopped) {
      await runPhase(
        'Очередь 3/4: зумы (ffmpeg)',
        eligible.filter((i) => vt(i) === 'still_kenburns' && live[i].image_output && !live[i].video_output),
        (i) => runGenerateCard(i, 'route'),
      )
    }

    // 4) All LTX renders last (LTX loaded once)
    if (!stopped) {
      await runPhase(
        'Очередь 4/4: видео (LTX)',
        eligible.filter((i) => vt(i) === 'ai_image_then_i2v' && live[i].image_output && !live[i].video_output),
        (i) => runGenerateCard(i, 'video'),
      )
    }

    finishBatch()
    if (stopped) toast.info(`Очередь остановлена (успешно ${ok}, ошибок ${fail})`)
    else toast.success(`Очередь готова: операций ${ok}, ошибок ${fail}`)
  }

  const batchStock = async () => {
    const candidates = checkedIndexes.length ? checkedIndexes : visibleIndexes
    const targets = candidates.filter((index) => {
      const card = cards[index]
      if (isExcluded(card)) return false
      return !card.video_output // skip cards that already have a clip
    })
    if (!targets.length) { toast.error('Нет кадров без видео для подбора стока'); return }
    const label = 'Подбор стока'
    startBatch('batch-stock', label, targets.length)
    let ok = 0
    let fail = 0
    let done = 0
    for (let i = 0; i < targets.length; i++) {
      if (cancelBatchRef.current) break
      const index = targets[i]
      const success = await runPickStock(index, false)
      if (success) ok++
      else fail++
      done++
      setStockProgress({ label, done, total: targets.length })
    }
    setMediaVersion((v) => v + 1)
    const stopped = cancelBatchRef.current
    finishBatch()
    if (stopped) toast.info(`Сток по карточкам: остановлено после ${done} из ${targets.length}`)
    else toast.success(`Сток по карточкам: подобрано ${ok}, без результата ${fail}`)
  }

  // Fill only the gaps: AI-generate the cards that still have no video (where
  // stock found nothing). skip_stock=true forces AI without re-hitting stock, and
  // cards that already have a stock clip are left untouched.
  const batchFillGaps = async () => {
    const candidates = checkedIndexes.length ? checkedIndexes : visibleIndexes
    const targets = candidates.filter((index) => {
      const card = cards[index]
      if (isExcluded(card)) return false
      return !card.video_output // only cards without a video (stock miss)
    })
    if (!targets.length) { toast.error('Нет пустых кадров — везде уже есть видео'); return }
    const label = 'Дополнение AI'
    startBatch('batch-fill', label, targets.length)
    let ok = 0
    let fail = 0
    let done = 0
    for (let i = 0; i < targets.length; i++) {
      if (cancelBatchRef.current) break
      const index = targets[i]
      const success = await runGenerateCard(index, 'route', true)
      if (success) ok++
      else fail++
      done++
      setStockProgress({ label, done, total: targets.length })
    }
    setMediaVersion((v) => v + 1)
    const stopped = cancelBatchRef.current
    finishBatch()
    if (stopped) toast.info(`Дополнение AI: остановлено после ${done} из ${targets.length}`)
    else toast.success(`Дополнение AI: готово ${ok}, ошибок ${fail}`)
  }

  const batchExclude = async () => {
    const targets = checkedIndexes.length ? checkedIndexes : visibleIndexes
    setLocalBusy('batch-exclude')
    try {
      let n = 0
      for (const index of targets) {
        const updated = await api.updateCard(projectId, index, { status: 'skipped' })
        updateCardAt(index, updated)
        n++
      }
      toast.success(`Исключено из финала: ${n}`)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setLocalBusy(null)
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
          <p className="text-sm text-slate-500 mt-1">{counts.included}/{counts.total} в финале, {counts.excluded} исключено, {counts.review} на проверке</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* Primary flow: build by route (skips finished), then fill gaps with AI. */}
          <button onClick={() => batchGenerate('route')} disabled={Boolean(busy)} className="px-3 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45" title="Очередь по фазам: 1) весь сток, 2) все Krea-картинки, 3) все зумы, 4) все LTX-видео — каждая модель грузится в VRAM один раз. Готовые кадры не перезаписываются.">
            Собрать
          </button>
          <button onClick={batchFillGaps} disabled={Boolean(busy)} className="px-3 py-2 rounded-lg bg-fuchsia-700 text-white text-sm font-semibold disabled:opacity-45" title="Догенерировать AI только кадры без видео (где сток не нашёлся); подобранные стоки не трогаются">
            Дополнить AI
          </button>
          <button onClick={batchFinalize} disabled={Boolean(busy)} className="px-3 py-2 rounded-lg bg-amber-600 text-white text-sm font-semibold disabled:opacity-45" title="Перерендерить все черновые AI-кадры в финальном качестве (полный LTX)">
            Финал
          </button>

          <span className="mx-1 h-6 w-px bg-slate-700" aria-hidden="true" />
          <span className="text-xs text-slate-500">вручную:</span>

          <button onClick={batchStock} disabled={Boolean(busy)} className="px-2.5 py-2 rounded-lg bg-slate-800 text-slate-300 text-sm border border-slate-700 disabled:opacity-45" title="Форсировать сток на все пустые кадры (игнорирует маршрут)">
            Все стоки
          </button>
          <button onClick={() => batchGenerate('image')} disabled={Boolean(busy)} className="px-2.5 py-2 rounded-lg bg-slate-800 text-slate-300 text-sm border border-slate-700 disabled:opacity-45" title="Сгенерировать только Krea-картинку (без видео, мимо маршрута)">
            Image
          </button>
          <button onClick={() => batchGenerate('video')} disabled={Boolean(busy)} className="px-2.5 py-2 rounded-lg bg-slate-800 text-slate-300 text-sm border border-slate-700 disabled:opacity-45" title="Сделать LTX-видео из готовой картинки (мимо маршрута)">
            Video
          </button>
          <button onClick={batchExclude} disabled={Boolean(busy)} className="px-2.5 py-2 rounded-lg bg-slate-800 text-slate-300 text-sm border border-slate-700 disabled:opacity-45" title="Исключить выбранные (или видимые) кадры из финального видео">
            Исключить
          </button>
          {isBatchBusy && (
            <button
              onClick={requestCancelBatch}
              disabled={cancelRequested}
              className="px-3 py-2 rounded-lg bg-red-950 text-red-200 text-sm font-semibold border border-red-800 disabled:opacity-45"
              title="Остановить очередь после текущей карточки. Уже запущенный ComfyUI/сток-запрос завершится сам."
            >
              {cancelRequested ? 'Останавливаю...' : 'Остановить очередь'}
            </button>
          )}
        </div>
      </div>

      {stockProgress && (
        <div className="flex items-center gap-3 px-4 py-3 bg-teal-900/30 border border-teal-700/50 rounded-lg">
          <span className="w-3 h-3 rounded-full bg-teal-400 animate-ping shrink-0" />
          <span className="text-teal-200 text-sm font-semibold shrink-0">
            {stockProgress.label}… {stockProgress.done} из {stockProgress.total}
          </span>
          {cancelRequested && (
            <span className="text-xs text-red-200 shrink-0">остановка после текущей карточки</span>
          )}
          <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-teal-500 rounded-full transition-all"
              style={{ width: `${Math.round((stockProgress.done / Math.max(1, stockProgress.total)) * 100)}%` }}
            />
          </div>
          <span className="text-xs text-slate-400 shrink-0">{Math.round((stockProgress.done / Math.max(1, stockProgress.total)) * 100)}%</span>
        </div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        {[
          ['Всего', counts.total],
          ['С медиа', counts.media],
          ['В финале', counts.included],
          ['Исключено', counts.excluded],
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
        <section className="overflow-x-auto rounded-lg bg-slate-900 border border-slate-800">
          <div className="grid min-w-[800px] grid-cols-[42px_74px_82px_minmax(180px,1fr)_116px_116px_150px] gap-2 px-3 py-2 border-b border-slate-800 text-xs text-slate-500">
            <div />
            <div>Shot</div>
            <div>Status</div>
            <div>Prompt</div>
            <div>Route</div>
            <div>Media</div>
            <div className="text-right">Actions</div>
          </div>
          <div className="max-h-[68vh] min-w-[800px] overflow-y-auto">
            {visibleIndexes.map((index) => {
              const card = cards[index]
              const media = bestMedia(card)
              const color = STATUS_COLORS[card.status] || '#64748b'
              return (
                <div
                  role="group"
                  aria-label={`Shot ${card.shot_index}`}
                  tabIndex={0}
                  key={`${card.scene_index}-${card.shot_index}-${index}`}
                  onClick={() => setSelectedIndex(index)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      setSelectedIndex(index)
                    }
                  }}
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
                    {isExcluded(card) ? <div className="text-[11px] text-rose-400 mt-1">исключён</div> : isIncluded(card) && <div className="text-[11px] text-emerald-400 mt-1">в финале</div>}
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm text-slate-200 truncate">{card.description || card.screen_text || card.scene_title || 'Без описания'}</div>
                    <div className="text-xs text-slate-500 truncate mt-0.5">{card.image_prompt || card.video_prompt || card.asset_query}</div>
                  </div>
                  <div className="text-xs text-slate-400 self-center">{card.route || '-'}</div>
                  <div className="text-xs text-slate-400 self-center">
                    {media ? media.type : 'none'}
                    {card.ltx_draft ? <span className="ml-1 text-amber-400" title="Отрендерено черновиком">· черновик</span> : null}
                    {card.visual_type === 'web_reference_card' || card.route === 'reference' ? <span className="ml-1 text-sky-300" title={card.reference_url || card.reference_query || 'Инсерт-источник'}>· источник</span> : null}
                    {card.text_overlay_enabled ? <span className="ml-1 text-cyan-300" title={card.text_overlay_text || card.screen_text || 'Overlay'}>· text</span> : null}
                  </div>
                  <div onClick={(e) => e.stopPropagation()} className="flex justify-end gap-1">
                    <button onClick={() => generate(index, 'route')} disabled={Boolean(busy)} className="px-2 py-1 rounded bg-indigo-600 text-white text-xs disabled:opacity-45">AI</button>
                    {card.ltx_draft ? (
                      <button onClick={() => finalize(index)} disabled={Boolean(busy)} title="Перерендерить в финальном качестве (полный LTX)" className="px-2 py-1 rounded bg-amber-600 text-white text-xs disabled:opacity-45">Финал</button>
                    ) : (
                      <button onClick={() => pickStock(index)} disabled={Boolean(busy)} title="Подобрать сток-видео" className="px-2 py-1 rounded bg-teal-700 text-white text-xs disabled:opacity-45">Сток</button>
                    )}
                    <button onClick={() => toggleExclude(index)} disabled={Boolean(busy)} title={isExcluded(card) ? 'Вернуть в финал' : 'Исключить из финала'} className={`px-2 py-1 rounded text-white text-xs disabled:opacity-45 ${isExcluded(card) ? 'bg-slate-700' : 'bg-rose-800'}`}>{isExcluded(card) ? 'Верн.' : 'Искл.'}</button>
                  </div>
                </div>
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
                <button onClick={() => selectedIndex !== null && generate(selectedIndex, 'route')} disabled={Boolean(busy)} className="py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45">AI</button>
                <button onClick={() => selectedIndex !== null && generate(selectedIndex, 'image')} disabled={Boolean(busy)} className="py-2 rounded-lg bg-slate-800 text-slate-100 text-sm font-semibold border border-slate-700 disabled:opacity-45">Image</button>
                <button onClick={() => selectedIndex !== null && generate(selectedIndex, 'video')} disabled={Boolean(busy)} className="py-2 rounded-lg bg-slate-800 text-slate-100 text-sm font-semibold border border-slate-700 disabled:opacity-45">Video</button>
              </div>
              <button onClick={() => selectedIndex !== null && pickStock(selectedIndex)} disabled={Boolean(busy)} className="w-full py-2 rounded-lg bg-teal-700 text-white text-sm font-semibold disabled:opacity-45">Подобрать сток</button>
              {(selected.source_url || selected.source_license || selected.source_author) ? (
                <div className="-mt-2 space-y-0.5 text-[11px] text-slate-500">
                  <p>
                    Сток{selected.stock_score ? ` · score ${selected.stock_score.toFixed(2)}` : ''}
                    {safeExternalUrl(selected.source_url) ? <> · <a href={safeExternalUrl(selected.source_url)} target="_blank" rel="noopener noreferrer" className="text-teal-400 underline">источник</a></> : null}
                  </p>
                  <p className="break-words">
                    {selected.source_license || 'Лицензия не зафиксирована'}
                    {selected.source_author ? ` · ${selected.source_author}` : ''}
                  </p>
                </div>
              ) : null}

              <details open className="rounded-lg border border-slate-800 bg-slate-950/40">
                <summary className="cursor-pointer select-none px-3 py-2.5 text-sm font-semibold text-white">
                  История версий
                  <span className="ml-2 text-xs font-normal text-slate-500">
                    {variantsLoading ? 'загрузка…' : variantHistory?.variants.length || 0}
                  </span>
                </summary>
                <div className="max-h-64 space-y-2 overflow-y-auto border-t border-slate-800 p-2">
                  {[...(variantHistory?.variants || [])].reverse().map((variant) => (
                    <div key={variant.id} className={`rounded-lg border p-2.5 ${variant.selected ? 'border-emerald-700/70 bg-emerald-950/20' : 'border-slate-800 bg-slate-950/70'}`}>
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-semibold text-slate-200">
                              {VARIANT_FIELD_LABELS[variant.output_field] || variant.output_field}
                            </span>
                            <span className="text-[11px] text-slate-400">{variant.source} · {variant.mode}</span>
                          </div>
                          <div className="mt-1 truncate text-xs text-slate-300" title={variant.file_path}>{variantFileName(variant.file_path)}</div>
                          <div className="mt-1 text-[10px] text-slate-600">
                            {new Date(variant.created_at).toLocaleString('ru-RU')} · {variantSize(variant.file_size)} · {variant.file_hash.slice(0, 10)}
                            {variant.model ? ` · ${variant.model}` : ''}
                          </div>
                        </div>
                        {variant.selected ? (
                          <span className="shrink-0 text-[11px] font-semibold text-emerald-400">Активна</span>
                        ) : (
                          <button
                            type="button"
                            onClick={() => void activateVariant(variant)}
                            disabled={Boolean(busy) || selectingVariant !== null}
                            className="shrink-0 rounded bg-slate-800 px-2 py-1 text-[11px] font-semibold text-slate-200 disabled:opacity-45"
                          >
                            {selectingVariant === variant.id ? '…' : 'Выбрать'}
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                  {!variantsLoading && !variantHistory?.variants.length && (
                    <p className="px-2 py-4 text-center text-xs leading-5 text-slate-600">
                      Версии появятся после следующей генерации или назначения медиа.
                    </p>
                  )}
                </div>
              </details>

              <div className="space-y-3">
                <label className="block">
                  <span className="text-xs text-slate-500">Description</span>
                  <textarea value={selected.description} onChange={(e) => updateCardAt(selectedIndex!, { ...selected, description: e.target.value })} className="mt-1 h-16 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500" />
                </label>

                <div className="grid grid-cols-2 gap-3">
                  <label className="block">
                    <span className="text-xs text-slate-500">Route (как показать)</span>
                    <select
                      value={CARD_ROUTES.includes(selected.route) ? selected.route : 'image+video'}
                      onChange={(e) => updateCardAt(selectedIndex!, { ...selected, ...routePatch(e.target.value) })}
                      className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500"
                    >
                      {CARD_ROUTES.map((r) => <option key={r} value={r}>{ROUTE_LABELS[r]}</option>)}
                    </select>
                  </label>
                </div>
                <p className="text-[11px] text-slate-600 -mt-1">
                  Роут определяет простой AI-процесс: картинка или картинка с последующей анимацией в видео.
                </p>

                <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3 space-y-3">
                  <label className="flex items-center justify-between gap-3">
                    <span className="text-sm font-semibold text-white">Текст поверх видео</span>
                    <input
                      type="checkbox"
                      checked={Boolean(selected.text_overlay_enabled)}
                      onChange={(e) => updateCardAt(selectedIndex!, {
                        ...selected,
                        text_overlay_enabled: e.target.checked,
                        text_overlay_text: selected.text_overlay_text || selected.screen_text,
                      })}
                      className="h-4 w-4 accent-indigo-500"
                    />
                  </label>
                  {selected.text_overlay_enabled && (
                    <>
                      <textarea
                        value={selected.text_overlay_text || selected.screen_text || ''}
                        onChange={(e) => updateCardAt(selectedIndex!, { ...selected, text_overlay_text: e.target.value })}
                        className="h-14 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
                      />
                      <div className="grid grid-cols-2 gap-2">
                        <label className="block">
                          <span className="text-xs text-slate-500">Стиль</span>
                          <select
                            value={selected.text_overlay_style || 'subtitle'}
                            onChange={(e) => updateCardAt(selectedIndex!, { ...selected, text_overlay_style: e.target.value })}
                            className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500"
                          >
                            {TEXT_STYLES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                          </select>
                        </label>
                        <label className="block">
                          <span className="text-xs text-slate-500">Позиция</span>
                          <select
                            value={selected.text_overlay_position || 'bottom'}
                            onChange={(e) => updateCardAt(selectedIndex!, { ...selected, text_overlay_position: e.target.value })}
                            className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500"
                          >
                            {TEXT_POSITIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                          </select>
                        </label>
                      </div>
                    </>
                  )}
                </div>

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
                <button onClick={() => selectedIndex !== null && toggleExclude(selectedIndex)} disabled={Boolean(busy)} className={`px-4 py-2 rounded-lg text-white text-sm font-semibold disabled:opacity-45 ${selected && isExcluded(selected) ? 'bg-slate-700' : 'bg-rose-800'}`}>{selected && isExcluded(selected) ? 'Вернуть в финал' : 'Исключить'}</button>
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
