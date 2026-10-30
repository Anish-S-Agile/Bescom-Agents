import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { useToast } from '../context/ToastContext';
import { HttpError } from '../lib/http';

export function loginErrorMessage(error: unknown): string {
  const e = error instanceof HttpError ? error : null;
  if (e?.status === 401) return 'Invalid username or password';
  if (e?.status === 403) return 'Account disabled. Please contact support.';
  if (e?.status === 0) return 'Cannot connect to server. Please check your connection.';
  if (e?.detail) return e.detail;
  if (e?.message) return e.message;
  return 'Login failed. Please try again.';
}

export function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (loading) return;

    if (!username || !password) {
      const msg = 'Please enter both username and password';
      setError(msg);
      toast(msg, 'error');
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const user = await login(username, password);
      toast('Login successful!', 'success');
      navigate(user?.role === 'admin' ? '/admin' : '/assistant');
    } catch (err) {
      console.error('Login error:', err);
      const msg = loginErrorMessage(err);
      setError(msg);
      toast(msg, 'error');
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="login-wrapper">
        <div className="card auth-card">
          <h2 className="auth-title">Login</h2>
          <form onSubmit={onSubmit} noValidate>
            <label className="field">
              <span className="field-label">Username</span>
              <input
                className="text-input"
                name="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </label>
            <label className="field">
              <span className="field-label">Password</span>
              <input
                className="text-input"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </label>

            <button type="submit" className="btn btn-primary" disabled={loading}>
              {loading ? 'Logging in...' : 'Login'}
            </button>

            {error && <div className="form-error" role="alert">{error}</div>}
          </form>
        </div>
      </div>

      <div className="auth-links">
        <span>
          Don't have an account? <Link to="/register">Register</Link>
        </span>
      </div>
    </>
  );
}
