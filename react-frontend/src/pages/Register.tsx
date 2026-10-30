import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { useToast } from '../context/ToastContext';
import { HttpError } from '../lib/http';
import { Icon } from '../components/ui';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateRegistration(f: {
  username: string;
  email: string;
  password: string;
  confirmPassword: string;
}): string | null {
  if (!f.username || !f.email || !f.password || !f.confirmPassword) return 'Please fill in all fields';
  if (f.username.length < 3) return 'Username must be at least 3 characters';
  if (!EMAIL_RE.test(f.email)) return 'Please enter a valid email address';
  if (f.password.length < 6) return 'Password must be at least 6 characters';
  if (f.password !== f.confirmPassword) return 'Passwords do not match';
  return null;
}

function registerErrorMessage(error: unknown): string {
  const e = error instanceof HttpError ? error : null;
  if (e?.status === 400) return 'User already exists';
  if (e?.status === 0) return 'Cannot connect to server. Please check your connection.';
  if (e?.detail) return e.detail;
  if (e?.message) return e.message;
  return 'Registration failed. Please try again.';
}

export function Register() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();

  const [form, setForm] = useState({ username: '', email: '', password: '', confirmPassword: '' });
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const redirectTimer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => () => { if (redirectTimer.current) clearTimeout(redirectTimer.current); }, []);

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  // Submit is enabled once the required fields meet their minimum lengths.
  const canSubmit =
    !!form.username && form.username.length >= 3 &&
    !!form.email &&
    !!form.password && form.password.length >= 6 &&
    !!form.confirmPassword;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (loading) return;
    setError(null);

    const invalid = validateRegistration(form);
    if (invalid) {
      setError(invalid);
      toast(invalid, 'error');
      return;
    }

    setLoading(true);
    try {
      const user = await register({ username: form.username, email: form.email, password: form.password });
      toast('Registration successful! Redirecting...', 'success');
      redirectTimer.current = setTimeout(
        () => navigate(user?.role === 'admin' ? '/admin' : '/user'),
        1500,
      );
    } catch (err) {
      console.error('Registration error:', err);
      const msg = registerErrorMessage(err);
      setError(msg);
      toast(msg, 'error');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="register-container">
      <h2>Register</h2>
      <form onSubmit={onSubmit} noValidate>
        <label className="field">
          <span className="field-label">Username</span>
          <input className="text-input" name="username" autoComplete="username" value={form.username} onChange={set('username')} required minLength={3} />
        </label>

        <label className="field">
          <span className="field-label">Email</span>
          <input className="text-input" name="email" type="email" autoComplete="email" value={form.email} onChange={set('email')} required />
        </label>

        <label className="field">
          <span className="field-label">Password</span>
          <span className="input-with-suffix">
            <input
              className="text-input"
              name="password"
              type={showPassword ? 'text' : 'password'}
              autoComplete="new-password"
              value={form.password}
              onChange={set('password')}
              required
              minLength={6}
            />
            <button type="button" className="icon-btn suffix" aria-label={showPassword ? 'Hide password' : 'Show password'} onClick={() => setShowPassword((s) => !s)}>
              <Icon name={showPassword ? 'visibility' : 'visibility_off'} />
            </button>
          </span>
        </label>

        <label className="field">
          <span className="field-label">Confirm Password</span>
          <span className="input-with-suffix">
            <input
              className="text-input"
              name="confirmPassword"
              type={showConfirm ? 'text' : 'password'}
              autoComplete="new-password"
              value={form.confirmPassword}
              onChange={set('confirmPassword')}
              required
            />
            <button type="button" className="icon-btn suffix" aria-label={showConfirm ? 'Hide password' : 'Show password'} onClick={() => setShowConfirm((s) => !s)}>
              <Icon name={showConfirm ? 'visibility' : 'visibility_off'} />
            </button>
          </span>
        </label>

        <button type="submit" className="btn btn-primary full-width" disabled={loading || !canSubmit}>
          {loading ? <span className="spinner" aria-label="Registering" /> : 'Register'}
        </button>

        {error && <div className="form-error" role="alert">{error}</div>}

        <div className="login-link">
          Already have an account? <Link to="/login">Login</Link>
        </div>
      </form>
    </div>
  );
}
