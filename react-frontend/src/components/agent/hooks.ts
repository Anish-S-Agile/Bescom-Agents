import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '../../context/AuthContext';
import { useToast } from '../../context/ToastContext';
import { HttpError } from '../../lib/http';
import { agentApi } from './api';
import type { AgentHealth, ChatEntry } from './types';

const SESSION_KEY = 'agent_session';

function newSessionId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  return c?.randomUUID ? c.randomUUID() : `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function loadSession(): string {
  try {
    const existing = sessionStorage.getItem(SESSION_KEY);
    if (existing) return existing;
    const fresh = newSessionId();
    sessionStorage.setItem(SESSION_KEY, fresh);
    return fresh;
  } catch {
    return newSessionId(); // storage blocked: the conversation just won't survive a reload
  }
}

function storeSession(id: string): void {
  try {
    sessionStorage.setItem(SESSION_KEY, id);
  } catch {
    /* storage unavailable */
  }
}

export function errorText(err: unknown): string {
  if (err instanceof HttpError) {
    if (err.timedOut) return 'The assistant took too long to respond. The language model may still be loading - please try again.';
    if (err.status === 0) return 'Cannot connect to the server. Please check your connection.';
    if (err.status === 429) return err.detail ?? 'Too many requests. Please wait a moment.';
    return err.detail ?? err.message;
  }
  return 'Something went wrong. Please try again.';
}

/** Chat state lives above the tabs so switching to Contacts/History does not lose the conversation. */
export function useAgentChat() {
  const { logout } = useAuth();
  const toast = useToast();
  const [sessionId, setSessionId] = useState(loadSession);
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const nextId = useRef(1);
  const busy = useRef(false);

  const push = useCallback((e: Omit<ChatEntry, 'id' | 'time'>) => {
    setEntries((prev) => [...prev, { ...e, id: nextId.current++, time: new Date() }]);
  }, []);

  const send = useCallback(
    async (raw: string) => {
      const message = raw.trim();
      if (!message || busy.current) return;
      busy.current = true;
      setLoading(true);
      push({ role: 'user', text: message });
      try {
        const res = await agentApi.chat(message, sessionId);
        if (res.session_id && res.session_id !== sessionId) {
          setSessionId(res.session_id);
          storeSession(res.session_id);
        }
        push({
          role: 'bot',
          text: res.response || (res.error ?? 'No response received.'),
          meta: res.metadata,
          results: res.execution_result?.data?.results,
          failed: !res.success,
        });
      } catch (err) {
        if (err instanceof HttpError && err.status === 401) {
          toast('Your session has expired. Please sign in again.', 'error');
          logout();
          return;
        }
        push({ role: 'bot', text: errorText(err), failed: true });
      } finally {
        busy.current = false;
        setLoading(false);
      }
    },
    [sessionId, push, toast, logout],
  );

  const reset = useCallback(async () => {
    const old = sessionId;
    const fresh = newSessionId();
    setSessionId(fresh);
    storeSession(fresh);
    setEntries([]);
    try {
      await agentApi.resetConversation(old); // best effort: drops any half-finished draft server-side
    } catch {
      /* the old session simply expires */
    }
  }, [sessionId]);

  return { entries, loading, send, reset, sessionId };
}

/** Polls /api/agent/health so the user can see whether Ollama and the two senders are ready. */
export function useAgentHealth(intervalMs = 30_000) {
  const [health, setHealth] = useState<AgentHealth | null>(null);

  const refresh = useCallback(async () => {
    try {
      setHealth(await agentApi.health());
    } catch {
      setHealth({ status: 'unhealthy', error: 'API unreachable' });
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), intervalMs);
    return () => clearInterval(t);
  }, [refresh, intervalMs]);

  return { health, refresh };
}
