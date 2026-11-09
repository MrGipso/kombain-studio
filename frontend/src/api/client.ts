import type { ProjectInfo, ProjectDetail, ProjectState, ProjectJobs, ProjectRunLogs, AssetCard, AssetQASummary, AssetVariantHistory, AssetVariantSelection, Settings, Preset, ValidationReport, TimelineMedia, VideoStyle, MusicState, SfxState, SfxItem, ScriptState, ExportMode, ExportProfileCatalog, ExportProfileName, MasterManifestInfo, Scene, VoicePlan, VoiceProfile, YoutubeInspection, TranscriptionSource, MaterialPreflight, MaterialRunPolicy, MaterialRunState, ComfyWatchdogStatus, ContextBible } from './types'

const BASE = '/api'

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export function isApiError(error: unknown, status?: number): error is ApiError {
  return error instanceof ApiError && (status === undefined || error.status === status)
}

export function isBoardRevisionConflict(error: unknown): boolean {
  if (!isApiError(error, 409) || !error.detail || typeof error.detail !== 'object' || Array.isArray(error.detail)) {
    return false
  }
  return 'expected' in error.detail && 'current' in error.detail
}

async function responseError(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => '')
  let message = text || res.statusText || `HTTP ${res.status}`
  let errorDetail: unknown
  try {
    const payload = JSON.parse(text) as { detail?: unknown; message?: unknown }
    const detail = payload.detail ?? payload.message
    errorDetail = detail
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
  return new ApiError(res.status, message, errorDetail)
}

async function r<T>(url: string, opts?: RequestInit, onResponse?: (response: Response) => void): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  })
  if (!res.ok) throw await responseError(res)
  onResponse?.(res)
  return res.json()
}

// Multipart upload: the browser must set its own Content-Type boundary, so
// this cannot go through r(), but errors are reported the same way.
async function upload<T>(url: string, form: FormData): Promise<T> {
  const res = await fetch(`${BASE}${url}`, { method: 'POST', body: form })
  if (!res.ok) throw await responseError(res)
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
export const getTranscriptionSource = (projectId: string) =>
  r<TranscriptionSource>(`/projects/${encodeURIComponent(projectId)}/transcription-source`)
export const getNarrationSource = (projectId: string) =>
  r<TranscriptionSource>(`/projects/${encodeURIComponent(projectId)}/transcription-source/narration`)
export const selectNarrationForTranscription = (projectId: string) =>
  r<TranscriptionSource>(
    `/projects/${encodeURIComponent(projectId)}/transcription-source/narration`,
    { method: 'POST' },
  )
export const uploadAudio = async (projectId: string, file: File): Promise<{ path: string; filename: string }> => {
  const form = new FormData()
  form.append('file', file)
  return upload(`/projects/${encodeURIComponent(projectId)}/upload-audio`, form)
}

// Scenes
export const getScenes = (id: string) => r<Scene[]>(`/projects/${encodeURIComponent(id)}/scenes`)
export const updateScene = (id: string, index: number, data: Partial<Scene>) =>
  r<Scene>(`/projects/${encodeURIComponent(id)}/scenes/${index}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })
export const exportScenes = (id: string) =>
  r<{ ok: boolean; csv: string; md: string; count: number }>(
    `/projects/${encodeURIComponent(id)}/export-scenes`, { method: 'POST' },
  )

// Shots
export const getShots = (id: string) => r(`/projects/${encodeURIComponent(id)}/shots`)
export const getContextBible = (id: string) =>
  r<ContextBible>(`/projects/${encodeURIComponent(id)}/context-bible`)

// Asset Board.
// Optimistic concurrency: board mutations send the latest accepted ETag as
// If-Match. Read responses are sequenced and cannot roll the revision back
// after a newer read or a completed mutation.
const boardRev: Record<string, string> = {}
const boardReadSequence: Record<string, number> = {}
const boardAppliedReadSequence: Record<string, number> = {}
const boardWriteVersion: Record<string, number> = {}

function trackBoardReadRevision(id: string) {
  const sequence = (boardReadSequence[id] || 0) + 1
  const writeVersionAtStart = boardWriteVersion[id] || 0
  boardReadSequence[id] = sequence
  return (response: Response) => {
    const etag = response.headers.get('ETag')
    if (!etag) return
    if (writeVersionAtStart !== (boardWriteVersion[id] || 0)) return
    if (sequence < (boardAppliedReadSequence[id] || 0)) return
    boardAppliedReadSequence[id] = sequence
    boardRev[id] = etag
  }
}

function rememberBoardWriteRevision(id: string, response: Response) {
  boardWriteVersion[id] = (boardWriteVersion[id] || 0) + 1
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

export const getAssetBoard = (id: string) => {
  const rememberRevision = trackBoardReadRevision(id)
  return r<AssetCard[]>(
    `/projects/${encodeURIComponent(id)}/asset-board`,
    undefined,
    rememberRevision,
  )
}
export const getAssetQa = (id: string) =>
  r<AssetQASummary>(`/projects/${encodeURIComponent(id)}/asset-board/qa`)

export const saveAssetBoard = async (id: string, cards: AssetCard[]) => {
  const result = await r<{ ok: boolean; count: number; rev?: string }>(
    `/projects/${encodeURIComponent(id)}/asset-board/save`,
    { method: 'POST', headers: boardHeaders(id), body: JSON.stringify(cards) },
    (response) => rememberBoardWriteRevision(id, response),
  )
  if (result.rev) boardRev[id] = result.rev
  return result
}
export const rebuildAssetBoard = (id: string) =>
  r<{ ok: boolean; count: number; cards: AssetCard[] }>(
    `/projects/${encodeURIComponent(id)}/asset-board/rebuild`, { method: 'POST' },
    (response) => rememberBoardWriteRevision(id, response),
  )
export const updateCard = (id: string, cardIndex: number, data: Partial<AssetCard>) =>
  r<AssetCard>(`/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}`, {
    method: 'PUT',
    headers: boardHeaders(id),
    body: JSON.stringify(data),
  }, (response) => rememberBoardWriteRevision(id, response))
export const generateCard = (id: string, cardIndex: number, cardId: string, mode: string = 'route', skipStock = false, forceFull = false, deferQa = false) =>
  r<{ ok: boolean; card?: AssetCard }>(
    `/projects/${encodeURIComponent(id)}/generate/${cardIndex}?mode=${mode}${skipStock ? '&skip_stock=true' : ''}${forceFull ? '&force_full=true' : ''}${deferQa ? '&defer_qa=true' : ''}`, {
    method: 'POST',
    headers: cardOperationHeaders(id, cardId),
  }, (response) => rememberBoardWriteRevision(id, response))
export const runCardQa = (id: string, cardIndex: number, cardId: string) =>
  r<{ ok: boolean; card: AssetCard; qa: unknown[] }>(
    `/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/qa`, {
      method: 'POST',
      headers: cardOperationHeaders(id, cardId),
    }, (response) => rememberBoardWriteRevision(id, response),
  )
export const getMaterialPreflight = (id: string) =>
  r<MaterialPreflight>(`/projects/${encodeURIComponent(id)}/materials/preflight`)
export const getMaterialRun = (id: string) =>
  r<MaterialRunState>(`/projects/${encodeURIComponent(id)}/materials/run`)
export const startMaterialRun = (
  id: string,
  policy: Partial<MaterialRunPolicy> = {},
) =>
  r<{ ok: boolean; job_id: string; preflight: MaterialPreflight; policy: MaterialRunPolicy }>(
    `/projects/${encodeURIComponent(id)}/materials/run`,
    {
      method: 'POST',
      body: JSON.stringify({
        mode: 'unattended',
        max_image_attempts: 2,
        max_video_attempts: 2,
        fallback: 'kenburns',
        skip_ready: true,
        auto_resume: true,
        technical_validation: true,
        ...policy,
      }),
    },
  )
export const resolveStock = (id: string, cardIndex: number, cardId: string) =>
  r<{ ok: boolean; card?: AssetCard; message?: string; source?: string; score?: number }>(
    `/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/stock`, {
      method: 'POST',
      headers: cardOperationHeaders(id, cardId),
  }, (response) => rememberBoardWriteRevision(id, response))
export const regeneratePrompt = (id: string, cardIndex: number, cardId: string, target: 'image_prompt' | 'video_prompt') =>
  r<{ ok: boolean; target: string; prompt: string; card: AssetCard }>(
    `/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/regenerate-prompt`, {
    method: 'POST',
    headers: cardOperationHeaders(id, cardId),
    body: JSON.stringify({ target }),
  }, (response) => rememberBoardWriteRevision(id, response))
export const getAssetVariants = (id: string, cardIndex: number) =>
  r<AssetVariantHistory>(`/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/variants`)
export const selectAssetVariant = (id: string, cardIndex: number, variantId: string) =>
  r<AssetVariantSelection>(
    `/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/variants/${encodeURIComponent(variantId)}/select`,
    { method: 'POST', headers: boardHeaders(id) },
    (response) => rememberBoardWriteRevision(id, response),
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
export const getComfyWatchdog = () =>
  r<ComfyWatchdogStatus>('/system/comfy-watchdog')
export const getPresets = () => r<Preset[]>('/presets')

// Diagnostics — test external dependencies
export interface DiagnosticResult { target: string; ok: boolean; message: string; detail?: string }
export const runDiagnostic = (target: 'lm' | 'embed' | 'comfy' | 'ffmpeg' | 'whisper' | 'omnivoice' | 'woosh') =>
  r<DiagnosticResult>(`/diagnostics/${target}`)

// First-run setup wizard — read-only discovery of tools and local services
export interface SetupToolCandidates { current: string; current_ok: boolean; candidates: string[] }
export interface SetupServiceProbe { url: string; reachable: boolean; models: string[]; detail: string }
export interface SetupDetection {
  platform: 'windows' | 'linux' | 'macos'
  project_root: string
  output_base: SetupToolCandidates
  ffmpeg: SetupToolCandidates
  whisper_cli: SetupToolCandidates
  whisper_model: SetupToolCandidates
  omnivoice_cli: SetupToolCandidates
  lm: SetupServiceProbe[]
  comfy: SetupServiceProbe[]
}
export const detectSetup = () => r<SetupDetection>('/setup/detect')

export interface UpdateStatus {
  enabled: boolean
  current: string
  latest: string
  update_available: boolean
  release_url: string
  published_at: string
  checked_at: number
  error: string
  /** "git": update.bat / update.sh; "installer": run the new installer; "archive": download a new copy. */
  install: 'git' | 'installer' | 'archive'
  platform: string
}

export const getUpdates = (force = false) => r<UpdateStatus>(`/updates${force ? '?force=true' : ''}`)

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
export const importScriptText = (id: string, data: { mode: 'ready' | 'rewrite'; text: string; target_minutes?: number; source_name?: string }) =>
  r<ScriptState>(`/projects/${encodeURIComponent(id)}/script/import-text`, {
    method: 'POST', body: JSON.stringify(data),
  })
export const importScriptFile = async (id: string, file: File, mode: 'ready' | 'rewrite', targetMinutes?: number): Promise<ScriptState> => {
  const form = new FormData()
  form.append('file', file)
  form.append('mode', mode)
  if (targetMinutes) form.append('target_minutes', String(targetMinutes))
  return upload(`/projects/${encodeURIComponent(id)}/script/import-file`, form)
}
export const inspectYoutubeSubtitles = (id: string, url: string) =>
  r<YoutubeInspection>(`/projects/${encodeURIComponent(id)}/script/youtube/inspect`, {
    method: 'POST', body: JSON.stringify({ url }),
  })
export const importYoutubeSubtitles = (id: string, data: { url: string; language: string; kind: string; fallback_language?: string; fallback_kind?: string; target_minutes?: number; source_duration_sec?: number }) =>
  r<ScriptState>(`/projects/${encodeURIComponent(id)}/script/youtube/import`, {
    method: 'POST', body: JSON.stringify(data),
  })
export const processScript = (id: string, rewriteStyle = 'deep', targetMinutes?: number) =>
  r<ScriptState>(`/projects/${encodeURIComponent(id)}/script/process`, {
    method: 'POST', body: JSON.stringify({ rewrite_style: rewriteStyle, target_minutes: targetMinutes }),
  })
export const startRewriteAgent = (id: string) =>
  `/projects/${encodeURIComponent(id)}/script/agent-stream`
export const expandScript = (id: string) =>
  r<ScriptState>(`/projects/${encodeURIComponent(id)}/script/expand`, { method: 'POST' })
export const acceptActualScriptDuration = (id: string) =>
  r<ScriptState>(`/projects/${encodeURIComponent(id)}/script/accept-actual`, { method: 'POST' })
export const createVoicePlan = (id: string, speed = 1, pauseScale = 1) =>
  r<{ script: ScriptState; plan: VoicePlan }>(`/projects/${encodeURIComponent(id)}/script/voice-plan`, {
    method: 'POST', body: JSON.stringify({ speed, pause_scale: pauseScale }),
  })
export const getVoiceProfiles = (id: string) =>
  r<VoiceProfile[]>(`/projects/${encodeURIComponent(id)}/voices`)
export const transcribeReferenceVoice = async (id: string, file: File): Promise<{ text: string; language: string; segments: { start: string; end: string; text: string }[] }> => {
  const form = new FormData()
  form.append('file', file)
  return upload(`/projects/${encodeURIComponent(id)}/voices/reference/transcribe`, form)
}
export const addReferenceVoice = async (id: string, file: File, data: { display_name: string; language: string; transcript: string }): Promise<VoiceProfile> => {
  const form = new FormData()
  form.append('file', file)
  form.append('display_name', data.display_name)
  form.append('language', data.language)
  form.append('transcript', data.transcript)
  return upload(`/projects/${encodeURIComponent(id)}/voices/reference`, form)
}
export interface VoiceRenderSettings {
  speed: number
  pause_scale: number
  num_step: number
  guidance_scale: number
  denoise: boolean
  postprocess_output: boolean
  device: 'auto' | 'cuda' | 'cpu' | 'mps'
}
export const renderOmniVoice = (id: string, profileId: string, settings: VoiceRenderSettings) =>
  r<{ ok: boolean; script: ScriptState; path: string; duration_sec: number }>(`/projects/${encodeURIComponent(id)}/tts/render`, {
    method: 'POST', body: JSON.stringify({ profile_id: profileId, ...settings }),
  })
export const startOmniVoiceRender = (id: string) =>
  `/projects/${encodeURIComponent(id)}/tts/render-stream`

// SFX (per-shot ambience layer)
export const getSfx = (id: string) => r<SfxState>(`/projects/${encodeURIComponent(id)}/sfx`)
export type SfxTarget = 'ambience' | 'accent'
export const setSfxQuery = (id: string, cardIndices: number[], sfxQuery: string, target: SfxTarget = 'ambience') =>
  r<{ ok: boolean; items: SfxItem[] }>(`/projects/${encodeURIComponent(id)}/sfx/query`, {
    method: 'PUT',
    body: JSON.stringify({ card_indices: cardIndices, sfx_query: sfxQuery, target }),
  })
export const generateSfx = (id: string, cardIndices: number[], fresh = false, target: SfxTarget = 'ambience') =>
  r<{ ok: boolean; url: string; items: SfxItem[] }>(`/projects/${encodeURIComponent(id)}/sfx/generate`, {
    method: 'POST',
    body: JSON.stringify({ card_indices: cardIndices, fresh, target }),
  })
export const uploadSfx = async (id: string, cardIndices: number[], file: File, target: SfxTarget = 'ambience'): Promise<{ ok: boolean; url: string; items: SfxItem[] }> => {
  const form = new FormData()
  form.append('file', file)
  form.append('card_indices', cardIndices.join(','))
  form.append('target', target)
  return upload(`/projects/${encodeURIComponent(id)}/sfx/upload`, form)
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
