import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';

/** Top navigation for every signed-in page. */
export function AppShell() {
  const { user, logout } = useAuth();
  return (
    <>
      <header className="app-nav">
        <strong className="app-title">Bescom</strong>
        <nav aria-label="Main">
          <NavLink to="/assistant">Assistant</NavLink>
          {user?.role === 'admin' && <NavLink to="/admin">Admin</NavLink>}
        </nav>
        <span className="spacer" />
        {user && <span className="app-user">{user.username}</span>}
        <button type="button" className="btn btn-stroked" onClick={logout}>
          Sign out
        </button>
      </header>
      <Outlet />
    </>
  );
}
