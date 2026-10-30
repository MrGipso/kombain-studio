import type { AssetCard } from '../api/types'

// Single source of truth on the frontend for "will this card ship in the final".
// Mirrors core/export.py card_included(): a card is included when it has usable
// media AND was not explicitly excluded (skipped/rejected). This replaces the
// old `approved` whitelist so the board, dashboard, timeline and export agree.
const EXCLUDED = new Set(['skipped', 'rejected'])

// Mirror core/export.py: routes whose deliverable is a video file, vs the one
// image route. `reference` (Wikipedia/arXiv insert) ships a video clip.
const VIDEO_ROUTES = new Set(['stock', 'kenburns', 'image+video', 'reference'])
const KNOWN_ROUTES = new Set([...VIDEO_ROUTES, 'image'])

export function cardRoute(card: AssetCard): string {
  const route = (card.route || '').toLowerCase()
  const visualType = (card.visual_type || '').toLowerCase()
  // Legacy boards may carry a stale, otherwise-valid route alongside the new
  // reference visual type. A reference insert always delivers a video clip.
  if (route === 'reference' || visualType === 'web_reference_card') return 'reference'
  if (KNOWN_ROUTES.has(route)) return route
  if (visualType === 'stock_video') return 'stock'
  if (visualType === 'still_kenburns') return 'kenburns'
  if (visualType === 'ai_image') return 'image'
  return card.video_model ? 'image+video' : 'image'
}

export function cardMediaPath(card: AssetCard): string {
  if (card.assigned_media) return card.assigned_media
  const route = cardRoute(card)
  if (VIDEO_ROUTES.has(route)) return card.video_output || ''
  if (route === 'image') return card.image_output || ''
  return ''
}

export function hasMedia(card: AssetCard): boolean {
  return Boolean(cardMediaPath(card))
}

export function isExcluded(card: AssetCard): boolean {
  return EXCLUDED.has((card.status || '').toLowerCase())
}

export function isIncluded(card: AssetCard): boolean {
  return hasMedia(card) && !isExcluded(card)
}

// ---------------------------------------------------------------------------
// Exclusion impact estimation.
//
// The exporter lays clips position-based (core/export.py tiled_intervals):
// every included clip runs until the NEXT included clip's start, the last one
// until the end of the narration. Excluding a card therefore does not shorten
// the video — its time is absorbed by the previous surviving clip. These
// helpers compute that consequence so the UI can state it at the moment of
// the decision instead of leaving the button's effect invisible.

export function tcToSec(tc: string): number {
  // SRT-style timecodes use a decimal comma ("00:01:02,500").
  const parts = String(tc || '').replace(',', '.').split(':').map((p) => Number(p))
  if (parts.some((n) => Number.isNaN(n))) return 0
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
  if (parts.length === 2) return parts[0] * 60 + parts[1]
  return parts[0] || 0
}

function slotStart(card: AssetCard): number {
  return tcToSec(card.timeline_start || card.start)
}

function slotEnd(card: AssetCard): number {
  return tcToSec(card.timeline_end || card.end)
}

export interface ExcludeImpact {
  // Shot number of the clip that absorbs the excluded card's time.
  recipientShot: number
  // The recipient's on-screen duration after the exclusion, in seconds.
  recipientSeconds: number
  // How many seconds the recipient gains.
  gainedSeconds: number
}

// Replicates the exporter's tiling for one hypothetical exclusion. Returns
// null when the card does not ship anyway (no media / already excluded) or
// when no other clip remains to absorb the time.
export function excludeImpact(cards: AssetCard[], index: number): ExcludeImpact | null {
  const target = cards[index]
  if (!target || !isIncluded(target)) return null

  const shipping = cards
    .map((card, i) => ({ card, i }))
    .filter(({ card }) => isIncluded(card))
    .sort((a, b) => slotStart(a.card) - slotStart(b.card))
  if (shipping.length < 2) return null

  const pos = shipping.findIndex(({ i }) => i === index)
  if (pos < 0) return null
  // Time flows to the previous surviving clip; excluding the first clip makes
  // the next one start earlier instead (the first interval is pinned to 0).
  const recipient = pos > 0 ? shipping[pos - 1] : shipping[1]
  const after = shipping.filter(({ i }) => i !== index)
  const recipientPos = after.findIndex(({ i }) => i === recipient.i)

  const totalEnd = Math.max(...cards.map((card) => slotEnd(card)))
  const startOf = (entry: { card: AssetCard }, posInList: number) =>
    posInList === 0 ? 0 : slotStart(entry.card)
  const endOf = (list: typeof after, posInList: number) =>
    posInList + 1 < list.length ? slotStart(list[posInList + 1].card) : totalEnd

  const currentPos = shipping.findIndex(({ i }) => i === recipient.i)
  const currentSeconds = Math.max(1, endOf(shipping, currentPos) - startOf(recipient, currentPos))
  const recipientSeconds = Math.max(1, endOf(after, recipientPos) - startOf(recipient, recipientPos))
  return {
    recipientShot: recipient.card.shot_index,
    recipientSeconds,
    gainedSeconds: Math.max(0, recipientSeconds - currentSeconds),
  }
}

// Above this the held/stretched plan starts to read as a frozen frame.
export const LONG_SLOT_WARNING_SECONDS = 12
