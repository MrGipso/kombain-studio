import type { ProjectInfo, ProjectDetail, ProjectState, AssetCard, Settings, Preset, ValidationReport, TimelineMedia, VideoStyle, MusicState } from './types'

const BASE = '/api'

async function r<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(text)
  }
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

// Asset Board
export const getAssetBoard = (id: string) => r<AssetCard[]>(`/projects/${encodeURIComponent(id)}/asset-board`)
export const saveAssetBoard = (id: string, cards: AssetCard[]) =>
  r(`/projects/${encodeURIComponent(id)}/asset-board/save`, {
    method: 'POST',
    body: JSON.stringify(cards),
  })
export const updateCard = (id: string, cardIndex: number, data: Partial<AssetCard>) =>
  r<AssetCard>(`/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })
export const generateCard = (id: string, cardIndex: number, mode: string = 'route') =>
  r<{ ok: boolean; card?: AssetCard }>(`/projects/${encodeURIComponent(id)}/generate/${cardIndex}?mode=${mode}`, {
    method: 'POST',
  })
export const regeneratePrompt = (id: string, cardIndex: number, target: 'image_prompt' | 'video_prompt') =>
  r<{ ok: boolean; target: string; prompt: string; card: AssetCard }>(
    `/projects/${encodeURIComponent(id)}/asset-board/${cardIndex}/regenerate-prompt`, {
    method: 'POST',
    body: JSON.stringify({ target }),
  })

// Project settings (video style + optional custom notes)
export interface ProjectStyleSettings {
  video_style: string
  style_anchor: string
  character_description: string
  seg_min_seconds: number
  seg_max_seconds: number
  seg_sensitivity: number
  seg_target_scenes: number
}
export const updateProjectSettings = (id: string, data: ProjectStyleSettings) =>
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
export const runDiagnostic = (target: 'lm' | 'embed' | 'comfy' | 'ffmpeg' | 'whisper') =>
  r<DiagnosticResult>(`/diagnostics/${target}`)

// Validation
export const validateProject = (id: string) => r<ValidationReport>(`/projects/${encodeURIComponent(id)}/validate`, { method: 'POST' })

// Download
export const getDownloadUrl = (id: string) => `${BASE}/projects/${encodeURIComponent(id)}/download`

// SSE stream
export function streamSSE(
  url: string,
  handlers: {
    onLog?: (msg: string) => void
    onProgress?: (step: string, progress: number) => void
    onDone?: (data: Record<string, unknown>) => void
    onError?: (msg: string) => void
    onEvent?: (event: string, data: Record<string, unknown>) => void
  },
): AbortController {
  const controller = new AbortController()
  const fullUrl = `${BASE}${url}`

  fetch(fullUrl, { method: 'POST', signal: controller.signal }).then(async (res) => {
    if (!res.ok || !res.body) return
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const parsed = JSON.parse(line.slice(6))
            const ev = parsed.event || 'message'
            const data = parsed.data || parsed

            if (handlers.onEvent) handlers.onEvent(ev, data)
            if (ev === 'log' && handlers.onLog) handlers.onLog(data.message as string)
            if (ev === 'progress' && handlers.onProgress) handlers.onProgress(data.step as string, data.progress as number)
            if (ev === 'done' && handlers.onDone) handlers.onDone(data as Record<string, unknown>)
            if (ev === 'error' && handlers.onError) handlers.onError(data.message as string)
          } catch {
            // ignore parse errors
          }
        }
      }
    }
  }).catch(() => {})

  return controller
}

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

// SSE event endpoints
export const startTranscription = (id: string) => `/projects/${encodeURIComponent(id)}/transcribe`
export const startSceneAnalysis = (id: string) => `/projects/${encodeURIComponent(id)}/analyze-scenes`
export const startShotGeneration = (id: string) => `/projects/${encodeURIComponent(id)}/generate-shots`
export const startMusicGeneration = (id: string) => `/projects/${encodeURIComponent(id)}/music/generate`
export const startExport = (id: string) => `/projects/${encodeURIComponent(id)}/export`
