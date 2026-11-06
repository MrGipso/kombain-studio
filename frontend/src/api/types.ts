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
  shot_mode: string
  route_priority: string
  stock_mode: boolean
  stock_cut_seconds: number
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
  schema_version?: number
  stage_states?: Record<string, PipelineStageState>
}

export interface PipelineStageState {
  status: 'pending' | 'completed' | 'failed' | 'stale' | string
  hash?: string | null
  input_hash?: string | null
  revision?: number
  updated_at?: string | null
  completed_at?: string | null
  invalidated_by?: string | null
  error?: string | null
}

export interface JobRecord {
  id: string
  project_id: string
  name: string
  resource: string | null
  status: 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted' | string
  progress: number
  message: string
  error: string
  created_at: string
  started_at: string
  updated_at: string
  finished_at: string | null
}

export interface ProjectJobs {
  running: string | null
  items: JobRecord[]
}

export interface RunLogRecord {
  v: number
  ts: string
  job: string
  msg: string
  run_id: string
  job_id: string
  event: string
  level: string
  fields: Record<string, unknown>
}

export interface ProjectRunLogs {
  items: RunLogRecord[]
}

export interface MusicTrack {
  filename: string
  url: string
  tags: string
  bpm: number
  duration: number
  created: number
  trim_start?: number
  trim_end?: number
}

export interface MusicState {
  music_tags: string
  music_bpm: number
  music_duration: number
  music_enabled: boolean
  music_volume_db: number
  music_duck: boolean
  music_selected?: string
  tracks?: MusicTrack[]
  track_url?: string
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

export interface TranscriptionSource {
  available: boolean
  filename: string
  path: string
  url: string
  selected: boolean
  origin: 'omnivoice' | 'upload' | string
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
  sfx_query?: string
  reference_query?: string
  reference_source?: string
  segment_ids?: number[]
  visual_concept_id?: string
  purpose: string
  continuity_mode: string
  continuity_group_id: string
  location_id: string
  subject_ids: string[]
  state_before: string
  action: string
  state_after: string
  shot_size: string
  camera_angle: string
  screen_direction: string
  reference_frame_id: string
  transition_in: string
  transition_out: string
  change_from_previous: string
}

export interface ContextBible {
  schema_version: number
  source_hash: string
  context_hash: string
  scenes: Array<{
    scene_index: number
    story_thread: string
    continuity_mode: string
    location_id: string
    subject_ids: string[]
    entry_state: string
    scene_event: string
    exit_state: string
    transition_out: string
  }>
  entities: Array<{
    id: string
    description: string
    appearance_anchor: string
    scene_indices: number[]
  }>
  locations: Array<{
    id: string
    description: string
    scene_indices: number[]
  }>
  continuity_groups: Array<{
    id: string
    mode: string
    location_id: string
    subject_ids: string[]
    shot_indices: number[]
  }>
  shots: Array<Record<string, unknown>>
}

export interface MaterialPreflightIssue {
  code: string
  message: string
  level: 'blocker' | 'warning'
  scene_index?: number | null
  shot_index?: number | null
  details?: Record<string, unknown>
}

export interface MaterialPreflight {
  allowed: boolean
  plan_hash: string
  scenes: number
  shots: number
  cards: number
  segments: number
  pending_images: number
  pending_videos: number
  ready_cards: number
  excluded_cards: number
  required_services: string[]
  blockers: number
  warnings: number
  issues: MaterialPreflightIssue[]
}

export interface MaterialRunPolicy {
  mode: 'unattended'
  max_image_attempts: number
  max_video_attempts: number
  fallback: 'kenburns'
  skip_ready: boolean
  auto_resume: boolean
  technical_validation: boolean
}

export interface MaterialRunState {
  schema_version: number
  run_id?: string
  project_id: string
  plan_hash?: string
  status: 'not_started' | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted' | string
  policy?: MaterialRunPolicy
  created_at?: string
  updated_at?: string
  started_at?: string | null
  finished_at?: string | null
  current?: {
    phase?: string
    card_id?: string
    card_index?: number
    shot_index?: number
    mode?: string
    attempt?: number
  }
  summary?: {
    cards_total?: number
    cards_ready?: number
    operations_total?: number
    operations_completed?: number
    retries?: number
    fallbacks?: number
    failures?: number
  }
  error?: string
}

export interface AssetCard {
  card_id: string
  plan_fingerprint?: string
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
  source_url?: string
  source_license?: string
  source_author?: string
  stock_score?: number
  sfx_query?: string
  sfx_output?: string
  ltx_draft?: boolean
  priority?: string
  qa_score?: number
  reference_query?: string
  reference_source?: string
  reference_url?: string
  reference_output?: string
  reference_credit?: string
  purpose?: string
  continuity_mode?: string
  continuity_group_id?: string
  location_id?: string
  subject_ids?: string[]
  state_before?: string
  action?: string
  state_after?: string
  shot_size?: string
  camera_angle?: string
  screen_direction?: string
  reference_frame_id?: string
  transition_in?: string
  transition_out?: string
  change_from_previous?: string
}

export type AssetQARecommendation = 'accept' | 'needs_review' | 'retry_image' | 'retry_video'

export interface AssetQAReport {
  report_id?: string
  created_at?: string
  card_id: string
  shot_index: number
  kind: 'image' | 'video'
  mode: 'shadow' | 'review'
  recommendation: AssetQARecommendation
  score: number
  reason_codes: string[]
  summary: string
  model?: string
  transport?: string
  attempts?: number
  error?: string
  stale?: boolean
  media: { output_field: string; filename: string; sha256: string; size_bytes: number }
  technical?: Record<string, unknown>
  vision?: Record<string, unknown> | null
}

export interface AssetQASummary {
  schema_version: number
  revision: number
  history_count: number
  reports: Record<string, Partial<Record<'image' | 'video', AssetQAReport>>>
}

export type AssetVariantOutputField =
  | 'image_output'
  | 'video_output'
  | 'reference_output'
  | 'remotion_output'
  | 'assigned_media'

export interface AssetVariant {
  id: string
  created_at: string
  card_id: string
  card_index: number
  shot_index: number
  source: string
  mode: string
  output_field: AssetVariantOutputField
  file_path: string
  file_hash: string
  file_size: number
  media_type: 'image' | 'video' | 'audio' | 'file' | string
  parent_id: string | null
  prompt: string
  query: string
  model: string
  settings_hash: string
  model_hash: string
  workflow?: string
  workflow_hash: string
  metadata: Record<string, unknown>
  selected: boolean
}

export interface AssetVariantHistory {
  card_id: string
  card_index: number
  shot_index: number
  selections: Partial<Record<AssetVariantOutputField, string | null>>
  variants: AssetVariant[]
}

export interface AssetVariantSelection {
  ok: boolean
  card: AssetCard
  variant: AssetVariant
  selections: Partial<Record<AssetVariantOutputField, string | null>>
}

export interface Settings {
  lm_url: string
  lm_provider: 'lmstudio' | 'openai_compatible'
  lm_api_key: string
  lm_api_key_set?: boolean
  lm_cloud_daily_limit: number
  lm_cloud_requests_today?: number
  lm_model: string
  lm_embed_model: string
  lm_context: string
  comfy_url: string
  comfy_api_key: string
  comfy_api_key_set?: boolean
  comfy_watchdog_enabled: boolean
  comfy_install_dir: string
  comfy_watchdog_listen_address: string
  comfy_watchdog_sage_attention: boolean
  comfy_watchdog_poll_seconds: number
  comfy_watchdog_failure_threshold: number
  comfy_watchdog_startup_timeout_seconds: number
  comfy_watchdog_max_restarts: number
  comfy_watchdog_restart_window_seconds: number
  comfy_krea_workflow: string
  comfy_reference_workflow: string
  reference_conditioning_mode: 'off' | 'auto' | 'required'
  comfy_ideogram_workflow: string
  comfy_ltx_workflow: string
  comfy_ace_workflow: string
  image_model_preference: string
  whisper_cli: string
  whisper_model: string
  ffmpeg: string
  whisper_language: string
  omnivoice_cli: string
  omnivoice_model: string
  youtube_cookies_browser: '' | 'edge' | 'chrome' | 'firefox' | 'brave' | 'opera' | 'vivaldi'
  youtube_retry_count: number
  youtube_request_delay_sec: number
  youtube_fallback_original: boolean
  output_base: string
  lm_auto_unload: boolean
  lm_unload_delay: string
  pexels_api_key: string
  pexels_api_key_set?: boolean
  pixabay_api_key: string
  pixabay_api_key_set?: boolean
  lm_vision_model: string
  stock_score_threshold: number
  stock_floor: number
  stock_retry_queries: number
  stock_max_seconds: number
  stock_candidates: number
  stock_uniquify: boolean
  stock_mirror: boolean
  stock_free_sources: boolean
  reference_enable: boolean
  reference_density: string
  sfx_enabled: boolean
  woosh_url: string
  sfx_volume_db: number
  export_loudnorm: boolean
  music_normalize: boolean
  export_unify_grade: boolean
  export_grain: number
  kenburns_variety: boolean
  stock_variable_rhythm: boolean
  ltx_draft_mode: string
  comfy_ltx_draft_workflow: string
  video_engine: 'ltx' | 'minimax_h3'
  comfy_h3_workflow: string
  ltx_resolution: string
  ai_frame_qa: boolean
  ai_qa_mode: 'shadow' | 'review'
  ai_qa_timing: 'per_card' | 'after_queue'
  ai_qa_threshold: number
}

export interface ComfyWatchdogStatus {
  schema_version: number
  enabled: boolean
  status: 'stopped' | 'disabled' | 'healthy' | 'degraded' | 'restarting' | 'restart_limit' | 'misconfigured' | 'error' | string
  healthy: boolean
  comfy_url: string
  listen_address: string
  consecutive_failures: number
  restart_count_window: number
  max_restarts: number
  managed_pid: number | null
  last_check_at: string | null
  last_healthy_at: string | null
  last_restart_at: string | null
  last_recovery_at: string | null
  last_error: string
  message: string
  resumed_projects: string[]
}

export interface ScriptBeat {
  id: string
  title: string
  goal: string
  duration_sec: number
  visual_hint: string
  content: string
  text: string
  target_words: number
}

export interface ScriptAgentIssue {
  id: string
  type: string
  severity: 'critical' | 'high' | 'medium' | 'low' | string
  block_index: number | null
  message: string
  evidence: string
  suggested_action: string
  occurrence_count?: number
  block_indices?: number[]
}

export interface ScriptAgentState {
  status: 'idle' | 'running' | 'ready' | 'ready_with_warnings' | 'needs_review' | 'failed' | 'cancelled' | string
  stage: string
  progress: number
  iteration: number
  max_iterations: number
  score: number
  issues: ScriptAgentIssue[]
  history: Array<Record<string, unknown>>
  last_report_signature: string
  last_checked_revision: number
}

export interface ScriptState {
  schema_version: number
  mode: 'ready' | 'rewrite'
  source: { type: string; name: string; url: string; language: string; caption_kind: string }
  source_text: string
  cleaned_text: string
  final_text: string
  target_duration_sec: number | null
  target_duration_min_sec: number | null
  target_duration_max_sec: number | null
  duration_mode: 'auto' | 'manual' | string
  duration_tolerance_pct: number
  speech_style: string
  estimated_wpm: number
  estimated_duration_sec: number
  source_estimated_duration_sec: number
  actual_audio_duration_sec: number | null
  voice_profile_id: string
  text_revision: number
  voice_source_revision: number | null
  voice_outdated: boolean
  revision: number
  analysis: { facts: string[]; theses: string[]; warnings: string[]; quality_notes?: string[]; removed?: { text: string; reason: string }[] }
  voice_plan: VoicePlan
  quality: {
    status: 'empty' | 'incomplete' | 'ready' | string
    target_words: number
    actual_words: number
    word_ratio: number
    total_beats: number
    completed_beats: number
    coverage_ratio: number
    accepted_actual: boolean
  }
  agent: ScriptAgentState
  topic: string
  target_minutes: number
  audience_notes: string
  facts: string
  hooks: string[]
  hook_selected: number
  beats: ScriptBeat[]
}

export interface VoiceChunk {
  id: string
  text: string
  emotion: string
  intensity: number
  speed: number
  pause_before_ms: number
  pause_after_ms: number
  provider_tags: string[]
}

export interface VoicePlan {
  status: string
  voice_profile_id?: string
  settings?: { speed: number; pause_scale: number }
  chunks: VoiceChunk[]
}

export interface VoiceProfile {
  id: string
  type: 'preset' | 'clone' | string
  display_name: string
  language: string
  description: string
  calibrated_wpm: number
  reference_transcript: string
  scope: string
  duration_sec?: number | null
}

export interface YoutubeSubtitleTrack {
  language: string
  kind: 'manual' | 'auto'
  formats: string[]
  is_original?: boolean
}

export interface YoutubeInspection {
  id: string
  title: string
  channel: string
  duration: number
  webpage_url: string
  original_language: string
  tracks: YoutubeSubtitleTrack[]
}

export interface SfxItem {
  card_index: number
  shot_index: number
  scene_title: string
  start: string
  end: string
  description: string
  status: string
  sfx_query: string
  sfx_output: string
  url: string
  sfx_accent_query: string
  sfx_accent_output: string
  accent_url: string
}

export interface SfxState {
  sfx_enabled: boolean
  woosh_url: string
  woosh_online: boolean
  sfx_volume_db: number
  items: SfxItem[]
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

export type ExportMode = 'draft' | 'master'
export type ExportProfileName = 'preview' | 'delivery' | 'master'

export interface ExportProfile {
  name: ExportProfileName
  label: string
  width: number
  height: number
  fps: number
  video_codec: string
  pixel_format: string
  crf: number
  video_bitrate_kbps: number | null
  max_video_bitrate_kbps: number | null
  cpu_preset: string
  nvenc_preset: string
  nvenc_cq: number
  audio_codec: string
  audio_bitrate_kbps: number
  audio_sample_rate: number
  audio_channels: number
}

export interface ExportProfileCatalog {
  default: ExportProfileName
  profiles: ExportProfile[]
}

export interface MasterManifestInfo {
  path: string
  sha256: string
  size: number
  manifest_id: string
  created_at: string
  output_sha256: string
}

export interface ExportGateReason {
  code: string
  message: string
  card_index: number | null
  shot_index: number | null
  item: string
  details: Record<string, unknown> | null
}

export interface ExportGateSummary {
  blockers: number
  validation_errors: number
  cards_total: number
  cards_required: number
  cards_ready: number
  cards_excluded: number
}

export interface ExportGateReport {
  mode: ExportMode
  allowed: boolean
  summary: ExportGateSummary
  reasons: ExportGateReason[]
}

export interface MediaQcIssue {
  level: 'error' | 'warning' | string
  code: string
  message: string
  details?: Record<string, unknown> | null
}

export interface TechnicalMediaQc {
  ok: boolean
  counts: { error: number; warning: number }
  duration: number
  profile?: ExportProfileName | string
  expected?: {
    width: number
    height: number
    fps: number
    video_codec: string
    pixel_format: string
    audio_codec: string
    audio_sample_rate: number
    audio_channels?: number
    audio_bitrate_kbps?: number
    max_video_bitrate_kbps?: number | null
  }
  path?: string
  size_bytes?: number
  issues: MediaQcIssue[]
}
