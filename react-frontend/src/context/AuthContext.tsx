import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { TOKEN_KEY, USER_KEY } from '../lib/config';
import { request } from '../lib/http';

export interface User {
  id: number;
  username: string;
  email?: string;
  role: 'user' | 'admin';
  is_verified?: boolean;
  created_at?: string;
  last_login?: string;
}

interface AuthResponse {
  access_token: string;
  token_type: string;
  user: User;
}

interface AuthContextValue {
  user: User | null;
  isAuthenticated: boolean;
  login: (username: string, password: string) => Promise<User | null>;
  register: (data: { username: string; email: string; password: string }) => Promise<User | null>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function readStored(): User | null {
  try {
    const token = localStorage.getItem(TOKEN_KEY);
    const raw = localStorage.getItem(USER_KEY);
    if (token && raw) return JSON.parse(raw) as User;
  } catch {
    // corrupt storage: treat as logged out
    clearStored();
  }
  return null;
}

function clearStored(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  } catch {
    /* storage unavailable */
  }
}

function persist(res: AuthResponse): User | null {
  localStorage.setItem(TOKEN_KEY, res.access_token);
  if (res.user) localStorage.setItem(USER_KEY, JSON.stringify(res.user));
  return res.user ?? null;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [user, setUser] = useState<User | null>(readStored);

  const logout = useCallback(() => {
    clearStored();
    setUser(null);
    navigate('/login');
  }, [navigate]);

  const login = useCallback(async (username: string, password: string) => {
    // FastAPI OAuth2PasswordRequestForm expects form-encoded credentials.
    const res = await request<AuthResponse>('/api/auth/login', {
      method: 'POST',
      form: new URLSearchParams({ username, password }),
    });
    const next = persist(res);
    setUser(next);
    return next;
  }, []);

  const register = useCallback(
    async (data: { username: string; email: string; password: string }) => {
      const res = await request<AuthResponse>('/api/auth/register', { method: 'POST', json: data });
      const next = persist(res);
      setUser(next);
      return next;
    },
    [],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      // A user object AND a token must both exist.
      isAuthenticated: !!user && !!localStorage.getItem(TOKEN_KEY),
      login,
      register,
      logout,
    }),
    [user, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
