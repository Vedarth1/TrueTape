import { Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './lib/auth'
import AppShell from './components/AppShell'
import LandingPage from './pages/LandingPage'
import LoginPage from './pages/LoginPage'
import SignupPage from './pages/SignupPage'
import OperatorDashboard from './pages/OperatorDashboard'
import ReviewerQueue from './pages/ReviewerQueue'
import RulesPage from './pages/RulesPage'
import ConsumerDashboard from './pages/ConsumerDashboard'

function Protected({ roles, children }) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/login" replace />
  if (roles && !roles.includes(user.role)) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-500">
        Your role ({user.role}) does not have access to this page.
      </div>
    )
  }
  return children
}

// Landing + auth pages are public; the app shell (everything else) requires
// a session. Signing in from any of them routes by role.
export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route element={<Protected><AppShell /></Protected>}>
          <Route path="/operator" element={<Protected roles={['operator', 'reviewer']}><OperatorDashboard /></Protected>} />
          <Route path="/reviewer" element={<Protected roles={['reviewer']}><ReviewerQueue /></Protected>} />
          <Route path="/rules" element={<Protected roles={['reviewer']}><RulesPage /></Protected>} />
          <Route path="/consumer" element={<Protected roles={['operator', 'reviewer', 'consumer']}><ConsumerDashboard /></Protected>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </AuthProvider>
  )
}
