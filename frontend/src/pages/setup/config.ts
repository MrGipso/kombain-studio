import type { SetupDetection } from '../../api/client'
import type { Settings } from '../../api/types'
import type { DiagTarget } from './ui'

export type StepId = 'welcome' | 'output' | 'ffmpeg' | 'whisper' | 'lm' | 'comfy' | 'voice' | 'stock' | 'finish'

export interface StepDef {
  id: StepId
  title: string
  description: string
  optional: boolean
  keys: (keyof Settings)[]
  checks: DiagTarget[]
}

export const STEPS: StepDef[] = [
  { id: 'welcome', title: 'Добро пожаловать', description: 'Что настроим и зачем', optional: false, keys: [], checks: [] },
  { id: 'output', title: 'Папка проектов', description: 'Где хранить видео и материалы', optional: false, keys: ['output_base'], checks: [] },
  { id: 'ffmpeg', title: 'FFmpeg', description: 'Сборка и экспорт видео', optional: false, keys: ['ffmpeg'], checks: ['ffmpeg'] },
  { id: 'whisper', title: 'Whisper', description: 'Расшифровка озвучки', optional: false, keys: ['whisper_cli', 'whisper_model', 'whisper_language'], checks: ['whisper'] },
  { id: 'lm', title: 'Языковая модель', description: 'Сцены, план кадров, сценарии', optional: false, keys: ['lm_provider', 'lm_url', 'lm_api_key', 'lm_model', 'lm_embed_model'], checks: ['lm', 'embed'] },
  { id: 'comfy', title: 'ComfyUI', description: 'Генерация картинок, видео и музыки', optional: true, keys: ['comfy_url'], checks: ['comfy'] },
  { id: 'voice', title: 'Голос', description: 'OmniVoice — озвучка текста', optional: true, keys: ['omnivoice_cli', 'omnivoice_model'], checks: ['omnivoice'] },
  { id: 'stock', title: 'Стоки и звуки', description: 'Pexels, Pixabay, Woosh SFX', optional: true, keys: ['pexels_api_key', 'pixabay_api_key', 'sfx_enabled', 'woosh_url'], checks: ['woosh'] },
  { id: 'finish', title: 'Готово', description: 'Итоговая проверка', optional: false, keys: [], checks: ['ffmpeg', 'whisper', 'lm', 'embed', 'comfy', 'omnivoice', 'woosh'] },
]

export const SECRET_KEYS = new Set<keyof Settings>(['lm_api_key', 'pexels_api_key', 'pixabay_api_key'])

export const DIAG_LABELS: Record<DiagTarget, string> = {
  ffmpeg: 'FFmpeg',
  whisper: 'Whisper',
  lm: 'Языковая модель',
  embed: 'Embeddings',
  comfy: 'ComfyUI',
  omnivoice: 'OmniVoice',
  woosh: 'Woosh SFX',
}
export const REQUIRED_DIAG = new Set<DiagTarget>(['ffmpeg', 'whisper', 'lm'])

export const isEmbedding = (id: string) => /embed/i.test(id)
export const sameUrl = (a: string, b: string) => a.trim().replace(/\/+$/, '') === b.trim().replace(/\/+$/, '')

/**
 * Pre-fill the draft with the best discovered value wherever the saved one is
 * empty or broken, so a typical install is "Далее, Далее, Готово".
 */
export function autofill(settings: Settings, detection: SetupDetection): Partial<Settings> {
  const draft: Partial<Settings> = {}
  const tools = ['output_base', 'ffmpeg', 'whisper_cli', 'whisper_model', 'omnivoice_cli'] as const
  for (const key of tools) {
    const found = detection[key]
    if (!found.current_ok && found.candidates[0] && found.candidates[0] !== settings[key]) {
      draft[key] = found.candidates[0]
    }
  }
  if ((settings.lm_provider || 'lmstudio') === 'lmstudio') {
    const configured = detection.lm.find((probe) => sameUrl(probe.url, settings.lm_url || ''))
    const reachable = configured?.reachable ? configured : detection.lm.find((probe) => probe.reachable)
    if (reachable) {
      if (!sameUrl(reachable.url, settings.lm_url || '')) draft.lm_url = reachable.url
      const chat = reachable.models.filter((id) => !isEmbedding(id))
      const embed = reachable.models.filter(isEmbedding)
      if (chat[0] && !chat.includes(settings.lm_model)) draft.lm_model = chat[0]
      if (embed[0] && !embed.includes(settings.lm_embed_model)) draft.lm_embed_model = embed[0]
    }
  }
  const comfyConfigured = detection.comfy.find((probe) => sameUrl(probe.url, settings.comfy_url || ''))
  const comfyReachable = detection.comfy.find((probe) => probe.reachable)
  if (!comfyConfigured?.reachable && comfyReachable) draft.comfy_url = comfyReachable.url
  return draft
}
