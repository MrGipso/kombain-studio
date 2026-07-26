import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { type BlockerFunction, useBlocker, useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useSSE } from '../hooks/useSSE'
import { useToast } from '../components/toast-context'
import type { ProjectDetail, Scene, VideoStyle } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

interface SceneSettingsDraft {
  videoStyle: string
  styleAnchor: string
  charDesc: string
  segMin: number
  segMax: number
  segSensitivity: number
  segTarget: number
}

const SCENE_FIELDS: { key: Exclude<keyof Scene, 'index'>; label: string; multiline?: boolean }[] = [
  { key: 'title', label: 'Название' },
  { key: 'start', label: 'Начало' },
  { key: 'end', label: 'Конец' },
  { key: 'voice_text', label: 'Текст', multiline: true },
  { key: 'visual_direction', label: 'Визуальное направление', multiline: true },
  { key: 'screen_text', label: 'Текст на экране', multiline: true },
  { key: 'music_note', label: 'Музыка', multiline: true },
  { key: 'retention_note', label: 'Заметка по удержанию', multiline: true },
]

function settingsFromProject(project: ProjectDetail): SceneSettingsDraft {
  return {
    videoStyle: project.video_style,
    styleAnchor: project.style_anchor,
    charDesc: project.character_description,
    segMin: project.seg_min_seconds,
    segMax: project.seg_max_seconds,
    segSensitivity: project.seg_sensitivity,
    segTarget: project.seg_target_scenes,
  }
}

function settingsEqual(left: SceneSettingsDraft, right: SceneSettingsDraft): boolean {
  return Object.keys(left).every((key) => (
    left[key as keyof SceneSettingsDraft] === right[key as keyof SceneSettingsDraft]
  ))
}

function sceneEqual(left: Scene, right: Scene): boolean {
  return SCENE_FIELDS.every(({ key }) => left[key] === right[key])
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export default function Scenes() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const { startStream, stopStream } = useSSE()
  const toast = useToast()
  const [scenes, setScenes] = useState<Scene[]>([])
  const [selected, setSelected] = useState<Scene | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [hasSegments, setHasSegments] = useState(false)
  const [styles, setStyles] = useState<VideoStyle[]>([])
  const [videoStyle, setVideoStyle] = useState('')
  const [styleAnchor, setStyleAnchor] = useState('')
  const [charDesc, setCharDesc] = useState('')
  const [segMin, setSegMin] = useState(20)
  const [segMax, setSegMax] = useState(90)
  const [segSensitivity, setSegSensitivity] = useState(0.6)
  const [segTarget, setSegTarget] = useState(0)
  const [settingsBaseline, setSettingsBaseline] = useState<SceneSettingsDraft | null>(null)
  const [pageLoading, setPageLoading] = useState(true)
  const [settingsLoaded, setSettingsLoaded] = useState(false)
  const [scenesLoaded, setScenesLoaded] = useState(false)
  const [sceneStageStatus, setSceneStageStatus] = useState<string | null>(null)
  const [sceneConflict, setSceneConflict] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [stylesError, setStylesError] = useState<string | null>(null)
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [refreshingScenes, setRefreshingScenes] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)
  const [savingScene, setSavingScene] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [loadVersion, setLoadVersion] = useState(0)
  const previousBusyRef = useRef<string | null>(busy)
  const busyRef = useRef<string | null>(busy)
  const mountedRef = useRef(false)
  const pageGenerationRef = useRef(0)
  const refreshSequenceRef = useRef(0)
  const sceneStageRevisionRef = useRef<number | null>(null)
  const disconnectPollTimerRef = useRef<number | null>(null)

  const appendLog = useCallback((message: string) => {
    setLogs((previous) => [...previous.slice(-199), message])
  }, [])

  const clearDisconnectPoll = useCallback(() => {
    if (disconnectPollTimerRef.current !== null) {
      window.clearTimeout(disconnectPollTimerRef.current)
      disconnectPollTimerRef.current = null
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      clearDisconnectPoll()
      mountedRef.current = false
      pageGenerationRef.current += 1
      refreshSequenceRef.current += 1
    }
  }, [clearDisconnectPoll])

  useEffect(() => {
    busyRef.current = busy
  }, [busy])

  useEffect(() => {
    let active = true
    const generation = pageGenerationRef.current + 1
    pageGenerationRef.current = generation
    refreshSequenceRef.current += 1
    sceneStageRevisionRef.current = null
    clearDisconnectPoll()
    stopStream()
    previousBusyRef.current = busyRef.current
    setRunning(false)
    setCancelling(false)
    setPageLoading(true)
    setSettingsLoaded(false)
    setScenesLoaded(false)
    setSceneStageStatus(null)
    setSceneConflict(false)
    setRefreshingScenes(false)
    setLoadError(null)
    setStylesError(null)
    setRefreshError(null)
    setSettingsBaseline(null)
    setScenes([])
    setSelected(null)
    setStyles([])
    setVideoStyle('')
    setStyleAnchor('')
    setCharDesc('')
    setHasSegments(false)
    setLogs([])

    Promise.allSettled([
      api.getProject(projectId),
      api.getScenes(projectId),
      api.getVideoStyles(),
      api.getProjectState(projectId),
    ]).then(([projectResult, scenesResult, stylesResult, stateResult]) => {
      if (!active || generation !== pageGenerationRef.current) return
      const failures: string[] = []

      if (projectResult.status === 'fulfilled') {
        const project = projectResult.value
        const draft = settingsFromProject(project)
        setVideoStyle(draft.videoStyle)
        setStyleAnchor(draft.styleAnchor)
        setCharDesc(draft.charDesc)
        setSegMin(draft.segMin)
        setSegMax(draft.segMax)
        setSegSensitivity(draft.segSensitivity)
        setSegTarget(draft.segTarget)
        setSettingsBaseline(draft)
        setHasSegments(Array.isArray(project.segments) && project.segments.length > 0)
        setSettingsLoaded(true)
      } else {
        failures.push(`настройки проекта: ${errorMessage(projectResult.reason)}`)
      }

      if (scenesResult.status === 'fulfilled') {
        const nextScenes = scenesResult.value
        setScenes(nextScenes)
        setSelected(nextScenes[0] ? { ...nextScenes[0] } : null)
        setScenesLoaded(true)
      } else {
        failures.push(`список сцен: ${errorMessage(scenesResult.reason)}`)
      }

      if (stylesResult.status === 'fulfilled') {
        setStyles(stylesResult.value)
      } else {
        setStylesError(`Каталог стилей недоступен: ${errorMessage(stylesResult.reason)}`)
      }

      if (stateResult.status === 'fulfilled') {
        const stage = stateResult.value.stage_states?.scenes
        setSceneStageStatus(stage?.status || null)
        sceneStageRevisionRef.current = stage?.revision ?? null
      } else {
        setRefreshError(`Не удалось проверить актуальность сцен: ${errorMessage(stateResult.reason)}`)
      }

      setLoadError(failures.length ? `Не удалось загрузить ${failures.join('; ')}` : null)
      setPageLoading(false)
    })

    return () => {
      active = false
      stopStream()
    }
  }, [clearDisconnectPoll, loadVersion, projectId, stopStream])

  const currentSettings = useMemo<SceneSettingsDraft>(() => ({
    videoStyle,
    styleAnchor,
    charDesc,
    segMin,
    segMax,
    segSensitivity,
    segTarget,
  }), [charDesc, segMax, segMin, segSensitivity, segTarget, styleAnchor, videoStyle])

  const settingsDirty = Boolean(settingsBaseline && !settingsEqual(settingsBaseline, currentSettings))
  const persistedSelected = selected
    ? scenes.find((scene) => scene.index === selected.index) || null
    : null
  const sceneDirty = Boolean(selected && (!persistedSelected || !sceneEqual(selected, persistedSelected)))
  const hasUnsavedChanges = sceneDirty || settingsDirty
  const shouldBlockNavigation = useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) => (
      hasUnsavedChanges && currentLocation.pathname !== nextLocation.pathname
    ),
    [hasUnsavedChanges],
  )
  const navigationBlocker = useBlocker(shouldBlockNavigation)
  const resetBlockedNavigation = navigationBlocker.state === 'blocked' ? navigationBlocker.reset : null
  const proceedBlockedNavigation = navigationBlocker.state === 'blocked' ? navigationBlocker.proceed : null
  // useBlocker only covers in-app navigation. Closing the tab or reloading
  // bypasses the router, so unsaved scene text and segmentation settings would
  // be discarded without any prompt. The browser supplies its own message.
  useEffect(() => {
    if (!hasUnsavedChanges) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      // Older engines still require returnValue to be set for the prompt.
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [hasUnsavedChanges])
  const selectedStyle = styles.find((style) => style.id === videoStyle)
  const lockedElsewhere = Boolean(busy) && !running
  const mutationsLocked = Boolean(busy)
    || running
    || pageLoading
    || Boolean(loadError)
    || savingSettings
    || savingScene
    || exporting

  const settingsValidationError = useMemo(() => {
    if (!Number.isInteger(segMin) || segMin < 2) return 'Минимальная длительность должна быть целым числом не меньше 2 секунд.'
    if (!Number.isInteger(segMax) || segMax < 5) return 'Максимальная длительность должна быть целым числом не меньше 5 секунд.'
    if (segMin > segMax) return 'Минимальная длительность не может быть больше максимальной.'
    if (!Number.isFinite(segSensitivity) || segSensitivity < 0 || segSensitivity > 1) return 'Чувствительность должна быть от 0 до 1.'
    if (!Number.isInteger(segTarget) || segTarget < 0) return 'Число сцен должно быть целым неотрицательным числом.'
    return null
  }, [segMax, segMin, segSensitivity, segTarget])

  useEffect(() => {
    if (!resetBlockedNavigation || !proceedBlockedNavigation) return
    const message = sceneDirty && settingsDirty
      ? 'Есть несохранённые изменения сцены и настроек. Покинуть раздел и потерять их?'
      : sceneDirty
        ? 'Есть несохранённые изменения сцены. Покинуть раздел и потерять их?'
        : 'Есть несохранённые настройки анализа. Покинуть раздел и потерять их?'
    if (window.confirm(message)) proceedBlockedNavigation()
    else resetBlockedNavigation()
  }, [proceedBlockedNavigation, resetBlockedNavigation, sceneDirty, settingsDirty])

  useEffect(() => {
    if (!hasUnsavedChanges) return
    const preventUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', preventUnload)
    return () => window.removeEventListener('beforeunload', preventUnload)
  }, [hasUnsavedChanges])

  const refreshScenes = useCallback(async (preserveDraft: boolean) => {
    const sequence = refreshSequenceRef.current + 1
    refreshSequenceRef.current = sequence
    const generation = pageGenerationRef.current
    setRefreshingScenes(true)
    setRefreshError(null)
    try {
      const [scenesResult, stateResult] = await Promise.allSettled([
        api.getScenes(projectId),
        api.getProjectState(projectId),
      ])
      if (!mountedRef.current
        || generation !== pageGenerationRef.current
        || sequence !== refreshSequenceRef.current) return
      if (scenesResult.status === 'rejected') throw scenesResult.reason
      const nextScenes = scenesResult.value
      if (stateResult.status === 'fulfilled') {
        const stage = stateResult.value.stage_states?.scenes
        const nextRevision = stage?.revision ?? null
        const revisionUnknown = sceneStageRevisionRef.current === null || nextRevision === null
        const revisionChanged = revisionUnknown || nextRevision !== sceneStageRevisionRef.current
        setSceneStageStatus(stage?.status || null)
        if (preserveDraft && revisionChanged) {
          setSceneConflict(true)
          setRefreshError('Серверная версия сцен изменилась, пока у вас были несохранённые правки. Сбросьте черновик и обновите данные; сохранение поверх новой версии заблокировано.')
          return
        }
        sceneStageRevisionRef.current = nextRevision
      } else {
        if (preserveDraft) {
          setSceneConflict(true)
          setRefreshError(`Не удалось безопасно сравнить черновик с серверной версией: ${errorMessage(stateResult.reason)}`)
          return
        }
        setRefreshError(`Сцены обновлены, но статус этапа недоступен: ${errorMessage(stateResult.reason)}`)
      }
      setScenes(nextScenes)
      setSelected((current) => {
        if (preserveDraft && current) return current
        const nextSelected = current
          ? nextScenes.find((scene) => scene.index === current.index)
          : null
        return nextSelected ? { ...nextSelected } : (nextScenes[0] ? { ...nextScenes[0] } : null)
      })
      if (!preserveDraft) setSceneConflict(false)
      setScenesLoaded(true)
    } catch (error) {
      if (mountedRef.current
        && generation === pageGenerationRef.current
        && sequence === refreshSequenceRef.current) {
        setRefreshError(`Не удалось обновить сцены: ${errorMessage(error)}`)
      }
    } finally {
      if (mountedRef.current
        && generation === pageGenerationRef.current
        && sequence === refreshSequenceRef.current) {
        setRefreshingScenes(false)
      }
    }
  }, [projectId])

  useEffect(() => {
    const previousBusy = previousBusyRef.current
    previousBusyRef.current = busy
    if (previousBusy && !busy && !running && scenesLoaded) {
      void refreshScenes(sceneDirty)
    }
  }, [busy, refreshScenes, running, sceneDirty, scenesLoaded])

  const saveSettings = async (announce = true): Promise<boolean> => {
    if (!settingsLoaded || pageLoading || loadError) {
      toast.error('Настройки проекта ещё не загружены')
      return false
    }
    if (!settingsDirty) return true
    if (busyRef.current || running) {
      toast.error('Дождитесь завершения текущей задачи')
      return false
    }
    if (settingsValidationError) {
      toast.error(settingsValidationError)
      return false
    }

    setSavingSettings(true)
    try {
      const saved = await api.updateProjectSettings(projectId, {
        video_style: videoStyle,
        style_anchor: styleAnchor,
        character_description: charDesc,
        seg_min_seconds: segMin,
        seg_max_seconds: segMax,
        seg_sensitivity: segSensitivity,
        seg_target_scenes: segTarget,
      })
      const baseline: SceneSettingsDraft = {
        videoStyle: saved.video_style,
        styleAnchor: saved.style_anchor,
        charDesc: saved.character_description,
        segMin: saved.seg_min_seconds,
        segMax: saved.seg_max_seconds,
        segSensitivity: saved.seg_sensitivity,
        segTarget: saved.seg_target_scenes,
      }
      setVideoStyle(baseline.videoStyle)
      setStyleAnchor(baseline.styleAnchor)
      setCharDesc(baseline.charDesc)
      setSegMin(baseline.segMin)
      setSegMax(baseline.segMax)
      setSegSensitivity(baseline.segSensitivity)
      setSegTarget(baseline.segTarget)
      setSettingsBaseline(baseline)
      if (scenes.length) setSceneStageStatus('stale')
      appendLog(announce ? '✅ Настройки анализа сохранены' : '✅ Настройки сохранены перед анализом')
      if (announce) toast.success('Настройки анализа сохранены')
      return true
    } catch (error) {
      appendLog(`❌ Ошибка сохранения настроек: ${errorMessage(error)}`)
      toast.error(`Сохранение: ${errorMessage(error)}`)
      return false
    } finally {
      setSavingSettings(false)
    }
  }

  const handleAnalyze = async () => {
    const generation = pageGenerationRef.current
    if (pageLoading || !settingsLoaded || !scenesLoaded || loadError) {
      toast.error('Дождитесь успешной загрузки проекта')
      return
    }
    if (!hasSegments) {
      toast.error('Сначала выполните транскрипцию')
      return
    }
    if (busyRef.current || running) {
      toast.error('Дождитесь завершения текущей задачи')
      return
    }
    if (refreshingScenes) {
      toast.error('Дождитесь обновления серверных данных')
      return
    }
    if (sceneDirty && !window.confirm('В выбранной сцене есть несохранённые изменения. Отбросить их и запустить повторный анализ?')) {
      return
    }
    if (settingsDirty && !(await saveSettings(false))) return
    if (!mountedRef.current || generation !== pageGenerationRef.current) return
    if (busyRef.current) {
      toast.error('Пока сохранялись настройки, была запущена другая задача. Повторите запуск позже.')
      return
    }

    if (sceneDirty) setSelected(persistedSelected ? { ...persistedSelected } : null)
    clearDisconnectPoll()
    refreshSequenceRef.current += 1
    setRefreshingScenes(false)
    setSceneConflict(false)
    setRunning(true)
    busyRef.current = 'Анализ сцен'
    setBusy('Анализ сцен')
    appendLog('⏳ Анализ сцен...')

    startStream(api.startSceneAnalysis(projectId), {
      onLog: appendLog,
      onDone: (data) => {
        if (!mountedRef.current || generation !== pageGenerationRef.current) return
        clearDisconnectPoll()
        refreshSequenceRef.current += 1
        setRefreshingScenes(false)
        const nextScenes = Array.isArray(data.scenes) ? data.scenes as Scene[] : []
        setScenes(nextScenes)
        setSelected(nextScenes[0] ? { ...nextScenes[0] } : null)
        setScenesLoaded(true)
        setSceneStageStatus('completed')
        setSceneConflict(false)
        api.getProjectState(projectId).then((state) => {
          if (!mountedRef.current || generation !== pageGenerationRef.current) return
          const stage = state.stage_states?.scenes
          sceneStageRevisionRef.current = stage?.revision ?? null
          setSceneStageStatus(stage?.status || 'completed')
        }).catch(() => {})
        appendLog(`✅ Сцен найдено: ${nextScenes.length}`)
        toast.success(`Найдено сцен: ${nextScenes.length}`)
        setRunning(false)
        setCancelling(false)
        busyRef.current = null
        setBusy(null)
      },
      onError: (message) => {
        if (!mountedRef.current || generation !== pageGenerationRef.current) return
        clearDisconnectPoll()
        appendLog(`❌ ${message}`)
        toast.error(`Анализ сцен: ${message}`)
        api.getProjectState(projectId).then((state) => {
          if (!mountedRef.current || generation !== pageGenerationRef.current) return
          setSceneStageStatus(state.stage_states?.scenes?.status || 'failed')
        }).catch(() => {
          if (mountedRef.current && generation === pageGenerationRef.current) {
            setSceneStageStatus('failed')
          }
        })
        setRunning(false)
        setCancelling(false)
        busyRef.current = null
        setBusy(null)
      },
      onDisconnect: (message) => {
        if (!mountedRef.current || generation !== pageGenerationRef.current) return
        appendLog(`⚠️ ${message}. Серверная задача может продолжаться.`)
      },
      onClose: (reason) => {
        if (reason !== 'closed'
          || !mountedRef.current
          || generation !== pageGenerationRef.current) return
        appendLog('⚠️ Поток анализа закрылся до итогового сообщения. Проверяем задачу на сервере...')
        setRunning(false)
        setCancelling(false)
        clearDisconnectPoll()
        const pollDisconnectedJob = (attempt = 0) => {
          api.getProjectJobs(projectId, 1).then((state) => {
            if (!mountedRef.current || generation !== pageGenerationRef.current) return
            busyRef.current = state.running
            setBusy(state.running)
            if (state.running) {
              disconnectPollTimerRef.current = window.setTimeout(
                () => pollDisconnectedJob(attempt + 1),
                1500,
              )
              return
            }
            disconnectPollTimerRef.current = null
            appendLog('✅ Серверная задача завершилась; обновляем сцены.')
            void refreshScenes(false)
          }).catch((error) => {
            if (!mountedRef.current || generation !== pageGenerationRef.current) return
            if (attempt === 0) {
              appendLog(`⚠️ Не удалось проверить задачу: ${errorMessage(error)}. Повторяем проверку...`)
            }
            disconnectPollTimerRef.current = window.setTimeout(
              () => pollDisconnectedJob(attempt + 1),
              Math.min(5000, 1500 + attempt * 500),
            )
          })
        }
        pollDisconnectedJob()
      },
    })
  }

  const handleSelectScene = (scene: Scene) => {
    if (selected?.index === scene.index) return
    if (sceneConflict) {
      toast.error('Сначала сбросьте конфликтующий черновик и обновите сцены')
      return
    }
    if (sceneDirty && !window.confirm(`Изменения в сцене ${selected?.index} не сохранены. Перейти к другой сцене и отбросить их?`)) {
      return
    }
    setSelected({ ...scene })
  }

  const handleSaveScene = async () => {
    if (!selected || !sceneDirty) return
    if (sceneConflict) {
      toast.error('Серверная версия изменилась. Сбросьте черновик и обновите сцены.')
      return
    }
    if (busyRef.current || running) {
      toast.error('Дождитесь завершения текущей задачи')
      return
    }
    setSavingScene(true)
    try {
      const saved = await api.updateScene(projectId, selected.index, selected)
      setScenes((previous) => previous.map((scene) => scene.index === saved.index ? saved : scene))
      setSelected({ ...saved })
      setSceneStageStatus((current) => (
        current === 'stale' || current === 'failed' ? current : 'completed'
      ))
      api.getProjectState(projectId).then((state) => {
        sceneStageRevisionRef.current = state.stage_states?.scenes?.revision ?? null
      }).catch(() => {
        sceneStageRevisionRef.current = null
      })
      appendLog(`✅ Сцена ${saved.index} сохранена`)
      toast.success(`Сцена ${saved.index} сохранена`)
    } catch (error) {
      appendLog(`❌ Ошибка сохранения сцены: ${errorMessage(error)}`)
      toast.error(`Сохранение: ${errorMessage(error)}`)
    } finally {
      setSavingScene(false)
    }
  }

  const handleExport = async () => {
    if (!scenes.length) {
      toast.error('Нет сцен для экспорта')
      return
    }
    if (busyRef.current || running) {
      toast.error('Дождитесь завершения текущей задачи')
      return
    }
    setExporting(true)
    try {
      const result = await api.exportScenes(projectId)
      appendLog(`📦 Экспортировано ${result.count} сцен`)
      appendLog(`   CSV: ${result.csv}`)
      appendLog(`   MD:  ${result.md}`)
      toast.success(`Экспортировано ${result.count} сцен (CSV + MD)`)
    } catch (error) {
      appendLog(`❌ Ошибка экспорта: ${errorMessage(error)}`)
      toast.error(`Экспорт: ${errorMessage(error)}`)
    } finally {
      setExporting(false)
    }
  }

  const handleCancel = async () => {
    setCancelling(true)
    try {
      const result = await api.cancelJob(projectId)
      if (result.cancelled) toast.info('Отмена запрошена — задача остановится на ближайшем шаге')
      else toast.info('Активная задача уже завершилась')
    } catch (error) {
      toast.error(`Не удалось отменить задачу: ${errorMessage(error)}`)
      setCancelling(false)
    }
  }

  const settingsControlsDisabled = !settingsLoaded || mutationsLocked || savingSettings
  const analyzeDisabled = pageLoading
    || !settingsLoaded
    || !scenesLoaded
    || Boolean(loadError)
    || !hasSegments
    || mutationsLocked
    || savingSettings
    || savingScene
    || refreshingScenes
    || Boolean(settingsValidationError)

  return (
    <div className="space-y-5 p-4 sm:p-6 xl:p-8" aria-busy={pageLoading || running || refreshingScenes}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">Шаг 03</p>
          <h1 className="mt-1 text-2xl font-bold text-white">Сцены</h1>
        </div>
        {scenesLoaded && (
          <span className="rounded-full border border-slate-700 bg-slate-900 px-3 py-1 text-xs text-slate-400">
            {scenes.length} {scenes.length === 1 ? 'сцена' : 'сцен'}
          </span>
        )}
      </div>

      {pageLoading && (
        <div role="status" aria-live="polite" className="rounded-xl border border-slate-800 bg-slate-900 px-4 py-4 text-sm text-slate-400">
          Загружаем сцены и настройки проекта…
        </div>
      )}

      {loadError && (
        <div role="alert" className="flex flex-col gap-3 rounded-xl border border-rose-800/70 bg-rose-950/35 p-4 text-sm text-rose-200 sm:flex-row sm:items-center sm:justify-between">
          <span>{loadError}</span>
          <button
            type="button"
            onClick={() => setLoadVersion((version) => version + 1)}
            className="shrink-0 rounded-lg border border-rose-700 bg-rose-900/60 px-3 py-2 font-semibold text-white hover:bg-rose-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-400"
          >
            Повторить загрузку
          </button>
        </div>
      )}

      <div className="flex flex-col gap-3 sm:flex-row">
        <button
          type="button"
          onClick={() => void handleAnalyze()}
          disabled={analyzeDisabled}
          title={settingsValidationError || (!hasSegments ? 'Сначала выполните транскрипцию' : undefined)}
          className="min-h-11 cursor-pointer rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-50 sm:min-w-64"
        >
          {running
            ? 'Анализ сцен…'
            : settingsDirty
              ? 'Сохранить настройки и анализировать'
              : 'Анализировать сцены'}
        </button>
        <button
          type="button"
          onClick={() => void handleExport()}
          disabled={!scenes.length || mutationsLocked || refreshingScenes}
          className="min-h-11 cursor-pointer rounded-lg border border-slate-700 bg-slate-800 px-5 py-2.5 text-sm font-semibold text-slate-200 transition-colors hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {exporting ? 'Экспорт…' : 'Экспорт'}
        </button>
      </div>

      {settingsLoaded && settingsValidationError && (
        <div role="alert" className="rounded-xl border border-amber-800/70 bg-amber-950/35 p-4 text-sm text-amber-200">
          Проверьте настройки анализа: {settingsValidationError}
        </div>
      )}

      {lockedElsewhere && (
        <div role="status" aria-live="polite" className="rounded-xl border border-sky-700/50 bg-sky-900/30 p-4 text-sm text-sky-300">
          ⏳ Идёт задача «{busy}». Редактирование и экспорт временно заблокированы.
        </div>
      )}

      {settingsLoaded && !hasSegments && !running && (
        <div role="status" className="rounded-xl border border-amber-700/50 bg-amber-900/30 p-4 text-sm text-amber-300">
          ⚠️ Сначала выполните транскрипцию, чтобы получить сегменты для анализа сцен.
        </div>
      )}

      {running && (
        <div role="status" aria-live="polite" className="flex flex-wrap items-center gap-3 rounded-lg border border-indigo-700/50 bg-indigo-900/40 px-4 py-3">
          <span aria-hidden="true" className="h-3 w-3 rounded-full bg-indigo-400 motion-safe:animate-ping" />
          <span className="min-w-0 flex-1 text-sm font-semibold text-indigo-300">
            Анализ сцен… Существующий список останется на экране до готовности нового результата.
          </span>
          <button
            type="button"
            onClick={() => void handleCancel()}
            disabled={cancelling}
            className="shrink-0 cursor-pointer rounded bg-rose-800 px-3 py-1.5 text-xs text-white hover:bg-rose-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-400 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {cancelling ? 'Отменяем…' : 'Отменить'}
          </button>
          <div role="progressbar" aria-label="Анализ сцен выполняется" className="h-1.5 w-full overflow-hidden rounded-full bg-slate-700 sm:w-40">
            <div className="h-full w-1/3 rounded-full bg-indigo-500 motion-safe:animate-[indeterminate_1.2s_ease-in-out_infinite]" />
          </div>
        </div>
      )}

      {refreshError && (
        <div role="alert" className="flex flex-col gap-3 rounded-xl border border-amber-800/60 bg-amber-950/30 p-4 text-sm text-amber-200 sm:flex-row sm:items-center sm:justify-between">
          <span>{refreshError}</span>
          <button
            type="button"
            onClick={() => {
              void refreshScenes(sceneConflict ? false : sceneDirty)
            }}
            className="shrink-0 rounded-lg border border-amber-700 px-3 py-2 font-semibold hover:bg-amber-900/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-400"
          >
            {sceneConflict ? 'Сбросить черновик и обновить' : 'Обновить снова'}
          </button>
        </div>
      )}

      {scenes.length > 0 && sceneStageStatus === 'stale' && !running && (
        <div role="status" className="rounded-xl border border-amber-800/70 bg-amber-950/35 p-4 text-sm text-amber-200">
          Эти сцены построены по прежним входным данным или настройкам. Они сохранены для сравнения, но перед генерацией кадров анализ нужно запустить заново.
        </div>
      )}

      {scenes.length > 0 && sceneStageStatus === 'failed' && !running && (
        <div role="alert" className="rounded-xl border border-rose-800/70 bg-rose-950/35 p-4 text-sm text-rose-200">
          Последняя попытка анализа завершилась ошибкой. Ниже оставлен предыдущий успешный план сцен; повторный запуск не удалит его до получения нового результата.
        </div>
      )}

      {scenesLoaded && scenes.length > 0 && (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(260px,320px)_minmax(0,1fr)] xl:gap-6">
          <section aria-label="Список сцен" className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900">
            <div className="border-b border-slate-800 px-4 py-3">
              <h2 className="text-sm font-semibold text-slate-200">План сцен</h2>
              {refreshingScenes && <p role="status" className="mt-1 text-xs text-slate-500">Обновляем данные…</p>}
            </div>
            <div className="max-h-80 divide-y divide-slate-800 overflow-y-auto xl:max-h-[65vh]">
              {scenes.map((scene) => (
                <button
                  type="button"
                  key={scene.index}
                  onClick={() => handleSelectScene(scene)}
                  aria-current={selected?.index === scene.index ? 'true' : undefined}
                  className={`w-full cursor-pointer border-l-2 px-4 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-indigo-400 ${
                    selected?.index === scene.index
                      ? 'border-indigo-500 bg-indigo-600/20'
                      : 'border-transparent hover:bg-slate-800'
                  }`}
                >
                  <div className="text-sm font-medium text-white">Сцена {scene.index}</div>
                  <div className="mt-0.5 text-xs text-slate-500">{scene.start} – {scene.end}</div>
                  <p className="mt-0.5 truncate text-sm text-slate-400">{scene.title}</p>
                </button>
              ))}
            </div>
          </section>

          {selected && (
            <section aria-labelledby="selected-scene-heading" className="min-w-0 space-y-4 rounded-xl border border-slate-800 bg-slate-900 p-4 sm:p-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <h2 id="selected-scene-heading" className="text-lg font-semibold text-white">Сцена {selected.index}</h2>
                  {sceneDirty && <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-semibold text-amber-300">не сохранено</span>}
                </div>
                <span className="text-xs text-slate-500">{selected.start} – {selected.end}</span>
              </div>
              <div className="space-y-3">
                {SCENE_FIELDS.map(({ key, label, multiline }) => {
                  const inputId = `scene-${selected.index}-${key}`
                  return (
                    <div key={key}>
                      <label htmlFor={inputId} className="mb-1 block text-xs text-slate-400">{label}</label>
                      {multiline ? (
                        <textarea
                          id={inputId}
                          value={selected[key] || ''}
                          onChange={(event) => setSelected({ ...selected, [key]: event.target.value })}
                          disabled={mutationsLocked || savingScene || refreshingScenes}
                          className="h-20 w-full resize-y rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
                        />
                      ) : (
                        <input
                          id={inputId}
                          value={selected[key] || ''}
                          onChange={(event) => setSelected({ ...selected, [key]: event.target.value })}
                          disabled={mutationsLocked || savingScene || refreshingScenes}
                          className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
                        />
                      )}
                    </div>
                  )
                })}
                <button
                  type="button"
                  onClick={() => void handleSaveScene()}
                  disabled={!sceneDirty || sceneConflict || mutationsLocked || savingScene || refreshingScenes}
                  className="cursor-pointer rounded-lg bg-indigo-600 px-6 py-2 text-sm font-semibold text-white hover:bg-indigo-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {savingScene ? 'Сохраняем…' : 'Сохранить сцену'}
                </button>
              </div>
            </section>
          )}
        </div>
      )}

      {scenesLoaded && !scenes.length && !pageLoading && (
        <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/60 px-6 py-10 text-center">
          <h2 className="text-base font-semibold text-slate-300">Сцен пока нет</h2>
          <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-slate-500">
            {hasSegments
              ? 'Запустите анализ — результат появится здесь после успешного завершения.'
              : 'Сначала подготовьте транскрипцию, затем вернитесь к анализу сцен.'}
          </p>
        </div>
      )}

      <details className="group overflow-hidden rounded-xl border border-slate-800 bg-slate-900">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-4 text-sm font-semibold text-slate-200 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-indigo-400 sm:px-5">
          <span>
            Настройки анализа и визуального стиля
            <span className="ml-2 font-normal text-slate-500">расширенные</span>
          </span>
          <span className="flex items-center gap-2">
            {settingsDirty && <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-300">есть изменения</span>}
            <span aria-hidden="true" className="text-slate-500 transition-transform group-open:rotate-180">⌄</span>
          </span>
        </summary>

        <div className="space-y-4 border-t border-slate-800 p-4 sm:p-5">
          {!settingsLoaded && (
            <p role="status" className="text-sm text-slate-500">Настройки недоступны до успешной загрузки проекта.</p>
          )}
          {stylesError && <p role="status" className="text-sm text-amber-300">{stylesError}</p>}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(220px,260px)_minmax(0,1fr)]">
            <div>
              <label htmlFor="scene-video-style" className="mb-1 block text-xs text-slate-400">Стиль видео</label>
              <select
                id="scene-video-style"
                value={videoStyle}
                onChange={(event) => setVideoStyle(event.target.value)}
                disabled={settingsControlsDisabled || !styles.length}
                className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {videoStyle && !styles.some((style) => style.id === videoStyle) && (
                  <option value={videoStyle}>{videoStyle}</option>
                )}
                {styles.map((style) => <option key={style.id} value={style.id}>{style.name}</option>)}
                {!videoStyle && <option value="">Загрузка…</option>}
              </select>
            </div>
            <div className="rounded-lg border border-slate-800 bg-slate-950/50 px-4 py-3">
              <div className="text-sm font-semibold text-white">{selectedStyle?.name || videoStyle || 'Стиль не загружен'}</div>
              <p className="mt-1 text-xs leading-relaxed text-slate-400">
                {selectedStyle?.description || 'Описание этого стиля сейчас недоступно; сохранённое значение проекта не изменено.'}
              </p>
            </div>
          </div>

          {selectedStyle && (
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              <div>
                <label htmlFor="scene-style-prompt" className="mb-1 block text-xs text-slate-400">Style prompt</label>
                <textarea
                  id="scene-style-prompt"
                  value={selectedStyle.base_prompt}
                  readOnly
                  className="h-24 w-full resize-none rounded border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400"
                />
              </div>
              <div>
                <p id="scene-style-rules-label" className="mb-1 text-xs text-slate-400">Правила планирования кадров</p>
                <div role="list" aria-labelledby="scene-style-rules-label" className="h-24 space-y-1 overflow-y-auto rounded border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-400">
                  {selectedStyle.shot_planning_rules.map((rule) => <div role="listitem" key={rule}>— {rule}</div>)}
                </div>
              </div>
            </div>
          )}

          <div>
            <label htmlFor="scene-character" className="mb-1 block text-xs text-slate-400">Описание персонажа (одинаковый во всех кадрах)</label>
            <input
              id="scene-character"
              value={charDesc}
              onChange={(event) => setCharDesc(event.target.value)}
              disabled={settingsControlsDisabled}
              placeholder="Главный герой: мужчина 35 лет, короткая стрижка, борода, кожаная куртка"
              className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
            />
          </div>

          <div>
            <label htmlFor="scene-style-anchor" className="mb-1 block text-xs text-slate-400">Дополнительные заметки к стилю</label>
            <textarea
              id="scene-style-anchor"
              value={styleAnchor}
              onChange={(event) => setStyleAnchor(event.target.value)}
              disabled={settingsControlsDisabled}
              placeholder="Например: warmer palette, more cave-like backgrounds, bigger yarn stitches"
              className="h-20 w-full resize-y rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
            />
          </div>

          <div className="border-t border-slate-800 pt-4">
            <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-400">Разбиение на сцены</h3>
            <div className="grid grid-cols-1 items-end gap-4 sm:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_110px_110px_140px]">
              <div className="sm:col-span-2 xl:col-span-1">
                <label htmlFor="scene-sensitivity" className="mb-1 block text-xs text-slate-400">
                  Чувствительность границ: {segSensitivity.toFixed(2)} {segTarget > 0 && '(игнорируется при заданном числе сцен)'}
                </label>
                <input
                  id="scene-sensitivity"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={segSensitivity}
                  onChange={(event) => setSegSensitivity(Number(event.target.value))}
                  disabled={settingsControlsDisabled || segTarget > 0}
                  aria-describedby="scene-sensitivity-hint"
                  className="w-full accent-indigo-500 disabled:cursor-not-allowed disabled:opacity-60"
                />
                <div id="scene-sensitivity-hint" className="flex justify-between text-[11px] text-slate-500"><span>меньше сцен</span><span>больше сцен</span></div>
              </div>
              <div>
                <label htmlFor="scene-seg-min" className="mb-1 block text-xs text-slate-400">Мин. сек.</label>
                <input
                  id="scene-seg-min"
                  type="number"
                  min={2}
                  step={1}
                  value={segMin}
                  onChange={(event) => setSegMin(Number(event.target.value))}
                  disabled={settingsControlsDisabled}
                  className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
                />
              </div>
              <div>
                <label htmlFor="scene-seg-max" className="mb-1 block text-xs text-slate-400">Макс. сек.</label>
                <input
                  id="scene-seg-max"
                  type="number"
                  min={5}
                  step={1}
                  value={segMax}
                  onChange={(event) => setSegMax(Number(event.target.value))}
                  disabled={settingsControlsDisabled}
                  className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
                />
              </div>
              <div>
                <label htmlFor="scene-seg-target" className="mb-1 block text-xs text-slate-400">Число сцен (0=авто)</label>
                <input
                  id="scene-seg-target"
                  type="number"
                  min={0}
                  step={1}
                  value={segTarget}
                  onChange={(event) => setSegTarget(Number(event.target.value))}
                  disabled={settingsControlsDisabled}
                  className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 font-mono text-sm text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
                />
              </div>
            </div>
            <p className="mt-2 text-xs leading-5 text-slate-500">
              При запуске несохранённые настройки сначала сохраняются автоматически. Число сцен — целевая величина; ограничения минимальной и максимальной длительности имеют приоритет.
            </p>
          </div>

          {settingsValidationError && <p role="alert" className="text-sm text-amber-300">{settingsValidationError}</p>}

          <button
            type="button"
            onClick={() => void saveSettings(true)}
            disabled={!settingsDirty || settingsControlsDisabled || Boolean(settingsValidationError)}
            className="cursor-pointer rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {savingSettings ? 'Сохраняем…' : settingsDirty ? 'Сохранить настройки' : 'Настройки сохранены'}
          </button>
        </div>
      </details>

      <section aria-labelledby="scene-logs-heading" className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
        <h2 id="scene-logs-heading" className="mb-2 text-sm text-slate-400">Логи</h2>
        <div role="log" aria-live="polite" aria-relevant="additions" className="max-h-32 space-y-0.5 overflow-y-auto break-words font-mono text-xs text-slate-500">
          {logs.map((line, index) => <div key={`${line}-${index}`}>{line}</div>)}
          {!logs.length && <div className="text-slate-600">Ожидание…</div>}
        </div>
      </section>
    </div>
  )
}
