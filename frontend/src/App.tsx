import { useEffect, useState } from 'react';
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { api } from './api/client';
import type { User } from './api/types';
import ApkPage from './pages/ApkPage';
import AuditPage from './pages/AuditPage';
import DeviceDetailPage from './pages/DeviceDetailPage';
import DevicesPage from './pages/DevicesPage';
import LoginPage from './pages/LoginPage';
import MonitoringPage from './pages/MonitoringPage';
import CommandsPage from './pages/CommandsPage';
import OperationsPage from './pages/OperationsPage';

export default function App() {
  const [authenticated, setAuthenticated] = useState(api.isAuthenticated);
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => api.onAuthChange(setAuthenticated), []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!api.isAuthenticated) {
        setReady(true);
        return;
      }
      try {
        await api.refreshSession();
        const profile = await api.me();
        if (!cancelled) setUser(profile);
      } catch {
        api.clearTokens();
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!ready) {
    return <div className="boot">Завантаження панелі…</div>;
  }

  if (!authenticated) {
    return (
      <Routes>
        <Route
          path="*"
          element={<LoginPage onLoggedIn={async () => setUser(await api.me())} />}
        />
      </Routes>
    );
  }

  return (
    <div className="layout">
      <Sidebar user={user} />
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/devices" replace />} />
          <Route path="/devices" element={<DevicesPage />} />
          <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
          <Route path="/monitoring" element={<MonitoringPage />} />
          <Route path="/apks" element={<ApkPage />} />
          <Route path="/operations" element={<OperationsPage />} />
          <Route path="/commands" element={<CommandsPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="*" element={<Navigate to="/devices" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Sidebar({ user }: { user: User | null }) {
  const navigate = useNavigate();

  const logout = async () => {
    await api.logout();
    navigate('/');
  };

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">FM</span>
        <div>
          <strong>Fleet MDM</strong>
          <div className="muted small">керування пристроями</div>
        </div>
      </div>

      <nav>
        <NavLink to="/devices">Пристрої</NavLink>
        <NavLink to="/monitoring">Моніторинг</NavLink>
        <NavLink to="/apks">Репозиторій APK</NavLink>
        <NavLink to="/operations">Історія операцій</NavLink>
        <NavLink to="/commands">Реєстр команд</NavLink>
        <NavLink to="/audit">Аудит</NavLink>
      </nav>

      <div className="sidebar-footer">
        <div className="small">{user?.email ?? '—'}</div>
        <div className="muted small">{user?.role ?? ''}</div>
        <button type="button" className="link" onClick={logout}>
          Вийти
        </button>
      </div>
    </aside>
  );
}
