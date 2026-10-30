import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { App } from '../App';
import { AuthProvider } from '../context/AuthContext';
import { ToastProvider } from '../context/ToastContext';
import { RichText } from '../components/agent/richText';

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
const fakeJwt = (role: string) => `h.${btoa(JSON.stringify({ role }))}.s`;

const HEALTH = {
  status: 'healthy',
  llm: { model: 'mistral', reachable: true, model_pulled: true },
  sub_agents: {
    email: { ready: true, configured: true, dry_run: false },
    whatsapp: { ready: true, configured: true, dry_run: false, provider: 'meta' },
  },
  database: { mode: 'fake', fake: true, healthy: true },
};

const PREVIEW = {
  channel: 'email',
  emails: ['bob@acme.com'],
  phones: [],
  subject: 'Hello',
  body: 'Office closed tomorrow',
  drafted_by_ai: true,
};

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response> | undefined;

function stubFetch(handler: Handler) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const custom = await handler(u, init);
    if (custom) return custom;
    if (u.includes('/api/agent/health')) return json(200, HEALTH);
    if (u.includes('/api/agent/contacts')) return json(200, { items: [] });
    if (u.includes('/api/agent/messages/stats')) return json(200, { total: 0, by_channel: {}, by_status: {} });
    if (u.includes('/api/agent/messages')) return json(200, { items: [] });
    return json(200, {});
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function signIn(role = 'user') {
  localStorage.setItem('token', fakeJwt(role));
  localStorage.setItem('user', JSON.stringify({ id: 1, username: 'alice', role }));
}

function renderAssistant() {
  return render(
    <MemoryRouter initialEntries={['/assistant']}>
      <ToastProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

const chatCalls = (fn: ReturnType<typeof stubFetch>) =>
  fn.mock.calls.filter(([u]) => String(u).endsWith('/api/agent/chat'));

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  signIn();
});
afterEach(() => vi.unstubAllGlobals());

describe('assistant page', () => {
  it('is protected and shows nav, tabs and service status when signed in', async () => {
    stubFetch(() => undefined);
    renderAssistant();
    expect(await screen.findByRole('heading', { name: 'Bescom Assistant' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Assistant' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Contacts' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'History' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Knowledge base' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin' })).not.toBeInTheDocument();
    // status pills: Fake DB is flagged as a warning, others ready
    expect(await screen.findByText('Fake DB')).toBeInTheDocument();
    expect(screen.getByText('mistral')).toBeInTheDocument();
  });

  it('shows the Admin link only to admins', async () => {
    signIn('admin');
    stubFetch(() => undefined);
    renderAssistant();
    expect(await screen.findByRole('link', { name: 'Admin' })).toBeInTheDocument();
  });

  it('redirects to login without a session', async () => {
    localStorage.clear();
    stubFetch(() => undefined);
    renderAssistant();
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
  });

  it('tells the user when Ollama or a sender needs attention', async () => {
    stubFetch((u) =>
      u.includes('/api/agent/health')
        ? json(200, {
            ...HEALTH,
            status: 'degraded',
            llm: { model: 'mistral', reachable: false, model_pulled: false },
            sub_agents: { ...HEALTH.sub_agents, email: { ready: false, configured: false, dry_run: false } },
          })
        : undefined,
    );
    renderAssistant();
    const mistral = await screen.findByText('mistral');
    expect(mistral.closest('.status-pill')).toHaveClass('status-bad');
    expect(screen.getByText('Email').closest('.status-pill')).toHaveClass('status-bad');
    expect(screen.getByText('WhatsApp').closest('.status-pill')).toHaveClass('status-ok');
  });
});

describe('chat', () => {
  it('sends the message with the token and session id, then renders the reply', async () => {
    const fetchMock = stubFetch((u) =>
      u.endsWith('/api/agent/chat')
        ? json(200, { success: true, handled: true, session_id: 's1', response: 'What should the message say?',
            metadata: { missing_params: ['body'] } })
        : undefined,
    );
    renderAssistant();
    await userEvent.type(await screen.findByLabelText('Message'), 'send an email to bob@acme.com');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('What should the message say?')).toBeInTheDocument();
    const [url, init] = chatCalls(fetchMock)[0];
    expect(String(url)).toMatch(/\/api\/agent\/chat$/);
    const req = init as RequestInit;
    expect((req.headers as Record<string, string>).Authorization).toBe(`Bearer ${fakeJwt('user')}`);
    const body = JSON.parse(req.body as string);
    expect(body.message).toBe('send an email to bob@acme.com');
    expect(typeof body.session_id).toBe('string');
    expect(sessionStorage.getItem('agent_session')).toBeTruthy();
  });

  it('shows a preview card with confirm buttons, and "Yes, send it" replies yes', async () => {
    let call = 0;
    const fetchMock = stubFetch((u) => {
      if (!u.endsWith('/api/agent/chat')) return undefined;
      call += 1;
      return call === 1
        ? json(200, { success: true, handled: true, session_id: 's1', response: "Here's what I'm about to send",
            metadata: { awaiting_confirmation: true, preview: PREVIEW } })
        : json(200, { success: true, handled: true, session_id: 's1', response: '✅ **Email**: sent',
            execution_result: { success: true, data: { results: [{ success: true, channel: 'email', message: "Email 'Hello' sent to 1 recipient(s)" }] } },
            metadata: { awaiting_confirmation: false } });
    });
    renderAssistant();
    await userEvent.type(await screen.findByLabelText('Message'), 'email bob');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));

    const card = await screen.findByLabelText('Message preview');
    expect(within(card).getByText('To: bob@acme.com')).toBeInTheDocument();
    expect(within(card).getByText('Office closed tomorrow')).toBeInTheDocument();
    expect(within(card).getByText(/Written by the AI assistant/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Yes, send it' }));
    expect(await screen.findByLabelText('Send results')).toHaveTextContent("Email 'Hello' sent to 1 recipient(s)");
    expect(JSON.parse(chatCalls(fetchMock)[1][1]!.body as string).message).toBe('yes');
    // confirm buttons disappear once the conversation moved on
    expect(screen.queryByRole('button', { name: 'Yes, send it' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Message preview')).not.toBeInTheDocument();
  });

  it('Cancel sends "cancel"', async () => {
    const fetchMock = stubFetch((u) =>
      u.endsWith('/api/agent/chat')
        ? json(200, { success: true, handled: true, session_id: 's1', response: 'preview',
            metadata: { awaiting_confirmation: true, preview: PREVIEW } })
        : undefined,
    );
    renderAssistant();
    await userEvent.type(await screen.findByLabelText('Message'), 'email bob');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(chatCalls(fetchMock)).toHaveLength(2));
    expect(JSON.parse(chatCalls(fetchMock)[1][1]!.body as string).message).toBe('cancel');
  });

  it('suggestion chips fill the input', async () => {
    stubFetch(() => undefined);
    renderAssistant();
    await userEvent.click(await screen.findByRole('button', { name: /Email alice@example.com that the meeting moved/ }));
    expect(screen.getByLabelText('Message')).toHaveValue('Email alice@example.com that the meeting moved to 4pm');
  });

  it('keeps the conversation when switching tabs', async () => {
    stubFetch((u) =>
      u.endsWith('/api/agent/chat')
        ? json(200, { success: true, handled: true, session_id: 's1', response: 'Who should I email?' })
        : undefined,
    );
    renderAssistant();
    await userEvent.type(await screen.findByLabelText('Message'), 'send an email');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findByText('Who should I email?');
    await userEvent.click(screen.getByRole('tab', { name: 'Contacts' }));
    await userEvent.click(screen.getByRole('tab', { name: 'Assistant' }));
    expect(screen.getByText('Who should I email?')).toBeInTheDocument();
  });

  it('shows a friendly error when the server is unreachable', async () => {
    stubFetch((u) => {
      if (u.endsWith('/api/agent/chat')) throw new TypeError('network down');
      return undefined;
    });
    renderAssistant();
    await userEvent.type(await screen.findByLabelText('Message'), 'hello');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText(/Cannot connect to the server/)).toBeInTheDocument();
  });

  it('signs the user out when the token is rejected (401)', async () => {
    stubFetch((u) => (u.endsWith('/api/agent/chat') ? json(401, { detail: 'Authentication required' }) : undefined));
    renderAssistant();
    await userEvent.type(await screen.findByLabelText('Message'), 'hello');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
    expect(localStorage.getItem('token')).toBeNull();
  });

  it('"New conversation" clears the chat and deletes the server-side session', async () => {
    const fetchMock = stubFetch((u) =>
      u.endsWith('/api/agent/chat')
        ? json(200, { success: true, handled: true, session_id: 's1', response: 'Who should I email?' })
        : undefined,
    );
    renderAssistant();
    await userEvent.type(await screen.findByLabelText('Message'), 'send an email');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findByText('Who should I email?');
    const before = sessionStorage.getItem('agent_session');
    await userEvent.click(screen.getByRole('button', { name: /New conversation/ }));
    await waitFor(() => expect(screen.queryByText('Who should I email?')).not.toBeInTheDocument());
    expect(sessionStorage.getItem('agent_session')).not.toBe(before);
    expect(fetchMock.mock.calls.some(([u, i]) => String(u).includes('/api/agent/conversation/') && (i as RequestInit)?.method === 'DELETE')).toBe(true);
  });
});

describe('contacts', () => {
  const alice = { id: 1, name: 'Alice Johnson', email: 'alice@acme.com', phone: '+14155550101', organization: 'Acme', notes: null };

  it('lists, adds (with the right payload) and surfaces server errors', async () => {
    let created = false;
    const fetchMock = stubFetch((u, init) => {
      if (u.endsWith('/api/agent/contacts') && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        if (body.name === 'Alice Johnson') return json(409, { detail: "A contact named 'Alice Johnson' already exists" });
        created = true;
        return json(201, { ...alice, id: 2, name: body.name, email: body.email, phone: null, organization: null });
      }
      if (u.endsWith('/api/agent/contacts')) return json(200, { items: created ? [alice, { ...alice, id: 2, name: 'Dan' }] : [alice] });
      return undefined;
    });
    renderAssistant();
    await userEvent.click(await screen.findByRole('tab', { name: 'Contacts' }));
    expect(await screen.findByText('Alice Johnson')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Name'), 'Alice Johnson');
    await userEvent.type(screen.getByLabelText('Email'), 'x@acme.com');
    await userEvent.click(screen.getByRole('button', { name: 'Add contact' }));
    expect(await screen.findByText("A contact named 'Alice Johnson' already exists")).toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText('Name'));
    await userEvent.type(screen.getByLabelText('Name'), 'Dan');
    await userEvent.clear(screen.getByLabelText('Email'));
    await userEvent.type(screen.getByLabelText('Email'), 'dan@acme.com');
    await userEvent.click(screen.getByRole('button', { name: 'Add contact' }));
    expect(await screen.findByText('Contact added')).toBeInTheDocument();
    const post = fetchMock.mock.calls.filter(([, i]) => (i as RequestInit)?.method === 'POST').pop()!;
    expect(JSON.parse((post[1] as RequestInit).body as string)).toMatchObject({ name: 'Dan', email: 'dan@acme.com', phone: null });
    expect(await screen.findByText('Dan')).toBeInTheDocument();
  });

  it('requires an email or a phone before calling the API', async () => {
    const fetchMock = stubFetch(() => undefined);
    renderAssistant();
    await userEvent.click(await screen.findByRole('tab', { name: 'Contacts' }));
    await userEvent.type(await screen.findByLabelText('Name'), 'Eve');
    await userEvent.click(screen.getByRole('button', { name: 'Add contact' }));
    expect(await screen.findByText('Add an email address or a phone number')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, i]) => (i as RequestInit)?.method === 'POST')).toBe(false);
  });

  it('edits and deletes (after confirmation)', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = stubFetch((u, init) => {
      if (u.endsWith('/api/agent/contacts/1') && init?.method === 'PUT') return json(200, { ...alice, name: 'Alice J' });
      if (u.endsWith('/api/agent/contacts/1') && init?.method === 'DELETE') return json(200, { success: true });
      if (u.endsWith('/api/agent/contacts')) return json(200, { items: [alice] });
      return undefined;
    });
    renderAssistant();
    await userEvent.click(await screen.findByRole('tab', { name: 'Contacts' }));
    await userEvent.click(await screen.findByLabelText('Edit Alice Johnson'));
    await userEvent.clear(screen.getByLabelText('Name'));
    await userEvent.type(screen.getByLabelText('Name'), 'Alice J');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByText('Contact updated')).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText('Delete Alice Johnson'));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => String(u).endsWith('/contacts/1') && (i as RequestInit)?.method === 'DELETE')).toBe(true));
  });
});

describe('history', () => {
  it('renders sent messages and applies filters server-side', async () => {
    const fetchMock = stubFetch((u) => {
      if (u.includes('/api/agent/messages/stats')) return json(200, { total: 2, by_channel: {}, by_status: { sent: 1, failed: 1 } });
      if (u.includes('/api/agent/messages')) {
        return json(200, { items: [
          { id: 2, channel: 'whatsapp', status: 'failed', recipients: ['+14155550100'], subject: null, body: 'late', error: 'HTTP 400 bad', drafted_by_ai: false, created_at: '2026-10-07T10:00:00' },
          { id: 1, channel: 'email', status: 'sent', recipients: ['bob@acme.com'], subject: 'Hello', body: 'Office closed', error: null, drafted_by_ai: true, created_at: '2026-10-07T09:00:00' },
        ] });
      }
      return undefined;
    });
    renderAssistant();
    await userEvent.click(await screen.findByRole('tab', { name: 'History' }));
    expect(await screen.findByText('bob@acme.com')).toBeInTheDocument();
    expect(screen.getByText('HTTP 400 bad')).toBeInTheDocument();
    expect(screen.getByLabelText('Totals')).toHaveTextContent('2 total · 1 sent · 1 failed');
    await userEvent.selectOptions(screen.getByLabelText('Channel'), 'email');
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/api/agent/messages?') && String(u).includes('channel=email'))).toBe(true));
  });
});

describe('RichText', () => {
  it('renders bold, quotes and line breaks', () => {
    const { container } = render(<RichText text={'**Hi** there\n> quoted *line*\nplain'} />);
    expect(container.querySelector('strong')).toHaveTextContent('Hi');
    expect(container.querySelector('blockquote')).toHaveTextContent('quoted line');
    expect(container.querySelector('em')).toHaveTextContent('line');
  });

  it('never turns model output into HTML', () => {
    const { container } = render(<RichText text={'<img src=x onerror=alert(1)> <script>alert(1)</script>'} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
  });
});
