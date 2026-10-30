import { request } from '../../lib/http';
import type {
  AgentChatResponse,
  AgentHealth,
  AgentStats,
  Contact,
  ContactInput,
  HistoryItem,
  HistoryStats,
  MessageStatus,
} from './types';

const BASE = '/api/agent';

export interface HistoryQuery {
  /** Admins only: everyone's messages (the server ignores this for other roles). */
  all?: boolean;
  channel?: 'email' | 'whatsapp' | '';
  status?: MessageStatus | '';
  limit?: number;
  offset?: number;
}

export const agentApi = {
  /** The first call after a cold start loads mistral into memory, so allow a generous timeout. */
  chat: (message: string, sessionId: string) =>
    request<AgentChatResponse>(`${BASE}/chat`, {
      method: 'POST',
      json: { message, session_id: sessionId },
      timeoutMs: 180_000,
    }),

  resetConversation: (sessionId: string) =>
    request(`${BASE}/conversation/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),

  health: () => request<AgentHealth>(`${BASE}/health`, { timeoutMs: 10_000 }),

  contacts: () => request<{ items: Contact[] }>(`${BASE}/contacts`).then((r) => r.items),
  createContact: (c: ContactInput) => request<Contact>(`${BASE}/contacts`, { method: 'POST', json: c }),
  updateContact: (id: number, c: ContactInput) =>
    request<Contact>(`${BASE}/contacts/${id}`, { method: 'PUT', json: c }),
  deleteContact: (id: number) => request(`${BASE}/contacts/${id}`, { method: 'DELETE' }),

  messages: (q: HistoryQuery = {}) => {
    const p = new URLSearchParams();
    if (q.channel) p.set('channel', q.channel);
    if (q.status) p.set('status', q.status);
    if (q.all) p.set('all', 'true');
    p.set('limit', String(q.limit ?? 25));
    p.set('offset', String(q.offset ?? 0));
    return request<{ items: HistoryItem[] }>(`${BASE}/messages?${p}`).then((r) => r.items);
  },
  messageStats: (all = false) => request<HistoryStats>(`${BASE}/messages/stats${all ? '?all=true' : ''}`),
  stats: () => request<AgentStats>(`${BASE}/stats`),
};
