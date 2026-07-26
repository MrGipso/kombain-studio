import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import { FinalToggle } from '../components/FinalToggle'
import { cardMediaPath, excludeImpact, hasMedia, isExcluded, isIncluded, LONG_SLOT_WARNING_SECONDS } from '../lib/cards'
import type { AssetCard, TimelineMedia, TimelineMediaItem } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

type SequenceEntry = {
  card: AssetCard
  index: number
  start: number
  end: number
  duration: number
}

const TEXT_STYLES = [
  ['subtitle', 'Subtitle'],
  ['title', 'Title'],
  ['caption', 'Caption'],
  ['label', 'Label'],
  ['quote', 'Quote'],
  ['stat', 'Stat'],
  ['terminal', 'Terminal'],
  ['badge', 'Badge'],
  ['warning', 'Warning'],
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

const TEXT_ANIMS = [
  ['fade', 'Fade'],
  ['none', 'None'],
]

function tcToSec(value: string): number {
  const text = String(value || '0').replace(',', '.')
  const parts = text.split(':').map(Number)
  if (parts.length === 3) return (parts[0] || 0) * 3600 + (parts[1] || 0) * 60 + (parts[2] || 0)
  if (parts.length === 2) return (parts[0] || 0) * 60 + (parts[1] || 0)
  return Number(text) || 0
}

function secToTc(value: number): string {
  const safe = Math.max(0, Math.round(value))
  const h = Math.floor(safe / 3600)
  const m = Math.floor((safe % 3600) / 60)
  const s = safe % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function mediaUrl(path: string) {
  return `/api/media/${encodeURIComponent(path)}`
}

function clipMediaPath(card: AssetCard) {
  return cardMediaPath(card)
}

function sourceDuration(card: AssetCard) {
  const start = tcToSec(card.timeline_start || card.start)
  const end = tcToSec(card.timeline_end || card.end)
  return Math.max(1, end - start)
}

function isVideo(path: string) {
  return /\.(mp4|webm|mov|mkv)$/i.test(path)
}

function isImage(path: string) {
  return /\.(png|jpe?g|webp|bmp)$/i.test(path)
}

function overlayText(card: AssetCard | null) {
  if (!card?.text_overlay_enabled) return ''
  return (card.text_overlay_text || card.screen_text || '').trim()
}

function overlayStyleClass(card: AssetCard) {
  const style = card.text_overlay_style || 'subtitle'
  if (style === 'title') return 'text-4xl font-bold tracking-normal max-w-[78%]'
  if (style === 'caption') return 'text-lg font-semibold max-w-[46%] text-left'
  if (style === 'label') return 'text-xl font-bold uppercase tracking-normal max-w-[50%] text-left'
  if (style === 'quote') return 'text-3xl font-semibold italic max-w-[72%]'
  if (style === 'stat') return 'text-4xl font-bold tracking-normal max-w-[46%] bg-black/40 px-4 py-2 rounded-md'
  if (style === 'terminal') return 'text-sm font-mono tracking-normal max-w-[44%] bg-black/50 px-3 py-2 rounded-md text-left'
  if (style === 'badge') return 'text-lg font-bold uppercase tracking-normal max-w-[42%] bg-teal-700/60 px-3 py-1.5 rounded-md text-left'
  if (style === 'warning') return 'text-xl font-bold tracking-normal max-w-[54%] bg-red-800/70 px-4 py-2 rounded-md'
  return 'text-2xl font-semibold max-w-[76%]'
}

function overlayPositionStyle(card: AssetCard): React.CSSProperties {
  const position = card.text_overlay_position || 'bottom'
  if (position === 'manual') {
    return {
      left: `${Math.max(0, Math.min(100, Number(card.text_overlay_x ?? 50)))}%`,
      top: `${Math.max(0, Math.min(100, Number(card.text_overlay_y ?? 86)))}%`,
      transform: 'translate(-50%, -50%)',
    }
  }
  if (position === 'top') return { left: '50%', top: '12%', transform: 'translate(-50%, 0)' }
  if (position === 'center') return { left: '50%', top: '50%', transform: 'translate(-50%, -50%)' }
  if (position === 'left') return { left: '7%', top: '50%', transform: 'translate(0, -50%)' }
  if (position === 'right') return { right: '7%', top: '50%', transform: 'translate(0, -50%)' }
  if (position === 'top_left') return { left: '7%', top: '10%', transform: 'translate(0, 0)' }
  if (position === 'top_right') return { right: '7%', top: '10%', transform: 'translate(0, 0)' }
  if (position === 'bottom_left') return { left: '7%', bottom: '10%', transform: 'translate(0, 0)' }
  if (position === 'bottom_right') return { right: '7%', bottom: '10%', transform: 'translate(0, 0)' }
  return { left: '50%', bottom: '10%', transform: 'translate(-50%, 0)' }
}

function buildSequence(cards: AssetCard[], totalEnd = 0): SequenceEntry[] {
  // Mirror core/export.py tiled_intervals(): every surviving clip runs until
  // the NEXT surviving clip's start, and the last one runs to the end of the
  // narration. Excluding a card therefore stretches its previous neighbour.
  // The old implementation compacted the tail leftwards instead, so this
  // preview lied about the montage exactly when a card was excluded.
  // Deliberate deviation from the exporter: cards without media are kept as
  // placeholder slots — during production that is the useful view.
  const included = cards
    .map((card, index) => ({ card, index, plannedStart: tcToSec(card.timeline_start || card.start) }))
    .filter(({ card }) => !isExcluded(card))
    .sort((a, b) => a.plannedStart - b.plannedStart)
  if (!included.length) return []
  const starts: number[] = []
  for (const entry of included) {
    // The first interval is pinned to 0; later starts stay monotonic with a
    // 1-second minimum slot, exactly like the exporter.
    if (!starts.length) starts.push(0)
    else starts.push(Math.max(entry.plannedStart, starts[starts.length - 1] + 1))
  }
  const plannedEnd = Math.max(...included.map(({ card }) => tcToSec(card.timeline_end || card.end)))
  const end = Math.max(totalEnd > 0 ? totalEnd : plannedEnd, starts[starts.length - 1] + 1)
  return included.map(({ card, index }, i) => {
    const start = starts[i]
    const stop = i + 1 < starts.length ? starts[i + 1] : end
    return { card, index, start, end: stop, duration: stop - start }
  })
}

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

export default function Timeline() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const toast = useToast()
  const [cards, setCards] = useState<AssetCard[]>([])
  const [media, setMedia] = useState<TimelineMedia | null>(null)
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [playhead, setPlayhead] = useState(0)
  const [voiceStart, setVoiceStart] = useState(0)
  const [voiceDuration, setVoiceDuration] = useState(0)
  const [zoom, setZoom] = useState(10)
  const [playing, setPlaying] = useState(false)
  const [saving, setSaving] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const frameRef = useRef<number | null>(null)

  const load = useCallback(async () => {
    const [board, timelineMedia] = await Promise.all([
      api.getAssetBoard(projectId).catch(() => [] as AssetCard[]),
      api.getTimelineMedia(projectId).catch(() => null),
    ])
    setCards(board)
    setMedia(timelineMedia)
    setVoiceStart(tcToSec(timelineMedia?.voice_start || '00:00:00'))
    setSelectedIndex((current) => (board.length && current < board.length ? current : 0))
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  // Tile to the narration end when the audio duration is known — the exported
  // video always matches the voice length, so the preview should too.
  const sequence = useMemo(
    () => buildSequence(cards, voiceDuration > 0 ? voiceStart + voiceDuration : 0),
    [cards, voiceDuration, voiceStart],
  )
  const sequenceByIndex = useMemo(() => new Map(sequence.map((entry) => [entry.index, entry])), [sequence])
  // Excluded cards stay visible on the track as ghost bars at their planned
  // position, so a clip never silently "disappears" — one click selects it and
  // the editor offers the return toggle.
  const ghostClips = useMemo(
    () => cards
      .map((card, index) => ({ card, index, start: tcToSec(card.timeline_start || card.start), duration: sourceDuration(card) }))
      .filter(({ card }) => isExcluded(card)),
    [cards],
  )
  const videoEnd = sequence.length ? sequence[sequence.length - 1].end : 0
  const totalDuration = Math.max(60, videoEnd + 8, voiceStart + Math.max(voiceDuration, 0) + 8)
  const trackWidth = Math.max(960, totalDuration * zoom)
  const selectedCard = cards[selectedIndex] || null

  const currentEntry = useMemo(() => {
    return sequence.find((entry) => playhead >= entry.start && playhead < entry.end) || null
  }, [sequence, playhead])

  const currentCard = currentEntry?.card || (playing ? null : selectedCard)
  const currentSequenceStart = currentEntry?.start ?? sequenceByIndex.get(selectedIndex)?.start ?? 0
  const currentMediaPath = currentCard ? clipMediaPath(currentCard) : ''

  const generatedItems = useMemo(() => media?.items || [], [media])
  const generatedByPath = useMemo(() => {
    const map = new Map<string, TimelineMediaItem>()
    generatedItems.forEach((item) => map.set(item.path, item))
    return map
  }, [generatedItems])

  const stats = useMemo(() => {
    const missing = cards.filter((card) => !clipMediaPath(card)).length
    const generated = cards.length - missing
    const included = cards.filter(isIncluded).length
    return { missing, generated, included }
  }, [cards])

  const updateCard = (index: number, patch: Partial<AssetCard>) => {
    setCards((prev) => {
      if (index < 0 || index >= prev.length) return prev
      const next = [...prev]
      next[index] = { ...next[index], ...patch }
      return next
    })
  }

  const activeAt = useCallback((seconds: number) => {
    const entry = sequence.find((item) => seconds >= item.start && seconds < item.end)
    return entry?.index ?? -1
  }, [sequence])

  const syncPreviewVideo = useCallback((seconds: number, shouldPlay = playing) => {
    const video = videoRef.current
    if (!video || !currentMediaPath || !isVideo(currentMediaPath) || !currentCard) return
    const hasDuration = Number.isFinite(video.duration) && video.duration > 0
    const clipDuration = sourceDuration(currentCard)
    const fitRate = hasDuration ? Math.min(4, Math.max(0.25, video.duration / clipDuration)) : 1
    const clipTime = Math.max(0, seconds - currentSequenceStart)
    const sourceTime = clipTime * fitRate
    const lastFrameTime = hasDuration ? Math.max(0, video.duration - 0.05) : sourceTime
    const pastSourceEnd = hasDuration && sourceTime >= lastFrameTime
    const targetTime = pastSourceEnd ? lastFrameTime : sourceTime
    const drift = Math.abs(video.currentTime - targetTime)
    if (Math.abs(video.playbackRate - fitRate) > 0.02) video.playbackRate = fitRate
    if (!shouldPlay || pastSourceEnd || drift > 0.55) video.currentTime = targetTime
    if (pastSourceEnd) video.pause()
    else if (shouldPlay && video.paused) video.play().catch(() => {})
    else if (!shouldPlay) video.pause()
  }, [currentCard, currentMediaPath, currentSequenceStart, playing])

  const seek = useCallback((seconds: number) => {
    const next = Math.max(0, Math.min(totalDuration, seconds))
    setPlayhead(next)
    const active = activeAt(next)
    if (active >= 0) setSelectedIndex(active)
    const audio = audioRef.current
    if (audio && media?.voice_url) {
      const audioTime = next - voiceStart
      if (audioTime >= 0 && audioTime <= (voiceDuration || audio.duration || Number.MAX_SAFE_INTEGER)) {
        audio.currentTime = audioTime
      }
    }
  }, [activeAt, media?.voice_url, totalDuration, voiceDuration, voiceStart])

  const stopFrameLoop = useCallback(() => {
    if (frameRef.current) cancelAnimationFrame(frameRef.current)
    frameRef.current = null
  }, [])

  const runFrameLoop = useCallback(() => {
    stopFrameLoop()
    let lastUiUpdate = 0
    let lastActive = -1
    const tick = (timestamp: number) => {
      const audio = audioRef.current
      if (audio && !audio.paused) {
        const next = voiceStart + audio.currentTime
        if (timestamp - lastUiUpdate > 120) {
          setPlayhead(next)
          lastUiUpdate = timestamp
        }
        const active = activeAt(next)
        if (active >= 0 && active !== lastActive) {
          setSelectedIndex(active)
          lastActive = active
        }
      } else {
        setPlaying(false)
        return
      }
      frameRef.current = requestAnimationFrame(tick)
    }
    frameRef.current = requestAnimationFrame(tick)
  }, [activeAt, stopFrameLoop, voiceStart])

  const togglePlay = useCallback(async () => {
    const audio = audioRef.current
    if (!audio || !media?.voice_url) return
    if (playing) {
      audio.pause()
      videoRef.current?.pause()
      stopFrameLoop()
      setPlaying(false)
      return
    }
    audio.currentTime = Math.max(0, playhead - voiceStart)
    await audio.play().catch(() => {})
    syncPreviewVideo(playhead, true)
    setPlaying(true)
    runFrameLoop()
  }, [media?.voice_url, playhead, playing, runFrameLoop, stopFrameLoop, syncPreviewVideo, voiceStart])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space') return
      const target = event.target as HTMLElement | null
      const tagName = target?.tagName?.toLowerCase()
      const isEditing = tagName === 'input' || tagName === 'textarea' || tagName === 'select' || tagName === 'button' || Boolean(target?.isContentEditable)
      if (isEditing) return
      event.preventDefault()
      void togglePlay()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [togglePlay])

  useEffect(() => {
    syncPreviewVideo(playhead, playing)
  }, [currentMediaPath, currentSequenceStart, playhead, playing, syncPreviewVideo])

  useEffect(() => () => stopFrameLoop(), [stopFrameLoop])

  const sequentialCardsForSave = () => {
    let cursor = 0
    return cards.map((card) => {
      if (isExcluded(card)) return card
      const duration = sourceDuration(card)
      const next = { ...card, timeline_start: secToTc(cursor), timeline_end: secToTc(cursor + duration) }
      cursor += duration
      return next
    })
  }

  const saveTimeline = async () => {
    if (busy && !saving) {
      toast.error(`Проект занят: ${busy}`)
      return
    }
    setSaving(true)
    setBusy('Сохранение Timeline')
    try {
      const normalized = sequentialCardsForSave()
      setCards(normalized)
      await api.saveAssetBoard(projectId, normalized)
      await api.saveTimelineSettings(projectId, { voice_start: secToTc(voiceStart) })
      await load()
      toast.success('Timeline сохранён')
    } catch (e) {
      toast.error(`Сохранение timeline: ${(e as Error).message}`)
    } finally {
      setSaving(false)
      setBusy(null)
    }
  }

  const toggleExcludeSelected = async () => {
    if (!selectedCard || busy) {
      if (busy) toast.error(`Проект занят: ${busy}`)
      return
    }
    setBusy('Изменение Timeline')
    try {
      const returning = isExcluded(selectedCard)
      // Consequence is computed against the pre-mutation board state.
      const impact = returning ? null : excludeImpact(cards, selectedIndex)
      const patch = returning
        ? { status: hasMedia(selectedCard) ? 'generated' : 'planned' }
        : { status: 'skipped' }
      const updated = await api.updateCard(projectId, selectedIndex, patch)
      updateCard(selectedIndex, updated)
      if (returning) {
        toast.success(`Кадр #${selectedCard.shot_index} возвращён в финал.`)
      } else if (impact) {
        toast.success(`Кадр #${selectedCard.shot_index} исключён — ${impact.gainedSeconds} с уйдут кадру #${impact.recipientShot} (станет ${impact.recipientSeconds} с).`)
        if (impact.recipientSeconds > LONG_SLOT_WARNING_SECONDS) {
          toast.info(`Внимание: кадр #${impact.recipientShot} растянется до ${impact.recipientSeconds} с — долгий статичный план может выглядеть застывшим.`)
        }
      } else {
        toast.success(`Кадр #${selectedCard.shot_index} исключён из финала.`)
      }
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const assignMedia = (index: number, path: string) => {
    const item = generatedByPath.get(path)
    updateCard(index, {
      assigned_media: path,
      status: item ? 'generated' : cards[index].status,
    })
    setSelectedIndex(index)
  }

  const beginClipTrim = useCallback((event: React.MouseEvent, index: number) => {
    event.preventDefault()
    event.stopPropagation()
    const card = cards[index]
    if (!card) return
    setSelectedIndex(index)
    const startX = event.clientX
    const initialDuration = sourceDuration(card)
    const startTime = tcToSec(card.timeline_start || card.start)

    const onMove = (moveEvent: MouseEvent) => {
      const delta = Math.round((moveEvent.clientX - startX) / zoom)
      const duration = Math.max(1, initialDuration + delta)
      updateCard(index, { timeline_end: secToTc(startTime + duration) })
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [cards, zoom])

  const beginVoiceDrag = (event: React.MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    const startX = event.clientX
    const initial = voiceStart
    const onMove = (moveEvent: MouseEvent) => {
      const delta = Math.round((moveEvent.clientX - startX) / zoom)
      setVoiceStart(Math.max(0, initial + delta))
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  const beginOverlayDrag = (event: React.MouseEvent) => {
    if (!currentCard) return
    event.preventDefault()
    event.stopPropagation()
    const preview = event.currentTarget.parentElement
    if (!preview) return
    const updateFromEvent = (moveEvent: MouseEvent) => {
      const rect = preview.getBoundingClientRect()
      const x = ((moveEvent.clientX - rect.left) / rect.width) * 100
      const y = ((moveEvent.clientY - rect.top) / rect.height) * 100
      const index = currentEntry?.index ?? selectedIndex
      updateCard(index, {
        text_overlay_position: 'manual',
        text_overlay_x: Math.round(Math.max(0, Math.min(100, x))),
        text_overlay_y: Math.round(Math.max(0, Math.min(100, y))),
      })
    }
    const onMove = (moveEvent: MouseEvent) => updateFromEvent(moveEvent)
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    updateFromEvent(event.nativeEvent)
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  const rulerMarks = []
  for (let t = 0; t <= totalDuration; t += totalDuration > 240 ? 30 : 10) rulerMarks.push(t)

  return (
    <div className="h-screen min-h-[780px] bg-slate-950 text-slate-200 flex flex-col">
      <audio
        ref={audioRef}
        src={media?.voice_url || undefined}
        preload="metadata"
        onLoadedMetadata={(event) => setVoiceDuration(event.currentTarget.duration || 0)}
        onEnded={() => {
          setPlaying(false)
          stopFrameLoop()
          videoRef.current?.pause()
        }}
      />

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(620px,1fr)_360px] border-b border-slate-800">
        <main className="min-w-0 p-5 space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h1 className="text-xl font-semibold text-white">Assembly Timeline</h1>
              <p className="text-xs text-slate-500 mt-1">
                {stats.included} в финале / {stats.generated} с медиа / {stats.missing} без медиа
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button onClick={() => seek(Math.max(0, playhead - 5))} className="px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-sm">-5s</button>
              <button onClick={togglePlay} disabled={!media?.voice_url} className="px-5 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45">
                {playing ? 'Pause Full Preview' : 'Play Full Preview'}
              </button>
              <button onClick={() => seek(playhead + 5)} className="px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-sm">+5s</button>
              <div className="px-3 py-2 rounded-lg bg-slate-900 border border-slate-800 font-mono text-sm text-white">{secToTc(playhead)}</div>
            </div>
          </div>

          <section className="grid grid-cols-[minmax(420px,1fr)_340px] gap-4">
            <div className="rounded-lg bg-black border border-slate-800 overflow-hidden">
              <div className="relative aspect-video flex items-center justify-center">
                {currentMediaPath && isVideo(currentMediaPath) && (
                  <video key={currentMediaPath} ref={videoRef} src={mediaUrl(currentMediaPath)} muted playsInline className="h-full w-full object-contain" />
                )}
                {currentMediaPath && isImage(currentMediaPath) && (
                  <img src={mediaUrl(currentMediaPath)} alt="" className="h-full w-full object-contain" />
                )}
                {!currentMediaPath && currentCard && (
                  <div className="max-w-lg p-8 text-center">
                    <div className="text-sm uppercase tracking-wide text-slate-600">Placeholder</div>
                    <div className="text-2xl font-semibold text-white mt-2">Shot {currentCard.shot_index}</div>
                    <div className="text-sm text-slate-400 mt-3">{currentCard.description || currentCard.screen_text || currentCard.scene_title || 'Кадр еще не сгенерирован'}</div>
                  </div>
                )}
                {!currentCard && <div className="text-sm text-slate-700">Нет кадров на timeline.</div>}
                {currentCard && overlayText(currentCard) && (
                  <div
                    onMouseDown={beginOverlayDrag}
                    className={`absolute cursor-move select-none text-center leading-tight text-white [text-shadow:0_3px_12px_rgba(0,0,0,0.95),0_0_2px_rgba(0,0,0,0.95)] ${overlayStyleClass(currentCard)}`}
                    style={overlayPositionStyle(currentCard)}
                  >
                    {overlayText(currentCard)}
                  </div>
                )}
              </div>
            </div>

            <aside className="rounded-lg bg-slate-900 border border-slate-800 p-4 space-y-3 max-h-[calc(100vh-180px)] overflow-y-auto">
              {selectedCard ? (
                <>
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h2 className="text-base font-semibold text-white">Shot {selectedCard.shot_index}</h2>
                      <p className="text-xs text-slate-500">Duration {secToTc(sourceDuration(selectedCard))}</p>
                    </div>
                    <span className="rounded px-2 py-1 text-xs" style={{ backgroundColor: `${STATUS_COLORS[selectedCard.status] || '#64748b'}22`, color: STATUS_COLORS[selectedCard.status] || '#94a3b8' }}>
                      {selectedCard.status || 'planned'}
                    </span>
                  </div>

                  <label className="block">
                    <span className="text-xs text-slate-500">Описание</span>
                    <textarea value={selectedCard.description} onChange={(event) => updateCard(selectedIndex, { description: event.target.value })} className="mt-1 h-20 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500" />
                  </label>

                  <label className="block">
                    <span className="text-xs text-slate-500">Assigned media</span>
                    <input value={selectedCard.assigned_media || clipMediaPath(selectedCard)} onChange={(event) => updateCard(selectedIndex, { assigned_media: event.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-xs text-white outline-none focus:border-indigo-500" />
                  </label>

                  <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3 space-y-3">
                    <label className="flex items-center justify-between gap-3">
                      <span className="text-sm font-semibold text-white">Текст</span>
                      <input type="checkbox" checked={Boolean(selectedCard.text_overlay_enabled)} onChange={(event) => updateCard(selectedIndex, { text_overlay_enabled: event.target.checked, text_overlay_text: selectedCard.text_overlay_text || selectedCard.screen_text })} className="h-4 w-4 accent-indigo-500" />
                    </label>
                    {selectedCard.text_overlay_enabled && (
                      <>
                        <textarea value={selectedCard.text_overlay_text || ''} onChange={(event) => updateCard(selectedIndex, { text_overlay_text: event.target.value })} className="h-16 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500" />
                        <div className="grid grid-cols-2 gap-2">
                          <label className="block">
                            <span className="text-xs text-slate-500">Стиль</span>
                            <select value={selectedCard.text_overlay_style || 'subtitle'} onChange={(event) => updateCard(selectedIndex, { text_overlay_style: event.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500">
                              {TEXT_STYLES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                            </select>
                          </label>
                          <label className="block">
                            <span className="text-xs text-slate-500">Позиция</span>
                            <select value={selectedCard.text_overlay_position || 'bottom'} onChange={(event) => updateCard(selectedIndex, { text_overlay_position: event.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500">
                              {TEXT_POSITIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                            </select>
                          </label>
                          <label className="block">
                            <span className="text-xs text-slate-500">Появление</span>
                            <select value={selectedCard.text_overlay_enter || 'fade'} onChange={(event) => updateCard(selectedIndex, { text_overlay_enter: event.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500">
                              {TEXT_ANIMS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                            </select>
                          </label>
                          <label className="block">
                            <span className="text-xs text-slate-500">Исчезание</span>
                            <select value={selectedCard.text_overlay_exit || 'fade'} onChange={(event) => updateCard(selectedIndex, { text_overlay_exit: event.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white outline-none focus:border-indigo-500">
                              {TEXT_ANIMS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                            </select>
                          </label>
                        </div>
                        {(selectedCard.text_overlay_position || 'bottom') === 'manual' && (
                          <div className="grid grid-cols-2 gap-2">
                            <label className="block">
                              <span className="text-xs text-slate-500">X</span>
                              <input type="range" min="0" max="100" value={selectedCard.text_overlay_x ?? 50} onChange={(event) => updateCard(selectedIndex, { text_overlay_x: Number(event.target.value) })} className="mt-2 w-full accent-indigo-500" />
                            </label>
                            <label className="block">
                              <span className="text-xs text-slate-500">Y</span>
                              <input type="range" min="0" max="100" value={selectedCard.text_overlay_y ?? 86} onChange={(event) => updateCard(selectedIndex, { text_overlay_y: Number(event.target.value) })} className="mt-2 w-full accent-indigo-500" />
                            </label>
                          </div>
                        )}
                      </>
                    )}
                  </div>

                  <label className="block">
                    <span className="text-xs text-slate-500">Voice start</span>
                    <input value={secToTc(voiceStart)} onChange={(event) => setVoiceStart(tcToSec(event.target.value))} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white font-mono outline-none focus:border-indigo-500" />
                  </label>

                  <div className="flex items-center justify-between gap-2">
                    <FinalToggle
                      size="md"
                      on={!isExcluded(selectedCard)}
                      disabled={Boolean(busy)}
                      title={isExcluded(selectedCard)
                        ? 'Вернуть кадр в финальное видео'
                        : (() => {
                            const impact = excludeImpact(cards, selectedIndex)
                            return impact
                              ? `Убрать из финала: ${impact.gainedSeconds} с перейдут кадру #${impact.recipientShot} (станет ${impact.recipientSeconds} с)`
                              : 'Убрать кадр из финала: файлы сохранятся, действие обратимо'
                          })()}
                      onToggle={() => void toggleExcludeSelected()}
                    />
                    <button onClick={() => updateCard(selectedIndex, { assigned_media: '', timeline_start: '', timeline_end: '' })} className="rounded-lg bg-slate-800 border border-slate-700 px-3 py-2 text-sm">Reset</button>
                  </div>
                </>
              ) : (
                <div className="py-20 text-center text-sm text-slate-600">Выберите клип.</div>
              )}
            </aside>
          </section>
        </main>

        <aside className="min-h-0 border-l border-slate-800 bg-slate-900 flex flex-col">
          <div className="p-4 border-b border-slate-800">
            <h2 className="text-base font-semibold text-white">Сгенерированные кадры</h2>
            <p className="text-xs text-slate-500 mt-1">Перетащи файл на клип внизу.</p>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3 space-y-3">
            {generatedItems.map((item) => (
              <button
                key={`${item.card_index}-${item.path}`}
                draggable
                onDragStart={(event) => {
                  event.dataTransfer.setData('text/plain', item.path)
                  event.dataTransfer.effectAllowed = 'copy'
                }}
                onClick={() => {
                  setSelectedIndex(item.card_index)
                  seek(sequenceByIndex.get(item.card_index)?.start ?? 0)
                }}
                className="w-full text-left rounded-lg border border-slate-800 bg-slate-950/70 overflow-hidden hover:border-indigo-500"
              >
                <div className="aspect-video bg-black flex items-center justify-center">
                  {/* The generated-clip library and the track below render one
                      thumbnail per shot; on a few hundred shots that is a few
                      hundred simultaneous requests. Defer offscreen ones. */}
                  {item.media_type === 'image' && <img loading="lazy" decoding="async" src={item.url} alt="" className="h-full w-full object-cover" />}
                  {item.media_type === 'video' && item.poster_url && <img loading="lazy" decoding="async" src={item.poster_url} alt="" className="h-full w-full object-cover" />}
                  {item.media_type === 'video' && !item.poster_url && <div className="text-xs text-slate-500">Video file</div>}
                </div>
                <div className="p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-white">Shot {item.shot_index}</span>
                    <span className="text-xs text-slate-500">{item.media_type}</span>
                  </div>
                  <div className="truncate text-xs text-slate-500 mt-1">{item.title || item.path}</div>
                </div>
              </button>
            ))}
            {!generatedItems.length && (
              <div className="rounded-lg border border-dashed border-slate-800 p-8 text-center text-sm text-slate-600">
                Сгенерированные файлы появятся здесь по мере готовности.
              </div>
            )}
          </div>
        </aside>
      </div>

      <section className="h-72 shrink-0 bg-slate-950 border-t border-slate-800">
        <div className="flex items-center justify-between px-4 py-2 border-b border-slate-800">
          <div className="text-sm font-semibold text-white">Timeline</div>
          <div className="flex items-center gap-3">
            <button onClick={saveTimeline} disabled={saving || Boolean(busy)} className="rounded-lg bg-indigo-600 px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-45">
              {saving ? 'Saving' : 'Save'}
            </button>
            <label className="flex items-center gap-2 text-xs text-slate-500">
              Zoom
              <input value={zoom} min={5} max={24} type="range" onChange={(event) => setZoom(Number(event.target.value))} className="w-28 accent-indigo-500" />
            </label>
          </div>
        </div>

        <div className="h-[calc(100%-41px)] overflow-x-auto overflow-y-hidden">
          <div style={{ minWidth: trackWidth + 120 }} className="relative h-full">
            <div className="absolute left-0 top-0 bottom-0 z-20 w-28 border-r border-slate-800 bg-slate-950">
              <div className="h-8 border-b border-slate-800 px-3 py-2 text-xs text-slate-500">Time</div>
              <div className="h-24 border-b border-slate-800 px-3 py-9 text-xs font-semibold text-slate-400">Video</div>
              <div className="h-20 px-3 py-7 text-xs font-semibold text-slate-400">Voice</div>
            </div>

            <div className="relative h-full" style={{ marginLeft: 112, width: trackWidth }}>
              <div className="relative h-8 border-b border-slate-800" onMouseDown={(event) => {
                const rect = event.currentTarget.getBoundingClientRect()
                seek((event.clientX - rect.left) / zoom)
              }}>
                {rulerMarks.map((mark) => (
                  <div key={mark} className="absolute top-0 h-full border-l border-slate-800 pl-1 text-[10px] text-slate-500" style={{ left: mark * zoom }}>
                    {secToTc(mark).slice(3)}
                  </div>
                ))}
              </div>

              <div className="relative h-24 border-b border-slate-800 bg-slate-900/35">
                {sequence.map(({ card, index, start, duration }) => {
                  const width = Math.max(34, duration * zoom)
                  const path = clipMediaPath(card)
                  const color = path ? (STATUS_COLORS[card.status] || '#16a34a') : '#64748b'
                  return (
                    <div
                      key={`${card.scene_index}-${card.shot_index}-${index}`}
                      onClick={() => {
                        setSelectedIndex(index)
                        seek(start)
                      }}
                      onDragOver={(event) => {
                        event.preventDefault()
                        event.dataTransfer.dropEffect = 'copy'
                      }}
                      onDrop={(event) => {
                        event.preventDefault()
                        const droppedPath = event.dataTransfer.getData('text/plain')
                        if (droppedPath) assignMedia(index, droppedPath)
                      }}
                      className={`absolute top-3 h-16 rounded-md border overflow-hidden cursor-pointer ${selectedIndex === index ? 'ring-2 ring-indigo-400' : ''}`}
                      style={{ left: start * zoom, width, borderColor: color, backgroundColor: `${color}22` }}
                    >
                      <div onMouseDown={(event) => beginClipTrim(event, index)} className="absolute right-0 top-0 bottom-0 z-20 w-2 cursor-col-resize bg-white/10" />
                      {path && isImage(path) && <img loading="lazy" decoding="async" src={mediaUrl(path)} alt="" className="absolute inset-0 h-full w-full object-cover opacity-60" />}
                      {path && isVideo(path) && card.image_output && <img loading="lazy" decoding="async" src={mediaUrl(card.image_output)} alt="" className="absolute inset-0 h-full w-full object-cover opacity-60" />}
                      <div className="absolute inset-0 bg-gradient-to-r from-black/70 via-black/25 to-transparent" />
                      <div className="absolute left-3 right-3 top-2 min-w-0">
                        <div className="truncate text-xs font-semibold text-white">Shot {card.shot_index}</div>
                        <div className="truncate text-[11px] text-slate-300">{path ? 'media assigned' : 'placeholder'}</div>
                        {overlayText(card) && <div className="mt-1 truncate text-[10px] text-fuchsia-200">Text: {overlayText(card)}</div>}
                      </div>
                    </div>
                  )
                })}
                {ghostClips.map(({ card, index, start, duration }) => (
                  <div
                    key={`ghost-${card.scene_index}-${card.shot_index}-${index}`}
                    onClick={() => setSelectedIndex(index)}
                    title={`Shot ${card.shot_index} вне финала — его время отдано соседнему плану. Нажмите, чтобы выбрать и вернуть.`}
                    className={`absolute top-[78px] h-4 cursor-pointer overflow-hidden rounded-sm border border-dashed ${selectedIndex === index ? 'border-indigo-400' : 'border-slate-600'} bg-slate-800/50`}
                    style={{
                      left: start * zoom,
                      width: Math.max(30, duration * zoom),
                      backgroundImage: 'repeating-linear-gradient(45deg, transparent, transparent 4px, rgba(100,116,139,0.35) 4px, rgba(100,116,139,0.35) 8px)',
                    }}
                  >
                    <div className="truncate px-1.5 text-[9px] font-semibold leading-4 text-slate-400">S{card.shot_index} · вне финала</div>
                  </div>
                ))}
                <div className="absolute top-0 bottom-0 z-30 w-px bg-red-400" style={{ left: playhead * zoom }} />
              </div>

              <div className="relative h-20 bg-slate-900/20" onMouseDown={(event) => {
                const rect = event.currentTarget.getBoundingClientRect()
                seek((event.clientX - rect.left) / zoom)
              }}>
                {media?.voice_url ? (
                  <div
                    onMouseDown={beginVoiceDrag}
                    className="absolute top-5 h-10 rounded-md border border-sky-500 bg-sky-500/15 cursor-grab active:cursor-grabbing overflow-hidden"
                    style={{ left: voiceStart * zoom, width: Math.max(120, (voiceDuration || totalDuration) * zoom) }}
                  >
                    {Array.from({ length: Math.ceil((voiceDuration || totalDuration) / 3) }).map((_, index) => (
                      <div
                        key={index}
                        className="absolute bottom-0 w-1 bg-sky-400"
                        style={{ left: index * 3 * zoom, height: `${12 + ((index * 19) % 24)}px` }}
                      />
                    ))}
                    <div className="absolute left-3 top-2 text-xs font-semibold text-sky-100">Voice / {secToTc(voiceStart)}</div>
                  </div>
                ) : (
                  <div className="absolute left-0 right-0 top-5 h-10 rounded bg-slate-900 border border-slate-800 flex items-center justify-center text-xs text-slate-700">voice track missing</div>
                )}
                <div className="absolute top-0 bottom-0 z-30 w-px bg-red-400" style={{ left: playhead * zoom }} />
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>
  )
}
