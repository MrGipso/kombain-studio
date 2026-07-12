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
