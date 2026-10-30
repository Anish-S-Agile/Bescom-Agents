import { Icon } from '../ui';
import type { AgentHealth } from './types';

type Tone = 'ok' | 'warn' | 'bad' | 'unknown';

function Dot({ label, tone, title }: { label: string; tone: Tone; title: string }) {
  return (
    <span className={`status-pill status-${tone}`} title={title}>
      <span className="status-dot" aria-hidden="true" />
      {label}
      <span className="sr-only"> {tone === 'ok' ? 'ready' : tone === 'unknown' ? 'checking' : 'needs attention'}</span>
    </span>
  );
}

/** Live readiness of the pieces the orchestrator depends on: Ollama/mistral, Email, WhatsApp, database. */
export function StatusBar({ health, onRefresh }: { health: AgentHealth | null; onRefresh: () => void }) {
  if (!health) {
    return (
      <div className="status-bar" aria-label="Service status">
        <Dot label="Checking services…" tone="unknown" title="Checking" />
      </div>
    );
  }
  const llm = health.llm;
  const email = health.sub_agents?.email;
  const wa = health.sub_agents?.whatsapp;
  const db = health.database;

  const llmTone: Tone = !llm ? 'bad' : llm.reachable && llm.model_pulled ? 'ok' : 'bad';
  const llmTitle = !llm
    ? health.error ?? 'Unavailable'
    : !llm.reachable
      ? 'Ollama is not reachable. Start it with `ollama serve`.'
      : !llm.model_pulled
        ? (llm.hint ?? `Model ${llm.model} is not pulled.`)
        : `${llm.model} is ready`;

  const sender = (s: typeof email, name: string): [Tone, string] =>
    !s
      ? ['bad', `${name} agent unavailable`]
      : s.dry_run
        ? ['warn', `${name}: dry-run mode (nothing is actually sent)`]
        : s.configured
          ? ['ok', `${name} is configured`]
          : ['bad', `${name} is not configured on the server`];
  const [emailTone, emailTitle] = sender(email, 'Email');
  const [waTone, waTitle] = sender(wa, 'WhatsApp');

  return (
    <div className="status-bar" aria-label="Service status">
      <Dot label={llm?.model ? `${llm.model}` : 'LLM'} tone={llmTone} title={llmTitle} />
      <Dot label="Email" tone={emailTone} title={emailTitle} />
      <Dot label="WhatsApp" tone={waTone} title={waTitle} />
      {db && (
        <Dot
          label={db.fake ? 'Fake DB' : 'Postgres'}
          tone={db.healthy ? (db.fake ? 'warn' : 'ok') : 'bad'}
          title={db.fake ? 'Using the fake (test) Postgres - not for production' : 'PostgreSQL connected'}
        />
      )}
      <button type="button" className="icon-btn status-refresh" aria-label="Refresh status" title="Refresh status" onClick={onRefresh}>
        <Icon name="refresh" />
      </button>
    </div>
  );
}
