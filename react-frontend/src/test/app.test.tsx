import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { App } from '../App';
import { AuthProvider } from '../context/AuthContext';
import { ToastProvider } from '../context/ToastContext';

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
const fakeJwt = (role: string) => `h.${btoa(JSON.stringify({ role }))}.s`;

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => handler(String(url), init));
  vi.stubGlobal('fetch', fn);
  return fn;
}

const quiet = () => json(200, { status: 'healthy', items: [], total: 0, by_channel: {}, by_status: {} });

function signIn(role: 'user' | 'admin', username = 'a') {
  localStorage.setItem('token', fakeJwt(role));
  localStorage.setItem('user', JSON.stringify({ id: 1, username, role }));
}

describe('routing + guard', () => {
  it('redirects unauthenticated users from /assistant to /login', async () => {
    stubFetch(quiet);
    renderAt('/assistant');
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
  });

  it('redirects unknown routes to /login', async () => {
    stubFetch(quiet);
    renderAt('/nope');
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
  });

  it('removed pages no longer exist (/user, /rag_widget, /embed)', async () => {
    stubFetch(quiet);
    for (const path of ['/user', '/rag_widget', '/embed']) {
      const { unmount } = renderAt(path);
      expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
      unmount();
    }
  });

  it('requires BOTH token and user to count as authenticated', async () => {
    localStorage.setItem('user', JSON.stringify({ id: 1, username: 'a', role: 'user' }));
    stubFetch(quiet);
    renderAt('/assistant');
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
  });

  it('"/" lands on the assistant for a signed-in user', async () => {
    signIn('user');
    stubFetch(quiet);
    renderAt('/');
    expect(await screen.findByRole('heading', { name: 'Bescom Assistant' })).toBeInTheDocument();
  });

  it('keeps non-admins out of /admin', async () => {
    signIn('user');
    stubFetch(quiet);
    renderAt('/admin');
    expect(await screen.findByRole('heading', { name: 'Bescom Assistant' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Admin' })).not.toBeInTheDocument();
  });

  it('shows admins the admin dashboard with all-user stats', async () => {
    signIn('admin', 'root');
    const fetchMock = stubFetch((url) => {
      if (url.endsWith('/api/agent/stats')) {
        return json(200, {
          initialized: true,
          total_requests: 12,
          active_sessions: 2,
          agents: {},
          agent_state: { OrchestratorAgent: { execution_count: 12, success_count: 11, failure_count: 1, last_error: null } },
        });
      }
      if (url.includes('/messages/stats')) {
        return json(200, { total: 5, by_channel: { email: 3, whatsapp: 2 }, by_status: { sent: 4, failed: 1 } });
      }
      if (url.includes('/api/agent/messages')) {
        return json(200, {
          items: [
            { id: 1, channel: 'email', status: 'sent', recipients: ['a@acme.com'], subject: 's', body: 'b', error: null,
              drafted_by_ai: false, created_at: '2026-10-07T09:00:00' },
          ],
        });
      }
      return quiet();
    });
    renderAt('/admin');
    expect(await screen.findByRole('heading', { name: 'Admin' })).toBeInTheDocument();
    expect(await screen.findByText('a@acme.com')).toBeInTheDocument();
    expect(screen.getByText(/chat requests/).textContent).toContain('12 chat requests');
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/messages/stats?all=true'))).toBe(true);
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).includes('/api/agent/messages?') && String(u).includes('all=true')),
    ).toBe(true);
  });
});

describe('login', () => {
  it('validates empty fields without calling the API', async () => {
    const fetchMock = stubFetch(quiet);
    renderAt('/login');
    await userEvent.click(screen.getByRole('button', { name: 'Login' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Please enter both username and password');
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/auth/login'))).toBe(false);
  });

  it('does not mount any knowledge widget on the login page', async () => {
    stubFetch(quiet);
    renderAt('/login');
    expect(screen.queryByRole('tab', { name: 'Scrape' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open chat' })).not.toBeInTheDocument();
  });

  it('shows the 401 message', async () => {
    stubFetch((url) => (url.includes('/auth/login') ? json(401, { detail: 'nope' }) : quiet()));
    renderAt('/login');
    await userEvent.type(screen.getByLabelText('Username'), 'bob');
    await userEvent.type(screen.getByLabelText('Password'), 'wrong');
    await userEvent.click(screen.getByRole('button', { name: 'Login' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid username or password');
    expect(localStorage.getItem('token')).toBeNull();
  });

  it('sends form-encoded credentials and routes a normal user to the assistant', async () => {
    const user = { id: 2, username: 'bob', role: 'user' };
    const fetchMock = stubFetch((url) =>
      url.includes('/auth/login') ? json(200, { access_token: fakeJwt('user'), token_type: 'bearer', user }) : quiet(),
    );
    renderAt('/login');
    await userEvent.type(screen.getByLabelText('Username'), 'bob');
    await userEvent.type(screen.getByLabelText('Password'), 'secret1');
    await userEvent.click(screen.getByRole('button', { name: 'Login' }));

    expect(await screen.findByRole('heading', { name: 'Bescom Assistant' })).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([u]) => String(u).includes('/auth/login'))!;
    const init = call[1] as RequestInit;
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/x-www-form-urlencoded');
    expect(init.body).toBe('username=bob&password=secret1');
    expect(localStorage.getItem('token')).toBe(fakeJwt('user'));
    expect(JSON.parse(localStorage.getItem('user')!)).toEqual(user);
  });

  it('routes admins to /admin', async () => {
    const user = { id: 1, username: 'root', role: 'admin' };
    stubFetch((url) =>
      url.includes('/auth/login') ? json(200, { access_token: fakeJwt('admin'), token_type: 'bearer', user }) : quiet(),
    );
    renderAt('/login');
    await userEvent.type(screen.getByLabelText('Username'), 'root');
    await userEvent.type(screen.getByLabelText('Password'), 'pw');
    await userEvent.click(screen.getByRole('button', { name: 'Login' }));
    expect(await screen.findByRole('heading', { name: 'Admin' })).toBeInTheDocument();
  });

  it('signs out from the navigation bar', async () => {
    signIn('user');
    stubFetch(quiet);
    renderAt('/assistant');
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
    expect(localStorage.getItem('token')).toBeNull();
  });
});

describe('register', () => {
  async function fillRegister(confirm: string) {
    await userEvent.type(screen.getByLabelText('Username'), 'newuser');
    await userEvent.type(screen.getByLabelText('Email'), 'a@b.co');
    await userEvent.type(screen.getAllByLabelText(/^Password$/)[0], 'secret1');
    await userEvent.type(screen.getByLabelText('Confirm Password'), confirm);
    await userEvent.click(screen.getByRole('button', { name: 'Register' }));
  }

  it('rejects mismatched passwords client-side', async () => {
    const fetchMock = stubFetch(quiet);
    renderAt('/register');
    await fillRegister('secret2');
    expect(await screen.findByRole('alert')).toHaveTextContent('Passwords do not match');
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/auth/register'))).toBe(false);
  });

  it('disables submit until required fields meet min lengths', async () => {
    stubFetch(quiet);
    renderAt('/register');
    expect(screen.getByRole('button', { name: 'Register' })).toBeDisabled();
  });

  it('maps a 400 to "User already exists"', async () => {
    stubFetch((url) => (url.includes('/auth/register') ? json(400, { detail: 'User already exists' }) : quiet()));
    renderAt('/register');
    await fillRegister('secret1');
    expect(await screen.findByRole('alert')).toHaveTextContent('User already exists');
  });
});
