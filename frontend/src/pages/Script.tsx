import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import { useSSE } from '../hooks/useSSE'
import type { ScriptState, YoutubeInspection, YoutubeSubtitleTrack } from '../api/types'

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
  estimated_duration_sec: 0, source_estimated_duration_sec: 0, actual_audio_duration_sec: null, voice_profile_id: '',
  text_revision: 0, voice_source_revision: null, voice_outdated: false, revision: 0,
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

export default function Script() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const navigate = useNavigate()
  const toast = useToast()
  const { startStream } = useSSE()
  const [script, setScript] = useState<ScriptState>(EMPTY)
  const [working, setWorking] = useState<string | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)
  const [draftText, setDraftText] = useState('')
  const [rewriteStyle, setRewriteStyle] = useState('deep')
  const [sourceFile, setSourceFile] = useState<File | null>(null)
  const [youtubeUrl, setYoutubeUrl] = useState('')
  const [youtube, setYoutube] = useState<YoutubeInspection | null>(null)
  const [trackKey, setTrackKey] = useState('')
  const [agentLogs, setAgentLogs] = useState<string[]>([])
  const [agentLogsOpen, setAgentLogsOpen] = useState(true)
  const [agentStage, setAgentStage] = useState('')
  const [agentProgress, setAgentProgress] = useState(0)
  const [cancellingAgent, setCancellingAgent] = useState(false)
  const agentLogRef = useRef<HTMLDivElement | null>(null)
  const lockedElsewhere = Boolean(busy) && !working

  useEffect(() => {
    api.getScript(projectId).then((data) => {
      const next = { ...EMPTY, ...data, agent: { ...EMPTY.agent, ...data.agent } }
      setScript(next)
      setDraftText(next.source_text || next.final_text || '')
    }).catch((error) => toast.error(`Сценарий: ${messageOf(error)}`))
  }, [projectId, toast])

  const selectedTrack = useMemo(() => {
    if (!youtube || !trackKey) return null
    return youtube.tracks.find((track) => `${track.kind}:${track.language}` === trackKey) || null
  }, [youtube, trackKey])
  useEffect(() => {
    if (agentLogsOpen && agentLogRef.current) {
      agentLogRef.current.scrollTop = agentLogRef.current.scrollHeight
    }
  }, [agentLogs, agentLogsOpen])

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

  const selectMode = async (mode: 'ready' | 'rewrite') => {
    if (mode === script.mode) return
    if (script.source_text && !window.confirm('Сменить режим? Текущий источник останется в проекте, но рабочая форма будет сброшена.')) return
    const next = {
      ...script, mode, target_duration_sec: null, target_duration_min_sec: null,
      target_duration_max_sec: null, duration_mode: 'auto',
      source: { type: 'text', name: '', url: '', language: 'ru', caption_kind: '' },
      source_text: '', cleaned_text: '', final_text: '', beats: [],
      analysis: { facts: [], theses: [], warnings: [] },
      voice_plan: { status: 'empty', chunks: [] }, actual_audio_duration_sec: null,
      agent: { ...EMPTY.agent },
    }
    setScript(next); setDraftText(''); setSourceFile(null); setYoutube(null)
    try { setScript(await api.saveScript(projectId, next)) } catch (error) { toast.error(messageOf(error)) }
  }

  const importText = () => run('Импорт текста', async () => {
    if (!draftText.trim()) throw new Error('Добавьте исходный текст')
    const next = await api.importScriptText(projectId, {
      mode: script.mode, text: draftText,
    })
    setScript(next); toast.success('Источник сохранён')
  })

  const importFile = () => run('Импорт файла', async () => {
    if (!sourceFile) throw new Error('Выберите файл')
    const next = await api.importScriptFile(projectId, sourceFile, script.mode)
    setScript(next); setDraftText(next.source_text); toast.success(`Импортирован ${sourceFile.name}`)
  })

  const inspectYoutube = () => run('Проверка YouTube', async () => {
    const result = await api.inspectYoutubeSubtitles(projectId, youtubeUrl)
    setYoutube(result)
    const preferred = result.tracks.find((track) => track.is_original)
      || result.tracks.find((track) => track.kind === 'manual')
      || result.tracks[0]
    setTrackKey(preferred ? `${preferred.kind}:${preferred.language}` : '')
    if (!result.tracks.length) toast.error('У видео нет доступных субтитров')
  })

  const importYoutube = () => run('Импорт YouTube', async () => {
    if (!selectedTrack) throw new Error('Выберите дорожку субтитров')
    const fallbackTrack = youtube?.tracks.find((track) => track.is_original)
    const next = await api.importYoutubeSubtitles(projectId, {
      url: youtubeUrl, language: selectedTrack.language, kind: selectedTrack.kind,
      fallback_language: fallbackTrack?.language,
      fallback_kind: fallbackTrack?.kind,
      source_duration_sec: youtube?.duration,
    })
    setScript(next)
    setDraftText(next.source_text)
    if (next.source.language !== selectedTrack.language || next.source.caption_kind !== selectedTrack.kind) {
      toast.info(`Выбранная дорожка недоступна — загружена исходная: ${next.source.language}`)
    } else {
      toast.success('Субтитры загружены через yt-dlp')
    }
  })

  const appendAgentLog = (message: string) => {
    const time = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    setAgentLogs((current) => [...current.slice(-1999), `[${time}] ${message}`])
  }

  const runRewriteAgent = () => {
    if (!script.source_text.trim()) { toast.error('Сначала добавьте исходный текст или субтитры'); return }
    if (busy) { toast.error(`Сейчас выполняется другая задача: ${busy}`); return }
    const taskName = 'Агент-редактор'
    setLastError(null)
    setAgentLogs([])
    setAgentLogsOpen(true)
    setAgentStage('Подготовка')
    setAgentProgress(0)
    setCancellingAgent(false)
    setWorking(taskName)
    setBusy(taskName)
    startStream(api.startRewriteAgent(projectId), {
      onLog: appendAgentLog,
      onProgress: (stage, progress) => {
        setAgentStage(stage)
        setAgentProgress(progress)
      },
      onDone: (data) => {
        const next = data.script as unknown as ScriptState
        if (next) {
          setScript({ ...EMPTY, ...next, agent: { ...EMPTY.agent, ...next.agent } })
          const status = next.agent?.status
          if (status === 'needs_review') {
            appendAgentLog('Цикл завершён с важными замечаниями; озвучка доступна')
            toast.info('Сценарий готов с замечаниями — озвучка доступна')
          } else {
            appendAgentLog(`Цикл завершён: ${next.agent?.score || 0}/100`)
            toast.success(status === 'ready_with_warnings' ? 'Сценарий готов с замечаниями' : 'Сценарий готов')
          }
        }
        window.setTimeout(() => document.getElementById('final-script')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
      },
      onError: (message, data) => {
        const cancelled = data?.code === 'cancelled'
        appendAgentLog(cancelled ? 'Работа агента отменена пользователем' : `Ошибка: ${message}`)
        if (cancelled) toast.info('Работа агента отменена')
        else {
          setLastError(message)
          toast.error(message)
        }
      },
      onDisconnect: (message) => {
        appendAgentLog(`Соединение с журналом потеряно: ${message}`)
        setLastError('Соединение с журналом потеряно. Агент мог продолжить работу в фоне.')
      },
      onClose: () => {
        setWorking(null)
        setBusy(null)
        setCancellingAgent(false)
      },
    }, {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rewrite_style: rewriteStyle,
        max_iterations: 1,
        regenerate: false,
      }),
    })
  }

  const processSource = () => {
    if (script.mode === 'rewrite') {
      runRewriteAgent()
      return
    }
    void run('Очистка', async () => {
      const next = await api.processScript(projectId, rewriteStyle)
      setScript(next)
      toast.success('Сценарий очищен и структурирован')
      window.setTimeout(() => document.getElementById('final-script')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
    })
  }

  const saveFinalText = async () => {
    try {
      const next = await api.saveScript(projectId, { final_text: script.final_text, beats: script.beats })
      setScript((current) => ({ ...current, ...next }))
    } catch (error) { toast.error(messageOf(error)) }
  }

  const cancelAgent = async () => {
    setCancellingAgent(true)
    appendAgentLog('Запрошена отмена; агент завершит текущий безопасный шаг…')
    try {
      const result = await api.cancelJob(projectId)
      if (!result.cancelled) {
        appendAgentLog('Активная задача уже завершилась')
        setCancellingAgent(false)
      }
    } catch (error) {
      const message = messageOf(error)
      appendAgentLog(`Не удалось запросить отмену: ${message}`)
      toast.error(message)
      setCancellingAgent(false)
    }
  }

  const copyAgentLogs = async () => {
    try {
      await navigator.clipboard.writeText(agentLogs.join('\n'))
      toast.success('Лог агента скопирован')
    } catch {
      toast.error('Не удалось скопировать лог')
    }
  }

  const sourceReady = Boolean(script.source_text.trim())
  const finalReady = Boolean(script.final_text.trim())
  const importantNoticeCount = script.agent.issues.filter((issue) => ['critical', 'high'].includes(issue.severity)).length
  const rewriteHasNotices = script.mode === 'rewrite' && (
    script.agent.issues.length > 0
    || script.agent.status !== 'ready'
    || script.quality.status !== 'ready'
  )
  const agentCheckRunning = script.mode === 'rewrite' && script.agent.status === 'running'
  const youtubeAccessError = Boolean(lastError) && (
    lastError!.includes('HTTP 429')
    || lastError!.includes('YouTube запросил авторизацию')
  )
  const rewriteStyleHelp: Record<string, string> = {
    light: 'Сохраняет порядок и узнаваемую структуру источника, убирая повторы и разговорный мусор.',
    deep: 'Полностью переформулирует материал и улучшает драматургию без добавления внешних фактов.',
    author: 'Меняет композицию и подачу сильнее всего, создавая самостоятельный рассказ только по фактам источника.',
  }

  return (
    <div className="mx-auto max-w-[1180px] space-y-6 p-4 sm:p-6 xl:p-8">
      <header className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-red-600">Подготовка материала</p>
          <h1 className="mt-1 text-2xl font-bold text-white">Сценарий</h1>
          <p className="mt-1 text-sm text-slate-500">Источник → переработка → итоговый текст</p>
        </div>
        {working && <span className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-600">{working}…</span>}
      </header>

      {lockedElsewhere && <div className="rounded-xl border border-slate-300 bg-slate-100 p-4 text-sm text-slate-600">Идёт другая задача: {busy}</div>}

      {lastError && (
        <div role="alert" className="rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-700">
          <p className="font-semibold">Операция не завершена</p>
          <p className="mt-1 break-words">{lastError}</p>
          {youtubeAccessError && (
            <button
              type="button"
              onClick={() => navigate('/settings')}
              className="mt-3 rounded-lg border border-red-500/50 px-3 py-2 text-xs font-semibold text-red-700 hover:bg-red-600 hover:text-white"
            >
              Открыть настройки YouTube
            </button>
          )}
        </div>
      )}

      <section className="grid gap-3 sm:grid-cols-2">
        {([
          ['ready', 'Готовый сценарий', 'Очистить и подготовить собственный текст без изменения длительности.'],
          ['rewrite', 'Рерайт', 'Создать авторский сценарий естественной длительности по исходному материалу.'],
        ] as const).map(([mode, title, description]) => (
          <button key={mode} onClick={() => selectMode(mode)} disabled={Boolean(working)}
            className={`rounded-2xl border p-5 text-left transition ${script.mode === mode ? 'border-red-600 bg-red-600 text-white shadow-lg shadow-red-600/15' : 'border-slate-700 bg-slate-900 text-slate-200 hover:border-red-500/60'}`}>
            <span className="block text-lg font-bold">{title}</span>
            <span className={`mt-1 block text-sm ${script.mode === mode ? 'text-red-50' : 'text-slate-500'}`}>{description}</span>
          </button>
        ))}
      </section>

      <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-6">
        <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-semibold text-white">1. Исходный материал</h2>
            <p className="mt-1 text-sm text-slate-500">Оригинал сохраняется отдельно и никогда не перезаписывается.</p>
          </div>
          {script.mode === 'rewrite' && sourceReady && (
            <div className="rounded-xl border border-slate-700 bg-slate-800 px-3 py-2 text-right">
              <p className="text-xs font-semibold text-slate-300">Длительность определяется автоматически</p>
              <p className="mt-0.5 text-xs text-slate-500">
                ориентир {fmt(script.target_duration_sec)}
                {script.target_duration_min_sec && script.target_duration_max_sec
                  ? ` · рабочий диапазон ${fmt(script.target_duration_min_sec)}–${fmt(script.target_duration_max_sec)}`
                  : ''}
              </p>
            </div>
          )}
        </div>

        {script.mode === 'rewrite' && (
          <div className="mb-5 rounded-xl border border-slate-700 bg-slate-950/40 p-4">
            <div className="flex flex-col gap-3 lg:flex-row">
              <input value={youtubeUrl} onChange={(e) => setYoutubeUrl(e.target.value)} placeholder="Ссылка YouTube для получения субтитров через yt-dlp"
                className="min-h-11 flex-1 rounded-lg border border-slate-700 bg-slate-800 px-3 text-sm text-white outline-none focus:border-red-500" />
              <button onClick={inspectYoutube} disabled={Boolean(working) || !youtubeUrl.trim()} className="rounded-lg border border-slate-600 px-4 py-2 text-sm font-semibold text-slate-200 hover:border-red-500 disabled:opacity-50">Найти субтитры</button>
            </div>
            {youtube && (
              <div className="mt-4 grid gap-3 md:grid-cols-[1fr_260px_auto] md:items-end">
                <div><p className="font-medium text-white">{youtube.title}</p><p className="text-xs text-slate-500">{youtube.channel} · {fmt(youtube.duration)}</p></div>
                <label><span className="mb-1 block text-xs text-slate-500">Дорожка</span>
                  <select value={trackKey} onChange={(e) => setTrackKey(e.target.value)} className="min-h-10 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 text-sm text-white">
                    {youtube.tracks.map((track: YoutubeSubtitleTrack) => <option key={`${track.kind}:${track.language}`} value={`${track.kind}:${track.language}`}>{track.language} · {track.kind === 'manual' ? 'ручные' : 'автоматические'}{track.is_original ? ' · исходная' : ''}</option>)}
                  </select>
                </label>
                <button onClick={importYoutube} disabled={!selectedTrack || Boolean(working)} className="min-h-10 rounded-lg bg-red-600 px-4 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50">Импортировать</button>
              </div>
            )}
          </div>
        )}

        <textarea value={draftText} onChange={(e) => setDraftText(e.target.value)} placeholder={script.mode === 'ready' ? 'Вставьте готовый сценарий…' : 'Или вставьте исходный текст / субтитры для рерайта…'}
          className="min-h-[180px] w-full resize-y rounded-xl border border-slate-700 bg-slate-950/50 p-4 text-sm leading-6 text-slate-200 outline-none focus:border-red-500" />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input type="file" accept=".txt,.md,.srt,.vtt" onChange={(e) => setSourceFile(e.target.files?.[0] || null)} className="max-w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-800 file:px-3 file:py-2 file:text-slate-200" />
          <button onClick={importFile} disabled={!sourceFile || Boolean(working)} className="rounded-lg border border-slate-600 px-4 py-2 text-sm text-slate-200 disabled:opacity-50">Загрузить файл</button>
          <button onClick={importText} disabled={!draftText.trim() || Boolean(working)} className="ml-auto rounded-lg bg-red-600 px-5 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50">Сохранить источник</button>
        </div>
      </section>

      {sourceReady && (
        <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div><h2 className="font-semibold text-white">2. {script.mode === 'rewrite' ? 'Переработка' : 'Очистка и структура'}</h2><p className="mt-1 text-sm text-slate-500">Источник: {script.source.name || script.source.type}</p></div>
            {script.mode === 'rewrite' && <select value={rewriteStyle} onChange={(e) => setRewriteStyle(e.target.value)} className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-white"><option value="light">Лёгкая редактура</option><option value="deep">Глубокий рерайт</option><option value="author">Авторская адаптация</option></select>}
            <button onClick={processSource} disabled={Boolean(working) || Boolean(busy)} className="rounded-lg bg-red-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50">{script.mode === 'rewrite' ? (finalReady ? 'Проверить результат' : 'Подготовить сценарий') : 'Очистить и структурировать'}</button>
          </div>
          {script.mode === 'rewrite' && <p className="mt-3 text-xs leading-5 text-slate-500">{rewriteStyleHelp[rewriteStyle]}</p>}
          {!!script.analysis.warnings?.length && <div className="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-300">{script.analysis.warnings.join(' · ')}</div>}
          {!!script.analysis.removed?.length && <details className="mt-4 rounded-xl border border-slate-700 bg-slate-950/40 p-4"><summary className="cursor-pointer text-sm font-medium text-slate-300">Удалённые технические фрагменты · {script.analysis.removed.length}</summary><div className="mt-3 space-y-2">{script.analysis.removed.map((item, index) => <div key={`${item.text}-${index}`} className="rounded-lg border border-slate-800 p-3 text-sm"><p className="text-slate-300">{item.text}</p><p className="mt-1 text-xs text-slate-500">{item.reason}</p></div>)}</div></details>}

          {script.mode === 'rewrite' && (
            <div className="mt-4 overflow-hidden rounded-xl border border-slate-700 bg-slate-950/30">
              <div className="flex flex-wrap items-center justify-between gap-3 p-4">
                <div className="min-w-0">
                  <p className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Агент-редактор</p>
                  <div className="mt-1 flex items-center gap-2">
                    {(working === 'Агент-редактор' || script.agent.status === 'running') && <span className="h-2.5 w-2.5 shrink-0 animate-pulse rounded-full bg-red-600" />}
                    <p className="truncate text-sm font-semibold text-slate-200">
                      {working === 'Агент-редактор' ? (agentStage || 'Подготовка') : (script.agent.stage || 'Готов к запуску')}
                    </p>
                  </div>
                  <p className="mt-1 text-xs text-slate-500">
                    Проход {script.agent.iteration || 0}/{script.agent.max_iterations || 1}
                    {script.agent.score > 0 && ` · контроль ${script.agent.score}/100`}
                  </p>
                </div>
                {working === 'Агент-редактор' && (
                  <button onClick={cancelAgent} disabled={cancellingAgent} className="rounded-lg border border-red-600 px-4 py-2 text-sm font-semibold text-red-600 hover:bg-red-600 hover:text-white disabled:opacity-50">
                    {cancellingAgent ? 'Отменяю…' : 'Отменить'}
                  </button>
                )}
              </div>
              <div className="h-1.5 bg-slate-800">
                <div className="h-full bg-red-600 transition-[width] duration-300" style={{ width: `${working === 'Агент-редактор' ? agentProgress : script.agent.progress || 0}%` }} />
              </div>
              <details open={agentLogsOpen} onToggle={(event) => setAgentLogsOpen(event.currentTarget.open)} className="border-t border-slate-700">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-semibold text-slate-300">
                  <span>Журнал работы{agentLogs.length ? ` · ${agentLogs.length}` : ''}</span>
                  <button type="button" onClick={(event) => { event.preventDefault(); event.stopPropagation(); void copyAgentLogs() }} disabled={!agentLogs.length} className="rounded-md border border-slate-600 px-2.5 py-1 text-xs font-medium text-slate-400 hover:border-red-500 hover:text-red-500 disabled:opacity-40">Копировать</button>
                </summary>
                <div ref={agentLogRef} role="log" aria-live="polite" className="max-h-64 overflow-y-auto border-t border-slate-700 bg-slate-950 p-4 font-mono text-xs leading-5 text-slate-400">
                  {agentLogs.length ? agentLogs.map((line, index) => <div key={`${index}-${line}`} className={line.includes('Ошибка:') ? 'text-red-400' : line.includes('готов') || line.includes('приняты') ? 'text-emerald-400' : ''}>{line}</div>) : <div className="text-slate-600">После запуска здесь будут видны анализ, генерация блоков, контроль, исправления и выгрузка модели.</div>}
                </div>
              </details>
              {!!script.agent.issues?.length && script.agent.status !== 'running' && (
                <div className="border-t border-slate-700 p-4">
                  <p className="text-sm font-semibold text-slate-300">Замечания после проверки · {script.agent.issues.length}</p>
                  <div className="mt-2 space-y-2">
                    {script.agent.issues.slice(0, 8).map((issue) => (
                      <div key={issue.id} className={`rounded-lg border p-3 text-xs ${['critical', 'high'].includes(issue.severity) ? 'border-red-500/30 bg-red-500/10 text-red-700' : 'border-amber-500/30 bg-amber-500/10 text-amber-700'}`}>
                        <p className="font-semibold">
                          {issue.block_indices && issue.block_indices.length > 1
                            ? `Блоки ${issue.block_indices.map((index) => index + 1).join(', ')}`
                            : issue.block_index == null ? 'Весь сценарий' : `Блок ${issue.block_index + 1}`}
                          {' · '}{issue.message}
                        </p>
                        {issue.evidence && <p className="mt-1 opacity-80">{issue.evidence}</p>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      )}

      {finalReady && (
        <section id="final-script" className="scroll-mt-6 rounded-2xl border border-slate-800 bg-slate-900 p-5 sm:p-6">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div><h2 className="font-semibold text-white">3. Итоговый сценарий</h2><p className="mt-1 text-sm text-slate-500">{script.quality.actual_words} слов · расчётная длительность {fmt(script.estimated_duration_sec)}{script.mode === 'rewrite' && ` · цель ${fmt(script.target_duration_sec)}`}</p></div>
            <span className="rounded-lg bg-slate-800 px-3 py-2 text-xs text-slate-400">{script.beats.length} смысловых блоков</span>
          </div>
          {script.mode === 'rewrite' && (
            <div className={`mb-4 rounded-xl border p-3 text-sm ${rewriteHasNotices ? 'border-amber-500/40 bg-amber-500/10 text-amber-700' : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700'}`}>
              <p className="font-semibold">{agentCheckRunning ? 'Проверка сценария выполняется' : rewriteHasNotices ? 'Сценарий готов с уведомлениями' : 'Контроль сценария пройден'}</p>
              <p className="mt-1">
                {script.quality.actual_words} слов · {fmt(script.estimated_duration_sec)} · блоки {script.quality.completed_beats}/{script.quality.total_beats}.
                {script.agent.score > 0 && ` Агент: ${script.agent.score}/100.`}
                {importantNoticeCount > 0 && ` Важных замечаний: ${importantNoticeCount}.`}
                {rewriteHasNotices && ' Все замечания носят уведомительный характер и не блокируют озвучку.'}
              </p>
            </div>
          )}
          <textarea value={script.final_text} onChange={(e) => setScript((current) => ({ ...current, final_text: e.target.value }))} onBlur={saveFinalText}
            className="min-h-[260px] w-full resize-y rounded-xl border border-slate-700 bg-slate-950/50 p-4 text-sm leading-6 text-slate-200 outline-none focus:border-red-500" />
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-800 pt-4">
            <p className="text-sm text-slate-500">Текст можно озвучивать даже при наличии редакторских уведомлений.</p>
            <button
              type="button"
              onClick={() => navigate(`/project/${encodeURIComponent(projectId)}/voice`)}
              disabled={Boolean(working) || agentCheckRunning}
              className="rounded-lg bg-red-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50"
            >
              Перейти к озвучке →
            </button>
          </div>
        </section>
      )}
    </div>
  )
}
