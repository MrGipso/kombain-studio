import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ToastProvider } from './components/Toast'
import AppErrorBoundary from './components/AppErrorBoundary'

const StartScreen = lazy(() => import('./pages/StartScreen'))
const ProjectLayout = lazy(() => import('./pages/ProjectLayout'))
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Transcription = lazy(() => import('./pages/Transcription'))
const Scenes = lazy(() => import('./pages/Scenes'))
const Shots = lazy(() => import('./pages/Shots'))
const Timeline = lazy(() => import('./pages/Timeline'))
const AssetBoard = lazy(() => import('./pages/AssetBoard'))
const Music = lazy(() => import('./pages/Music'))
const Sounds = lazy(() => import('./pages/Sounds'))
const Script = lazy(() => import('./pages/Script'))
const Preview = lazy(() => import('./pages/Preview'))
const Settings = lazy(() => import('./pages/Settings'))

function PageLoader() {
  return (
    <div role="status" aria-live="polite" className="flex min-h-48 items-center justify-center bg-slate-950 text-sm text-slate-400">
      Загрузка интерфейса…
    </div>
  )
}

export default function App() {
  return (
    <AppErrorBoundary>
      <ToastProvider>
        <BrowserRouter>
          <Suspense fallback={<PageLoader />}>
            <Routes>
              <Route path="/" element={<StartScreen />} />
              <Route path="/project/:projectId" element={<ProjectLayout />}>
                <Route index element={<Dashboard />} />
                <Route path="script" element={<Script />} />
                <Route path="transcription" element={<Transcription />} />
                <Route path="scenes" element={<Scenes />} />
                <Route path="shots" element={<Shots />} />
                <Route path="board" element={<AssetBoard />} />
                <Route path="timeline" element={<Timeline />} />
                <Route path="music" element={<Music />} />
                <Route path="sounds" element={<Sounds />} />
                <Route path="preview" element={<Preview />} />
              </Route>
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </BrowserRouter>
      </ToastProvider>
    </AppErrorBoundary>
  )
}
