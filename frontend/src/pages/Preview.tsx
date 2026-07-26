import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import type { AssetCard, AssetQASummary, ExportGateReport, ExportMode, ExportProfile, ExportProfileName, MasterManifestInfo, TechnicalMediaQc, TimelineMedia } from '../api/types'
import { useSSE } from '../hooks/useSSE'
import { useToast } from '../components/toast-context'
import { isExcluded } from '../lib/cards'

const tcSec = (tc: string) => {
  const p = (tc || '0:0:0').split(':').map((x) => parseInt(x, 10) || 0)
  while (p.length < 3) p.unshift(0)
  return p[0] * 3600 + p[1] * 60 + p[2]
}

interface WeakSpot {
  cardIndex: number
  shotIndex: number
  reason: string
  severity: 'warn' | 'info'
}

const isMasterManifestInfo = (value: unknown): value is MasterManifestInfo => {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return typeof item.path === 'string'
    && typeof item.sha256 === 'string'
    && typeof item.size === 'number'
    && typeof item.manifest_id === 'string'
    && typeof item.created_at === 'string'
    && typeof item.output_sha256 === 'string'
}

const formatBytes = (size: number) => {
  if (!Number.isFinite(size) || size < 0) return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

const formatManifestTime = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}

// The professional pre-ship ritual, automated: surface the shots most likely
// to hurt the video so a human can review/replace the worst 5-10%.
function findWeakSpots(cards: AssetCard[], qa: AssetQASummary | null): WeakSpot[] {
  const out: WeakSpot[] = []
  cards.forEach((c, i) => {
    if (isExcluded(c)) return
    const dur = tcSec(c.timeline_end || c.end) - tcSec(c.timeline_start || c.start)
    if (c.status === 'needs_review') {
      out.push({ cardIndex: i, shotIndex: c.shot_index, reason: c.notes?.split('\n').pop() || 'Требует проверки', severity: 'warn' })
    }
    const reports = qa?.reports[c.card_id]
    Object.values(reports || {}).forEach((report) => {
      if (!report || report.stale || report.recommendation === 'accept') return
      if (report.mode === 'review' && c.status === 'needs_review') return
      const label = report.mode === 'shadow' ? 'QA shadow' : 'QA review'
      out.push({
        cardIndex: i,
        shotIndex: c.shot_index,
        reason: `${label} · ${report.kind}: ${report.summary || report.recommendation} (${report.recommendation})`,
        severity: report.mode === 'shadow' ? 'info' : 'warn',
      })
    })
    if (!reports && typeof c.qa_score === 'number' && c.qa_score >= 0) {
      out.push({ cardIndex: i, shotIndex: c.shot_index, reason: `Устаревшая scalar Vision-QA: ${c.qa_score.toFixed(2)} — перезапустите QA`, severity: 'info' })
    }
    if (c.route === 'stock' && (c.stock_score ?? 0) > 0 && (c.stock_score ?? 0) < 0.5) {
      out.push({ cardIndex: i, shotIndex: c.shot_index, reason: `Слабый сток (score ${(c.stock_score ?? 0).toFixed(2)}) — проверь соответствие`, severity: 'warn' })
    }
    if ((c.route === 'kenburns' || c.visual_type === 'still_kenburns') && dur > 8) {
      out.push({ cardIndex: i, shotIndex: c.shot_index, reason: `Ken Burns ${dur}с — статика дольше 8с скучает, подумай про LTX или разрез`, severity: 'info' })
    }
    if (c.ltx_draft && c.video_output) {
      out.push({ cardIndex: i, shotIndex: c.shot_index, reason: 'LTX-черновик в финале — для hero-кадра прогони «Финал»', severity: 'info' })
    }
    if (!c.video_output && !c.image_output) {
      out.push({ cardIndex: i, shotIndex: c.shot_index, reason: 'Нет медиа — кадр выпадет из финала', severity: 'warn' })
    }
  })
  return out
}

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

export default function Preview() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const { startStream } = useSSE()
  const toast = useToast()
  const navigate = useNavigate()
  const [media, setMedia] = useState<TimelineMedia | null>(null)
  const [cards, setCards] = useState<AssetCard[]>([])
  const [assetQa, setAssetQa] = useState<AssetQASummary | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [exporting, setExporting] = useState(false)
  const [profiles, setProfiles] = useState<ExportProfile[]>([])
  const [draftProfile, setDraftProfile] = useState<ExportProfileName>('delivery')
  const [activeProfile, setActiveProfile] = useState<ExportProfileName | null>(null)
  const [gateReport, setGateReport] = useState<ExportGateReport | null>(null)
  const [technicalQc, setTechnicalQc] = useState<TechnicalMediaQc | null>(null)
  const [masterManifest, setMasterManifest] = useState<MasterManifestInfo | null>(null)
  const [selectedUrl, setSelectedUrl] = useState('')
  const logEndRef = useRef<HTMLDivElement>(null)
  const lockedElsewhere = Boolean(busy) && !exporting

  const load = useCallback(async () => {
    const [next, board, qa, catalog, persistedManifest] = await Promise.all([
      api.getTimelineMedia(projectId).catch(() => null),
      api.getAssetBoard(projectId).catch(() => [] as AssetCard[]),
      api.getAssetQa(projectId).catch(() => null),
      api.getExportProfiles(projectId).catch(() => null),
      api.getMasterManifest(projectId).catch(() => null),
    ])
    setMedia(next)
    if (next?.export_url) setSelectedUrl(next.export_url)
    else if (next?.items[0]?.url) setSelectedUrl(next.items[0].url)
    setCards(board)
    setAssetQa(qa)
    setMasterManifest(persistedManifest)
    if (catalog) {
      setProfiles(catalog.profiles)
      setDraftProfile((current) => catalog.profiles.some((item) => item.name === current) ? current : catalog.default)
    }
  }, [projectId])

  useEffect(() => {
    load()
  }, [load])

  const weakSpots = useMemo(() => findWeakSpots(cards, assetQa), [cards, assetQa])

  const handleCancel = async () => {
    try {
      await api.cancelJob(projectId)
      toast.info('Отмена запрошена — задача остановится на ближайшем шаге')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  // One action, one quality selector. Draft/Master used to be two buttons with
  // different rules; the quality profile is the only difference that matters to
  // the operator, and the strict gate behind "Master" is advisory now. The
  // top-quality profile still writes the audit manifest.
  const handleExport = (profile: ExportProfileName) => {
    if (exporting || busy) { if (busy && !exporting) toast.error('Дождитесь завершения текущей задачи'); return }
    const mode: ExportMode = profile === 'master' ? 'master' : 'draft'
    const profileLabel = profiles.find((item) => item.name === profile)?.label || profile
    setExporting(true)

    setActiveProfile(profile)
    setTechnicalQc(null)
    if (mode === 'master') setMasterManifest(null)
    const label = `Сборка · ${profileLabel}`
    setBusy(label)
    setLogs((prev) => [...prev.slice(-120), `${label}: старт`])

    startStream(api.startExport(projectId, mode, profile), {
      onLog: (msg) => setLogs((prev) => [...prev.slice(-180), msg]),
      onEvent: (event, data) => {
        if (event === 'gate' && data && typeof data.allowed === 'boolean') {
          setGateReport(data as unknown as ExportGateReport)
        }
        if (event === 'error' && data.gate && typeof data.gate === 'object') {
          setGateReport(data.gate as ExportGateReport)
        }
        if (event === 'error' && data.technical_qc && typeof data.technical_qc === 'object') {
          setTechnicalQc(data.technical_qc as TechnicalMediaQc)
        }
      },
      onDone: async (data) => {
        if (data.technical_qc && typeof data.technical_qc === 'object') {
          setTechnicalQc(data.technical_qc as TechnicalMediaQc)
        }
        if (isMasterManifestInfo(data.master_manifest)) {
          setMasterManifest(data.master_manifest)
        }
        setLogs((prev) => [...prev.slice(-180), `${label}: готово ${data.path || ''}`])
        toast.success(`Видео готово · ${profileLabel}`)
        setExporting(false)

        setActiveProfile(null)
        setBusy(null)
        await load()
      },
      onError: (msg) => {
        setLogs((prev) => [...prev.slice(-180), `${label}: ${msg}`])
        toast.error(`${label}: ${msg}`)
        setExporting(false)

        setActiveProfile(null)
        setBusy(null)
      },
    })
  }

  const selectedIsVideo = selectedUrl.toLowerCase().match(/\.(mp4|webm|mov)(\?|$)/)
  const selectedIsImage = selectedUrl.toLowerCase().match(/\.(png|jpg|jpeg|webp)(\?|$)/)
  const draftProfileInfo = profiles.find((item) => item.name === draftProfile)
  const masterProfileInfo = profiles.find((item) => item.name === 'master')
  const activeProfileInfo = profiles.find((item) => item.name === activeProfile)

  return (
    <div className="p-6 space-y-5 max-w-[1600px]">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-white">Экспорт</h1>
          <p className="text-sm text-slate-500 mt-1">Финальное видео со звуком по таймлайну · {media?.items.length || 0} клипов с медиа</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link to="../timeline" className="px-4 py-2 rounded-lg bg-slate-800 border border-slate-700 text-sm text-slate-100">
            Монтаж
          </Link>
          <label className="flex items-center gap-2 rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-xs text-slate-400">
            Качество
            <select
              value={draftProfile}
              onChange={(event) => setDraftProfile(event.target.value as ExportProfileName)}
              disabled={exporting || lockedElsewhere}
              className="bg-slate-950 px-2 py-1 text-sm text-white outline-none disabled:opacity-45"
            >
              {profiles.map((profile) => <option key={profile.name} value={profile.name}>{profile.label}</option>)}
              {!profiles.length && <option value="delivery">Delivery 1080p</option>}
            </select>
          </label>
          <button
            onClick={() => handleExport(draftProfile)}
            disabled={exporting || lockedElsewhere}
            title="Собрать видео с выбранным качеством. Замечания ниже не мешают сборке."
            className="px-5 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold disabled:opacity-45"
          >
            {exporting ? `Собираю · ${activeProfileInfo?.label || activeProfile || ''}…` : 'Собрать видео'}
          </button>
          {exporting && (
            <button onClick={handleCancel} className="px-4 py-2 rounded-lg bg-rose-800 hover:bg-rose-700 text-white text-sm font-semibold">
              Остановить
            </button>
          )}
          {media?.export_url && (
            <a href={api.getDownloadUrl(projectId)} download className="px-4 py-2 rounded-lg bg-emerald-700 text-white text-sm font-semibold">
              Скачать
            </a>
          )}
        </div>
      </div>

      <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-lg border border-slate-800 bg-slate-900/60 px-4 py-2.5 text-xs text-slate-400">
        <span>
          {draftProfileInfo
            ? `${draftProfileInfo.label}: ${draftProfileInfo.width}×${draftProfileInfo.height} · ${draftProfileInfo.fps} fps · ${draftProfileInfo.video_codec.toUpperCase()} · AAC ${draftProfileInfo.audio_bitrate_kbps}k`
            : 'Delivery 1080p'}
        </span>
        <span className="text-slate-600">
          {draftProfile === 'master'
            ? 'Максимальное качество: медленнее, и к файлу выписывается аудит-манифест.'
            : `Для финальной версии выберите «${masterProfileInfo?.label || 'Master'}» — медленнее, но с аудит-манифестом.`}
        </span>
      </div>

      {/* Readiness: findings, not permissions. Shown before the render so the
          answer to "will this ship well" does not require starting a render. */}
      {gateReport && (
        <section className={`rounded-lg border ${gateReport.allowed ? 'border-emerald-700/70 bg-emerald-950/20' : 'border-amber-700/70 bg-amber-950/20'}`}>
          <div className="px-4 py-3 border-b border-white/10 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-white">Готовность материалов</h2>
              <p className="mt-1 text-xs text-slate-400">
                Готово {gateReport.summary.cards_ready} из {gateReport.summary.cards_required} кадров
                {gateReport.summary.cards_excluded ? ` · вне финала ${gateReport.summary.cards_excluded}` : ''}
              </p>
            </div>
            <span className={`rounded px-2.5 py-1 text-xs font-semibold ${gateReport.allowed ? 'bg-emerald-600/20 text-emerald-300' : 'bg-amber-600/20 text-amber-300'}`}>
              {gateReport.allowed ? 'Замечаний нет' : `${gateReport.summary.blockers} замечаний`}
            </span>
          </div>
          {gateReport.reasons.length ? (
            <>
              <p className="px-4 pt-3 text-xs text-slate-400">
                Это подсказки, а не запреты — сборку можно запускать в любом случае.
                Кадр без медиа просто не попадёт в видео, его время займёт соседний план.
              </p>
              <div className="max-h-80 overflow-y-auto divide-y divide-white/5">
                {gateReport.reasons.map((reason, index) => (
                  <div key={`${reason.code}-${reason.card_index ?? 'project'}-${index}`} className="flex items-center gap-3 px-4 py-2.5">
                    <span className="rounded bg-amber-500/15 px-2 py-0.5 text-[10px] font-mono text-amber-300">{reason.code}</span>
                    <span className="min-w-0 flex-1 text-sm text-slate-200">{reason.message}</span>
                    {reason.card_index !== null && (
                      <button
                        onClick={() => navigate(`/project/${projectId}/board?card=${reason.card_index}`)}
                        className="shrink-0 rounded bg-slate-800 border border-slate-700 px-2.5 py-1 text-xs text-slate-200"
                      >
                        Открыть
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="px-4 py-4 text-sm text-emerald-300">Все проверки пройдены — материалы готовы.</div>
          )}
        </section>
      )}

      {technicalQc && (
        <section className={`rounded-lg border ${technicalQc.ok ? 'border-emerald-800 bg-emerald-950/20' : 'border-red-800 bg-red-950/20'}`}>
          <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
            <div>
              <h2 className="text-base font-semibold text-white">Технический QC файла</h2>
              <p className="mt-1 text-xs text-slate-400">
                Профиль {technicalQc.profile || 'delivery'} · длительность {technicalQc.duration.toFixed(1)}с
                {technicalQc.expected ? ` · ${technicalQc.expected.width}×${technicalQc.expected.height}/${technicalQc.expected.fps} fps` : ''}
                {technicalQc.size_bytes ? ` · ${(technicalQc.size_bytes / (1024 * 1024)).toFixed(1)} MB` : ''}
              </p>
              {technicalQc.path && <p className="mt-1 max-w-3xl truncate text-[10px] text-slate-600" title={technicalQc.path}>{technicalQc.path}</p>}
            </div>
            <span className={technicalQc.ok ? 'text-sm text-emerald-300' : 'text-sm text-red-300'}>
              {technicalQc.ok ? `Пройден · предупреждений ${technicalQc.counts.warning}` : `Ошибок ${technicalQc.counts.error}`}
            </span>
          </div>
          {technicalQc.issues.length > 0 && (
            <div className="divide-y divide-white/5">
              {technicalQc.issues.map((issue, index) => (
                <div key={`${issue.code}-${index}`} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                  <span className={issue.level === 'error' ? 'text-red-400' : 'text-amber-400'}>{issue.level}</span>
                  <span className="font-mono text-xs text-slate-500">{issue.code}</span>
                  <span className="text-slate-200">{issue.message}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {masterManifest && (
        <section aria-labelledby="master-manifest-heading" className="overflow-hidden rounded-lg border border-indigo-700/70 bg-indigo-950/20">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-4 py-3">
            <div className="min-w-0">
              <h2 id="master-manifest-heading" className="text-base font-semibold text-white">Master audit manifest</h2>
              <p className="mt-1 truncate text-xs text-slate-400" title={masterManifest.path}>
                Неизменяемая запись сборки · {formatManifestTime(masterManifest.created_at)}
              </p>
            </div>
            <a
              href={api.getMasterManifestDownloadUrl(projectId)}
              download
              className="shrink-0 rounded-lg bg-indigo-600 px-3 py-2 text-sm font-semibold text-white hover:bg-indigo-500"
            >
              Скачать JSON
            </a>
          </div>
          <dl className="grid grid-cols-1 gap-px bg-white/5 sm:grid-cols-2 xl:grid-cols-4">
            <div className="min-w-0 bg-slate-950/40 px-4 py-3">
              <dt className="text-[11px] uppercase tracking-wide text-slate-500">Manifest ID</dt>
              <dd className="mt-1 break-all font-mono text-xs text-slate-200" title={masterManifest.manifest_id}>{masterManifest.manifest_id}</dd>
            </div>
            <div className="min-w-0 bg-slate-950/40 px-4 py-3">
              <dt className="text-[11px] uppercase tracking-wide text-slate-500">Manifest SHA-256</dt>
              <dd className="mt-1 break-all font-mono text-xs text-slate-200" title={masterManifest.sha256}>{masterManifest.sha256}</dd>
            </div>
            <div className="min-w-0 bg-slate-950/40 px-4 py-3">
              <dt className="text-[11px] uppercase tracking-wide text-slate-500">Output SHA-256</dt>
              <dd className="mt-1 break-all font-mono text-xs text-slate-200" title={masterManifest.output_sha256}>{masterManifest.output_sha256}</dd>
            </div>
            <div className="min-w-0 bg-slate-950/40 px-4 py-3">
              <dt className="text-[11px] uppercase tracking-wide text-slate-500">Manifest size</dt>
              <dd className="mt-1 text-sm text-slate-200">{formatBytes(masterManifest.size)}</dd>
            </div>
          </dl>
        </section>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(620px,1fr)_420px] gap-5">
        <section className="rounded-lg bg-slate-900 border border-slate-800 overflow-hidden">
          <div className="aspect-video bg-black flex items-center justify-center">
            {selectedUrl && selectedIsVideo && <video key={selectedUrl} src={selectedUrl} controls className="h-full w-full object-contain" />}
            {selectedUrl && selectedIsImage && <img src={selectedUrl} alt="" className="h-full w-full object-contain" />}
            {!selectedUrl && <div className="text-sm text-slate-700">Нет файла для предпросмотра</div>}
          </div>
          <div className="border-t border-slate-800 p-4">
            <div className="text-sm font-semibold text-white">{media?.export_url && selectedUrl === media.export_url ? 'Final export' : 'Clip preview'}</div>
            <div className="mt-1 truncate text-xs text-slate-500">{selectedUrl || '-'}</div>
          </div>
        </section>

        <aside className="space-y-5">
          <section className="rounded-lg bg-slate-900 border border-slate-800 p-4">
            <h2 className="text-base font-semibold text-white mb-3">Project media</h2>
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {media?.export_url && (
                <button onClick={() => setSelectedUrl(media.export_url)} className={`w-full rounded-lg border px-3 py-2 text-left ${selectedUrl === media.export_url ? 'border-indigo-500 bg-indigo-950/30' : 'border-slate-800 bg-slate-950/50'}`}>
                  <div className="text-sm text-white">Final MP4</div>
                  <div className="truncate text-xs text-slate-500">{media.export_path}</div>
                </button>
              )}
              {media?.items.map((item) => (
                <button
                  key={`${item.card_index}-${item.path}`}
                  onClick={() => setSelectedUrl(item.url)}
                  className={`w-full rounded-lg border px-3 py-2 text-left ${selectedUrl === item.url ? 'border-indigo-500 bg-indigo-950/30' : 'border-slate-800 bg-slate-950/50 hover:border-slate-700'}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm text-white">Shot {item.shot_index}</span>
                    <span className="text-xs text-slate-500">{item.media_type}</span>
                  </div>
                  <div className="truncate text-xs text-slate-500 mt-1">{item.title || item.path}</div>
                </button>
              ))}
              {!media?.items.length && !media?.export_url && <div className="py-10 text-center text-sm text-slate-600">Файлы появятся после генерации ассетов.</div>}
            </div>
          </section>

          <section className="rounded-lg bg-slate-900 border border-slate-800 p-4">
            <h2 className="text-base font-semibold text-white mb-3">Voice track</h2>
            {media?.voice_url ? (
              <>
                <audio src={media.voice_url} controls className="w-full" />
                <div className="mt-2 truncate text-xs text-slate-500">{media.voice_path}</div>
              </>
            ) : (
              <div className="text-sm text-slate-600">Озвучка не найдена.</div>
            )}
          </section>
        </aside>
      </div>

      {/* QC: the weakest shots to review before shipping */}
      <section className="rounded-lg bg-slate-900 border border-slate-800">
        <div className="px-4 py-3 border-b border-slate-800 flex items-center justify-between">
          <h2 className="text-base font-semibold text-white">Контроль качества · слабые места</h2>
          <span className={`text-xs px-2.5 py-1 rounded ${weakSpots.length ? 'text-amber-300 bg-amber-600/20' : 'text-emerald-300 bg-emerald-600/20'}`}>
            {weakSpots.length ? `${weakSpots.length} к проверке` : 'чисто'}
          </span>
        </div>
        <div className="max-h-72 overflow-y-auto divide-y divide-slate-800/60">
          {weakSpots.map((w, i) => (
            <div key={`${w.cardIndex}-${i}`} className="flex items-center gap-3 px-4 py-2.5">
              <span className={`h-2 w-2 shrink-0 rounded-full ${w.severity === 'warn' ? 'bg-amber-400' : 'bg-sky-500'}`} />
              <span className="w-16 shrink-0 text-xs font-mono text-slate-400">#{w.shotIndex}</span>
              <span className="flex-1 min-w-0 text-sm text-slate-300 truncate" title={w.reason}>{w.reason}</span>
              <button
                onClick={() => navigate(`/project/${projectId}/board?card=${w.cardIndex}`)}
                className="shrink-0 text-xs px-2.5 py-1 rounded bg-slate-800 border border-slate-700 text-slate-200 hover:bg-slate-700 cursor-pointer"
              >
                Открыть
              </button>
            </div>
          ))}
          {!weakSpots.length && (
            <div className="px-4 py-6 text-center text-sm text-slate-600">
              Подозрительных кадров не найдено. Посмотри финал целиком глазами — и в публикацию.
            </div>
          )}
        </div>
      </section>

      <section className="rounded-lg bg-slate-900 border border-slate-800">
        <div className="px-4 py-3 border-b border-slate-800">
          <h2 className="text-base font-semibold text-white">Export log</h2>
        </div>
        <div className="h-64 overflow-y-auto p-4 font-mono text-xs text-slate-400 space-y-1">
          {logs.map((line, index) => <div key={`${line}-${index}`}>{line}</div>)}
          {!logs.length && <div className="text-slate-700">Нет активного экспорта.</div>}
          <div ref={logEndRef} />
        </div>
      </section>
    </div>
  )
}
