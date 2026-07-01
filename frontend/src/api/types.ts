export interface ProjectInfo {
  name: string
  dir: string
  preset: string
  language: string
}

export interface ProjectDetail {
  project: ProjectInfo
  scenes: Scene[]
  shots: Shot[]
  asset_board: AssetCard[]
  segments: TranscriptSegment[]
  transcript: string
  video_style: string
  style_anchor: string
  character_description: string
  seg_min_seconds: number
  seg_max_seconds: number
  seg_sensitivity: number
  seg_target_scenes: number
}

export interface VideoStyle {
  id: string
  name: string
  description: string
  scene_brief: string
  base_prompt: string
  character_rules: string
  shot_planning_rules: string[]
  visual_translation_rules: string[]
  negative_prompt: string
}

export interface ProjectState {
  project_dir: string
  steps: Record<string, boolean>
}

export interface TimelineMediaItem {
  card_index: number
  shot_index: number
  scene_index: number
  start: string
  end: string
  title: string
  status: string
  approved: boolean
  route: string
  visual_type: string
  media_type: 'video' | 'image' | 'audio' | 'file' | string
  path: string
  url: string
  poster_url: string
  duration: number | null
}

export interface TimelineMedia {
  project_dir: string
  voice_path: string
  voice_url: string
  voice_start: string
  export_path: string
  export_url: string
  items: TimelineMediaItem[]
}

export interface TranscriptSegment {
  start: string
  end: string
  text: string
}

export interface Scene {
  index: number
  start: string
  end: string
  title: string
  voice_text: string
  visual_direction: string
  screen_text: string
  music_note: string
  retention_note: string
}

export interface Shot {
  index: number
  scene_index: number
  start: string
  end: string
  visual_type: string
  description: string
  asset_query: string
  image_model: string
  video_model: string
  image_prompt: string
  video_prompt: string
  screen_text: string
  motion: string
  priority: string
  fallback: string
}

export interface AssetCard {
  shot_index: number
  scene_index: number
  scene_title: string
  start: string
  end: string
  visual_type: string
  route: string
  description: string
  asset_query: string
  image_model: string
  video_model: string
  image_prompt: string
  video_prompt: string
  screen_text: string
  motion: string
  fallback: string
  image_output: string
  video_output: string
  status: string
  notes: string
  approved: boolean
  timeline_start: string
  timeline_end: string
  assigned_media: string
  track: string
  text_overlay_enabled: boolean
  text_overlay_text: string
  text_overlay_position: string
  text_overlay_style: string
  text_overlay_x: number
  text_overlay_y: number
  text_overlay_enter: string
  text_overlay_exit: string
}

export interface Settings {
  lm_url: string
  lm_model: string
  lm_embed_model: string
  lm_context: string
  comfy_url: string
  comfy_krea_workflow: string
  comfy_ideogram_workflow: string
  comfy_ltx_workflow: string
  image_model_preference: string
  whisper_cli: string
  whisper_model: string
  ffmpeg: string
  whisper_language: string
  output_base: string
  lm_auto_unload: boolean
  lm_unload_delay: string
}

export interface Preset {
  name: string
  path: string
  info: Record<string, unknown>
  routes: unknown[]
}

export interface SSEEvent {
  event: string
  data: Record<string, unknown>
}

export interface ValidationIssue {
  level: string
  stage: string
  message: string
  item: string
  details: Record<string, unknown> | null
}

export interface ValidationReport {
  project: string
  summary: { status: string; counts: Record<string, number> }
  issues: ValidationIssue[]
}
