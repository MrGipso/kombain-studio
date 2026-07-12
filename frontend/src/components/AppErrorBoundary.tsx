import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  failed: boolean
}

export default class AppErrorBoundary extends Component<Props, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Pipeline Studio UI crashed', error, info.componentStack)
  }

  render() {
    if (!this.state.failed) return this.props.children

    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-950 px-6 text-slate-100">
        <section role="alert" className="w-full max-w-lg rounded-xl border border-rose-900/70 bg-slate-900 p-6 shadow-2xl">
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-rose-400">Ошибка интерфейса</p>
          <h1 className="mt-2 text-xl font-semibold text-white">Страница не смогла отобразиться</h1>
          <p className="mt-3 text-sm leading-6 text-slate-400">
            Данные проекта не повреждены. Перезагрузите веб-интерфейс; активная серверная задача продолжит работу.
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500"
          >
            Перезагрузить страницу
          </button>
        </section>
      </main>
    )
  }
}
