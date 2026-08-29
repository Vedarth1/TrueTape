import { Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './lib/auth'
import AppShell from './components/AppShell'
import LoginPage from './pages/LoginPage'
import OperatorDashboard from './pages/OperatorDashboard'
import ReviewerQueue from './pages/ReviewerQueue'
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

function HomeRedirect() {
  const { user } = useAuth()
  const home = { operator: '/operator', reviewer: '/reviewer', consumer: '/consumer' }
  return <Navigate to={user ? home[user.role] ?? '/login' : '/login'} replace />
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<Protected><AppShell /></Protected>}>
          <Route path="/operator" element={<Protected roles={['operator', 'reviewer']}><OperatorDashboard /></Protected>} />
          <Route path="/reviewer" element={<Protected roles={['reviewer']}><ReviewerQueue /></Protected>} />
          <Route path="/consumer" element={<Protected roles={['operator', 'reviewer', 'consumer']}><ConsumerDashboard /></Protected>} />
          <Route path="/" element={<HomeRedirect />} />
          <Route path="*" element={<div>Not found</div>} />
        </Route>
      </Routes>
    </AuthProvider>
  )
}
