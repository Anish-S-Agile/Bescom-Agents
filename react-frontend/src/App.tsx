import { useEffect } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AppShell } from './components/AppShell';
import { AdminOnly, ProtectedRoute } from './components/ProtectedRoute';
import { Admin } from './pages/Admin';
import { Assistant } from './pages/Assistant';
import { Login } from './pages/Login';
import { Register } from './pages/Register';

/** Body class per area (login vs signed-in pages) for the page background. */
function useBodyClass() {
  const { pathname } = useLocation();
  useEffect(() => {
    const cls = document.body.classList;
    cls.remove('login-page', 'dashboard-page');
    cls.add(pathname.startsWith('/login') || pathname.startsWith('/register') ? 'login-page' : 'dashboard-page');
  }, [pathname]);
}

export function App() {
  useBodyClass();
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/assistant" replace />} />
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />

      <Route element={<ProtectedRoute />}>
        <Route element={<AppShell />}>
          <Route path="/assistant" element={<Assistant />} />
          <Route
            path="/admin"
            element={
              <AdminOnly>
                <Admin />
              </AdminOnly>
            }
          />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/login" replace />} />
    </Routes>
  );
}
