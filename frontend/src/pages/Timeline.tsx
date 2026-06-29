import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import type { AssetCard, TimelineMedia, TimelineMediaItem } from '../api/types'

interface Context {
  projectId: string
}

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

function timelineStart(card: AssetCard) {
  return card.timeline_start || card.start
}

function timelineEnd(card: AssetCard) {
  return card.timeline_end || card.end
}

function clipMediaPath(card: AssetCard) {
  return card.assigned_media || card.video_output || card.remotion_output || card.image_output || ''
}

function isVideo(path: string) {
  return /\.(mp4|webm|mov|mkv)$/i.test(path)
}

function isImage(path: string) {
  return /\.(png|jpe?g|webp|bmp)$/i.test(path)
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
  const { projectId } = useOutletContext<Context>()
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

  const load = async () => {
    const [board, timelineMedia] = await Promise.all([
      api.getAssetBoard(projectId).catch(() => [] as AssetCard[]),
      api.getTimelineMedia(projectId).catch(() => null),
    ])
    setCards(board)
    setMedia(timelineMedia)
    setVoiceStart(tcToSec(timelineMedia?.voice_start || '00:00:00'))
    if (board.length && selectedIndex >= board.length) setSelectedIndex(0)
  }

  useEffect(() => {
    load()
  }, [projectId])

  const videoEnd = useMemo(() => {
    return cards.length ? Math.max(...cards.map((card) => tcToSec(timelineEnd(card)))) : 0
  }, [cards])

  const totalDuration = Math.max(60, videoEnd + 8, voiceStart + Math.max(voiceDuration, 0) + 8)
  const trackWidth = Math.max(960, totalDuration * zoom)
  const selectedCard = cards[selectedIndex] || null

  const currentIndex = useMemo(() => {
    const index = cards.findIndex((card) => {
      const start = tcToSec(timelineStart(card))
      const end = tcToSec(timelineEnd(card))
      return playhead >= start && playhead < end
    })
    return index >= 0 ? index : selectedIndex
  }, [cards, playhead, selectedIndex])

  const currentCard = cards[currentIndex] || selectedCard
  const currentClipStart = currentCard ? tcToSec(timelineStart(currentCard)) : 0
  const currentMediaPath = currentCard ? clipMediaPath(currentCard) : ''

  const generatedItems = media?.items || []
  const generatedByPath = useMemo(() => {
    const map = new Map<string, TimelineMediaItem>()
    generatedItems.forEach((item) => map.set(item.path, item))
    return map
  }, [generatedItems])

  const stats = useMemo(() => {
    const missing = cards.filter((card) => !clipMediaPath(card)).length
    const generated = cards.length - missing
    const approved = cards.filter((card) => card.approved || card.status === 'approved').length
    return { missing, generated, approved }
  }, [cards])

  const updateCard = (index: number, patch: Partial<AssetCard>) => {
    setCards((prev) => {
      const next = [...prev]
      next[index] = { ...next[index], ...patch }
      return next
    })
  }

  const activeAt = (seconds: number) => {
    return cards.findIndex((card) => seconds >= tcToSec(timelineStart(card)) && seconds < tcToSec(timelineEnd(card)))
  }

  const syncPreviewVideo = (seconds: number, shouldPlay = playing) => {
    const video = videoRef.current
    if (!video || !currentMediaPath || !isVideo(currentMediaPath)) return
    const hasDuration = Number.isFinite(video.duration) && video.duration > 0
    const clipDuration = currentCard
      ? Math.max(0.2, tcToSec(timelineEnd(currentCard)) - tcToSec(timelineStart(currentCard)))
      : 1
    const fitRate = hasDuration ? Math.min(4, Math.max(0.25, video.duration / clipDuration)) : 1
    const clipTime = Math.max(0, seconds - currentClipStart)
    const sourceTime = clipTime * fitRate
    const lastFrameTime = hasDuration ? Math.max(0, video.duration - 0.05) : sourceTime
    const pastSourceEnd = hasDuration && sourceTime >= lastFrameTime
    const targetTime = pastSourceEnd ? lastFrameTime : sourceTime
    const drift = Math.abs(video.currentTime - targetTime)
    if (Math.abs(video.playbackRate - fitRate) > 0.02) {
      video.playbackRate = fitRate
    }

    if (!shouldPlay || pastSourceEnd || drift > 0.55) {
      video.currentTime = targetTime
    }

    if (pastSourceEnd) {
      video.pause()
    } else if (shouldPlay && video.paused) {
      video.play().catch(() => {})
    }
    else if (!shouldPlay) video.pause()
  }

  const syncPreviewVideoHard = (seconds: number, shouldPlay = playing) => {
    const video = videoRef.current
    if (!video || !currentMediaPath || !isVideo(currentMediaPath)) return
    const hasDuration = Number.isFinite(video.duration) && video.duration > 0
    const clipDuration = currentCard
      ? Math.max(0.2, tcToSec(timelineEnd(currentCard)) - tcToSec(timelineStart(currentCard)))
      : 1
    const fitRate = hasDuration ? Math.min(4, Math.max(0.25, video.duration / clipDuration)) : 1
    const clipTime = Math.max(0, seconds - currentClipStart)
    const sourceTime = clipTime * fitRate
    const lastFrameTime = hasDuration ? Math.max(0, video.duration - 0.05) : sourceTime
    const pastSourceEnd = hasDuration && sourceTime >= lastFrameTime
    if (Math.abs(video.playbackRate - fitRate) > 0.02) {
      video.playbackRate = fitRate
    }
    if (hasDuration) {
      video.currentTime = pastSourceEnd ? lastFrameTime : sourceTime
    } else if (Math.abs(video.currentTime - sourceTime) > 0.25) {
      video.currentTime = sourceTime
    }
    if (pastSourceEnd) {
      video.pause()
    } else if (shouldPlay) {
      video.play().catch(() => {})
    }
    else video.pause()
  }

  const seek = (seconds: number) => {
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
  }

  const stopFrameLoop = () => {
    if (frameRef.current) cancelAnimationFrame(frameRef.current)
    frameRef.current = null
  }

  const runFrameLoop = () => {
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
  }

  const togglePlay = async () => {
    const audio = audioRef.current
    if (!audio || !media?.voice_url) return
    if (playing) {
      audio.pause()
      videoRef.current?.pause()
      stopFrameLoop()
      setPlaying(false)
      return
    }
    const audioTime = Math.max(0, playhead - voiceStart)
    audio.currentTime = audioTime
    await audio.play().catch(() => {})
    syncPreviewVideoHard(playhead, true)
    setPlaying(true)
    runFrameLoop()
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space') return
      const target = event.target as HTMLElement | null
      const tagName = target?.tagName?.toLowerCase()
      const isEditing =
        tagName === 'input' ||
        tagName === 'textarea' ||
        tagName === 'select' ||
        tagName === 'button' ||
        Boolean(target?.isContentEditable)
      if (isEditing) return
      event.preventDefault()
      void togglePlay()
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [togglePlay])

  useEffect(() => {
    syncPreviewVideo(playhead, playing)
  }, [currentMediaPath, currentClipStart, playhead, playing])

  useEffect(() => () => stopFrameLoop(), [])

  const saveTimeline = async () => {
    setSaving(true)
    try {
      await api.saveAssetBoard(projectId, cards)
      await api.saveTimelineSettings(projectId, { voice_start: secToTc(voiceStart) })
      await load()
    } finally {
      setSaving(false)
    }
  }

  const approveSelected = async () => {
    if (!selectedCard) return
    const updated = await api.updateCard(projectId, selectedIndex, { approved: true, status: 'approved' })
    updateCard(selectedIndex, updated)
  }

  const assignMedia = (index: number, path: string) => {
    const item = generatedByPath.get(path)
    updateCard(index, {
      assigned_media: path,
      status: item ? 'generated' : cards[index].status,
    })
    setSelectedIndex(index)
  }

  const dragRef = useRef<{
    index?: number
    startX: number
    start: number
    end: number
    mode: 'move' | 'trim-start' | 'trim-end' | 'voice'
  } | null>(null)

  const beginClipDrag = useCallback((event: React.MouseEvent, index: number, mode: 'move' | 'trim-start' | 'trim-end') => {
    event.preventDefault()
    event.stopPropagation()
    const card = cards[index]
    if (!card) return
    setSelectedIndex(index)
    dragRef.current = {
      index,
      startX: event.clientX,
      start: tcToSec(timelineStart(card)),
      end: tcToSec(timelineEnd(card)),
      mode,
    }

    const onMove = (moveEvent: MouseEvent) => {
      const drag = dragRef.current
      if (!drag || drag.index === undefined) return
      const delta = Math.round((moveEvent.clientX - drag.startX) / zoom)
      const duration = Math.max(1, drag.end - drag.start)
      if (drag.mode === 'move') {
        const start = Math.max(0, drag.start + delta)
        updateCard(drag.index, { timeline_start: secToTc(start), timeline_end: secToTc(start + duration) })
      } else if (drag.mode === 'trim-start') {
        const start = Math.max(0, Math.min(drag.start + delta, drag.end - 1))
        updateCard(drag.index, { timeline_start: secToTc(start) })
      } else if (drag.mode === 'trim-end') {
        const end = Math.max(drag.start + 1, drag.end + delta)
        updateCard(drag.index, { timeline_end: secToTc(end) })
      }
    }

    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      dragRef.current = null
    }

    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [cards, zoom])

  const beginVoiceDrag = (event: React.MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    dragRef.current = { startX: event.clientX, start: voiceStart, end: voiceStart + voiceDuration, mode: 'voice' }

    const onMove = (moveEvent: MouseEvent) => {
      const drag = dragRef.current
      if (!drag || drag.mode !== 'voice') return
      const delta = Math.round((moveEvent.clientX - drag.startX) / zoom)
      setVoiceStart(Math.max(0, drag.start + delta))
    }

    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      dragRef.current = null
    }

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
                {stats.generated} generated / {stats.missing} placeholders / {stats.approved} approved
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

          <section className="grid grid-cols-[minmax(420px,1fr)_320px] gap-4">
            <div className="rounded-lg bg-black border border-slate-800 overflow-hidden">
              <div className="aspect-video flex items-center justify-center">
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
              </div>
            </div>

            <aside className="rounded-lg bg-slate-900 border border-slate-800 p-4 space-y-3">
              {selectedCard ? (
                <>
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h2 className="text-base font-semibold text-white">Shot {selectedCard.shot_index}</h2>
                      <p className="text-xs text-slate-500">Scene {selectedCard.scene_index} / {timelineStart(selectedCard)} - {timelineEnd(selectedCard)}</p>
                    </div>
                    <span className="rounded px-2 py-1 text-xs" style={{ backgroundColor: `${STATUS_COLORS[selectedCard.status] || '#64748b'}22`, color: STATUS_COLORS[selectedCard.status] || '#94a3b8' }}>
                      {selectedCard.status || 'planned'}
                    </span>
                  </div>

                  <div className="grid grid-cols-2 gap-2">
                    <label className="block">
                      <span className="text-xs text-slate-500">Timeline start</span>
                      <input value={timelineStart(selectedCard)} onChange={(event) => updateCard(selectedIndex, { timeline_start: event.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white font-mono outline-none focus:border-indigo-500" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-slate-500">Timeline end</span>
                      <input value={timelineEnd(selectedCard)} onChange={(event) => updateCard(selectedIndex, { timeline_end: event.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-2 py-2 text-sm text-white font-mono outline-none focus:border-indigo-500" />
                    </label>
                  </div>

                  <label className="block">
                    <span className="text-xs text-slate-500">Описание</span>
                    <textarea value={selectedCard.description} onChange={(event) => updateCard(selectedIndex, { description: event.target.value })} className="mt-1 h-20 w-full resize-none rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500" />
                  </label>

                  <label className="block">
                    <span className="text-xs text-slate-500">Assigned media</span>
                    <input value={selectedCard.assigned_media || clipMediaPath(selectedCard)} onChange={(event) => updateCard(selectedIndex, { assigned_media: event.target.value })} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-xs text-white outline-none focus:border-indigo-500" />
                  </label>

                  <label className="block">
                    <span className="text-xs text-slate-500">Voice start</span>
                    <input value={secToTc(voiceStart)} onChange={(event) => setVoiceStart(tcToSec(event.target.value))} className="mt-1 w-full rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-white font-mono outline-none focus:border-indigo-500" />
                  </label>

                  <div className="flex gap-2">
                    <button onClick={approveSelected} className="flex-1 rounded-lg bg-emerald-700 px-3 py-2 text-sm font-semibold text-white">Approve</button>
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
            <p className="text-xs text-slate-500 mt-1">Перетащи файл на плейсхолдер внизу.</p>
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
                  seek(tcToSec(item.start))
                }}
                className="w-full text-left rounded-lg border border-slate-800 bg-slate-950/70 overflow-hidden hover:border-indigo-500"
              >
                <div className="aspect-video bg-black flex items-center justify-center">
                  {item.media_type === 'image' && <img src={item.url} alt="" className="h-full w-full object-cover" />}
                  {item.media_type === 'video' && item.poster_url && <img src={item.poster_url} alt="" className="h-full w-full object-cover" />}
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

      <section className="h-64 shrink-0 bg-slate-950 border-t border-slate-800">
        <div className="flex items-center justify-between px-4 py-2 border-b border-slate-800">
          <div className="text-sm font-semibold text-white">Timeline</div>
          <div className="flex items-center gap-3">
            <button onClick={saveTimeline} disabled={saving} className="rounded-lg bg-indigo-600 px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-45">
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
              <div className="h-24 border-b border-slate-800 px-3 py-9 text-xs font-semibold text-slate-400">Video 1</div>
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
                {cards.map((card, index) => {
                  const start = tcToSec(timelineStart(card))
                  const end = tcToSec(timelineEnd(card))
                  const width = Math.max(26, (end - start) * zoom)
                  const path = clipMediaPath(card)
                  const color = path ? (STATUS_COLORS[card.status] || '#16a34a') : '#64748b'
                  return (
                    <div
                      key={`${card.scene_index}-${card.shot_index}-${index}`}
                      onMouseDown={(event) => beginClipDrag(event, index, 'move')}
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
                        const path = event.dataTransfer.getData('text/plain')
                        if (path) assignMedia(index, path)
                      }}
                      className={`absolute top-3 h-16 rounded-md border overflow-hidden cursor-grab active:cursor-grabbing ${selectedIndex === index ? 'ring-2 ring-indigo-400' : ''}`}
                      style={{ left: start * zoom, width, borderColor: color, backgroundColor: `${color}22` }}
                    >
                      <div onMouseDown={(event) => beginClipDrag(event, index, 'trim-start')} className="absolute left-0 top-0 bottom-0 z-20 w-2 cursor-col-resize bg-white/10" />
                      <div onMouseDown={(event) => beginClipDrag(event, index, 'trim-end')} className="absolute right-0 top-0 bottom-0 z-20 w-2 cursor-col-resize bg-white/10" />
                      {path && isImage(path) && <img src={mediaUrl(path)} alt="" className="absolute inset-0 h-full w-full object-cover opacity-60" />}
                      {path && isVideo(path) && card.image_output && <img src={mediaUrl(card.image_output)} alt="" className="absolute inset-0 h-full w-full object-cover opacity-60" />}
                      <div className="absolute inset-0 bg-gradient-to-r from-black/70 via-black/25 to-transparent" />
                      <div className="absolute left-3 right-3 top-2 min-w-0">
                        <div className="truncate text-xs font-semibold text-white">Shot {card.shot_index}</div>
                        <div className="truncate text-[11px] text-slate-300">{path ? 'media assigned' : 'placeholder'}</div>
                      </div>
                    </div>
                  )
                })}
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
