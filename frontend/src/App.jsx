import { Routes, Route, Link, Navigate } from 'react-router-dom'
import OperatorDashboard from './pages/OperatorDashboard'
import ReviewerQueue from './pages/ReviewerQueue'
import ConsumerDashboard from './pages/ConsumerDashboard'
import HealthBadge from './components/HealthBadge'

export default function App() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3">
        <div className="flex items-center gap-6">
          <span className="font-semibold tracking-tight">TrueTape</span>
          <nav className="flex gap-4 text-sm">
            <Link to="/operator" className="hover:underline">Operator</Link>
            <Link to="/reviewer" className="hover:underline">Reviewer</Link>
            <Link to="/consumer" className="hover:underline">Consumer</Link>
          </nav>
        </div>
        <HealthBadge />
      </header>

      <main className="p-6">
        <Routes>
          <Route path="/" element={<Navigate to="/operator" replace />} />
          <Route path="/operator" element={<OperatorDashboard />} />
          <Route path="/reviewer" element={<ReviewerQueue />} />
          <Route path="/consumer" element={<ConsumerDashboard />} />
          <Route path="*" element={<div>Not found</div>} />
        </Routes>
      </main>
    </div>
  )
}