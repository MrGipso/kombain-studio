import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { type BlockerFunction, useBlocker, useOutletContext, useSearchParams } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import { FinalToggle } from '../components/FinalToggle'
import { usePolling } from '../hooks/usePolling'
import { excludeImpact, hasMedia, isExcluded, isIncluded, LONG_SLOT_WARNING_SECONDS, tcToSec } from '../lib/cards'
import type { AssetCard, AssetVariant, AssetVariantHistory, ComfyWatchdogStatus, MaterialPreflight, MaterialRunState } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

// Filterable statuses. `approved` and `rejected` are legacy vocabulary from the
// removed approve-gate workflow: nothing sets them anymore, so offering them as
// filters taught users a workflow that does not exist. Legacy cards carrying
// those statuses still render via STATUS_LABELS/STATUS_COLORS.
const STATUSES = ['all', 'planned', 'image_ready', 'generating', 'generated', 'needs_review', 'skipped']
const ROUTES = ['all', 'image+video', 'image', 'stock', 'kenburns', 'reference']

const STATUS_LABELS: Record<string, string> = {
  all: 'Все статусы',
  planned: 'Запланировано',
  image_ready: 'Картинка готова',
  generating: 'Создаётся',
  generated: 'Готово',
  needs_review: 'Нужна проверка',
  approved: 'Одобрено',
  rejected: 'Отклонено',
  skipped: 'Исключено',
}

const ROUTE_FILTER_LABELS: Record<string, string> = {
  all: 'Все способы',
  'image+video': 'Картинка → видео',
  image: 'Только картинка',
  stock: 'Сток-видео',
  kenburns: 'Картинка + зум',
  reference: 'Карточка источника',
}

// Manually-selectable routes in the card editor (no "all" pseudo-filter).
const CARD_ROUTES = ['image+video', 'image', 'stock', 'kenburns', 'reference']
const ROUTE_LABELS: Record<string, string> = {
  'image+video': 'Реалистичный (картинка→видео)',
  image: 'Реалистичная картинка',
  stock: 'Сток-видео',
  kenburns: 'Картинка + зум',
  reference: 'Источник (Википедия / arXiv)',
}

// One batch action. The target count is part of the label because the scope
// line only says how many cards are in range: each operation then filters that
// range by its own rule, so the user could not tell whether a click meant 180
// renders or nothing at all. A zero-target button is disabled rather than
// answering a click with a red error — having nothing left to do is a normal
// state, not a failure.
function BatchButton({
  label, targets, busy, blocked, blockedHint, onClick, hint, primary = false, danger = false,
}: {
  label: string
  targets: number
  busy: boolean
  blocked: boolean
  blockedHint?: string
  onClick: () => void
  hint: string
  primary?: boolean
  danger?: boolean
}) {
  const empty = targets === 0
  const disabled = busy || blocked || empty
  const tone = danger
    ? 'border border-rose-800 bg-transparent text-rose-300 hover:bg-rose-950/20 focus-visible:ring-rose-500'
    : primary
      ? 'bg-indigo-600 text-white hover:bg-indigo-500 focus-visible:ring-indigo-400'
      : 'border border-slate-700 bg-slate-800 text-slate-200 hover:bg-slate-700 focus-visible:ring-indigo-500'
  const title = blocked
    ? blockedHint || 'Сначала сохраните изменённые карточки.'
    : empty
      ? `${hint}\n\nСейчас подходящих кадров нет.`
      : `${hint}\n\nБудет обработано: ${targets}.`
  return (
    <button
      type="button"
      aria-describedby={blocked ? 'bulk-scope bulk-dirty' : 'bulk-scope'}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`min-h-10 rounded-lg px-4 py-2 text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-45 ${tone}`}
    >
      {label}
      <span className={`ml-2 font-mono text-xs ${danger ? 'text-rose-400/80' : primary ? 'text-indigo-200' : 'text-slate-400'}`}>{targets}</span>
    </button>
  )
}

function routePatch(route: string): Partial<AssetCard> {
  if (route === 'stock') return { route, visual_type: 'stock_video', image_model: '', video_model: '' }
  if (route === 'kenburns') return { route, visual_type: 'still_kenburns', video_model: '' }
  if (route === 'image') return { route, visual_type: 'ai_image', video_model: '' }
  if (route === 'reference') return { route, visual_type: 'web_reference_card', image_model: '', video_model: '' }
  return { route: 'image+video', visual_type: 'ai_image_then_i2v', video_model: 'ltx2.3_i2v' }
}

function editableCardPatch(card: Partial<AssetCard>): Partial<AssetCard> {
  const patch: Partial<AssetCard> = {}
  if (typeof card.description === 'string') patch.description = card.description
  if (typeof card.route === 'string') patch.route = card.route
  if (typeof card.visual_type === 'string') patch.visual_type = card.visual_type
  if (typeof card.image_model === 'string') patch.image_model = card.image_model
  if (typeof card.video_model === 'string') patch.video_model = card.video_model
  if (typeof card.image_prompt === 'string') patch.image_prompt = card.image_prompt
  if (typeof card.video_prompt === 'string') patch.video_prompt = card.video_prompt
  if (typeof card.start === 'string') patch.start = card.start
  if (typeof card.end === 'string') patch.end = card.end
  if (typeof card.notes === 'string') patch.notes = card.notes
  if (typeof card.text_overlay_enabled === 'boolean') patch.text_overlay_enabled = card.text_overlay_enabled
  if (typeof card.text_overlay_text === 'string') patch.text_overlay_text = card.text_overlay_text
  if (typeof card.text_overlay_position === 'string') patch.text_overlay_position = card.text_overlay_position
  if (typeof card.text_overlay_style === 'string') patch.text_overlay_style = card.text_overlay_style
  return patch
}

function cardPatchConflicts(serverCard: AssetCard, baseline: Partial<AssetCard>, patch: Partial<AssetCard>) {
  const serverValues = serverCard as unknown as Record<string, unknown>
  const baselineValues = baseline as unknown as Record<string, unknown>
  const localValues = patch as unknown as Record<string, unknown>
  return Object.keys(patch).some((key) => (
    !Object.is(serverValues[key], baselineValues[key])
    && !Object.is(serverValues[key], localValues[key])
  ))
}

const TEXT_STYLES = [
  ['subtitle', 'Субтитр'],
  ['caption', 'Подпись'],
  ['title', 'Заголовок'],
  ['stat', 'Показатель'],
  ['terminal', 'Терминал'],
  ['badge', 'Метка'],
  ['warning', 'Предупреждение'],
  ['label', 'Ярлык'],
  ['quote', 'Цитата'],
]

const TEXT_POSITIONS = [
  ['bottom', 'Снизу'],
  ['top', 'Сверху'],
  ['center', 'По центру'],
  ['top_left', 'Сверху слева'],
  ['top_right', 'Сверху справа'],
  ['bottom_left', 'Снизу слева'],
  ['bottom_right', 'Снизу справа'],
  ['left', 'Слева'],
  ['right', 'Справа'],
  ['manual', 'Вручную'],
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

// A card whose vision-QA actually ran and came back below the review bar.
// -1 (and an absent score) means "never inspected", which is not a problem.
function isWeakQa(card: AssetCard): boolean {
  const score = card.qa_score
  return typeof score === 'number' && score >= 0 && score < 0.45
}

function isActiveMaterialStatus(status?: string): boolean {
  return status === 'queued' || status === 'running' || status === 'interrupted'
}

// Planned on-screen length of a shot, from its timeline slot.
function cardSeconds(card: AssetCard): number {
  return Math.max(0, tcToSec(card.timeline_end || card.end) - tcToSec(card.timeline_start || card.start))
}

function bestMedia(card: AssetCard) {
  if (card.video_output) return { type: 'video', path: card.video_output }
  if (card.image_output) return { type: 'image', path: card.image_output }
  return null
}

const VARIANT_FIELD_LABELS: Record<string, string> = {
  image_output: 'Изображение',
  video_output: 'Видео',
  reference_output: 'Источник',
  remotion_output: 'Графика',
  assigned_media: 'Назначенное медиа',
}

function variantFileName(path: string) {
  return path.split(/[\\/]/).pop() || path
}

function variantSize(bytes: number) {
  if (!bytes) return '0 Б'
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} КБ`
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`
}

function activityLabel(value: string) {
  if (value === 'save') return 'Сохранение карточки'
  if (value.startsWith('generate-')) return 'Создание материала'
  if (value.startsWith('finalize-')) return 'Финальный рендер'
  if (value.startsWith('stock-')) return 'Подбор сток-видео'
  if (value.startsWith('exclude-')) return 'Изменение участия в финале'
  if (value.startsWith('prompt-')) return 'Улучшение промпта'
  if (value.startsWith('variant-')) return 'Выбор версии материала'
  if (value === 'batch-route') return 'Сборка материалов'
  if (value === 'batch-fill') return 'Заполнение через ИИ'
  if (value === 'batch-finalize') return 'Финальное качество'
  if (value === 'batch-stock') return 'Пакетный подбор стока'
  if (value === 'batch-image') return 'Пакетная генерация картинок'
  if (value === 'batch-video') return 'Пакетная генерация видео'
  if (value === 'batch-exclude') return 'Пакетное исключение из финала'
  if (value === 'Автономная сборка материалов') return 'Автономная сборка материалов'
  return 'Операция проекта'
}

export default function AssetBoard() {
  const { projectId, busy: projectBusy, setBusy } = useOutletContext<Context>()
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()
  const [cards, setCards] = useState<AssetCard[]>([])
  const [boardLoading, setBoardLoading] = useState(true)
  const [boardError, setBoardError] = useState<string | null>(null)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [dirtyCardIds, setDirtyCardIds] = useState<Set<string>>(new Set())
  const [conflictedCardIds, setConflictedCardIds] = useState<Set<string>>(new Set())
  const [filterStatus, setFilterStatus] = useState('all')
  const [filterRoute, setFilterRoute] = useState('all')
  const [query, setQuery] = useState('')
  const [compactMode, setCompactMode] = useState(false)
  // Two jobs, two views: the card grid is for judging generated imagery, the
  // list is for production management (what is missing, draft, or awaiting a
  // decision). At a few hundred shots the grid stops being scannable, and its
  // thumbnails dominate rendering cost, so the list is also the cheap view.
  const [viewMode, setViewMode] = useState<'cards' | 'list'>('cards')
  const [sortMode, setSortMode] = useState<'shot' | 'problems' | 'duration'>('shot')
  const [localBusy, setLocalBusyState] = useState<string | null>(null)
  const [mediaVersion, setMediaVersion] = useState(0)
  const [stockProgress, setStockProgress] = useState<{ label: string; done: number; total: number } | null>(null)
  const [materialRun, setMaterialRun] = useState<MaterialRunState | null>(null)
  const [materialPreflight, setMaterialPreflight] = useState<MaterialPreflight | null>(null)
  const [comfyWatchdog, setComfyWatchdog] = useState<ComfyWatchdogStatus | null>(null)
  const [cancelRequested, setCancelRequested] = useState(false)
  const [variantHistory, setVariantHistory] = useState<AssetVariantHistory | null>(null)
  const [variantsLoading, setVariantsLoading] = useState(false)
  const [variantsError, setVariantsError] = useState<string | null>(null)
  const [selectingVariant, setSelectingVariant] = useState<string | null>(null)
  const cancelBatchRef = useRef(false)
  const materialRunStatusRef = useRef<string>('not_started')
  const materialRunLoadedRef = useRef(false)
  const boardRequestRef = useRef(0)
  const variantRequestRef = useRef(0)
  const hasLoadedBoardRef = useRef(false)
  const selectedCardIdRef = useRef<string | null>(null)
  const selectedIndexRef = useRef<number | null>(null)
  const lastHandledCardParamRef = useRef<string | null>(null)
  const dirtyCardIdsRef = useRef<Set<string>>(new Set())
  const dirtyPatchesRef = useRef<Map<string, Partial<AssetCard>>>(new Map())
  const dirtyBaselinesRef = useRef<Map<string, Partial<AssetCard>>>(new Map())
  const conflictedCardIdsRef = useRef<Set<string>>(new Set())
  const serverCardSignaturesRef = useRef<Map<string, string>>(new Map())
  const editorRef = useRef<HTMLElement | null>(null)
  const materialsListRef = useRef<HTMLElement | null>(null)
  const navigationDialogRef = useRef<HTMLDivElement | null>(null)
  // The router re-creates `setSearchParams` on every URL change. Referencing it
  // directly inside `load` made `load` unstable, and the board-reset effect
  // below depends on `load`: selecting a card rewrote the URL, rebuilt `load`,
  // and the reset then wiped the selection, the checkboxes and every unsaved
  // card patch — as if another project had been opened. Holding it in a ref
  // keeps `load` stable so the reset only runs when the project really changes.
  const setSearchParamsRef = useRef(setSearchParams)
  useEffect(() => {
    setSearchParamsRef.current = setSearchParams
  }, [setSearchParams])
  const busy = localBusy || projectBusy
  const setLocalBusy = (value: string | null) => {
    if (value) boardRequestRef.current += 1
    setLocalBusyState(value)
    setBusy(value)
  }
  const scrollToSection = useCallback((element: HTMLElement | null) => {
    if (!element) return
    const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
    window.requestAnimationFrame(() => element.scrollIntoView({ behavior, block: 'start' }))
  }, [])
  // Generation/render is blocked while a pipeline step runs on another page.

  const load = useCallback(async (): Promise<boolean> => {
    const requestId = ++boardRequestRef.current
    try {
      const board = await api.getAssetBoard(projectId)
      if (requestId !== boardRequestRef.current) return false
      const previousSignatures = serverCardSignaturesRef.current
      const nextSignatures = new Map(board.map((card) => [card.card_id, JSON.stringify(card)]))
      serverCardSignaturesRef.current = nextSignatures
      const boardCardIds = new Set(board.map((card) => card.card_id))
      const retainedPatches = new Map(
        [...dirtyPatchesRef.current].filter(([cardId]) => boardCardIds.has(cardId)),
      )
      const retainedBaselines = new Map(
        [...dirtyBaselinesRef.current].filter(([cardId]) => boardCardIds.has(cardId)),
      )
      const retainedDirtyIds = new Set(retainedPatches.keys())
      const nextConflicts = new Set<string>()
      for (const serverCard of board) {
        const patch = retainedPatches.get(serverCard.card_id)
        const baseline = retainedBaselines.get(serverCard.card_id)
        if (patch && baseline && cardPatchConflicts(serverCard, baseline, patch)) nextConflicts.add(serverCard.card_id)
      }
      dirtyPatchesRef.current = retainedPatches
      dirtyBaselinesRef.current = retainedBaselines
      dirtyCardIdsRef.current = retainedDirtyIds
      conflictedCardIdsRef.current = nextConflicts
      setDirtyCardIds(retainedDirtyIds)
      setConflictedCardIds(nextConflicts)
      setChecked((current) => new Set([...current].filter((cardId) => boardCardIds.has(cardId))))
      setCards(() => board.map((serverCard) => {
        const localPatch = retainedPatches.get(serverCard.card_id)
        return hasLoadedBoardRef.current
          && localPatch
          ? { ...serverCard, ...localPatch }
          : serverCard
      }))
      const previousIndex = selectedIndexRef.current
      const wasLoaded = hasLoadedBoardRef.current
      if (!board.length) {
        selectedCardIdRef.current = null
        selectedIndexRef.current = null
        setSelectedIndex(null)
      } else {
        const selectedById = selectedCardIdRef.current
          ? board.findIndex((card) => card.card_id === selectedCardIdRef.current)
          : -1
        const nextIndex = selectedById >= 0
          ? selectedById
          : previousIndex === null || previousIndex >= board.length ? 0 : previousIndex
        selectedCardIdRef.current = board[nextIndex].card_id
        selectedIndexRef.current = nextIndex
        setSelectedIndex(nextIndex)
        if (wasLoaded && nextIndex !== previousIndex) {
          const cardParam = String(nextIndex)
          lastHandledCardParamRef.current = cardParam
          setSearchParamsRef.current((current) => {
            const next = new URLSearchParams(current)
            next.set('card', cardParam)
            return next
          }, { replace: true })
        }
      }
      // Background polls reload the whole board every few seconds. Re-keying
      // the preview on each of them remounted the <video> and restarted
      // playback, so only a changed server record of the selected card (e.g.
      // a Ken Burns clip re-rendered under the same path) busts its cache.
      const selectedCardId = selectedCardIdRef.current
      const previousSignature = selectedCardId ? previousSignatures.get(selectedCardId) : undefined
      if (selectedCardId && previousSignature !== undefined && previousSignature !== nextSignatures.get(selectedCardId)) {
        setMediaVersion((value) => value + 1)
      }
      hasLoadedBoardRef.current = true
      setBoardError(null)
      return true
    } catch (error) {
      if (requestId !== boardRequestRef.current) return false
      const message = (error as Error).message || 'Неизвестная ошибка'
      setBoardError(message)
      if (!hasLoadedBoardRef.current) toast.error(`Не удалось загрузить материалы: ${message}`)
      return false
    } finally {
      if (requestId === boardRequestRef.current) setBoardLoading(false)
    }
  }, [projectId, toast])

  const refreshBoardAfterConflict = useCallback(async (successMessage: string, failureMessage: string) => {
    setBoardLoading(true)
    const refreshed = await load()
    if (refreshed) toast.info(successMessage)
    else toast.error(failureMessage)
    return refreshed
  }, [load, toast])

  useEffect(() => {
    boardRequestRef.current += 1
    hasLoadedBoardRef.current = false
    selectedCardIdRef.current = null
    selectedIndexRef.current = null
    lastHandledCardParamRef.current = null
    dirtyCardIdsRef.current = new Set()
    dirtyPatchesRef.current = new Map()
    dirtyBaselinesRef.current = new Map()
    conflictedCardIdsRef.current = new Set()
    serverCardSignaturesRef.current = new Map()
    setDirtyCardIds(new Set())
    setConflictedCardIds(new Set())
    setChecked(new Set())
    setCards([])
    setBoardError(null)
    setBoardLoading(true)
    void load()
  }, [load])

  const selected = selectedIndex === null ? null : cards[selectedIndex]

  const loadVariants = useCallback(async (cardIndex: number, cardId: string) => {
    if (selectedCardIdRef.current !== cardId) return
    const requestId = ++variantRequestRef.current
    setVariantsLoading(true)
    setVariantHistory(null)
    setVariantsError(null)
    try {
      const history = await api.getAssetVariants(projectId, cardIndex)
      if (requestId === variantRequestRef.current && selectedCardIdRef.current === cardId) {
        if (history.card_id === cardId) {
          setVariantHistory(history)
        } else {
          setVariantHistory(null)
          setVariantsError('Состав доски изменился; обновляю выбранную карточку…')
          setBoardLoading(true)
          void load()
        }
      }
    } catch (error) {
      if (requestId === variantRequestRef.current && selectedCardIdRef.current === cardId) {
        setVariantHistory(null)
        setVariantsError((error as Error).message || 'Неизвестная ошибка')
        if (api.isApiError(error, 404)) {
          setBoardLoading(true)
          void load()
        }
      }
    } finally {
      if (requestId === variantRequestRef.current && selectedCardIdRef.current === cardId) {
        setVariantsLoading(false)
      }
    }
  }, [load, projectId])

  // Deep link from the Preview QC list: /board?card=N opens that card.
  useEffect(() => {
    const c = searchParams.get('card')
    if (c === null || !cards.length) return
    if (lastHandledCardParamRef.current === c) return
    const n = parseInt(c, 10)
    if (!isNaN(n) && n >= 0 && n < cards.length) {
      lastHandledCardParamRef.current = c
      selectedCardIdRef.current = cards[n].card_id
      selectedIndexRef.current = n
      setSelectedIndex(n)
    }
  }, [cards, searchParams])

  useEffect(() => {
    selectedIndexRef.current = selectedIndex
    selectedCardIdRef.current = selectedIndex === null ? null : cards[selectedIndex]?.card_id || null
  }, [cards, selectedIndex])

  const selectedVariantRevision = selected
    ? [
        selected.card_id,
        selected.status,
        selected.image_output,
        selected.video_output,
        selected.reference_output,
        selected.assigned_media,
      ].join('\u0000')
    : ''
  const selectedVariantCardId = selected?.card_id ?? ''

  useEffect(() => {
    if (selectedIndex === null || !selectedVariantCardId) {
      variantRequestRef.current += 1
      setVariantHistory(null)
      setVariantsError(null)
      setVariantsLoading(false)
      return
    }
    void loadVariants(selectedIndex, selectedVariantCardId)
  }, [loadVariants, selectedIndex, selectedVariantCardId, selectedVariantRevision])

  const selectCard = useCallback((index: number) => {
    selectedCardIdRef.current = cards[index]?.card_id || null
    selectedIndexRef.current = index
    lastHandledCardParamRef.current = String(index)
    setSelectedIndex(index)
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      next.set('card', String(index))
      return next
    }, { replace: true })
    if (window.matchMedia('(max-width: 1279px)').matches) scrollToSection(editorRef.current)
  }, [cards, scrollToSection, setSearchParams])

  // Auto-refresh while any card is still generating, so renders that finished
  // on the server (e.g. while you were on another tab) get picked up here.
  const anyGenerating = useMemo(
    () => cards.some((c) => (c.status || '').toLowerCase() === 'generating'),
    [cards],
  )
  usePolling(load, 4000, { enabled: anyGenerating && !busy })

  // Unlike the legacy browser-owned batch loop, an unattended run lives on
  // the server. Navigation/reload is safe; this page merely reconnects to its
  // checkpoint and refreshes the board while work continues.
  const refreshMaterialRun = useCallback(async (isCurrent: () => boolean) => {
    try {
      const state = await api.getMaterialRun(projectId)
      if (!isCurrent()) return
      setMaterialRun(state)
      const previous = materialRunStatusRef.current
      const hadLoaded = materialRunLoadedRef.current
      materialRunLoadedRef.current = true
      materialRunStatusRef.current = state.status
      if (hadLoaded && state.status === 'succeeded' && previous !== 'succeeded') {
        toast.success(`Все материалы готовы${state.summary?.fallbacks ? ` · резервных кадров: ${state.summary.fallbacks}` : ''}`)
      } else if (hadLoaded && state.status === 'failed' && previous !== 'failed') {
        toast.error(state.error || 'Автономная сборка материалов остановилась с ошибкой')
      }
      if (state.status === 'running' || state.status === 'queued') {
        await load()
      } else if (previous === 'running' || previous === 'queued') {
        // Also covers very short jobs which finish before ProjectLayout sees
        // the durable job record and would otherwise leave its busy banner on.
        setBusy(null)
        await load()
      }
    } catch {
      // The durable project-job poll in ProjectLayout remains the fallback.
    }
  }, [load, projectId, setBusy, toast])
  usePolling(refreshMaterialRun, 3000, { immediate: true })

  const refreshWatchdog = useCallback(async (isCurrent: () => boolean) => {
    try {
      const state = await api.getComfyWatchdog()
      if (isCurrent()) setComfyWatchdog(state)
    } catch {
      if (isCurrent()) setComfyWatchdog(null)
    }
  }, [])
  usePolling(refreshWatchdog, 5000, { immediate: true })

  useEffect(() => {
    let active = true
    api.getMaterialPreflight(projectId)
      .then((result) => {
        if (active) setMaterialPreflight(result)
      })
      .catch(() => {
        if (active) setMaterialPreflight(null)
      })
    return () => {
      active = false
    }
  }, [cards.length, projectId])

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

  const checkedIndexes = cards.reduce<number[]>((indexes, card, index) => {
    if (checked.has(card.card_id)) indexes.push(index)
    return indexes
  }, [])

  // Display order. Shot order is the production order and stays the default;
  // "problems first" is what you want right after a large batch, when the
  // question is where generation did not land rather than what comes next.
  const orderedIndexes = useMemo(() => {
    if (sortMode === 'shot') return visibleIndexes
    const weight = (card: AssetCard): number => {
      if (isExcluded(card)) return 90 // out of the cut: never a problem
      if (!hasMedia(card)) return 0 // nothing to ship at all
      if ((card.status || '') === 'needs_review') return 1
      if (isWeakQa(card)) return 2 // low vision-QA score
      if (card.ltx_draft) return 3
      return 50
    }
    const sorted = [...visibleIndexes]
    if (sortMode === 'problems') {
      sorted.sort((a, b) => weight(cards[a]) - weight(cards[b]) || a - b)
    } else {
      sorted.sort((a, b) => cardSeconds(cards[b]) - cardSeconds(cards[a]) || a - b)
    }
    return sorted
  }, [cards, sortMode, visibleIndexes])

  const counts = useMemo(() => ({
    total: cards.length,
    media: cards.filter(hasMedia).length,
    included: cards.filter(isIncluded).length,
    excluded: cards.filter(isExcluded).length,
    review: cards.filter((c) => c.status === 'needs_review').length,
  }), [cards])
  const isBatchBusy = Boolean(localBusy?.startsWith('batch-'))

  // How many cards each batch button will ACTUALLY touch. The scope label only
  // states how many cards are in range; every operation then filters that range
  // by its own rule, so "227 видимых" could mean 180 renders or none at all.
  // These predicates mirror the filters inside the handlers one-for-one — keep
  // them in sync when a handler's eligibility changes.
  const batchTargets = useMemo(() => {
    const scope = checkedIndexes.length ? checkedIndexes : visibleIndexes
    const count = (predicate: (card: AssetCard) => boolean) =>
      scope.filter((index) => {
        const card = cards[index]
        return Boolean(card) && !isExcluded(card) && predicate(card)
      }).length
    return {
      scope: scope.length,
      // batchGenerate('route') and batchFillGaps: cards still missing a clip.
      route: count((card) => !card.video_output),
      // batchFinalize: only draft-rendered clips can be re-rendered in full.
      finalize: count((card) => Boolean(card.ltx_draft) && Boolean(card.video_output)),
      // batchStock and batchGenerate('video'): cards without a clip.
      video: count((card) => !card.video_output),
      // batchGenerate('image'): cards without a still.
      image: count((card) => !card.image_output),
      // batchExclude works on the whole scope, excluded cards included.
      exclude: scope.length,
    }
  }, [cards, checkedIndexes, visibleIndexes])

  // Above this a queue stops being a quick action and becomes an evening of
  // GPU time, so it deserves the same confirmation the cheap, reversible
  // exclusion already had.
  const BULK_CONFIRM_THRESHOLD = 10

  const confirmHeavyBatch = (label: string, targets: number): boolean => {
    if (targets <= BULK_CONFIRM_THRESHOLD) return true
    return window.confirm(
      `${label}: будет обработано ${targets} кадр(ов).\n\n`
      + 'Это надолго — генерация занимает видеокарту и может идти часами. '
      + 'Очередь можно остановить кнопкой «Остановить очередь»: она прервётся '
      + 'после текущей карточки.\n\nПродолжить?',
    )
  }

  const clearDirtyCard = (cardId: string) => {
    if (!dirtyPatchesRef.current.has(cardId)) return
    const nextPatches = new Map(dirtyPatchesRef.current)
    nextPatches.delete(cardId)
    dirtyPatchesRef.current = nextPatches
    const nextBaselines = new Map(dirtyBaselinesRef.current)
    nextBaselines.delete(cardId)
    dirtyBaselinesRef.current = nextBaselines
    const nextConflicts = new Set(conflictedCardIdsRef.current)
    nextConflicts.delete(cardId)
    conflictedCardIdsRef.current = nextConflicts
    setConflictedCardIds(nextConflicts)
    const nextIds = new Set(nextPatches.keys())
    dirtyCardIdsRef.current = nextIds
    setDirtyCardIds(nextIds)
  }

  const updateCardAt = (index: number, card: AssetCard) => {
    // A poll may have started while the mutation was running. Its snapshot is
    // older than this successful response and must not overwrite the card.
    boardRequestRef.current += 1
    // Remember the server's record so the next poll does not mistake this
    // already-applied change for a new one and restart the preview again.
    serverCardSignaturesRef.current.set(card.card_id, JSON.stringify(card))
    setCards((prev) => {
      const targetIndex = prev[index]?.card_id === card.card_id
        ? index
        : prev.findIndex((candidate) => candidate.card_id === card.card_id)
      if (targetIndex < 0) return prev
      const next = [...prev]
      next[targetIndex] = card
      return next
    })
    clearDirtyCard(card.card_id)
  }

  const editCardAt = (index: number, changes: Partial<AssetCard>) => {
    const currentCard = cards[index]
    if (!currentCard) return
    const patch = editableCardPatch(changes)
    if (!Object.keys(patch).length) return
    setCards((prev) => {
      const next = [...prev]
      next[index] = { ...next[index], ...patch }
      return next
    })
    const nextPatches = new Map(dirtyPatchesRef.current)
    nextPatches.set(currentCard.card_id, { ...nextPatches.get(currentCard.card_id), ...patch })
    dirtyPatchesRef.current = nextPatches
    const nextBaselines = new Map(dirtyBaselinesRef.current)
    const baseline = { ...nextBaselines.get(currentCard.card_id) }
    const baselineValues = baseline as unknown as Record<string, unknown>
    const currentValues = currentCard as unknown as Record<string, unknown>
    for (const key of Object.keys(patch)) {
      if (!(key in baselineValues)) baselineValues[key] = currentValues[key]
    }
    nextBaselines.set(currentCard.card_id, baseline)
    dirtyBaselinesRef.current = nextBaselines
    const nextIds = new Set(nextPatches.keys())
    dirtyCardIdsRef.current = nextIds
    setDirtyCardIds(nextIds)
  }

  const patchCardAt = (index: number, patch: Partial<AssetCard>) => {
    setCards((prev) => {
      if (index < 0 || index >= prev.length) return prev
      const next = [...prev]
      next[index] = { ...next[index], ...patch }
      return next
    })
  }

  const saveSelected = async (overwriteConflict = false) => {
    if (selectedIndex === null || !selected) return
    const patch = dirtyPatchesRef.current.get(selected.card_id)
    if (!patch) return
    if (conflictedCardIdsRef.current.has(selected.card_id) && !overwriteConflict) {
      toast.error('Сначала выберите, какую версию конфликтующих полей сохранить')
      return
    }
    setLocalBusy('save')
    try {
      const updated = await api.updateCard(projectId, selectedIndex, { card_id: selected.card_id, ...patch })
      updateCardAt(selectedIndex, updated)
      toast.success('Карточка сохранена')
    } catch (e) {
      if (api.isBoardRevisionConflict(e)) {
        await refreshBoardAfterConflict(
          'Доска изменилась на сервере. Данные обновлены, а ваши правки сохранены локально — проверьте конфликт и повторите сохранение.',
          'Доска изменилась на сервере, но обновить её не удалось. Повторите загрузку.',
        )
      } else {
        toast.error((e as Error).message)
      }
    } finally {
      setLocalBusy(null)
    }
  }

  const discardSelectedChanges = () => {
    if (!selected) return
    clearDirtyCard(selected.card_id)
    setBoardLoading(true)
    void load()
    toast.info('Локальные правки отменены; загружаю серверную версию')
  }

  const activateVariant = async (variant: AssetVariant) => {
    if (selectedIndex === null || variant.selected || busy || selectingVariant !== null) return
    const card = cards[selectedIndex]
    if (!card || dirtyPatchesRef.current.has(card.card_id)) return
    setSelectingVariant(variant.id)
    setLocalBusy(`variant-${selectedIndex}`)
    try {
      const result = await api.selectAssetVariant(projectId, selectedIndex, variant.id)
      updateCardAt(selectedIndex, result.card)
      setMediaVersion((value) => value + 1)
      if (selectedCardIdRef.current === result.card.card_id) {
        await loadVariants(selectedIndex, result.card.card_id)
      }
      toast.success(`${VARIANT_FIELD_LABELS[variant.output_field] || variant.output_field}: версия выбрана`)
    } catch (error) {
      if (api.isBoardRevisionConflict(error)) {
        await refreshBoardAfterConflict(
          'Доска обновлена после параллельного изменения. Повторите выбор версии.',
          'Не удалось обновить доску после параллельного изменения.',
        )
      } else {
        toast.error(`Не удалось выбрать версию: ${(error as Error).message}`)
      }
    } finally {
      setSelectingVariant(null)
      setLocalBusy(null)
    }
  }

  const patchCard = async (index: number, patch: Partial<AssetCard>) => {
    const card = cards[index]
    if (!card) throw new Error('Карточка не найдена')
    const updated = await api.updateCard(projectId, index, { card_id: card.card_id, ...patch })
    updateCardAt(index, updated)
  }

  const saveCardForAction = async (index: number): Promise<boolean> => {
    const card = cards[index]
    if (!card) return false
    const patch = dirtyPatchesRef.current.get(card.card_id)
    if (!patch) return true
    if (conflictedCardIdsRef.current.has(card.card_id)) {
      toast.error(`Кадр #${card.shot_index}: сначала разрешите конфликт правок`)
      return false
    }
    try {
      const updated = await api.updateCard(projectId, index, { card_id: card.card_id, ...patch })
      updateCardAt(index, updated)
      return true
    } catch (e) {
      if (api.isBoardRevisionConflict(e)) {
        await refreshBoardAfterConflict(
          'Доска обновлена после параллельного изменения. Локальные правки сохранены; проверьте карточку и повторите запуск.',
          'Не удалось обновить доску после параллельного изменения. Повторите загрузку.',
        )
      } else {
        toast.error(`Не удалось сохранить карточку перед запуском: ${(e as Error).message}`)
      }
      return false
    }
  }

  // Toggle a card in/out of the final video (replaces the old approve gate).
  const toggleExclude = async (index: number) => {
    const card = cards[index]
    // Compute the montage consequence BEFORE mutating: the exporter gives the
    // excluded card's screen time to the previous surviving clip, and the user
    // should learn that at the moment of the decision, not after the export.
    const impact = !isExcluded(card) ? excludeImpact(cards, index) : null
    setLocalBusy(`exclude-${index}`)
    try {
      if (isExcluded(card)) {
        await patchCard(index, { status: hasMedia(card) ? 'generated' : 'planned' })
        toast.success(`Кадр #${card.shot_index} возвращён в финал.`)
      } else {
        await patchCard(index, { status: 'skipped' })
        if (impact) {
          toast.success(`Кадр #${card.shot_index} исключён — ${impact.gainedSeconds} с уйдут кадру #${impact.recipientShot} (станет ${impact.recipientSeconds} с).`)
          if (impact.recipientSeconds > LONG_SLOT_WARNING_SECONDS) {
            toast.info(`Внимание: кадр #${impact.recipientShot} растянется до ${impact.recipientSeconds} с — долгий статичный план может выглядеть застывшим.`)
          }
        } else {
          toast.success(`Кадр #${card.shot_index} исключён из финала${hasMedia(card) ? '' : ' (медиа не было — на монтаж не влияет)'}.`)
        }
      }
    } catch (e) {
      if (api.isBoardRevisionConflict(e)) {
        await refreshBoardAfterConflict(
          'Доска обновлена после параллельного изменения. Повторите изменение участия в финале.',
          'Не удалось обновить доску после параллельного изменения.',
        )
      } else {
        toast.error((e as Error).message)
      }
    } finally {
      setLocalBusy(null)
    }
  }

  const runGenerateCard = async (index: number, mode: 'route' | 'image' | 'video', skipStock = false, forceFull = false, deferQa = true): Promise<AssetCard | null> => {
    patchCardAt(index, { status: 'generating' }) // optimistic, so the UI reflects work immediately
    try {
      const result = await api.generateCard(projectId, index, cards[index].card_id, mode, skipStock, forceFull, deferQa)
      if (result.card) {
        updateCardAt(index, result.card)
        if (selectedCardIdRef.current === result.card.card_id) void loadVariants(index, result.card.card_id)
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
      const result = await runGenerateCard(index, 'video', false, true)
      if (result) toast.success('Кадр отрендерен в финальном качестве')
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
    if (!targets.length) { toast.info('Нет черновых клипов — перерендеривать нечего'); return }
    if (!confirmHeavyBatch('Финальное качество', targets.length)) return
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
        if (selectedCardIdRef.current === result.card.card_id) void loadVariants(index, result.card.card_id)
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

  const requestCancelWork = async () => {
    if (!isActiveMaterialStatus(materialRun?.status)) {
      requestCancelBatch()
      return
    }
    setCancelRequested(true)
    try {
      const result = await api.cancelJob(projectId)
      if (result.cancelled) {
        toast.info('Отмена запрошена — prompt будет удалён или остановлен в ComfyUI')
      } else {
        setCancelRequested(false)
        toast.info('Активная серверная задача уже не выполняется')
      }
    } catch (error) {
      setCancelRequested(false)
      toast.error((error as Error).message)
    }
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

  const startUnattendedMaterials = async () => {
    if (hasUnsavedChanges) {
      toast.error('Сначала сохраните изменённые карточки')
      return
    }
    try {
      const result = await api.startMaterialRun(projectId)
      setBusy('Автономная сборка материалов')
      setMaterialRun({
        schema_version: 1,
        project_id: projectId,
        status: 'running',
        policy: result.policy,
        summary: {
          cards_total: result.preflight.cards,
          cards_ready: result.preflight.ready_cards,
          operations_total: result.preflight.pending_images + result.preflight.pending_videos,
          operations_completed: 0,
          retries: 0,
          fallbacks: 0,
          failures: 0,
        },
      })
      materialRunLoadedRef.current = true
      materialRunStatusRef.current = 'running'
      const warnings = result.preflight.warnings
      toast.success(
        warnings
          ? `Автономная сборка запущена · предупреждений плана: ${warnings}`
          : 'Автономная сборка запущена — страницу можно закрыть',
      )
    } catch (error) {
      const detail = api.isApiError(error, 409) && error.detail && typeof error.detail === 'object'
        ? error.detail as { issues?: Array<{ level?: string; message?: string }> }
        : null
      const blockers = detail?.issues
        ?.filter((issue) => issue.level === 'blocker')
        .map((issue) => issue.message)
        .filter(Boolean)
      toast.error(blockers?.length ? blockers.join(' ') : (error as Error).message)
    }
  }

  const batchGenerate = async (mode: 'route' | 'image' | 'video') => {
    const candidates = checkedIndexes.length ? checkedIndexes : visibleIndexes
    // Generation from this interface never invokes the legacy vision-QA pass.
    // Technical file validation remains part of the unattended engine.
    const deferQa = true
    if (mode !== 'route') {
      const targets = candidates.filter((index) => {
        const card = cards[index]
        if (isExcluded(card)) return false
        if (mode === 'image') return !card.image_output
        return !card.video_output
      })
      if (!targets.length) { toast.info('Нет подходящих кадров — здесь уже всё готово'); return }
      const label = mode === 'image' ? 'Генерация картинок' : 'Генерация видео'
      if (!confirmHeavyBatch(label, targets.length)) return
      startBatch(`batch-${mode}`, label, targets.length)
      let ok = 0
      let fail = 0
      let done = 0
      for (const index of targets) {
        if (cancelBatchRef.current) break
        const card = await runGenerateCard(index, mode, false, false, deferQa)
        if (card) ok++
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
    if (!eligible.length) { toast.info('Нет подходящих кадров — везде уже есть видео'); return }
    if (!confirmHeavyBatch('Собрать материалы', eligible.length)) return
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
        (i) => runGenerateCard(i, 'route', false, false, deferQa),
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
            card_id: live[i].card_id,
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
        (i) => runGenerateCard(i, 'image', false, false, deferQa),
      )
    }

    // 3) Ken Burns zooms (ffmpeg only — image already exists, route skips Krea)
    if (!stopped) {
      await runPhase(
        'Очередь 3/4: зумы (ffmpeg)',
        eligible.filter((i) => vt(i) === 'still_kenburns' && live[i].image_output && !live[i].video_output),
        (i) => runGenerateCard(i, 'route', false, false, deferQa),
      )
    }

    // 4) All LTX renders last (LTX loaded once)
    if (!stopped) {
      await runPhase(
        'Очередь 4/4: видео',
        eligible.filter((i) => vt(i) === 'ai_image_then_i2v' && live[i].image_output && !live[i].video_output),
        (i) => runGenerateCard(i, 'video', false, false, deferQa),
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
    if (!targets.length) { toast.info('Нет кадров без видео — подбирать сток не для чего'); return }
    const label = 'Подбор стока'
    if (!confirmHeavyBatch(label, targets.length)) return
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
    if (!targets.length) { toast.info('Нет пустых кадров — везде уже есть видео'); return }
    const label = 'Дополнение AI'
    if (!confirmHeavyBatch('Заполнить через ИИ', targets.length)) return
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
    if (!targets.length) { toast.info('Нет материалов в текущей области'); return }
    // The scope silently depends on checkboxes and the active filter, which is
    // easy to misread. Listing the exact shot numbers removes the guesswork.
    const shots = targets.map((index) => cards[index]?.shot_index ?? index + 1)
    const listed = shots.slice(0, 12).map((n) => `#${n}`).join(', ')
    const tail = shots.length > 12 ? ` и ещё ${shots.length - 12}` : ''
    const source = checkedIndexes.length ? 'отмеченные галочками' : 'все видимые по текущему фильтру'
    if (!window.confirm(
      `Исключить из финального видео ${shots.length} кадр(ов) — ${source}:\n${listed}${tail}\n\n`
      + 'Время каждого перейдёт соседнему плану; длина ролика не изменится. Действие обратимо.',
    )) return
    const label = 'Исключение из финала'
    startBatch('batch-exclude', label, targets.length)
    let n = 0
    let failed = 0
    let done = 0
    let revisionConflict = false
    let fatalError: string | null = null
    for (const index of targets) {
      if (cancelBatchRef.current) break
      try {
        const updated = await api.updateCard(projectId, index, { card_id: cards[index].card_id, status: 'skipped' })
        updateCardAt(index, updated)
        n++
      } catch (error) {
        failed++
        if (api.isApiError(error, 409)) {
          if (api.isBoardRevisionConflict(error)) revisionConflict = true
          else fatalError = error.message
          break
        }
        toast.error(`Кадр #${cards[index]?.shot_index ?? index}: ${(error as Error).message}`)
      }
      done++
      setStockProgress({ label, done, total: targets.length })
    }
    const stopped = cancelBatchRef.current
    finishBatch()
    if (revisionConflict) {
      await refreshBoardAfterConflict(
        'Очередь остановлена: доска была изменена параллельно и уже обновлена.',
        'Очередь остановлена из-за параллельного изменения; обновить доску не удалось.',
      )
    } else if (fatalError) {
      toast.error(`${label}: очередь остановлена — ${fatalError}`)
    } else if (stopped) {
      toast.info(`${label}: остановлено после ${done} из ${targets.length}`)
    } else {
      toast.success(`${label}: исключено ${n}, ошибок ${failed}`)
    }
  }

  const toggleVisible = () => {
    setChecked((prev) => {
      const next = new Set(prev)
      const visibleCardIds = visibleIndexes.map((index) => cards[index].card_id)
      const allVisibleChecked = visibleCardIds.every((cardId) => next.has(cardId))
      for (const cardId of visibleCardIds) {
        if (allVisibleChecked) next.delete(cardId)
        else next.add(cardId)
      }
      return next
    })
  }

  const allVisibleSelected = visibleIndexes.length > 0 && visibleIndexes.every((index) => checked.has(cards[index].card_id))
  const hasActiveFilters = Boolean(query.trim()) || filterStatus !== 'all' || filterRoute !== 'all'
  const batchProgressPercent = stockProgress
    ? Math.round((stockProgress.done / Math.max(1, stockProgress.total)) * 100)
    : 0
  const selectedMedia = selected ? bestMedia(selected) : null
  const selectedDirty = Boolean(selected && dirtyCardIds.has(selected.card_id))
  const selectedConflict = Boolean(selected && conflictedCardIds.has(selected.card_id))
  const hasUnsavedChanges = dirtyCardIds.size > 0
  const shouldBlockNavigation = useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) => (hasUnsavedChanges || isBatchBusy) && currentLocation.pathname !== nextLocation.pathname,
    [hasUnsavedChanges, isBatchBusy],
  )
  const navigationBlocker = useBlocker(shouldBlockNavigation)
  const resetBlockedNavigation = navigationBlocker.state === 'blocked' ? navigationBlocker.reset : null
  const proceedBlockedNavigation = navigationBlocker.state === 'blocked' ? navigationBlocker.proceed : null
  useEffect(() => {
    if (proceedBlockedNavigation && !hasUnsavedChanges && !isBatchBusy) proceedBlockedNavigation()
  }, [hasUnsavedChanges, isBatchBusy, proceedBlockedNavigation])
  // useBlocker only covers in-app navigation. Closing the tab, reloading, or
  // leaving the SPA entirely bypasses the router, so card edits that were never
  // saved would be discarded without any prompt. The browser shows its own
  // generic confirmation; a custom message is not supported by design.
  useEffect(() => {
    if (!hasUnsavedChanges && !isBatchBusy) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      // Older engines still require returnValue to be set for the prompt.
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [hasUnsavedChanges, isBatchBusy])
  useEffect(() => {
    if (!resetBlockedNavigation) return
    const dialog = navigationDialogRef.current
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const focusable = () => [...(dialog?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') || [])]
    const keepFocusInside = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        resetBlockedNavigation()
        return
      }
      if (event.key !== 'Tab') return
      const elements = focusable()
      if (!elements.length) return
      const first = elements[0]
      const last = elements[elements.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', keepFocusInside, true)
    window.requestAnimationFrame(() => focusable()[0]?.focus())
    return () => {
      document.removeEventListener('keydown', keepFocusInside, true)
      previouslyFocused?.focus()
    }
  }, [resetBlockedNavigation])
  const currentActivityLabel = stockProgress?.label || (busy ? activityLabel(busy) : '')
  const unattendedActive = isActiveMaterialStatus(materialRun?.status)
  const unattendedRunning = materialRun?.status === 'running' || materialRun?.status === 'queued'
  const unattendedWaiting = materialRun?.status === 'interrupted'
  const unattendedCompleted = materialRun?.summary?.operations_completed || 0
  const unattendedTotal = materialRun?.summary?.operations_total || 0
  const unattendedPercent = Math.round((unattendedCompleted / Math.max(1, unattendedTotal)) * 100)
  const watchdogLabel = !comfyWatchdog
    ? 'watchdog: нет связи'
    : comfyWatchdog.status === 'healthy'
      ? 'ComfyUI под защитой'
      : comfyWatchdog.status === 'restarting'
        ? 'ComfyUI перезапускается'
        : comfyWatchdog.status === 'degraded'
          ? `ComfyUI: сбой ${comfyWatchdog.consecutive_failures}`
          : comfyWatchdog.status === 'disabled'
            ? 'watchdog выключен'
            : `watchdog: ${comfyWatchdog.status}`
  const watchdogTone = comfyWatchdog?.status === 'healthy'
    ? 'bg-emerald-950 text-emerald-300 ring-emerald-800/70'
    : comfyWatchdog?.status === 'restarting'
      ? 'bg-sky-950 text-sky-300 ring-sky-800/70 motion-safe:animate-pulse'
      : comfyWatchdog?.status === 'disabled'
        ? 'bg-slate-800 text-slate-400 ring-slate-700'
        : comfyWatchdog?.status === 'degraded'
          ? 'bg-amber-950 text-amber-300 ring-amber-800/70'
          : 'bg-rose-950 text-rose-300 ring-rose-800/70'
  const bulkScopeLabel = checkedIndexes.length
    ? `${checkedIndexes.length} выбранных`
    : `${visibleIndexes.length} видимых`

  return (
    <div className="mx-auto max-w-[1800px] space-y-5 p-4 sm:p-6">
      {navigationBlocker.state === 'blocked' && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm" role="presentation">
          <div ref={navigationDialogRef} role="dialog" aria-modal="true" aria-labelledby="unsaved-title" aria-describedby="unsaved-description" className="w-full max-w-md rounded-2xl border border-amber-800/70 bg-slate-900 p-5 shadow-2xl shadow-black/50">
            <h2 id="unsaved-title" className="text-lg font-semibold text-white">{isBatchBusy ? 'Идёт пакетная обработка' : 'Есть несохранённые правки'}</h2>
            <p id="unsaved-description" className="mt-2 text-sm leading-6 text-slate-400">
              {isBatchBusy
                ? 'Эта страница управляет очередью следующих карточек. Сначала вернитесь и остановите очередь штатной кнопкой.'
                : <>Несохранённых карточек: <strong className="font-semibold text-amber-300">{dirtyCardIds.size}</strong>. Если уйти со страницы, изменения будут потеряны.</>}
            </p>
            <div className="mt-5 flex flex-wrap justify-end gap-2">
              <button type="button" autoFocus onClick={() => navigationBlocker.reset()} className="min-h-10 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400">Вернуться к работе</button>
              {!isBatchBusy && <button type="button" onClick={() => navigationBlocker.proceed()} className="min-h-10 rounded-lg border border-rose-800 bg-rose-950 px-4 py-2 text-sm font-semibold text-rose-200 hover:bg-rose-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400">Уйти без сохранения</button>}
            </div>
          </div>
        </div>
      )}
      <header className="flex flex-col gap-4 rounded-2xl border border-slate-800 bg-gradient-to-br from-slate-900 to-slate-900/60 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-indigo-400">Визуальное производство</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-white sm:text-3xl">Материалы</h1>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-400">
            Проверьте план кадров один раз и запустите автономную сборку. Очередь продолжит работу после ухода со страницы и восстановится после перезапуска.
          </p>
        </div>
        {busy ? (
          <div role="status" aria-live="polite" className="flex max-w-full items-center gap-2 rounded-xl border border-sky-800/70 bg-sky-950/40 px-3 py-2 text-sm text-sky-200" title={busy}>
            <span aria-hidden="true" className="relative flex h-2.5 w-2.5 shrink-0">
              <span className="absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-60 motion-safe:animate-ping" />
              <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-sky-400" />
            </span>
            <span className="min-w-0">
              <span className="block truncate font-semibold">{currentActivityLabel}</span>
            </span>
          </div>
        ) : boardLoading ? (
          <span role="status" aria-live="polite" className="inline-flex w-fit items-center gap-2 rounded-full border border-sky-900/70 bg-sky-950/30 px-3 py-1.5 text-xs font-medium text-sky-300">
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-sky-400 motion-safe:animate-pulse" />
            {hasLoadedBoardRef.current ? 'Обновляю материалы' : 'Загружаю материалы'}
          </span>
        ) : boardError ? (
          <span role="status" className={`inline-flex w-fit items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium ${hasLoadedBoardRef.current ? 'border-amber-900/70 bg-amber-950/30 text-amber-300' : 'border-rose-900/70 bg-rose-950/30 text-rose-300'}`}>
            <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${hasLoadedBoardRef.current ? 'bg-amber-400' : 'bg-rose-400'}`} />
            {hasLoadedBoardRef.current ? 'Показана сохранённая версия' : 'Материалы недоступны'}
          </span>
        ) : (
          <span className="inline-flex w-fit items-center gap-2 rounded-full border border-emerald-900/70 bg-emerald-950/30 px-3 py-1.5 text-xs font-medium text-emerald-300">
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> Готово к работе
          </span>
        )}
      </header>

      <section aria-label="Сводка по материалам" className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
        {[
          { label: 'Всего кадров', value: counts.total, hint: 'в доске', accent: 'bg-slate-500' },
          { label: 'Медиа готово', value: counts.media, hint: `${counts.total ? Math.round((counts.media / counts.total) * 100) : 0}%`, accent: 'bg-indigo-400' },
          { label: 'В финале', value: counts.included, hint: 'включено', accent: 'bg-emerald-400' },
          { label: 'Исключено', value: counts.excluded, hint: 'не войдёт', accent: 'bg-rose-400' },
          { label: 'На проверке', value: counts.review, hint: 'нужно решение', accent: 'bg-amber-400' },
        ].map(({ label, value, hint, accent }) => (
          <div key={label} className="relative overflow-hidden rounded-xl border border-slate-800 bg-slate-900 px-4 py-3.5">
            <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-1 ${accent}`} />
            <div className="flex items-end justify-between gap-2">
              <span className="text-2xl font-semibold text-white">{value}</span>
              <span className="text-[10px] text-slate-600">{hint}</span>
            </div>
            <div className="mt-1 text-xs font-medium text-slate-400">{label}</div>
          </div>
        ))}
      </section>

      {materialPreflight && materialPreflight.issues.length > 0 && (
        <section aria-labelledby="preflight-title" className={`rounded-xl border p-4 ${
          materialPreflight.blockers
            ? 'border-rose-800/70 bg-rose-950/25'
            : 'border-amber-800/70 bg-amber-950/20'
        }`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 id="preflight-title" className="text-sm font-semibold text-white">
                Проверка плана перед производством
              </h2>
              <p className="mt-1 text-xs text-slate-400">
                Это проверка смысла и целостности плана, а не QA сгенерированных файлов.
              </p>
            </div>
            <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
              materialPreflight.blockers
                ? 'bg-rose-950 text-rose-300'
                : 'bg-amber-950 text-amber-300'
            }`}>
              {materialPreflight.blockers
                ? `Нужно исправить: ${materialPreflight.blockers}`
                : `Предупреждений: ${materialPreflight.warnings}`}
            </span>
          </div>
          <ul className="mt-3 space-y-1.5 text-xs">
            {materialPreflight.issues.map((issue, index) => (
              <li key={`${issue.code}-${issue.scene_index || 0}-${issue.shot_index || 0}-${index}`} className={issue.level === 'blocker' ? 'text-rose-300' : 'text-amber-200'}>
                <span className="mr-2 font-mono text-[10px] uppercase opacity-70">
                  {issue.level === 'blocker' ? 'стоп' : 'проверьте'}
                </span>
                {issue.message}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-labelledby="bulk-title" className="relative overflow-hidden rounded-2xl border border-slate-800 bg-slate-900 p-4 shadow-lg shadow-slate-950/20">
        <span aria-hidden="true" className="absolute inset-y-0 left-0 w-1 bg-indigo-500" />
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 id="bulk-title" className="text-sm font-semibold text-white">Автономное производство</h2>
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ${watchdogTone}`}
                title={comfyWatchdog
                  ? `${comfyWatchdog.message}${comfyWatchdog.last_error ? ` · ${comfyWatchdog.last_error}` : ''} · перезапусков ${comfyWatchdog.restart_count_window}/${comfyWatchdog.max_restarts}`
                  : 'Статус watchdog недоступен'}
              >
                {watchdogLabel}
              </span>
            </div>
            <p id="bulk-scope" className="mt-1 text-xs text-slate-400">
              Область действия: <span className="font-semibold text-indigo-300">{bulkScopeLabel}</span>
            </p>
            {hasUnsavedChanges && <p id="bulk-dirty" role="status" className="mt-1 text-xs text-amber-300">Сначала сохраните все изменённые карточки ({dirtyCardIds.size}).</p>}
          </div>
          {(isBatchBusy || unattendedActive) && (
            <button
              type="button"
              onClick={() => void requestCancelWork()}
              disabled={cancelRequested}
              className="min-h-10 rounded-lg border border-rose-800 bg-rose-950 px-3 py-2 text-sm font-semibold text-rose-200 transition-colors hover:bg-rose-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-500 disabled:cursor-not-allowed disabled:opacity-45"
              title={unattendedActive
                ? 'Отменить серверную очередь и удалить либо прервать её prompt в ComfyUI.'
                : 'Остановить локальную очередь после текущей карточки.'}
            >
              {cancelRequested ? 'Останавливаю…' : 'Остановить очередь'}
            </button>
          )}
        </div>

        <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          <div>
            <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-indigo-400">Собрать всё</p>
            <div className="flex flex-wrap gap-2">
              <BatchButton primary label="Создать все материалы" targets={batchTargets.route} busy={Boolean(busy)} blocked={hasUnsavedChanges || Boolean(materialPreflight?.blockers)} blockedHint={materialPreflight?.blockers ? 'Сначала устраните блокирующие замечания проверки плана.' : undefined} onClick={() => void startUnattendedMaterials()} hint="Серверная автономная очередь: две попытки на материал, затем безопасный Ken Burns. Страницу можно закрыть; готовые кадры не перезаписываются." />
              <BatchButton label="Заполнить через ИИ" targets={batchTargets.route} busy={Boolean(busy)} blocked={hasUnsavedChanges} onClick={batchFillGaps} hint="То же самое, но сток пропускается: кадры без видео создаются через ИИ. Найденный сток не трогается." />
            </div>
          </div>
          <div className="border-t border-indigo-900/50 pt-4 lg:border-l lg:border-t-0 lg:pl-4 lg:pt-0">
            <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">По этапам</p>
            <div className="flex flex-wrap gap-2">
              <BatchButton label="Только сток" targets={batchTargets.video} busy={Boolean(busy)} blocked={hasUnsavedChanges} onClick={batchStock} hint="Искать сток-видео для кадров без видео, игнорируя назначенный им способ." />
              <BatchButton label="Только картинки" targets={batchTargets.image} busy={Boolean(busy)} blocked={hasUnsavedChanges} onClick={() => batchGenerate('image')} hint="Создать только изображения в Krea, без анимации." />
              <BatchButton label="Только видео" targets={batchTargets.video} busy={Boolean(busy)} blocked={hasUnsavedChanges} onClick={() => batchGenerate('video')} hint="Создать видео (LTX или MiniMax H3, см. настройки) из уже готовых изображений." />
              <BatchButton label="Финальное качество" targets={batchTargets.finalize} busy={Boolean(busy)} blocked={hasUnsavedChanges} onClick={batchFinalize} hint="Перерендерить черновые видеоклипы в полном качестве." />
            </div>
          </div>
          <div className="border-t border-slate-800 pt-4 lg:col-span-2">
            <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">Монтаж</p>
            <div className="flex flex-wrap gap-2">
              <BatchButton danger label="Убрать из финала" targets={batchTargets.exclude} busy={Boolean(busy)} blocked={hasUnsavedChanges} onClick={batchExclude} hint={`Убрать из финального видео ${bulkScopeLabel}. Время перейдёт соседним планам, длина ролика не изменится, действие обратимо.`} />
            </div>
          </div>
        </div>

        {stockProgress && (
          <div role="status" aria-live="polite" className="mt-4 rounded-xl border border-teal-800/60 bg-teal-950/40 px-3 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="flex min-w-0 items-center gap-2 font-semibold text-teal-200">
                <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-teal-400 motion-safe:animate-pulse" />
                <span className="truncate">{stockProgress.label}: {stockProgress.done} из {stockProgress.total}</span>
              </span>
              <span className="font-mono text-xs text-slate-400">{batchProgressPercent}%</span>
              {cancelRequested && <span className="w-full text-xs text-rose-300">Остановка после текущей карточки…</span>}
            </div>
            <div role="progressbar" aria-label={stockProgress.label} aria-valuemin={0} aria-valuemax={stockProgress.total} aria-valuenow={stockProgress.done} aria-valuetext={`${stockProgress.done} из ${stockProgress.total}`} className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800">
              <div className="h-full rounded-full bg-teal-500 transition-[width]" style={{ width: `${batchProgressPercent}%` }} />
            </div>
          </div>
        )}
        {!stockProgress && materialRun && materialRun.status !== 'not_started' && (
          <div role="status" aria-live="polite" className={`mt-4 rounded-xl border px-3 py-3 ${
            materialRun.status === 'failed'
              ? 'border-rose-800/60 bg-rose-950/35'
              : materialRun.status === 'succeeded'
                ? 'border-emerald-800/60 bg-emerald-950/35'
                : unattendedWaiting
                  ? 'border-amber-800/60 bg-amber-950/30'
                  : 'border-sky-800/60 bg-sky-950/35'
          }`}>
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="font-semibold text-slate-200">
                {unattendedRunning
                  ? `Кадр ${materialRun.current?.shot_index || '—'} · ${materialRun.current?.phase || 'подготовка'}`
                  : unattendedWaiting
                    ? `Ожидание восстановления ComfyUI · кадр ${materialRun.current?.shot_index || '—'}`
                  : materialRun.status === 'succeeded'
                    ? 'Все материалы готовы'
                    : materialRun.status === 'failed'
                      ? 'Сборка остановлена'
                      : 'Сборка прервана'}
              </span>
              <span className="font-mono text-xs text-slate-400">{unattendedPercent}%</span>
              <span className="w-full text-xs text-slate-500">
                Операций: {unattendedCompleted}/{unattendedTotal}
                {' · '}повторов: {materialRun.summary?.retries || 0}
                {' · '}резервных кадров: {materialRun.summary?.fallbacks || 0}
              </span>
              {materialRun.error && <span className="w-full text-xs text-rose-300">{materialRun.error}</span>}
            </div>
            <div role="progressbar" aria-label="Автономная сборка материалов" aria-valuemin={0} aria-valuemax={100} aria-valuenow={unattendedPercent} className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800">
              <div className="h-full rounded-full bg-sky-500 transition-[width]" style={{ width: `${unattendedPercent}%` }} />
            </div>
          </div>
        )}
      </section>

      <section aria-label="Поиск и фильтры" className="rounded-xl border border-slate-800 bg-slate-900 p-3">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-[minmax(220px,1fr)_220px_220px_auto_auto] xl:items-end">
          <label className="block min-w-0">
            <span className="mb-1 block text-[11px] font-medium text-slate-500">Поиск по описанию и промптам</span>
            <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Введите текст…" className="h-10 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-white outline-none placeholder:text-slate-700 focus:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30" />
          </label>
          <label className="block min-w-0">
            <span className="mb-1 block text-[11px] font-medium text-slate-500">Статус</span>
            <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)} className="h-10 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-white outline-none focus:border-indigo-500">
              {STATUSES.map((status) => <option key={status} value={status}>{STATUS_LABELS[status] || status}</option>)}
            </select>
          </label>
          <label className="block min-w-0">
            <span className="mb-1 block text-[11px] font-medium text-slate-500">Способ показа</span>
            <select value={filterRoute} onChange={(e) => setFilterRoute(e.target.value)} className="h-10 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-white outline-none focus:border-indigo-500">
              {ROUTES.map((route) => <option key={route} value={route}>{ROUTE_FILTER_LABELS[route] || route}</option>)}
            </select>
          </label>
          <div className="flex h-10 items-stretch gap-2">
            <div role="group" aria-label="Вид отображения" className="flex overflow-hidden rounded-lg border border-slate-700">
              <button type="button" aria-pressed={viewMode === 'cards'} onClick={() => setViewMode('cards')} className={`px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-500 ${viewMode === 'cards' ? 'bg-indigo-950/60 text-indigo-300' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`} title="Карточки с превью — для оценки того, что получилось">
                Карточки
              </button>
              <button type="button" aria-pressed={viewMode === 'list'} onClick={() => setViewMode('list')} className={`border-l border-slate-700 px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-500 ${viewMode === 'list' ? 'bg-indigo-950/60 text-indigo-300' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`} title="Плотный список — для управления: где дыры, черновики и что требует решения">
                Список
              </button>
            </div>
            {viewMode === 'cards' && (
              <button type="button" aria-pressed={compactMode} onClick={() => setCompactMode((value) => !value)} className={`rounded-lg border px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 ${compactMode ? 'border-indigo-700 bg-indigo-950/60 text-indigo-300' : 'border-slate-700 bg-slate-800 text-slate-300 hover:bg-slate-700'}`} title="Переключить плотность карточек">
                {compactMode ? 'Обычный' : 'Компактный'}
              </button>
            )}
            {viewMode === 'list' && (
              <label className="flex items-center">
                <span className="sr-only">Порядок</span>
                <select value={sortMode} onChange={(e) => setSortMode(e.target.value as typeof sortMode)} className="h-full rounded-lg border border-slate-700 bg-slate-800 px-2 text-sm text-slate-200 outline-none focus:border-indigo-500" title="Порядок строк">
                  <option value="shot">По номеру кадра</option>
                  <option value="problems">Сначала проблемные</option>
                  <option value="duration">По длительности</option>
                </select>
              </label>
            )}
          </div>
          <div className="flex h-10 gap-2">
            {hasActiveFilters && <button type="button" onClick={() => { setQuery(''); setFilterStatus('all'); setFilterRoute('all') }} className="rounded-lg border border-slate-700 px-3 text-sm text-slate-400 hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500">Сбросить</button>}
            <button type="button" onClick={toggleVisible} className="whitespace-nowrap rounded-lg border border-slate-700 bg-slate-800 px-3 text-sm font-medium text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500">
              {allVisibleSelected ? 'Снять видимые' : 'Выбрать видимые'}
            </button>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-slate-800 pt-2 text-xs text-slate-500">
          <span>Показано: <strong className="font-semibold text-slate-300">{visibleIndexes.length}</strong> из {cards.length}</span>
          <span>Выбрано: <strong className="font-semibold text-indigo-300">{checkedIndexes.length}</strong></span>
        </div>
      </section>

      {boardError && hasLoadedBoardRef.current && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-800/70 bg-amber-950/30 px-4 py-3 text-sm text-amber-200">
          <span>Не удалось обновить материалы. Показана последняя успешно загруженная версия. <span className="font-mono text-xs">{boardError}</span></span>
          <button type="button" onClick={() => { setBoardLoading(true); setBoardError(null); void load() }} className="rounded-lg border border-amber-700 px-3 py-1.5 font-semibold hover:bg-amber-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400">Повторить</button>
        </div>
      )}

      <div className="grid min-w-0 grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(380px,420px)]">
        <section ref={materialsListRef} aria-labelledby="materials-list-title" aria-busy={boardLoading} className="order-2 min-w-0 scroll-mt-4 rounded-xl border border-slate-800 bg-slate-900 xl:order-1">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-3">
            <div>
              <h2 id="materials-list-title" className="text-sm font-semibold text-white">Список материалов</h2>
              <p className="mt-0.5 text-xs text-slate-500">Предпросмотр загружается только для выбранной карточки.</p>
            </div>
            <span className="rounded-full bg-slate-800 px-2.5 py-1 text-xs font-medium text-slate-400">{boardLoading && hasLoadedBoardRef.current ? 'Обновляю…' : visibleIndexes.length}</span>
          </div>

          <div className="p-3 xl:max-h-[calc(100vh-8rem)] xl:overflow-y-auto">
            {boardLoading && !hasLoadedBoardRef.current && (
              <div role="status" aria-live="polite" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <span className="sr-only">Загружаю материалы…</span>
                {Array.from({ length: 6 }, (_, index) => (
                  <div key={index} aria-hidden="true" className="h-36 rounded-xl border border-slate-800 bg-slate-950/50 p-3 motion-safe:animate-pulse">
                    <div className="h-4 w-24 rounded bg-slate-800" />
                    <div className="mt-4 h-3 w-full rounded bg-slate-800" />
                    <div className="mt-2 h-3 w-2/3 rounded bg-slate-800" />
                  </div>
                ))}
              </div>
            )}

            {!boardLoading && boardError && !hasLoadedBoardRef.current && (
              <div role="alert" className="flex min-h-64 flex-col items-center justify-center rounded-xl border border-dashed border-rose-900 bg-rose-950/20 px-6 text-center">
                <div aria-hidden="true" className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-rose-950 text-rose-300">!</div>
                <h3 className="font-semibold text-white">Материалы не загрузились</h3>
                <p className="mt-1 max-w-md text-sm leading-6 text-slate-400">{boardError}</p>
                <button type="button" onClick={() => { setBoardLoading(true); setBoardError(null); void load() }} className="mt-4 min-h-10 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400">Повторить загрузку</button>
              </div>
            )}

            {!boardLoading && hasLoadedBoardRef.current && !boardError && !cards.length && (
              <div className="flex min-h-64 flex-col items-center justify-center rounded-xl border border-dashed border-slate-700 bg-slate-950/30 px-6 text-center">
                <div aria-hidden="true" className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-slate-800 text-slate-500">0</div>
                <h3 className="font-semibold text-white">Материалов пока нет</h3>
                <p className="mt-1 max-w-md text-sm leading-6 text-slate-500">Сначала создайте список кадров — после этого здесь появятся карточки для генерации и подбора медиа.</p>
              </div>
            )}

            {hasLoadedBoardRef.current && cards.length > 0 && !visibleIndexes.length && (
              <div className="flex min-h-52 flex-col items-center justify-center rounded-xl border border-dashed border-slate-700 bg-slate-950/30 px-6 text-center">
                <h3 className="font-semibold text-white">Ничего не найдено</h3>
                <p className="mt-1 text-sm text-slate-500">Измените запрос или сбросьте фильтры.</p>
                <button type="button" onClick={() => { setQuery(''); setFilterStatus('all'); setFilterRoute('all') }} className="mt-4 min-h-10 rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500">Сбросить фильтры</button>
              </div>
            )}

            {hasLoadedBoardRef.current && orderedIndexes.length > 0 && viewMode === 'list' && (
              <ul className="min-w-0 divide-y divide-slate-800/70 overflow-hidden rounded-lg border border-slate-800">
                {orderedIndexes.map((index) => {
                  const card = cards[index]
                  const rawStatus = card.status || 'planned'
                  const color = STATUS_COLORS[rawStatus] || '#64748b'
                  const media = bestMedia(card)
                  const excluded = isExcluded(card)
                  const isSelected = selectedIndex === index
                  const isDirty = dirtyCardIds.has(card.card_id)
                  const isConflict = conflictedCardIds.has(card.card_id)
                  const seconds = cardSeconds(card)
                  return (
                    <li key={`row-${card.card_id}`}>
                      <div
                        role="button"
                        tabIndex={0}
                        aria-current={isSelected ? 'true' : undefined}
                        onClick={() => selectCard(index)}
                        onKeyDown={(event) => {
                          // Keys bubbling from the row's own checkbox and final
                          // toggle must keep their default action.
                          if (event.target !== event.currentTarget) return
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault()
                            selectCard(index)
                          }
                        }}
                        className={`flex min-w-0 cursor-pointer items-center gap-2 px-2 py-1.5 text-xs transition-colors ${
                          isSelected ? 'bg-indigo-950/40' : 'hover:bg-slate-800/50'
                        } ${excluded ? 'opacity-55 saturate-50' : ''}`}
                      >
                        <label onClick={(event) => event.stopPropagation()} className="flex shrink-0 cursor-pointer items-center" title={`Выбрать кадр ${card.shot_index} для пакетной операции`}>
                          <input
                            type="checkbox"
                            aria-label={`Выбрать кадр ${card.shot_index}`}
                            checked={checked.has(card.card_id)}
                            onChange={(event) => {
                              setChecked((prev) => {
                                const next = new Set(prev)
                                if (event.target.checked) next.add(card.card_id)
                                else next.delete(card.card_id)
                                return next
                              })
                            }}
                            className="h-4 w-4 accent-indigo-500"
                          />
                        </label>
                        <span aria-hidden="true" className="h-6 w-1 shrink-0 rounded-full" style={{ backgroundColor: color }} />
                        <span className="w-24 shrink-0 font-mono text-slate-300" title={`Сцена ${card.scene_index}, кадр ${card.shot_index}`}>
                          #{card.shot_index}<span className="text-slate-600"> · с{card.scene_index}</span>
                        </span>
                        <span className="w-12 shrink-0 text-right font-mono text-slate-500" title="Длительность на экране">{seconds}с</span>
                        <span className="w-24 shrink-0 truncate text-slate-400" title={`Способ: ${card.route || 'не задан'}`}>
                          {ROUTE_FILTER_LABELS[card.route || ''] || ROUTE_LABELS[card.route || ''] || '—'}
                        </span>
                        <span className="w-28 shrink-0 truncate" style={{ color }} title={`Статус: ${rawStatus}`}>
                          {STATUS_LABELS[rawStatus] || rawStatus}
                        </span>
                        <span className={`w-16 shrink-0 ${media ? 'text-emerald-400' : 'text-rose-400'}`} title={media ? 'Медиа готово' : 'Медиа не создано'}>
                          {media ? (media.type === 'video' ? 'видео' : 'кадр') : 'нет'}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-slate-500" title={card.description || card.image_prompt}>
                          {card.description || card.image_prompt || 'Без описания'}
                        </span>
                        <span className="flex shrink-0 items-center gap-1.5">
                          {card.ltx_draft && <span className="text-amber-400" title="Черновой рендер видео">черн.</span>}
                          {isWeakQa(card) && <span className="text-amber-300" title={`Оценка качества ${(card.qa_score ?? 0).toFixed(2)}`}>QA</span>}
                          {card.text_overlay_enabled && <span className="text-cyan-300" title="Есть текст поверх видео">текст</span>}
                          {isConflict ? <span className="text-rose-300" title="Конфликт правок">конфликт</span> : isDirty && <span className="text-amber-300" title="Есть несохранённые правки">не сохр.</span>}
                        </span>
                        <span role="presentation" onClick={(event) => event.stopPropagation()} className="shrink-0">
                          <FinalToggle
                            on={!excluded}
                            disabled={Boolean(busy) || isDirty}
                            title={isDirty ? 'Сначала сохраните правки карточки' : excluded ? 'Вернуть кадр в финальное видео' : 'Убрать кадр из финала: его время перейдёт соседнему плану'}
                            onToggle={() => toggleExclude(index)}
                          />
                        </span>
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}

            {hasLoadedBoardRef.current && orderedIndexes.length > 0 && viewMode === 'cards' && (
              <div className={`grid min-w-0 gap-2 ${compactMode ? 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-2 2xl:grid-cols-3' : 'grid-cols-1 sm:grid-cols-2'}`}>
                {orderedIndexes.map((index) => {
                  const card = cards[index]
                  const media = bestMedia(card)
                  const rawStatus = card.status || 'planned'
                  const color = STATUS_COLORS[rawStatus] || '#64748b'
                  const routeCode = card.route || '—'
                  const routeLabel = ROUTE_FILTER_LABELS[routeCode] || ROUTE_LABELS[routeCode] || 'Не задан'
                  const mediaLabel = media?.type === 'video' ? 'Видео готово' : media?.type === 'image' ? 'Картинка готова' : 'Медиа не создано'
                  const isSelected = selectedIndex === index
                  const isDirty = dirtyCardIds.has(card.card_id)
                  const isConflict = conflictedCardIds.has(card.card_id)
                  return (
                    <article
                      role="group"
                      aria-label={`Кадр ${card.shot_index}. ${STATUS_LABELS[rawStatus] || rawStatus}`}
                      aria-current={isSelected ? 'true' : undefined}
                      key={`${card.scene_index}-${card.shot_index}-${index}`}
                      onClick={() => selectCard(index)}
                      className={`min-w-0 cursor-pointer rounded-xl border text-left transition-colors ${compactMode ? 'p-2.5' : 'p-3.5'} ${
                        isSelected ? 'border-indigo-500/80 bg-indigo-950/30' : 'border-slate-800 bg-slate-950/45 hover:border-slate-700 hover:bg-slate-800/50'
                      } ${isExcluded(card) ? 'opacity-60 saturate-50' : ''}`}
                    >
                      <div className="flex items-start gap-2.5">
                        <label onClick={(event) => event.stopPropagation()} className="flex min-h-9 shrink-0 cursor-pointer items-start pt-0.5" title={`Выбрать кадр ${card.shot_index} для пакетной операции`}>
                          <input
                            type="checkbox"
                            aria-label={`Выбрать кадр ${card.shot_index}`}
                            checked={checked.has(card.card_id)}
                            onChange={(event) => {
                              setChecked((prev) => {
                                const next = new Set(prev)
                                if (event.target.checked) next.add(card.card_id)
                                else next.delete(card.card_id)
                                return next
                              })
                            }}
                            className="mt-0.5 h-4 w-4 accent-indigo-500"
                          />
                        </label>
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-start justify-between gap-2">
                            <div className="min-w-0">
                              <button type="button" onClick={(event) => { event.stopPropagation(); selectCard(index) }} className="rounded-sm font-mono text-sm font-semibold text-white underline-offset-4 hover:text-indigo-300 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500" aria-label={`Открыть редактор кадра ${card.shot_index}`}>Кадр #{card.shot_index}</button>
                              <span className="ml-2 text-[11px] text-slate-600">Сцена {card.scene_index}</span>
                            </div>
                            <span className="shrink-0 rounded-md px-2 py-0.5 text-[10px] font-semibold" style={{ backgroundColor: `${color}22`, color }} title={`Технический статус: ${rawStatus}`}>
                              {STATUS_LABELS[rawStatus] || rawStatus}
                            </span>
                          </div>
                          <p className={`mt-2 text-sm leading-5 text-slate-200 ${compactMode ? 'truncate' : 'line-clamp-2'}`} title={card.description || card.screen_text || card.scene_title || 'Без описания'}>
                            {card.description || card.screen_text || card.scene_title || 'Без описания'}
                          </p>
                          {!compactMode && <p className="mt-1 truncate text-xs text-slate-600" title={card.image_prompt || card.video_prompt || card.asset_query}>{card.image_prompt || card.video_prompt || card.asset_query || 'Промпт не задан'}</p>}
                        </div>
                      </div>

                      <div className={`mt-2 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 border-t border-slate-800/80 pt-2 text-[10px] ${compactMode ? '' : 'sm:text-[11px]'}`}>
                        <span className="truncate text-slate-400" title={`Способ: ${routeCode}`}>{routeLabel}</span>
                        <span aria-hidden="true" className="text-slate-800">•</span>
                        <span className={media ? 'text-emerald-400' : 'text-slate-600'}>{mediaLabel}</span>
                        {card.ltx_draft && <span className="text-amber-400" title="Отрендерено в черновом режиме (LTX — один проход, H3 — турбо-LoRA)">Черновик</span>}
                        {(card.visual_type === 'web_reference_card' || card.route === 'reference') && <span className="text-sky-300" title={card.reference_url || card.reference_query || 'Карточка источника'}>Источник</span>}
                        {card.text_overlay_enabled && <span className="text-cyan-300" title={card.text_overlay_text || card.screen_text || 'Текст поверх видео'}>Есть текст</span>}
                        {isConflict ? <span className="font-medium text-rose-300">Конфликт правок</span> : isDirty && <span className="font-medium text-amber-300">Не сохранено</span>}
                      </div>

                      <div role="presentation" onClick={(event) => event.stopPropagation()} className="mt-2 flex flex-wrap items-center justify-between gap-1.5 border-t border-slate-800/80 pt-2">
                        <FinalToggle
                          on={!isExcluded(card)}
                          disabled={Boolean(busy) || isDirty}
                          title={isDirty ? 'Сначала сохраните правки карточки' : isExcluded(card) ? 'Вернуть кадр в финальное видео' : 'Убрать кадр из финала: его время перейдёт соседнему плану, файлы сохранятся'}
                          onToggle={() => toggleExclude(index)}
                        />
                        <div className="flex flex-wrap justify-end gap-1.5">
                          <button type="button" onClick={() => generate(index, 'route')} disabled={Boolean(busy)} className="min-h-9 rounded-lg bg-indigo-600 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45" title="Создать материал по назначенному способу">Создать</button>
                          {card.ltx_draft ? (
                            <button type="button" onClick={() => finalize(index)} disabled={Boolean(busy)} title="Перерендерить в финальном качестве" className="min-h-9 rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs font-semibold text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 disabled:cursor-not-allowed disabled:opacity-45">Финал</button>
                          ) : (
                            <button type="button" onClick={() => pickStock(index)} disabled={Boolean(busy)} title="Подобрать сток-видео" className="min-h-9 rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs font-semibold text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 disabled:cursor-not-allowed disabled:opacity-45">Сток</button>
                          )}
                        </div>
                      </div>
                    </article>
                  )
                })}
              </div>
            )}
          </div>
        </section>

        <aside ref={editorRef} aria-label="Редактор выбранного материала" className="order-1 h-fit min-w-0 scroll-mt-4 rounded-xl border border-slate-800 bg-slate-900 xl:order-2 xl:sticky xl:top-4 xl:max-h-[calc(100vh-2rem)] xl:overflow-y-auto">
          {selected ? (
            <div className="min-w-0 space-y-4 p-4">
              <header className="flex min-w-0 items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-lg font-semibold text-white">Кадр #{selected.shot_index}</h2>
                    {selectedConflict
                      ? <span role="alert" className="rounded-md bg-rose-950 px-2 py-0.5 text-[10px] font-semibold text-rose-300">Конфликт правок</span>
                      : selectedDirty && <span role="status" className="rounded-md bg-amber-950 px-2 py-0.5 text-[10px] font-semibold text-amber-300">Есть несохранённые правки</span>}
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">Сцена {selected.scene_index} · {selected.start}–{selected.end}</p>
                  <code className="mt-1 block truncate text-[10px] text-slate-700" title={selected.card_id}>ID: {selected.card_id}</code>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-2">
                  <span className="rounded-md px-2 py-1 text-xs font-semibold" style={{ backgroundColor: `${STATUS_COLORS[selected.status] || '#64748b'}22`, color: STATUS_COLORS[selected.status] || '#94a3b8' }} title={`Технический статус: ${selected.status}`}>
                    {STATUS_LABELS[selected.status] || selected.status}
                  </span>
                  <button type="button" onClick={() => scrollToSection(materialsListRef.current)} className="rounded-md text-xs font-medium text-indigo-300 underline-offset-4 hover:text-indigo-200 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 xl:hidden">
                    К списку
                  </button>
                </div>
              </header>

              {selectedConflict && (
                <div role="alert" className="rounded-xl border border-rose-800/70 bg-rose-950/35 p-3 text-sm text-rose-100">
                  <p className="font-semibold">Эти же поля изменились на сервере</p>
                  <p className="mt-1 text-xs leading-5 text-rose-200/75">Выберите, оставить локальные правки или заменить их последней серверной версией. Медиа и остальные поля не будут затронуты.</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button type="button" onClick={() => void saveSelected(true)} disabled={Boolean(busy)} className="min-h-9 rounded-lg bg-rose-700 px-3 py-1.5 text-xs font-semibold text-white hover:bg-rose-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 disabled:cursor-not-allowed disabled:opacity-45">Сохранить мои правки</button>
                    <button type="button" onClick={discardSelectedChanges} disabled={Boolean(busy)} className="min-h-9 rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs font-semibold text-slate-100 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 disabled:cursor-not-allowed disabled:opacity-45">Принять серверную версию</button>
                  </div>
                </div>
              )}

              <section aria-label="Предпросмотр выбранного материала" className="overflow-hidden rounded-xl border border-slate-800 bg-slate-950">
                <div className="flex aspect-video items-center justify-center overflow-hidden">
                  {selectedMedia?.type === 'video' && (
                    <video
                      key={`${selectedMedia.path}-${mediaVersion}`}
                      src={mediaUrl(selectedMedia.path, mediaVersion)}
                      poster={selected.image_output ? mediaUrl(selected.image_output, mediaVersion) : undefined}
                      controls
                      playsInline
                      aria-label={`Предпросмотр видео для кадра ${selected.shot_index}`}
                      className="h-full w-full object-contain"
                    />
                  )}
                  {selectedMedia?.type === 'image' && (
                    <img key={`${selectedMedia.path}-${mediaVersion}`} src={mediaUrl(selectedMedia.path, mediaVersion)} alt={`Предпросмотр изображения для кадра ${selected.shot_index}`} className="h-full w-full object-contain" />
                  )}
                  {!selectedMedia && (
                    <div className="px-6 text-center">
                      <div aria-hidden="true" className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-full bg-slate-900 text-slate-600">+</div>
                      <p className="text-sm font-medium text-slate-400">Медиа ещё не создано</p>
                      <p className="mt-1 text-xs text-slate-700">Запустите создание или подберите сток.</p>
                    </div>
                  )}
                </div>
                {selectedMedia && <p className="truncate border-t border-slate-800 px-3 py-2 font-mono text-[10px] text-slate-600" title={selectedMedia.path}>{variantFileName(selectedMedia.path)}</p>}
              </section>

              <section aria-label="Создание медиа" className="space-y-2">
                <div className="grid grid-cols-3 gap-2">
                  <button type="button" onClick={() => selectedIndex !== null && generate(selectedIndex, 'route')} disabled={Boolean(busy)} className="min-h-10 rounded-lg bg-indigo-600 px-2 py-2 text-xs font-semibold text-white hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45" title="Создать материал по выбранному способу показа">Создать</button>
                  <button type="button" onClick={() => selectedIndex !== null && generate(selectedIndex, 'image')} disabled={Boolean(busy)} className="min-h-10 rounded-lg border border-slate-700 bg-slate-800 px-2 py-2 text-xs font-semibold text-slate-100 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45">Картинка</button>
                  <button type="button" onClick={() => selectedIndex !== null && generate(selectedIndex, 'video')} disabled={Boolean(busy)} className="min-h-10 rounded-lg border border-slate-700 bg-slate-800 px-2 py-2 text-xs font-semibold text-slate-100 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45">Видео</button>
                </div>
                <button type="button" onClick={() => selectedIndex !== null && pickStock(selectedIndex)} disabled={Boolean(busy)} className="min-h-10 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 disabled:cursor-not-allowed disabled:opacity-45">Подобрать сток-видео</button>
              </section>

              {(selected.source_url || selected.source_license || selected.source_author) && (
                <section aria-label="Источник стокового материала" className="rounded-lg border border-teal-900/60 bg-teal-950/20 px-3 py-2.5 text-[11px] text-slate-400">
                  <p>
                    Сток{selected.stock_score !== undefined ? ` · оценка ${selected.stock_score.toFixed(2)}` : ''}
                    {safeExternalUrl(selected.source_url) ? <> · <a href={safeExternalUrl(selected.source_url)} target="_blank" rel="noopener noreferrer" className="font-medium text-teal-400 underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400">Открыть источник</a></> : null}
                  </p>
                  <p className="mt-1 break-words">{selected.source_license || 'Лицензия не зафиксирована'}{selected.source_author ? ` · Автор: ${selected.source_author}` : ''}</p>
                </section>
              )}

              <details open className="rounded-lg border border-slate-800 bg-slate-950/40">
                <summary className="cursor-pointer select-none rounded-lg px-3 py-2.5 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500">
                  История вариантов
                  <span className="ml-2 text-xs font-normal text-slate-500">{variantsLoading ? 'загрузка…' : variantHistory?.variants.length || 0}</span>
                </summary>
                <div aria-busy={variantsLoading} className="max-h-64 space-y-2 overflow-y-auto border-t border-slate-800 p-2">
                  {variantsLoading && (
                    <div role="status" aria-live="polite" className="space-y-2 p-1">
                      <span className="sr-only">Загружаю историю вариантов…</span>
                      {[0, 1].map((item) => <div key={item} aria-hidden="true" className="h-16 rounded-lg bg-slate-900 motion-safe:animate-pulse" />)}
                    </div>
                  )}
                  {!variantsLoading && variantsError && (
                    <div role="alert" className="rounded-lg border border-rose-900/70 bg-rose-950/20 p-3 text-xs text-rose-200">
                      <p>Не удалось загрузить историю: {variantsError}</p>
                      <button type="button" onClick={() => selectedIndex !== null && selected && void loadVariants(selectedIndex, selected.card_id)} className="mt-2 rounded-md border border-rose-800 px-2 py-1 font-semibold hover:bg-rose-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400">Повторить</button>
                    </div>
                  )}
                  {!variantsLoading && !variantsError && [...(variantHistory?.variants || [])].reverse().map((variant) => (
                    <div key={variant.id} className={`rounded-lg border p-2.5 ${variant.selected ? 'border-indigo-500/80 bg-indigo-950/30' : 'border-slate-800 bg-slate-950/70'}`}>
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-semibold text-slate-200">{VARIANT_FIELD_LABELS[variant.output_field] || variant.output_field}</span>
                            <code className="text-[10px] text-slate-500" title="Технические источник и режим">{variant.source} · {variant.mode}</code>
                          </div>
                          <div className="mt-1 truncate text-xs text-slate-300" title={variant.file_path}>{variantFileName(variant.file_path)}</div>
                          <div className="mt-1 break-words text-[10px] text-slate-600">
                            {new Date(variant.created_at).toLocaleString('ru-RU')} · {variantSize(variant.file_size)} · {variant.file_hash.slice(0, 10)}{variant.model ? ` · ${variant.model}` : ''}
                          </div>
                        </div>
                        {variant.selected ? (
                          <span className="shrink-0 text-[11px] font-semibold text-emerald-400">Активен</span>
                        ) : (
                          <button type="button" onClick={() => void activateVariant(variant)} disabled={Boolean(busy) || selectingVariant !== null || selectedDirty} title={selectedDirty ? 'Сначала сохраните правки карточки' : 'Сделать этот вариант активным'} className="min-h-8 shrink-0 rounded-md bg-slate-800 px-2 py-1 text-[11px] font-semibold text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45">
                            {selectingVariant === variant.id ? 'Выбираю…' : 'Выбрать'}
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                  {!variantsLoading && !variantsError && !variantHistory?.variants.length && <p className="px-2 py-4 text-center text-xs leading-5 text-slate-600">Варианты появятся после генерации или назначения медиа.</p>}
                </div>
              </details>

              <fieldset disabled={Boolean(busy)} className="min-w-0 space-y-4 transition-opacity disabled:opacity-60">
                <legend className="sr-only">Параметры выбранной карточки</legend>

                <section aria-labelledby="card-settings-title" className="space-y-3 rounded-xl border border-slate-800 bg-slate-950/25 p-3">
                <div>
                  <h3 id="card-settings-title" className="text-sm font-semibold text-white">Параметры карточки</h3>
                  <p className="mt-0.5 text-[11px] text-slate-600">Правки сохраняются кнопкой внизу панели.</p>
                </div>
                <label className="block">
                  <span className="text-xs font-medium text-slate-400">Описание</span>
                  <textarea value={selected.description} onChange={(event) => editCardAt(selectedIndex!, { description: event.target.value })} className="mt-1 h-20 w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30" />
                </label>
                <label className="block">
                  <span className="text-xs font-medium text-slate-400">Способ показа</span>
                  <select value={CARD_ROUTES.includes(selected.route) ? selected.route : 'image+video'} onChange={(event) => editCardAt(selectedIndex!, routePatch(event.target.value))} className="mt-1 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500">
                    {CARD_ROUTES.map((route) => <option key={route} value={route}>{ROUTE_LABELS[route]}</option>)}
                  </select>
                  <span className="mt-1 block text-[11px] leading-4 text-slate-600">Определяет процесс создания: изображение, анимация, сток или карточка источника.</span>
                </label>
                </section>

                <section aria-labelledby="overlay-settings-title" className="space-y-3 rounded-xl border border-slate-800 bg-slate-950/25 p-3">
                <label className="flex min-h-10 cursor-pointer items-center justify-between gap-3">
                  <span id="overlay-settings-title" className="block text-sm font-semibold text-white">Текст поверх видео</span>
                  <input type="checkbox" checked={Boolean(selected.text_overlay_enabled)} onChange={(event) => editCardAt(selectedIndex!, { text_overlay_enabled: event.target.checked, text_overlay_text: selected.text_overlay_text || selected.screen_text })} className="h-4 w-4 accent-indigo-500" />
                </label>
                {selected.text_overlay_enabled && (
                  <>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-400">Текст</span>
                      <textarea value={selected.text_overlay_text || selected.screen_text || ''} onChange={(event) => editCardAt(selectedIndex!, { text_overlay_text: event.target.value })} className="mt-1 h-16 w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30" />
                    </label>
                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
                      <label className="block min-w-0">
                        <span className="text-xs text-slate-500">Стиль</span>
                        <select value={selected.text_overlay_style || 'subtitle'} onChange={(event) => editCardAt(selectedIndex!, { text_overlay_style: event.target.value })} className="mt-1 min-h-10 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500">
                          {TEXT_STYLES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                        </select>
                      </label>
                      <label className="block min-w-0">
                        <span className="text-xs text-slate-500">Позиция</span>
                        <select value={selected.text_overlay_position || 'bottom'} onChange={(event) => editCardAt(selectedIndex!, { text_overlay_position: event.target.value })} className="mt-1 min-h-10 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500">
                          {TEXT_POSITIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                        </select>
                      </label>
                    </div>
                  </>
                )}
                </section>

                <section aria-labelledby="prompts-title" className="space-y-3 rounded-xl border border-slate-800 bg-slate-950/25 p-3">
                <h3 id="prompts-title" className="text-sm font-semibold text-white">Промпты генерации</h3>
                <div>
                  <div className="mb-1 flex items-center justify-between gap-3 text-xs font-medium text-slate-400">
                    <label htmlFor="asset-image-prompt">Для изображения</label>
                    <button type="button" onClick={() => selectedIndex !== null && regeneratePrompt(selectedIndex, 'image_prompt')} disabled={Boolean(busy) || selectedDirty} title={selectedDirty ? 'Сначала сохраните правки карточки' : 'Перегенерировать промпт изображения'} className="min-h-8 rounded-md border border-slate-700 bg-slate-800 px-2 py-1 text-[11px] font-semibold text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45">Улучшить</button>
                  </div>
                  <textarea id="asset-image-prompt" value={selected.image_prompt} onChange={(event) => editCardAt(selectedIndex!, { image_prompt: event.target.value })} className="h-28 w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30" />
                </div>
                <div>
                  <div className="mb-1 flex items-center justify-between gap-3 text-xs font-medium text-slate-400">
                    <label htmlFor="asset-video-prompt">Для видео</label>
                    <button type="button" onClick={() => selectedIndex !== null && regeneratePrompt(selectedIndex, 'video_prompt')} disabled={Boolean(busy) || selectedDirty} title={selectedDirty ? 'Сначала сохраните правки карточки' : 'Перегенерировать промпт видео'} className="min-h-8 rounded-md border border-slate-700 bg-slate-800 px-2 py-1 text-[11px] font-semibold text-slate-200 hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45">Улучшить</button>
                  </div>
                  <textarea id="asset-video-prompt" value={selected.video_prompt} onChange={(event) => editCardAt(selectedIndex!, { video_prompt: event.target.value })} className="h-28 w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30" />
                </div>
                </section>

                <section aria-labelledby="timing-title" className="space-y-3 rounded-xl border border-slate-800 bg-slate-950/25 p-3">
                <h3 id="timing-title" className="text-sm font-semibold text-white">Тайминг и заметки</h3>
                <div className="grid grid-cols-2 gap-3">
                  <label className="block min-w-0">
                    <span className="text-xs text-slate-500">Начало</span>
                    <input value={selected.start} onChange={(event) => editCardAt(selectedIndex!, { start: event.target.value })} className="mt-1 min-h-10 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-white outline-none focus:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30" />
                  </label>
                  <label className="block min-w-0">
                    <span className="text-xs text-slate-500">Конец</span>
                    <input value={selected.end} onChange={(event) => editCardAt(selectedIndex!, { end: event.target.value })} className="mt-1 min-h-10 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-white outline-none focus:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30" />
                  </label>
                </div>
                <label className="block">
                  <span className="text-xs text-slate-500">Заметки</span>
                  <textarea value={selected.notes} onChange={(event) => editCardAt(selectedIndex!, { notes: event.target.value })} className="mt-1 h-20 w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30" />
                </label>
                </section>
              </fieldset>

              <div className="sticky -bottom-4 -mx-4 -mb-4 flex flex-wrap gap-2 border-t border-slate-800 bg-slate-900/95 p-4 backdrop-blur">
                <button type="button" onClick={() => void saveSelected()} disabled={Boolean(busy) || !selectedDirty || selectedConflict} title={selectedConflict ? 'Сначала разрешите конфликт правок' : selectedDirty ? 'Сохранить изменения карточки' : 'Нет несохранённых изменений'} className="min-h-10 min-w-32 flex-1 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-45">{busy === 'save' ? 'Сохраняю…' : selectedConflict ? 'Разрешите конфликт' : selectedDirty ? 'Сохранить правки' : 'Сохранено'}</button>
                <FinalToggle
                  size="md"
                  on={!isExcluded(selected)}
                  disabled={Boolean(busy) || selectedDirty}
                  title={selectedDirty
                    ? 'Сначала сохраните правки'
                    : isExcluded(selected)
                      ? 'Вернуть кадр в финальное видео'
                      : (() => {
                          const impact = selectedIndex !== null ? excludeImpact(cards, selectedIndex) : null
                          return impact
                            ? `Убрать из финала: ${impact.gainedSeconds} с перейдут кадру #${impact.recipientShot} (станет ${impact.recipientSeconds} с). Файлы сохранятся.`
                            : 'Убрать кадр из финала: файлы сохранятся, действие обратимо'
                        })()}
                  onToggle={() => selectedIndex !== null && toggleExclude(selectedIndex)}
                />
              </div>
            </div>
          ) : (
            <div className="flex min-h-64 flex-col items-center justify-center px-6 py-16 text-center">
              <div aria-hidden="true" className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-slate-800 text-slate-500">↖</div>
              <h2 className="font-semibold text-white">Материал не выбран</h2>
              <p className="mt-1 max-w-xs text-sm leading-6 text-slate-500">Выберите карточку в списке, чтобы открыть предпросмотр, параметры и историю вариантов.</p>
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
