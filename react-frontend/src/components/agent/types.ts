// Shapes owned by the backend (app/api/routes/agent_chat.py, app/agents/orchestrator_agent.py).

export type Channel = 'email' | 'whatsapp' | 'both';

export interface Preview {
  channel: Channel;
  emails: string[];
  phones: string[];
  subject?: string | null;
  body?: string | null;
  drafted_by_ai: boolean;
}

export interface AgentMeta {
  channel?: Channel | null;
  missing_params?: string[];
  awaiting_confirmation?: boolean;
  preview?: Preview;
  status?: string;
}

export interface SendResultItem {
  success: boolean;
  channel: 'email' | 'whatsapp';
  message: string;
  error?: string | null;
  dry_run?: boolean;
  duplicate?: boolean;
  retryable?: boolean;
}

export interface AgentChatResponse {
  success: boolean;
  response: string;
  session_id: string;
  handled: boolean;
  routing?: string | null;
  execution_result?: { success: boolean; data?: { results?: SendResultItem[] } } | null;
  metadata?: AgentMeta | null;
  error?: string | null;
}

export interface Contact {
  id: number;
  name: string;
  email: string | null;
  phone: string | null;
  organization: string | null;
  notes: string | null;
}

export type ContactInput = Pick<Contact, 'name'> &
  Partial<Pick<Contact, 'email' | 'phone' | 'organization' | 'notes'>>;

export type MessageStatus = 'sent' | 'failed' | 'dry_run' | 'duplicate';

export interface HistoryItem {
  id: number;
  channel: 'email' | 'whatsapp';
  status: MessageStatus;
  recipients: string[];
  subject: string | null;
  body: string | null;
  error: string | null;
  drafted_by_ai: boolean;
  created_at: string | null;
}

export interface HistoryStats {
  total: number;
  by_channel: Record<string, number>;
  by_status: Record<string, number>;
}

export interface AgentHealth {
  status: 'healthy' | 'degraded' | 'unhealthy';
  llm?: { model: string; reachable: boolean; model_pulled: boolean; hint?: string };
  sub_agents?: {
    email: { ready: boolean; configured: boolean; dry_run: boolean } | null;
    whatsapp: { ready: boolean; configured: boolean; dry_run: boolean; provider?: string } | null;
  };
  database?: { mode: string; fake: boolean; healthy: boolean };
  error?: string;
}

export interface AgentStats {
  initialized: boolean;
  total_requests: number;
  active_sessions: number;
  agents: Record<string, string | null>;
  agent_state?: Record<string, { execution_count: number; success_count: number; failure_count: number;
    last_error: string | null }>;
}

export interface ChatEntry {
  id: number;
  role: 'user' | 'bot';
  text: string;
  meta?: AgentMeta | null;
  results?: SendResultItem[];
  failed?: boolean;
  time: Date;
}
