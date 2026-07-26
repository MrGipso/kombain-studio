import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import type { ScriptState, TranscriptionSource, VoiceProfile } from '../api/types'
import { useToast } from '../components/toast-context'
import { useSSE } from '../hooks/useSSE'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

const EMPTY: ScriptState = {
  schema_version: 2,
  mode: 'ready',
  source: { type: 'text', name: '', url: '', language: 'ru', caption_kind: '' },
  source_text: '', cleaned_text: '', final_text: '', target_duration_sec: null,
  target_duration_min_sec: null, target_duration_max_sec: null, duration_mode: 'auto',
  duration_tolerance_pct: 5, speech_style: 'normal', estimated_wpm: 135,
  estimated_duration_sec: 0, source_estimated_duration_sec: 0, actual_audio_duration_sec: null,
  voice_profile_id: '', text_revision: 0, voice_source_revision: null, voice_outdated: false, revision: 0,
  analysis: { facts: [], theses: [], warnings: [] },
  voice_plan: { status: 'empty', chunks: [] },
  quality: { status: 'empty', target_words: 0, actual_words: 0, word_ratio: 0, total_beats: 0, completed_beats: 0, coverage_ratio: 0, accepted_actual: false },
  agent: { status: 'idle', stage: '', progress: 0, iteration: 0, max_iterations: 1, score: 0, issues: [], history: [], last_report_signature: '', last_checked_revision: -1 },
  topic: '', target_minutes: 8, audience_notes: '', facts: '', hooks: [], hook_selected: 0, beats: [],
}

const fmt = (seconds: number | null | undefined) => {
  const value = Math.max(0, Math.round(seconds || 0))
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`
}

const messageOf = (error: unknown) => {
  const text = (error as Error).message || String(error)
  try {
    const parsed = JSON.parse(text) as { detail?: string }
    return parsed.detail || text
  } catch { return text }
}

export default function Voice() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const navigate = useNavigate()
  const toast = useToast()
  const { startStream } = useSSE()
  const [script, setScript] = useState<ScriptState>(EMPTY)
  const [voices, setVoices] = useState<VoiceProfile[]>([])
  const [loaded, setLoaded] = useState(false)
  const [working, setWorking] = useState<string | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)
  const [voiceFile, setVoiceFile] = useState<File | null>(null)
  const [voiceName, setVoiceName] = useState('Мой голос')
  const [voiceTranscript, setVoiceTranscript] = useState('')
  const [voiceMode, setVoiceMode] = useState<'preset' | 'custom'>('preset')
  const [voiceSpeed, setVoiceSpeed] = useState(1)
  const [pauseScale, setPauseScale] = useState(1)
  const [voiceSteps, setVoiceSteps] = useState(32)
  const [guidanceScale, setGuidanceScale] = useState(2)
  const [voiceDenoise, setVoiceDenoise] = useState(true)
  const [voicePostprocess, setVoicePostprocess] = useState(true)
  const [voiceDevice, setVoiceDevice] = useState<'auto' | 'cuda' | 'cpu' | 'mps'>('auto')
  const [voiceLogs, setVoiceLogs] = useState<string[]>([])
  const [voiceLogsOpen, setVoiceLogsOpen] = useState(true)
  const [cancellingVoice, setCancellingVoice] = useState(false)
  const [narrationSource, setNarrationSource] = useState<TranscriptionSource | null>(null)
  const voiceLogRef = useRef<HTMLDivElement | null>(null)
  const lockedElsewhere = Boolean(busy) && !working

  const reloadVoices = useCallback(
    () => api.getVoiceProfiles(projectId).then(setVoices).catch(() => setVoices([])),
    [projectId],
  )

  useEffect(() => {
    api.getScript(projectId)
      .then((data) => setScript({ ...EMPTY, ...data, agent: { ...EMPTY.agent, ...data.agent } }))
      .catch((error) => toast.error(`Озвучка: ${messageOf(error)}`))
      .finally(() => setLoaded(true))
    api.getNarrationSource(projectId)
      .then((source) => setNarrationSource(source.available ? source : null))
      .catch(() => setNarrationSource(null))
    reloadVoices()
  }, [projectId, reloadVoices, toast])

  const selectedVoice = useMemo(
    () => voices.find((voice) => voice.id === script.voice_profile_id) || null,
    [voices, script.voice_profile_id],
  )
  const presetVoices = useMemo(() => voices.filter((voice) => voice.type === 'preset'), [voices])
  const customVoices = useMemo(() => voices.filter((voice) => voice.type !== 'preset'), [voices])
  const rewriteHasNotices = script.mode === 'rewrite' && (
    script.agent.issues.length > 0 || script.agent.status !== 'ready' || script.quality.status !== 'ready'
  )
  const hasNarration = script.actual_audio_duration_sec != null

  useEffect(() => {
    if (selectedVoice?.type === 'clone') setVoiceMode('custom')
  }, [selectedVoice])

  useEffect(() => {
    if (voiceLogsOpen && voiceLogRef.current) {
      voiceLogRef.current.scrollTop = voiceLogRef.current.scrollHeight
    }
  }, [voiceLogs, voiceLogsOpen])

  const run = async (name: string, action: () => Promise<void>) => {
    setLastError(null); setWorking(name); setBusy(name)
    try { await action() }
    catch (error) {
      const message = messageOf(error)
      setLastError(message)
      toast.error(message)
    }
    finally { setWorking(null); setBusy(null) }
  }

  const chooseVoice = async (profile: VoiceProfile) => {
    const next = { voice_profile_id: profile.id, estimated_wpm: profile.calibrated_wpm }
    setScript((current) => ({ ...current, ...next }))
    try {
      const saved = await api.saveScript(projectId, next)
      setScript((current) => ({ ...current, ...saved }))
    } catch (error) { toast.error(messageOf(error)) }
  }

  const addVoice = () => run('Клонирование голоса', async () => {
    if (!voiceFile) throw new Error('Выберите аудиореференс')
    if (!voiceTranscript.trim()) throw new Error('Сначала расшифруйте аудиореференс')
    const profile = await api.addReferenceVoice(projectId, voiceFile, {
      display_name: voiceName, language: 'ru', transcript: voiceTranscript,
    })
    await reloadVoices()
    await chooseVoice(profile)
    setVoiceFile(null)
    setVoiceTranscript('')
    toast.success('Собственный голос сохранён и выбран')
  })

  const transcribeVoice = () => run('Расшифровка референса', async () => {
    if (!voiceFile) throw new Error('Сначала выберите аудиореференс')
    const result = await api.transcribeReferenceVoice(projectId, voiceFile)
    setVoiceTranscript(result.text)
    toast.success('Речь распознана — проверьте текст перед сохранением')
  })

  const appendVoiceLog = (message: string) => {
    const time = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    setVoiceLogs((current) => [...current.slice(-1999), `[${time}] ${message}`])
  }

  const renderVoice = () => {
    if (!script.final_text.trim()) { toast.error('Сначала подготовьте итоговый сценарий'); return }
    if (!script.voice_profile_id) { toast.error('Выберите готовый голос или добавьте аудиореференс'); return }
    if (busy) { toast.error(`Сейчас выполняется другая задача: ${busy}`); return }
    const settings: api.VoiceRenderSettings = {
      speed: voiceSpeed,
      pause_scale: pauseScale,
      num_step: voiceSteps,
      guidance_scale: guidanceScale,
      denoise: voiceDenoise,
      postprocess_output: voicePostprocess,
      device: voiceDevice,
    }
    setLastError(null)
    setVoiceLogs([])
    setVoiceLogsOpen(true)
    setCancellingVoice(false)
    setWorking('Озвучка OmniVoice')
    setBusy('Озвучка OmniVoice')
    startStream(api.startOmniVoiceRender(projectId), {
      onLog: appendVoiceLog,
      onDone: (data) => {
        const next = data.script as unknown as ScriptState
        if (next) setScript({ ...EMPTY, ...next, agent: { ...EMPTY.agent, ...next.agent } })
        api.getNarrationSource(projectId)
          .then((source) => setNarrationSource(source.available ? source : null))
          .catch(() => {})
        const duration = Number(data.duration_sec || 0)
        appendVoiceLog(`Озвучка готова: ${fmt(duration)}`)
        toast.success(`Озвучка готова: ${fmt(duration)}`)
      },
      onError: (message, data) => {
        const cancelled = data?.code === 'cancelled'
        appendVoiceLog(cancelled ? 'Озвучка отменена пользователем' : `Ошибка: ${message}`)
        if (cancelled) toast.info('Озвучка отменена')
        else {
          setLastError(message)
          toast.error(message)
        }
      },
      onDisconnect: (message) => {
        appendVoiceLog(`Соединение с журналом потеряно: ${message}`)
        setLastError('Соединение с журналом потеряно. Фоновая задача могла продолжить работу.')
      },
      onClose: () => {
        setWorking(null)
        setBusy(null)
        setCancellingVoice(false)
      },
    }, {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: script.voice_profile_id, ...settings }),
    })
  }

  const cancelVoice = async () => {
    setCancellingVoice(true)
    appendVoiceLog('Запрошена отмена; завершаем активный процесс…')
    try {
      const result = await api.cancelJob(projectId)
      if (!result.cancelled) {
        appendVoiceLog('Активная задача уже завершилась')
        setCancellingVoice(false)
      }
    } catch (error) {
      const message = messageOf(error)
      appendVoiceLog(`Не удалось запросить отмену: ${message}`)
      toast.error(message)
      setCancellingVoice(false)
    }
  }

  const copyVoiceLogs = async () => {
    try {
      await navigator.clipboard.writeText(voiceLogs.join('\n'))
      toast.success('Логи скопированы')
    } catch {
      toast.error('Не удалось скопировать логи')
    }
  }

  const sendNarrationToTranscription = () => run('Передача в транскрипцию', async () => {
    if (script.voice_outdated) throw new Error('Сначала обновите озвучку: итоговый текст или выбранный голос изменились')
    const source = await api.selectNarrationForTranscription(projectId)
    setNarrationSource(source)
    toast.success('Озвучка выбрана для транскрипции')
    navigate(`/project/${encodeURIComponent(projectId)}/transcription?source=narration`)
  })

  if (!loaded) {
    return <div role="status" className="p-8 text-sm text-slate-500">Загружаем сценарий и голоса…</div>
  }

  if (!script.final_text.trim()) {
    return (
      <div className="mx-auto max-w-[900px] p-4 sm:p-6 xl:p-8">
        <section className="rounded-2xl border border-slate-800 bg-slate-900 p-6 text-center">
          <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-red-600">Этап озвучки</p>
          <h1 className="mt-2 text-2xl font-bold text-white">Сначала подготовьте сценарий</h1>
          <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-slate-500">Озвучка становится доступна сразу после появления итогового текста. Редакторские уведомления переход не блокируют.</p>
          <button onClick={() => navigate(`/project/${encodeURIComponent(projectId)}/script`)} className="mt-5 rounded-lg bg-red-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-red-700">Открыть сценарий</button>
        </section>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-[1180px] space-y-6 p-4 sm:p-6 xl:p-8">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-red-600">Производство голоса</p>
          <h1 className="mt-1 text-2xl font-bold text-white">Озвучка</h1>
          <p className="mt-1 text-sm text-slate-500">Итоговый сценарий → голос → генерация → готовая дорожка</p>
        </div>
        {working && <span className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-600">{working}…</span>}
      </header>

      {lockedElsewhere && <div className="rounded-xl border border-slate-300 bg-slate-100 p-4 text-sm text-slate-600">Идёт другая задача: {busy}</div>}
      {lastError && <div role="alert" className="rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-700"><p className="font-semibold">Операция не завершена</p><p className="mt-1 break-words">{lastError}</p></div>}

      <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-semibold text-white">1. Сценарий для озвучки</h2>
            <p className="mt-1 text-sm text-slate-500">{script.quality.actual_words} слов · ориентировочно {fmt(script.estimated_duration_sec)} · редакция {script.text_revision}</p>
          </div>
          <button onClick={() => navigate(`/project/${encodeURIComponent(projectId)}/script`)} className="rounded-lg border border-slate-600 px-4 py-2 text-sm font-semibold text-slate-300 hover:border-red-500 hover:text-red-600">Редактировать сценарий</button>
        </div>
        {rewriteHasNotices && <div className="mt-4 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700">У сценария есть {script.agent.issues.length || 'редакторские'} уведомления. Они не блокируют генерацию — при необходимости их можно посмотреть в разделе «Сценарий».</div>}
        <details className="mt-4 rounded-xl border border-slate-700 bg-slate-950/30">
          <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-slate-300">Просмотреть текст</summary>
          <div className="max-h-72 overflow-y-auto whitespace-pre-wrap border-t border-slate-700 p-4 text-sm leading-6 text-slate-400">{script.final_text}</div>
        </details>
      </section>

      <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-6">
        <div><h2 className="font-semibold text-white">2. Выбор голоса</h2><p className="mt-1 text-sm text-slate-500">Используйте готовый профиль OmniVoice или создайте собственный по чистому аудиореференсу.</p></div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <button type="button" onClick={() => setVoiceMode('preset')} className={`rounded-xl border p-4 text-left transition ${voiceMode === 'preset' ? 'border-red-600 bg-red-600 text-white' : 'border-slate-700 bg-slate-950/40 text-slate-200 hover:border-red-500'}`}>
            <span className="block font-semibold">Готовые голоса</span>
            <span className={`mt-1 block text-xs ${voiceMode === 'preset' ? 'text-red-100' : 'text-slate-500'}`}>Профили OmniVoice с заранее настроенной подачей.</span>
          </button>
          <button type="button" onClick={() => setVoiceMode('custom')} className={`rounded-xl border p-4 text-left transition ${voiceMode === 'custom' ? 'border-red-600 bg-red-600 text-white' : 'border-slate-700 bg-slate-950/40 text-slate-200 hover:border-red-500'}`}>
            <span className="block font-semibold">Свой голос</span>
            <span className={`mt-1 block text-xs ${voiceMode === 'custom' ? 'text-red-100' : 'text-slate-500'}`}>Клонирование по референсу и его точной расшифровке.</span>
          </button>
        </div>

        {voiceMode === 'preset' && (
          <div className="mt-5">
            <p className="mb-3 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Библиотека OmniVoice</p>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{presetVoices.map((voice) => (
              <button key={voice.id} onClick={() => chooseVoice(voice)} className={`rounded-xl border p-4 text-left ${script.voice_profile_id === voice.id ? 'border-red-600 bg-red-600 text-white' : 'border-slate-700 bg-slate-950/40 text-slate-200 hover:border-red-500'}`}>
                <span className="block font-semibold">{voice.display_name}</span>
                <span className={`mt-1 block text-xs ${script.voice_profile_id === voice.id ? 'text-red-100' : 'text-slate-500'}`}>{voice.description || 'Готовый голос'} · {voice.calibrated_wpm} слов/мин</span>
              </button>
            ))}</div>
          </div>
        )}

        {voiceMode === 'custom' && (
          <div className="mt-5 space-y-4">
            {customVoices.length > 0 && <div><p className="mb-3 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Сохранённые голоса</p><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{customVoices.map((voice) => (
              <button key={voice.id} onClick={() => chooseVoice(voice)} className={`rounded-xl border p-4 text-left ${script.voice_profile_id === voice.id ? 'border-red-600 bg-red-600 text-white' : 'border-slate-700 bg-slate-950/40 text-slate-200 hover:border-red-500'}`}>
                <span className="block font-semibold">{voice.display_name}</span>
                <span className={`mt-1 block text-xs ${script.voice_profile_id === voice.id ? 'text-red-100' : 'text-slate-500'}`}>Собственный референс · {voice.reference_transcript ? 'текст готов' : 'нет расшифровки'}</span>
              </button>
            ))}</div></div>}
            <div className="rounded-xl border border-slate-700 bg-slate-950/40 p-4">
              <div className="grid gap-4 lg:grid-cols-2">
                <label><span className="mb-1 block text-xs font-medium text-slate-500">1. Название голоса</span><input value={voiceName} onChange={(e) => setVoiceName(e.target.value)} className="min-h-10 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 text-sm text-white" /></label>
                <label><span className="mb-1 block text-xs font-medium text-slate-500">2. Аудиореференс</span><input type="file" accept="audio/*" onChange={(e) => { setVoiceFile(e.target.files?.[0] || null); setVoiceTranscript('') }} className="w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-800 file:px-3 file:py-2 file:text-slate-200" /><span className="mt-1 block text-[11px] text-slate-500">Чистая речь без музыки и эха, оптимально 10–30 секунд.</span></label>
              </div>
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <div><p className="text-xs font-medium text-slate-500">3. Расшифровка референса</p><p className="mt-1 text-xs text-slate-500">Whisper подготовит текст; перед сохранением его можно исправить.</p></div>
                <button onClick={transcribeVoice} disabled={!voiceFile || Boolean(working) || Boolean(busy)} className="rounded-lg border border-slate-600 px-4 py-2 text-sm font-semibold text-slate-200 hover:border-red-500 disabled:opacity-50">Расшифровать аудио</button>
              </div>
              <textarea value={voiceTranscript} onChange={(e) => setVoiceTranscript(e.target.value)} placeholder="Здесь появится точный текст аудиореференса…" className="mt-3 min-h-24 w-full resize-y rounded-lg border border-slate-700 bg-slate-800 p-3 text-sm leading-6 text-white outline-none focus:border-red-500" />
              <div className="mt-4 flex justify-end"><button onClick={addVoice} disabled={!voiceFile || !voiceTranscript.trim() || Boolean(working) || Boolean(busy)} className="rounded-lg bg-red-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50">Сохранить и выбрать голос</button></div>
            </div>
          </div>
        )}
      </section>

      <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div><h2 className="font-semibold text-white">3. Генерация дорожки</h2><p className="mt-1 text-sm text-slate-500">{selectedVoice ? `${selectedVoice.display_name} · паузы и подача формируются автоматически` : 'Сначала выберите голос.'}</p></div>
          <div className="flex flex-wrap gap-2">
            {busy === 'Озвучка OmniVoice' && <button onClick={cancelVoice} disabled={cancellingVoice} className="rounded-lg border border-red-600 px-4 py-2.5 text-sm font-semibold text-red-600 hover:bg-red-600 hover:text-white disabled:opacity-50">{cancellingVoice ? 'Отменяю…' : 'Отменить'}</button>}
            <button onClick={renderVoice} disabled={!selectedVoice || Boolean(working) || Boolean(busy)} className="rounded-lg bg-red-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50">{busy === 'Озвучка OmniVoice' ? 'Идёт озвучка…' : hasNarration || script.voice_outdated ? 'Создать новую озвучку' : 'Создать озвучку'}</button>
          </div>
        </div>

        <details className="mt-4 rounded-xl border border-slate-700 bg-slate-950/30">
          <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold text-slate-300">Настройки генерации озвучки</summary>
          <div className="grid gap-4 border-t border-slate-700 p-4 sm:grid-cols-2 lg:grid-cols-3">
            <label><span className="mb-1 block text-xs text-slate-500">Скорость речи · {voiceSpeed.toFixed(2)}×</span><input type="range" min="0.7" max="1.3" step="0.05" value={voiceSpeed} onChange={(e) => setVoiceSpeed(Number(e.target.value))} className="w-full accent-red-600" /></label>
            <label><span className="mb-1 block text-xs text-slate-500">Длина пауз · {pauseScale.toFixed(2)}×</span><input type="range" min="0.5" max="2" step="0.1" value={pauseScale} onChange={(e) => setPauseScale(Number(e.target.value))} className="w-full accent-red-600" /></label>
            <label><span className="mb-1 block text-xs text-slate-500">Качество</span><select value={voiceSteps} onChange={(e) => setVoiceSteps(Number(e.target.value))} className="min-h-10 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 text-sm text-white"><option value={16}>Быстро · 16 шагов</option><option value={32}>Стандарт · 32 шага</option><option value={48}>Высокое · 48 шагов</option></select></label>
            <label><span className="mb-1 block text-xs text-slate-500">Сходство с голосом · {guidanceScale.toFixed(1)}</span><input type="range" min="1" max="4" step="0.25" value={guidanceScale} onChange={(e) => setGuidanceScale(Number(e.target.value))} className="w-full accent-red-600" /></label>
            <label><span className="mb-1 block text-xs text-slate-500">Устройство</span><select value={voiceDevice} onChange={(e) => setVoiceDevice(e.target.value as 'auto' | 'cuda' | 'cpu' | 'mps')} className="min-h-10 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 text-sm text-white"><option value="auto">Автоматически</option><option value="cuda">NVIDIA CUDA</option><option value="cpu">Процессор</option><option value="mps">Apple MPS</option></select></label>
            <div className="space-y-2 text-sm text-slate-400"><label className="flex items-center gap-2"><input type="checkbox" checked={voiceDenoise} onChange={(e) => setVoiceDenoise(e.target.checked)} className="accent-red-600" />Подавление шума</label><label className="flex items-center gap-2"><input type="checkbox" checked={voicePostprocess} onChange={(e) => setVoicePostprocess(e.target.checked)} className="accent-red-600" />Финальная обработка</label></div>
            <p className="text-xs leading-5 text-slate-500 sm:col-span-2 lg:col-span-3">Паузы, вопросы, акценты и напряжённые фразы режиссируются через темп и интервалы. В OmniVoice передаются только поддерживаемые невербальные теги.</p>
          </div>
        </details>

        <details open={voiceLogsOpen} onToggle={(event) => setVoiceLogsOpen(event.currentTarget.open)} className="mt-4 overflow-hidden rounded-xl border border-slate-700 bg-slate-950/40">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-semibold text-slate-300">
            <span className="flex items-center gap-2"><span>Логи озвучки</span>{busy === 'Озвучка OmniVoice' && <span className="h-2 w-2 animate-pulse rounded-full bg-red-500" />}</span>
            <button type="button" onClick={(event) => { event.preventDefault(); event.stopPropagation(); void copyVoiceLogs() }} disabled={!voiceLogs.length} className="rounded-md border border-slate-600 px-2.5 py-1 text-xs font-medium text-slate-400 hover:border-red-500 hover:text-red-500 disabled:opacity-40">Копировать</button>
          </summary>
          <div ref={voiceLogRef} role="log" aria-live="polite" className="max-h-64 overflow-y-auto border-t border-slate-700 bg-slate-950 p-4 font-mono text-xs leading-5 text-slate-400">
            {voiceLogs.length ? voiceLogs.map((line, index) => <div key={`${index}-${line}`} className={line.includes('Ошибка:') ? 'text-red-400' : line.includes('готова') ? 'text-emerald-400' : ''}>{line}</div>) : <div className="text-slate-600">После запуска здесь появятся загрузка модели, прогресс по фрагментам, сборка дорожки и результат.</div>}
          </div>
        </details>
        {script.voice_plan.chunks.length > 0 && <p className="mt-4 text-sm text-slate-500">План: {script.voice_plan.chunks.length} фрагментов. Паузы сохраняются отдельно от текста.</p>}

        {script.actual_audio_duration_sec != null && (
          <div className={`mt-4 rounded-xl border p-4 ${script.voice_outdated ? 'border-amber-500/40 bg-amber-500/10' : 'border-emerald-500/30 bg-emerald-500/10'}`}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className={`text-sm font-semibold ${script.voice_outdated ? 'text-amber-700' : 'text-emerald-700'}`}>{script.voice_outdated ? 'Дорожка относится к предыдущей версии' : 'Готовая дорожка'} · {fmt(script.actual_audio_duration_sec)}</p>
                <p className={`mt-1 text-xs ${script.voice_outdated ? 'text-amber-700' : 'text-emerald-700'}`}>{script.voice_outdated ? 'Её можно прослушать, но перед следующим этапом создайте новую озвучку.' : 'voice/narration.wav · прослушайте результат перед следующим этапом'}</p>
              </div>
              <button onClick={sendNarrationToTranscription} disabled={script.voice_outdated || Boolean(working) || Boolean(busy) || !narrationSource?.url} className="rounded-lg bg-red-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50">Передать в транскрипцию</button>
            </div>
            {narrationSource?.url ? <audio key={`${narrationSource.url}-${script.revision}`} controls preload="metadata" src={`${narrationSource.url}?v=${script.revision}`} className="mt-4 w-full" /> : <p className="mt-3 text-xs text-amber-700">Дорожка создана, но ссылка для прослушивания пока недоступна. Обновите страницу.</p>}
          </div>
        )}
      </section>
    </div>
  )
}
