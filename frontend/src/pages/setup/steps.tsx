import type { ReactNode } from 'react'
import type { SetupDetection } from '../../api/client'
import type { Settings } from '../../api/types'
import { isEmbedding, sameUrl } from './config'
import { Command, Field, PathField, ProbeRow, inputClass } from './ui'

export interface StepProps {
  settings: Settings
  detection: SetupDetection | null
  value: <K extends keyof Settings>(key: K) => Settings[K]
  set: <K extends keyof Settings>(key: K, value: Settings[K]) => void
}

function Hint({ children }: { children: ReactNode }) {
  return <div className="rounded-xl border border-slate-700 bg-slate-950/30 p-4 text-sm leading-6 text-slate-400">{children}</div>
}

export function WelcomeStep({ detection }: StepProps) {
  const os = detection?.platform === 'windows' ? 'Windows' : detection?.platform === 'linux' ? 'Linux' : detection?.platform === 'macos' ? 'macOS' : '…'
  return (
    <div className="space-y-4 text-sm leading-6 text-slate-300">
      <p>
        Комбайн Studio опирается на внешние программы: FFmpeg, whisper.cpp, LM Studio, ComfyUI и другие.
        Мастер сам поищет их на этом компьютере, подставит найденное и проверит, что всё отвечает.
      </p>
      <ul className="list-disc space-y-1 pl-5 text-slate-400">
        <li><span className="text-slate-200">Обязательно:</span> папка проектов, FFmpeg, Whisper и языковая модель.</li>
        <li><span className="text-slate-200">По желанию:</span> ComfyUI, голос OmniVoice, стоки и звуковые эффекты — их шаги можно пропустить.</li>
      </ul>
      <p className="text-slate-400">
        Каждый шаг сохраняется сразу, так что мастер можно закрыть и вернуться позже. Всё это меняется и потом на странице «Настройки».
      </p>
      <p className="text-xs text-slate-500">Система: {os} · программа: <span className="font-mono">{detection?.project_root ?? '…'}</span></p>
    </div>
  )
}

export function OutputStep({ detection, value, set }: StepProps) {
  return (
    <div className="space-y-5">
      <PathField
        id="setup-output-base"
        label="Папка проектов"
        value={value('output_base') || ''}
        onChange={(v) => set('output_base', v)}
        found={detection?.output_base}
        placeholder={detection?.output_base.candidates[0]}
        hint="Сюда сохраняются проекты: озвучка, кадры, сгенерированные материалы и экспорт. Нужно место — один проект занимает гигабайты."
      />
      {detection && value('output_base') && sameUrl(value('output_base'), detection.output_base.current) && (
        <p className={`text-xs ${detection.output_base.current_ok ? 'text-emerald-300' : 'text-amber-300'}`}>
          {detection.output_base.current_ok ? '✓ Папка существует' : 'Папки пока нет — она будет создана с первым проектом'}
        </p>
      )}
    </div>
  )
}

export function FfmpegStep({ detection, value, set }: StepProps) {
  return (
    <div className="space-y-5">
      <PathField
        id="setup-ffmpeg"
        label={detection?.platform === 'windows' ? 'Путь к ffmpeg.exe' : 'Путь к ffmpeg'}
        value={value('ffmpeg') || ''}
        onChange={(v) => set('ffmpeg', v)}
        found={detection?.ffmpeg}
      />
      <Hint>
        {detection?.platform === 'windows' ? (
          <>Нет FFmpeg? Установите его командой <Command>winget install Gyan.FFmpeg</Command> или распакуйте сборку в <Command>C:\ffmpeg</Command>, затем нажмите «Искать снова».</>
        ) : (
          <>Нет FFmpeg? Установите его командой <Command>sudo apt install ffmpeg</Command>, затем нажмите «Искать снова».</>
        )}
      </Hint>
    </div>
  )
}

export function WhisperStep({ detection, value, set }: StepProps) {
  const windows = detection?.platform === 'windows'
  return (
    <div className="space-y-5">
      <PathField
        id="setup-whisper-cli"
        label={windows ? 'Программа whisper-cli.exe' : 'Программа whisper-cli'}
        value={value('whisper_cli') || ''}
        onChange={(v) => set('whisper_cli', v)}
        found={detection?.whisper_cli}
      />
      <PathField
        id="setup-whisper-model"
        label="Модель (файл ggml-*.bin)"
        value={value('whisper_model') || ''}
        onChange={(v) => set('whisper_model', v)}
        found={detection?.whisper_model}
        hint="Лучший баланс скорости и качества — ggml-large-v3-turbo. Модели с .en в названии знают только английский."
      />
      <Field id="setup-whisper-language" label="Язык озвучки">
        <select
          id="setup-whisper-language"
          value={value('whisper_language') || 'auto'}
          onChange={(e) => set('whisper_language', e.target.value)}
          className={inputClass}
        >
          <option value="auto">Определять автоматически</option>
          <option value="ru">Русский</option>
          <option value="en">Английский</option>
        </select>
      </Field>
      <Hint>
        {windows ? (
          <>Нет whisper.cpp? Скачайте релиз с GitHub (<Command>whisper-cublas-…-bin-x64.zip</Command> для видеокарт NVIDIA) и распакуйте в папку <Command>wishper</Command> рядом с программой. Модели кладите в <Command>models</Command>.</>
        ) : (
          <>Нет whisper.cpp? Соберите его: <Command>git clone https://github.com/ggml-org/whisper.cpp ~/src/whisper.cpp</Command>, затем <Command>cmake -B build -DGGML_CUDA=ON && cmake --build build -j</Command> в этой папке. Модели кладите в <Command>models</Command>.</>
        )}
      </Hint>
    </div>
  )
}

export function LmStep({ detection, settings, value, set }: StepProps) {
  const cloud = value('lm_provider') === 'openai_compatible'
  const url = value('lm_url') || ''
  const probes = detection?.lm ?? []
  const activeProbe = probes.find((probe) => sameUrl(probe.url, url))
  const models = activeProbe?.models ?? []
  const chatModels = models.filter((id) => !isEmbedding(id))
  const embedModels = models.filter(isEmbedding)
  return (
    <div className="space-y-5">
      <Field id="setup-lm-provider" label="Где работает модель">
        <select
          id="setup-lm-provider"
          value={value('lm_provider') || 'lmstudio'}
          onChange={(e) => set('lm_provider', e.target.value as Settings['lm_provider'])}
          className={inputClass}
        >
          <option value="lmstudio">LM Studio — локально или в своей сети, без ключа</option>
          <option value="openai_compatible">OpenAI-совместимый API — облако, по ключу</option>
        </select>
      </Field>

      {!cloud && probes.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-slate-400">Найденные серверы LM Studio</p>
          {probes.map((probe) => (
            <ProbeRow key={probe.url} probe={probe} active={sameUrl(probe.url, url)} onUse={() => set('lm_url', probe.url)} />
          ))}
        </div>
      )}

      <Field
        id="setup-lm-url"
        label="Адрес API (…/v1)"
        hint={cloud ? 'Например https://api.openai.com/v1 или https://openrouter.ai/api/v1.' : 'В LM Studio включите сервер: вкладка Developer → Start Server. Порт по умолчанию 1234.'}
      >
        <input id="setup-lm-url" type="text" spellCheck={false} value={url} onChange={(e) => set('lm_url', e.target.value)} className={inputClass} />
      </Field>

      {cloud && (
        <Field
          id="setup-lm-key"
          label={`API-ключ${settings.lm_api_key_set ? ' · ключ сохранён' : ''}`}
          hint="Текст сценария и сцен будет уходить провайдеру; полный прогон — это сотни платных запросов."
        >
          <input
            id="setup-lm-key"
            type="password"
            autoComplete="off"
            value={value('lm_api_key') || ''}
            placeholder={settings.lm_api_key_set ? 'Новый ключ или пусто без изменений' : ''}
            onChange={(e) => set('lm_api_key', e.target.value)}
            className={inputClass}
          />
        </Field>
      )}

      <div className="grid gap-5 md:grid-cols-2">
        <Field id="setup-lm-model" label="Модель для текста" hint={chatModels.length ? 'Выберите из списка или впишите имя.' : 'Имя модели, как оно указано на сервере.'}>
          <input id="setup-lm-model" type="text" list="setup-lm-models" spellCheck={false} value={value('lm_model') || ''} onChange={(e) => set('lm_model', e.target.value)} className={inputClass} />
          <datalist id="setup-lm-models">{chatModels.map((id) => <option key={id} value={id} />)}</datalist>
        </Field>
        <Field id="setup-lm-embed" label="Модель embeddings" hint="Нужна для деления текста на сцены.">
          <input id="setup-lm-embed" type="text" list="setup-lm-embeds" spellCheck={false} value={value('lm_embed_model') || ''} onChange={(e) => set('lm_embed_model', e.target.value)} className={inputClass} />
          <datalist id="setup-lm-embeds">{embedModels.map((id) => <option key={id} value={id} />)}</datalist>
        </Field>
      </div>
    </div>
  )
}

export function ComfyStep({ detection, value, set }: StepProps) {
  const url = value('comfy_url') || ''
  const probes = detection?.comfy ?? []
  return (
    <div className="space-y-5">
      {probes.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-slate-400">Найденные серверы ComfyUI</p>
          {probes.map((probe) => (
            <ProbeRow key={probe.url} probe={probe} active={sameUrl(probe.url, url)} onUse={() => set('comfy_url', probe.url)} />
          ))}
        </div>
      )}
      <Field id="setup-comfy-url" label="Адрес ComfyUI" hint="Порт по умолчанию 8188. Проверка также сверяет, что в ComfyUI есть узлы, нужные workflow проекта.">
        <input id="setup-comfy-url" type="text" spellCheck={false} value={url} onChange={(e) => set('comfy_url', e.target.value)} className={inputClass} />
      </Field>
      <Hint>
        Без ComfyUI программа всё равно соберёт видео: вместо генерации возьмёт стоки или сделает «Ken Burns» из стоп-кадров.
        Пути к своим workflow и ключ comfy.org задаются потом в «Настройках».
      </Hint>
    </div>
  )
}

export function VoiceStep({ detection, value, set }: StepProps) {
  return (
    <div className="space-y-5">
      <PathField
        id="setup-omnivoice"
        label="Программа omnivoice-infer"
        value={value('omnivoice_cli') || ''}
        onChange={(v) => set('omnivoice_cli', v)}
        found={detection?.omnivoice_cli}
        hint="Можно оставить просто omnivoice-infer, если команда доступна в PATH."
      />
      <Field id="setup-omnivoice-model" label="Модель OmniVoice">
        <input id="setup-omnivoice-model" type="text" spellCheck={false} value={value('omnivoice_model') || ''} onChange={(e) => set('omnivoice_model', e.target.value)} className={inputClass} />
      </Field>
      <Hint>Голос нужен, только если озвучку делает программа. Если вы записываете голос сами, шаг можно пропустить.</Hint>
    </div>
  )
}

export function StockStep({ settings, value, set }: StepProps) {
  const secret = (key: 'pexels_api_key' | 'pixabay_api_key', label: string, id: string) => {
    const saved = Boolean(settings[`${key}_set` as keyof Settings])
    return (
      <Field id={id} label={`${label}${saved ? ' · ключ сохранён' : ''}`}>
        <input
          id={id}
          type="password"
          autoComplete="off"
          value={value(key) || ''}
          placeholder={saved ? 'Новый ключ или пусто без изменений' : 'Не обязательно'}
          onChange={(e) => set(key, e.target.value)}
          className={inputClass}
        />
      </Field>
    )
  }
  return (
    <div className="space-y-5">
      <div className="grid gap-5 md:grid-cols-2">
        {secret('pexels_api_key', 'Ключ Pexels', 'setup-pexels')}
        {secret('pixabay_api_key', 'Ключ Pixabay', 'setup-pixabay')}
      </div>
      <p className="text-xs leading-5 text-slate-500">Бесплатные ключи выдают на pexels.com/api и pixabay.com/api/docs. Без них стоковое видео не ищется.</p>
      <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-slate-700 bg-slate-950/30 p-3">
        <input type="checkbox" checked={Boolean(value('sfx_enabled'))} onChange={(e) => set('sfx_enabled', e.target.checked)} className="mt-0.5 h-4 w-4" />
        <span>
          <span className="block text-sm font-medium text-slate-300">Генерировать звуковые эффекты (Woosh)</span>
          <span className="mt-1 block text-xs leading-5 text-slate-500">Фоновые звуки к кадрам. Веса Woosh только для некоммерческого использования.</span>
        </span>
      </label>
      {value('sfx_enabled') && (
        <Field id="setup-woosh-url" label="Адрес Woosh">
          <input id="setup-woosh-url" type="text" spellCheck={false} value={value('woosh_url') || ''} onChange={(e) => set('woosh_url', e.target.value)} className={inputClass} />
        </Field>
      )}
    </div>
  )
}
