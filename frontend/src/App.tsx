import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import StartScreen from './pages/StartScreen'
import ProjectLayout from './pages/ProjectLayout'
import Dashboard from './pages/Dashboard'
import Transcription from './pages/Transcription'
import Scenes from './pages/Scenes'
import Shots from './pages/Shots'
import Timeline from './pages/Timeline'
import AssetBoard from './pages/AssetBoard'
import Preview from './pages/Preview'
import Settings from './pages/Settings'
import { ToastProvider } from './components/Toast'

export default function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<StartScreen />} />
          <Route path="/project/:projectId" element={<ProjectLayout />}>
            <Route index element={<Dashboard />} />
            <Route path="transcription" element={<Transcription />} />
            <Route path="scenes" element={<Scenes />} />
            <Route path="shots" element={<Shots />} />
            <Route path="board" element={<AssetBoard />} />
            <Route path="timeline" element={<Timeline />} />
            <Route path="preview" element={<Preview />} />
          </Route>
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  )
}
