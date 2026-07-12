import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import * as api from '../api/client'
import { useToast } from '../components/toast-context'
import type { ScriptState } from '../api/types'

interface Context {
  projectId: string
  busy: string | null
  setBusy: (busy: string | null) => void
}

const EMPTY: ScriptState = {
  topic: '',
  target_minutes: 8,
  audience_notes: '',
  facts: '',
  hooks: [],
  hook_selected: 0,
  beats: [],
}

export default function Script() {
  const { projectId, busy, setBusy } = useOutletContext<Context>()
  const toast = useToast()
  const [script, setScript] = useState<ScriptState>(EMPTY)
  const [working, setWorking] = useState<string | null>(null) // 'structure' | 'beat-N' | 'all' | 'export'
  const [batchNote, setBatchNote] = useState('')
  const [exported, setExported] = useState<{ text: string; words: number; est: number } | null>(null)
  const lockedElsewhere = Boolean(busy) && !working

  useEffect(() => {
    api.getScript(projectId)
      .then((d) => setScript({ ...EMPTY, ...d }))
      .catch((e) => toast.error(`Сценарий: ${(e as Error).message}`))
  }, [projectId, toast])

  const patch = (p: Partial<ScriptState>) => setScript((s) => ({ ...s, ...p }))
  const save = async (next?: Partial<ScriptState>) => {
    try {
      const saved = await api.saveScript(projectId, { ...script, ...next })
      setScript((s) => ({ ...s, ...saved }))
    } catch (e) {
      toast.error(`Сохранение: ${(e as Error).message}`)
    }
  }

  const patchBeat = (i: number, p: Partial<ScriptState['beats'][number]>) =>
    setScript((s) => {
      const beats = [...s.beats]
      beats[i] = { ...beats[i], ...p }
      return { ...s, beats }
    })

  const handleStructure = async () => {
    if (!script.topic.trim()) { toast.error('Сначала укажи тему'); return }
    if (script.beats.length && !window.confirm('Перегенерировать структуру? Текущие биты и тексты будут заменены.')) return
    setWorking('structure')
    setBusy('Сценарий')
    try {
      await api.saveScript(projectId, script)
      const d = await api.generateScriptStructure(projectId)
      setScript((s) => ({ ...s, ...d }))
      toast.success(`Структура готова: ${d.beats.length} битов, ${d.hooks.length} хука`)
    } catch (e) {
      toast.error(`Структура: ${(e as Error).message}`)
    } finally {
      setWorking(null)
      setBusy(null)
    }
  }

  const handleBeatText = async (i: number) => {
    setWorking(`beat-${i}`)
    setBusy('Сценарий')
    try {
      await api.saveScript(projectId, script)
      const d = await api.generateBeatText(projectId, i)
      setScript((s) => ({ ...s, ...d.script }))
    } catch (e) {
      toast.error(`Бит ${i + 1}: ${(e as Error).message}`)
    } finally {
      setWorking(null)
      setBusy(null)
    }
  }

  const handleAllTexts = async () => {
    const missing = script.beats.map((b, i) => ({ b, i })).filter(({ b }) => !b.text.trim())
    if (!missing.length) { toast.success('Тексты всех битов уже готовы'); return }
    setWorking('all')
    setBusy('Сценарий')
    try {
      await api.saveScript(projectId, script)
      for (let k = 0; k < missing.length; k++) {
        setBatchNote(`${k + 1} / ${missing.length}: ${missing[k].b.title}`)
        try {
          const d = await api.generateBeatText(projectId, missing[k].i)
          setScript((s) => ({ ...s, ...d.script }))
        } catch (e) {
          toast.error(`«${missing[k].b.title}»: ${(e as Error).message}`)
        }
      }
      toast.success('Тексты готовы')
    } finally {
      setBatchNote('')
      setWorking(null)
      setBusy(null)
    }
  }

  const handleExport = async () => {
    setWorking('export')
    try {
      await api.saveScript(projectId, script)
      const d = await api.exportScript(projectId)
      setExported({ text: d.text, words: d.words, est: d.est_minutes })
      if (d.missing_beats.length) toast.error(`Без текста биты: ${d.missing_beats.join(', ')}`)
      else toast.success(`Сценарий собран: ${d.words} слов (~${d.est_minutes} мин)`)
    } catch (e) {
      toast.error(`Экспорт: ${(e as Error).message}`)
    } finally {
      setWorking(null)
    }
  }

  const copyText = async () => {
    if (!exported) return
    try {
      await navigator.clipboard.writeText(exported.text)
      toast.success('Скопировано в буфер')
    } catch {
      toast.error('Не удалось скопировать')
    }
  }

  const totalSec = script.beats.reduce((a, b) => a + (b.duration_sec || 0), 0)

  return (
    <div className="p-8 space-y-6 max-w-[1200px]">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Сценарий</h1>
        <span className="text-xs text-indigo-300 bg-indigo-600/20 px-2.5 py-1 rounded">
          шаг 00 · биты станут границами сцен после транскрипции
        </span>
      </div>

      {lockedElsewhere && (
        <div className="bg-sky-900/30 border border-sky-700/50 rounded-xl p-4 text-sky-300 text-sm">
          ⏳ Идёт другая задача ({busy}). Дождитесь завершения.
        </div>
      )}

      {/* Setup */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 p-5 space-y-4">
        <div>
          <label className="block text-sm text-slate-400 mb-1.5">Тема ролика</label>
          <input
            value={script.topic}
            onChange={(e) => patch({ topic: e.target.value })}
            onBlur={() => save()}
            placeholder="Почему молния не убивает самолёты"
            className="w-full bg-slate-800/60 text-slate-200 rounded-lg px-3 py-2 border border-slate-700 focus:border-indigo-500 outline-none"
          />
        </div>
        <div className="grid grid-cols-[140px_1fr] gap-3">
          <div>
            <label className="block text-sm text-slate-400 mb-1.5">Минут</label>
            <input
              type="number" min={2} max={40} value={script.target_minutes}
              onChange={(e) => patch({ target_minutes: Number(e.target.value) || 8 })}
              onBlur={() => save()}
              className="w-full bg-slate-800/60 text-slate-200 rounded-lg px-3 py-2 border border-slate-700 focus:border-indigo-500 outline-none"
            />
          </div>
          <div>
            <label className="block text-sm text-slate-400 mb-1.5">Аудитория / заметки (необязательно)</label>
            <input
              value={script.audience_notes}
              onChange={(e) => patch({ audience_notes: e.target.value })}
              onBlur={() => save()}
              placeholder="широкая аудитория, без формул, тон — увлечённый рассказчик"
              className="w-full bg-slate-800/60 text-slate-200 rounded-lg px-3 py-2 border border-slate-700 focus:border-indigo-500 outline-none"
            />
          </div>
        </div>
        <div>
          <label className="block text-sm text-slate-400 mb-1.5">
            Фактура — проверенные факты, цифры, источники (LLM будет опираться только на них; пусто = общие знания модели, проверяй сам)
          </label>
          <textarea
            value={script.facts}
            onChange={(e) => patch({ facts: e.target.value })}
            onBlur={() => save()}
            placeholder={'- В самолёт бьёт молния в среднем раз на 1000 лётных часов\n- 8 декабря 1963: Pan Am 214...'}
            className="w-full min-h-[90px] bg-slate-800/60 text-slate-200 text-sm rounded-lg p-3 border border-slate-700 focus:border-indigo-500 outline-none resize-y font-mono"
          />
        </div>
        <button
          onClick={handleStructure}
          disabled={Boolean(working) || lockedElsewhere}
          className="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg disabled:opacity-50 transition-colors cursor-pointer"
        >
          {working === 'structure' ? '⏳ Проектирую структуру…' : '🧠 Сгенерировать структуру (хуки + биты)'}
        </button>
      </div>

      {/* Hooks */}
      {script.hooks.length > 0 && (
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-5 space-y-3">
          <h2 className="text-sm text-slate-400 uppercase tracking-wider">Хук — первые 15–20 секунд (выбери вариант)</h2>
          {script.hooks.map((h, i) => (
            <label key={i} className={`flex items-start gap-3 rounded-lg border p-3 cursor-pointer ${script.hook_selected === i ? 'border-indigo-500 bg-indigo-600/10' : 'border-slate-700 bg-slate-800/40'}`}>
              <input
                type="radio" name="hook" checked={script.hook_selected === i}
                onChange={() => { patch({ hook_selected: i }); save({ hook_selected: i }) }}
                className="mt-1 accent-indigo-500"
              />
              <span className="text-sm text-slate-200">{h}</span>
            </label>
          ))}
        </div>
      )}

      {/* Beats */}
      {script.beats.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm text-slate-400 uppercase tracking-wider">
              Биты · {script.beats.length} шт · план ~{Math.round(totalSec / 60)} мин
            </h2>
            <div className="flex gap-2">
              <button
                onClick={handleAllTexts}
                disabled={Boolean(working) || lockedElsewhere}
                className="px-4 py-2 bg-indigo-600/80 hover:bg-indigo-500 text-white text-sm font-semibold rounded-lg disabled:opacity-50 cursor-pointer"
              >
                {working === 'all' ? `⏳ ${batchNote}` : '✍️ Тексты всех битов'}
              </button>
              <button
                onClick={handleExport}
                disabled={Boolean(working) || lockedElsewhere}
                className="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 text-white text-sm font-semibold rounded-lg disabled:opacity-50 cursor-pointer"
              >
                {working === 'export' ? '⏳…' : '📄 Собрать текст начитки'}
              </button>
            </div>
          </div>

          {script.beats.map((b, i) => (
            <div key={i} className="bg-slate-900 rounded-xl border border-slate-800 p-4 space-y-2">
              <div className="flex items-start gap-3 flex-wrap">
                <span className="text-xs font-mono text-slate-500 mt-2 w-6">{i + 1}</span>
                <input
                  value={b.title}
                  onChange={(e) => patchBeat(i, { title: e.target.value })}
                  onBlur={() => save()}
                  className="flex-1 min-w-[200px] bg-slate-800/60 text-white font-semibold rounded-lg px-3 py-1.5 border border-slate-700 focus:border-indigo-500 outline-none"
                />
                <input
                  type="number" min={10} max={300} value={b.duration_sec}
                  onChange={(e) => patchBeat(i, { duration_sec: Number(e.target.value) || 45 })}
                  onBlur={() => save()}
                  title="Целевая длительность, сек"
                  className="w-20 bg-slate-800/60 text-slate-300 text-sm rounded-lg px-2 py-1.5 border border-slate-700 focus:border-indigo-500 outline-none font-mono"
                />
                <button
                  onClick={() => handleBeatText(i)}
                  disabled={Boolean(working) || lockedElsewhere}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm text-slate-200 disabled:opacity-50 cursor-pointer"
                >
                  {working === `beat-${i}` ? '⏳…' : b.text ? '🔄 Переписать' : '✍️ Текст'}
                </button>
              </div>
              <div className="text-xs text-slate-500 pl-9">
                <span className="text-slate-400">Цель:</span> {b.goal} · <span className="text-slate-400">Визуал:</span> {b.visual_hint}
              </div>
              <div className="text-xs text-slate-600 pl-9">{b.content}</div>
              <textarea
                value={b.text}
                onChange={(e) => patchBeat(i, { text: e.target.value })}
                onBlur={() => save()}
                placeholder="Текст начитки этого бита появится здесь…"
                className="w-full min-h-[70px] bg-slate-950/60 text-slate-200 text-sm rounded-lg p-3 border border-slate-800 focus:border-indigo-500 outline-none resize-y ml-0"
              />
            </div>
          ))}
        </div>
      )}

      {/* Export result */}
      {exported && (
        <div className="bg-slate-900 rounded-xl border border-emerald-800/60 p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm text-emerald-300 uppercase tracking-wider">
              Текст начитки · {exported.words} слов · ~{exported.est} мин
            </h2>
            <button onClick={copyText} className="px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-sm text-slate-200 hover:bg-slate-700 cursor-pointer">
              📋 Копировать
            </button>
          </div>
          <textarea readOnly value={exported.text} className="w-full min-h-[200px] bg-slate-950/60 text-slate-200 text-sm rounded-lg p-3 border border-slate-800 outline-none resize-y" />
          <p className="text-xs text-slate-500">
            Файл сохранён в проект как 00_script.txt. Начитай текст (или прогони через TTS), загрузи аудио на шаге
            «Транскрипция» — сцены соберутся из этих битов автоматически, с авторскими границами и целями удержания.
          </p>
        </div>
      )}
    </div>
  )
}
