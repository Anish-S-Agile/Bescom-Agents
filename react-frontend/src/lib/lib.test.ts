import { afterEach, describe, expect, it, vi } from 'vitest';
import { HttpError, request } from './http';
import { shortDate } from './format';

afterEach(() => vi.unstubAllGlobals());

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal('fetch', fn);
  return fn;
}

describe('request()', () => {
  it('attaches the Bearer token to API requests', async () => {
    localStorage.setItem('token', 'abc');
    const fetchMock = mockFetch(200, { ok: true });
    await request('/api/agent/health');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/agent/health'); // same origin: the dev proxy / the API serving the UI
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer abc');
  });

  it('does not attach the token to /auth/login', async () => {
    localStorage.setItem('token', 'abc');
    const fetchMock = mockFetch(200, {});
    await request('/api/auth/login', { method: 'POST', form: new URLSearchParams({ username: 'u', password: 'p' }) });
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    expect(headers['Content-Type']).toBe('application/x-www-form-urlencoded');
    expect(init.body).toBe('username=u&password=p');
  });

  it('surfaces FastAPI `detail` and status', async () => {
    mockFetch(400, { detail: 'User already exists' });
    await expect(request('/x')).rejects.toMatchObject({ status: 400, message: 'User already exists', detail: 'User already exists' });
  });

  it('surfaces 422 validation messages and ignores unreadable detail objects', async () => {
    mockFetch(422, { detail: [{ msg: 'bad' }] });
    const err = (await request("/x").catch((e: unknown) => e)) as HttpError;
    expect(err.status).toBe(422);
    expect(err.detail).toBe('bad');
    mockFetch(422, { detail: { weird: true } });
    const err2 = (await request("/x").catch((e: unknown) => e)) as HttpError;
    expect(err2.detail).toBeUndefined();
  });

  it('maps network failure to status 0', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    await expect(request('/x')).rejects.toMatchObject({ status: 0, message: 'Cannot connect to server' });
  });

  it('times out', async () => {
    vi.stubGlobal('fetch', vi.fn((_u: string, init: RequestInit) => new Promise((_res, rej) => {
      init.signal!.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')));
    })));
    await expect(request('/x', { timeoutMs: 10 })).rejects.toMatchObject({ timedOut: true });
  });
});

describe('format', () => {
  it('shortDate formats like en-US short', () => {
    expect(shortDate(new Date(2026, 9, 6, 15, 45))).toMatch(/^10\/6\/26, 3:45\sPM$/);
  });
});

describe('extractDetail', () => {
  it('reads string and validation-array details', async () => {
    const { extractDetail } = await import('./http');
    expect(extractDetail({ detail: 'nope' })).toBe('nope');
    expect(extractDetail({ detail: [{ msg: 'field required' }, { msg: 'too short' }] })).toBe('field required; too short');
    expect(extractDetail({ detail: { any: 'object' } })).toBeUndefined();
    expect(extractDetail(null)).toBeUndefined();
  });
});
