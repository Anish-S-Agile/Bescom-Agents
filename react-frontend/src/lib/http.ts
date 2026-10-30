import { API_URL, TOKEN_KEY } from './config';

/** FastAPI errors: `detail` is a string, or (422 validation) a list of {msg}. Anything else yields undefined. */
export function extractDetail(body: unknown): string | undefined {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    const msgs = d.map((x) => (x as { msg?: unknown })?.msg).filter((m): m is string => typeof m === 'string');
    return msgs.length ? msgs.join('; ') : undefined;
  }
  return undefined;
}

export class HttpError extends Error {
  status: number;
  body: unknown;
  timedOut: boolean;

  constructor(message: string, status: number, body: unknown = null, timedOut = false) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.body = body;
    this.timedOut = timedOut;
  }

  /** Human-readable message from the server, when it sent one. */
  get detail(): string | undefined {
    return extractDetail(this.body);
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  json?: unknown;
  form?: URLSearchParams;
  formData?: FormData;
  timeoutMs?: number;
  signal?: AbortSignal;
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

/** Bearer token on everything except /auth/login. */
export function authHeaders(url: string): Record<string, string> {
  const token = getToken();
  return token && !url.includes('/auth/login') ? { Authorization: `Bearer ${token}` } : {};
}

export async function request<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = path.startsWith('http') ? path : `${API_URL}${path}`;
  const headers: Record<string, string> = { ...authHeaders(url) };
  let body: BodyInit | undefined;

  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.json);
  } else if (opts.form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
    body = opts.form.toString();
  } else if (opts.formData) {
    body = opts.formData; // browser sets the multipart boundary
  }

  const controller = new AbortController();
  let timedOut = false;
  const timer = opts.timeoutMs
    ? setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, opts.timeoutMs)
    : undefined;
  opts.signal?.addEventListener('abort', () => controller.abort());

  try {
    const res = await fetch(url, {
      method: opts.method ?? (body ? 'POST' : 'GET'),
      headers,
      body,
      signal: controller.signal,
    });

    const text = await res.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = text;
      }
    }

    if (!res.ok) {
      throw new HttpError(
        extractDetail(parsed) ?? `HTTP ${res.status} ${res.statusText}`.trim(),
        res.status,
        parsed,
      );
    }
    return parsed as T;
  } catch (err) {
    if (err instanceof HttpError) throw err;
    if (timedOut) throw new HttpError('Request timed out', 0, null, true);
    if ((err as Error)?.name === 'AbortError') throw err;
    throw new HttpError('Cannot connect to server', 0, null);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
