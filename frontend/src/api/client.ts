import type { ProjectInfo, ProjectDetail, ProjectState, ProjectJobs, ProjectRunLogs, AssetCard, AssetVariantHistory, AssetVariantSelection, Settings, Preset, ValidationReport, TimelineMedia, VideoStyle, MusicState, SfxState, SfxItem, ScriptState, ExportMode, ExportProfileCatalog, ExportProfileName, MasterManifestInfo } from './types'

const BASE = '/api'

async function r<T>(url: string, opts?: RequestInit, onResponse?: (response: Response) => void): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let message = text || res.statusText || `HTTP ${res.status}`
    try {
      const payload = JSON.parse(text) as { detail?: unknown; message?: unknown }
      const detail = payload.detail ?? payload.message
      if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail)) {
        message = detail.map((item) => {
          if (item && typeof item === 'object' && 'msg' in item) return String(item.msg)
          return String(item)
        }).join('; ')
      } else if (detail && typeof detail === 'object' && 'message' in detail) {
        message = String(detail.message)
      }
    } catch {
      // The response was plain text; keep it as-is.
    }
    throw new Error(message)
  }
  onResponse?.(res)
  return res.json()
}

// Projects
export const getProjects = () => r<ProjectInfo[]>('/projects')
export const createProject = (name: string, language: string) =>
  r<ProjectInfo>('/projects', {
    method: 'POST',
    body: JSON.stringify({ name, language }),
  })
export const getProject = (id: string) => r<ProjectDetail>(`/projects/${encodeURIComponent(id)}`)
export const getVideoStyles = () => r<VideoStyle[]>('/projects/styles')
export const getProjectState = (id: string) => r<ProjectState>(`/projects/${encodeURIComponent(id)}/state`)
export const getProjectJobs = (id: string, limit = 25) =>
  r<ProjectJobs>(`/projects/${encodeURIComponent(id)}/jobs?limit=${limit}`)
export const getProjectRunLogs = (id: string, limit = 100) =>
  r<ProjectRunLogs>(`/projects/${encodeURIComponent(id)}/run-logs?limit=${limit}`)
export const getTimelineMedia = (id: string) => r<TimelineMedia>(`/projects/${encodeURIComponent(id)}/timeline-media`)
export const saveTimelineSettings = (id: string, data: { voice_start: string }) =>
  r<{ voice_start: string }>(`/projects/${encodeURIComponent(id)}/timeline-settings`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

// Transcription
export const uploadAudio = async (projectId: string, file: File): Promise<{ path: string; filename: string }> => {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/projects/${encodeURIComponent(projectId)}/upload-audio`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

// Scenes
export const getScenes = (id: string) => r(`/projects/${encodeURIComponent(id)}/scenes`)
export const updateScene = (id: string, index: number, data: Record<string, unknown>) =>
  r(`/projects/${encodeURIComponent(id)}/scenes/${index}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })
export const exportScenes = (id: string) =>
  r<{ ok: boolean; csv: string; md: string; count: number }>(
    `/projects/${encodeURIComponent(id)}/export-scenes`, { method: 'POST' },
  )

// Shots
export const getShots = (id: string) => r(`/projects/${encodeURIComponent(id)}/shots`)

// Asset Board.
// Optimistic concurrency: getAssetBoard remembers the board's ETag per project,
// and saveAssetBoard sends it back as If-Match. If a background render changed
// the board meanwhile, the server returns 409 instead of letting a stale
// full-board save clobber the fresh image_output/video_output (P0-4).
const boardRev: Record<string, string> = {}

function rememberBoardRevision(id: string, response: Response) {
  const etag = response.headers.get('ETag')
  if (etag) boardRev[id] = etag
}

function boardHeaders(id: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (boardRev[id]) headers['If-Match'] = boardRev[id]
  return headers
}

function cardOperationHeaders(id: string, cardId: string): Record<string, string> {
  return { ...boardHeaders(id), 'X-Card-ID': cardId }
}

export const getAssetBoard = async (id: string): Promise<AssetCard[]> => {
  const res = await fetch(`${BASE}/projects/${encodeURIComponent(id)}/asset-board`, {
    headers: { 'Content-Type': 'application/json' },
  })
  if (!res.ok) throw new Error((await res.text().catch(() => '')) || `HTTP ${res.status}`)
  rememberBoardRevision(id, res)
  return res.json() as Promise<AssetCard[]>
}

export const saveAssetBoard = async (id: string, cards: AssetCard[]) => {
  const result = await r<{ ok: boolean; count: number; rev?: string }>(
    `/projects/${encodeURIComponent(id)}/asset-board/save`,
    { method: 'POST', headers: boardHeaders(id), body: JSON.stringify(cards) },
    (response) => rememberBoardRevision(id, response),
  )
  if (result.rev) boardRev[id] = result.rev
  return result
}
export const rebuildAssetBoard = (id: string) =>
  r<{ ok: boolean; count: number; cards: AssetCard[] }>(
    `/projects/${encodeURIComponent(id)}/asset-board/rebuild`, { method: 'POST' },
    (response) => rememberBoardRevision(id, response),
  )
export const updateCard = (id: string, cardIndex: number, data: Partial<AssetCard>) =>
  r<AssetCard>(`/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}`, {
    method: 'PUT',
    headers: boardHeaders(id),
    body: JSON.stringify(data),
  }, (response) => rememberBoardRevision(id, response))
export const generateCard = (id: string, cardIndex: number, cardId: string, mode: string = 'route', skipStock = false, forceFull = false) =>
  r<{ ok: boolean; card?: AssetCard }>(
    `/projects/${encodeURIComponent(id)}/generate/${cardIndex}?mode=${mode}${skipStock ? '&skip_stock=true' : ''}${forceFull ? '&force_full=true' : ''}`, {
    method: 'POST',
    headers: cardOperationHeaders(id, cardId),
  }, (response) => rememberBoardRevision(id, response))
export const resolveStock = (id: string, cardIndex: number, cardId: string) =>
  r<{ ok: boolean; card?: AssetCard; message?: string; source?: string; score?: number }>(
    `/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/stock`, {
      method: 'POST',
      headers: cardOperationHeaders(id, cardId),
  }, (response) => rememberBoardRevision(id, response))
export const regeneratePrompt = (id: string, cardIndex: number, cardId: string, target: 'image_prompt' | 'video_prompt') =>
  r<{ ok: boolean; target: string; prompt: string; card: AssetCard }>(
    `/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/regenerate-prompt`, {
    method: 'POST',
    headers: cardOperationHeaders(id, cardId),
    body: JSON.stringify({ target }),
  }, (response) => rememberBoardRevision(id, response))
export const getAssetVariants = (id: string, cardIndex: number) =>
  r<AssetVariantHistory>(`/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/variants`)
export const selectAssetVariant = (id: string, cardIndex: number, variantId: string) =>
  r<AssetVariantSelection>(
    `/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/variants/${encodeURIComponent(variantId)}/select`,
    { method: 'POST', headers: boardHeaders(id) },
    (response) => rememberBoardRevision(id, response),
  )

// Project settings (video style + optional custom notes)
export interface ProjectStyleSettings {
  video_style: string
  style_anchor: string
  character_description: string
  seg_min_seconds: number
  seg_max_seconds: number
  seg_sensitivity: number
  seg_target_scenes: number
  shot_mode: string
  route_priority: string
  stock_mode: boolean
  stock_cut_seconds: number
}
export const updateProjectSettings = (id: string, data: Partial<ProjectStyleSettings>) =>
  r<ProjectStyleSettings>(
    `/projects/${encodeURIComponent(id)}/settings`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

// Settings
export const getSettings = () => r<Settings>('/settings')
export const updateSettings = (data: Partial<Settings>) =>
  r('/settings', { method: 'PUT', body: JSON.stringify(data) })
export const getPresets = () => r<Preset[]>('/presets')

// Diagnostics — test external dependencies
export interface DiagnosticResult { target: string; ok: boolean; message: string; detail?: string }
export const runDiagnostic = (target: 'lm' | 'embed' | 'comfy' | 'ffmpeg' | 'whisper' | 'woosh') =>
  r<DiagnosticResult>(`/diagnostics/${target}`)

// Validation
export const validateProject = (id: string) => r<ValidationReport>(`/projects/${encodeURIComponent(id)}/validate`, { method: 'POST' })

// Download
export const getDownloadUrl = (id: string) => `${BASE}/projects/${encodeURIComponent(id)}/download`
export const getMasterManifestDownloadUrl = (id: string) => `${BASE}/projects/${encodeURIComponent(id)}/download-manifest`
export const getMasterManifest = (id: string) =>
  r<MasterManifestInfo>(`/projects/${encodeURIComponent(id)}/master-manifest`)

// Music (project-level background audio)
export const getMusic = (id: string) => r<MusicState>(`/projects/${encodeURIComponent(id)}/music`)
export const updateMusicSettings = (id: string, data: MusicState) =>
  r<MusicState>(`/projects/${encodeURIComponent(id)}/music/settings`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })
export const generateMusicBrief = (id: string) =>
  r<MusicState>(`/projects/${encodeURIComponent(id)}/music/brief`, { method: 'POST' })
export const selectMusicTrack = (id: string, filename: string) =>
  r<MusicState>(`/projects/${encodeURIComponent(id)}/music/select`, {
    method: 'PUT',
    body: JSON.stringify({ filename }),
  })
export const deleteMusicTrack = (id: string, filename: string) =>
  r<MusicState>(`/projects/${encodeURIComponent(id)}/music/track/${encodeURIComponent(filename)}`, {
    method: 'DELETE',
  })
export const saveMusicTrim = (id: string, filename: string, trim_start: number, trim_end: number) =>
  r<MusicState>(`/projects/${encodeURIComponent(id)}/music/trim`, {
    method: 'PUT',
    body: JSON.stringify({ filename, trim_start, trim_end }),
  })

// Job control: cancel the project's running long task (cooperative)
export const cancelJob = (id: string) =>
  r<{ ok: boolean; cancelled: string | null }>(`/projects/${encodeURIComponent(id)}/cancel`, { method: 'POST' })

// Script stage (step 00)
export const getScript = (id: string) => r<ScriptState>(`/projects/${encodeURIComponent(id)}/script`)
export const saveScript = (id: string, data: Partial<ScriptState>) =>
  r<ScriptState>(`/projects/${encodeURIComponent(id)}/script`, { method: 'PUT', body: JSON.stringify(data) })
export const generateScriptStructure = (id: string) =>
  r<ScriptState>(`/projects/${encodeURIComponent(id)}/script/structure`, { method: 'POST' })
export const generateBeatText = (id: string, beatIndex: number) =>
  r<{ ok: boolean; beat_index: number; text: string; script: ScriptState }>(
    `/projects/${encodeURIComponent(id)}/script/beat-text`, {
    method: 'POST',
    body: JSON.stringify({ beat_index: beatIndex }),
  })
export const exportScript = (id: string) =>
  r<{ ok: boolean; text: string; path: string; words: number; est_minutes: number; missing_beats: number[] }>(
    `/projects/${encodeURIComponent(id)}/script/export`, { method: 'POST' })

// SFX (per-shot ambience layer)
export const getSfx = (id: string) => r<SfxState>(`/projects/${encodeURIComponent(id)}/sfx`)
export const setSfxQuery = (id: string, cardIndices: number[], sfxQuery: string) =>
  r<{ ok: boolean; items: SfxItem[] }>(`/projects/${encodeURIComponent(id)}/sfx/query`, {
    method: 'PUT',
    body: JSON.stringify({ card_indices: cardIndices, sfx_query: sfxQuery }),
  })
export const generateSfx = (id: string, cardIndices: number[], fresh = false) =>
  r<{ ok: boolean; url: string; items: SfxItem[] }>(`/projects/${encodeURIComponent(id)}/sfx/generate`, {
    method: 'POST',
    body: JSON.stringify({ card_indices: cardIndices, fresh }),
  })
export const uploadSfx = async (id: string, cardIndices: number[], file: File): Promise<{ ok: boolean; url: string; items: SfxItem[] }> => {
  const form = new FormData()
  form.append('file', file)
  form.append('card_indices', cardIndices.join(','))
  const res = await fetch(`${BASE}/projects/${encodeURIComponent(id)}/sfx/upload`, { method: 'POST', body: form })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

// SSE event endpoints
export const startTranscription = (id: string) => `/projects/${encodeURIComponent(id)}/transcribe`
export const startSceneAnalysis = (id: string) => `/projects/${encodeURIComponent(id)}/analyze-scenes`
export const startShotGeneration = (id: string) => `/projects/${encodeURIComponent(id)}/generate-shots`
export const startMusicGeneration = (id: string) => `/projects/${encodeURIComponent(id)}/music/generate`
export const getExportProfiles = (id: string) =>
  r<ExportProfileCatalog>(`/projects/${encodeURIComponent(id)}/export-profiles`)
export const startExport = (id: string, mode: ExportMode = 'draft', profile: ExportProfileName = 'delivery') =>
  `/projects/${encodeURIComponent(id)}/export?mode=${encodeURIComponent(mode)}&profile=${encodeURIComponent(profile)}`
