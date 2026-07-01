import type { AssetCard } from '../api/types'

// Single source of truth on the frontend for "will this card ship in the final".
// Mirrors core/export.py card_included(): a card is included when it has usable
// media AND was not explicitly excluded (skipped/rejected). This replaces the
// old `approved` whitelist so the board, dashboard, timeline and export agree.
const EXCLUDED = new Set(['skipped', 'rejected'])

export function hasMedia(card: AssetCard): boolean {
  return Boolean(card.assigned_media || card.video_output || card.image_output)
}

export function isExcluded(card: AssetCard): boolean {
  return EXCLUDED.has((card.status || '').toLowerCase())
}

export function isIncluded(card: AssetCard): boolean {
  return hasMedia(card) && !isExcluded(card)
}
