import { useCallback, useEffect, useState } from 'react';
import { agentApi } from '../components/agent/api';
import { useAgentHealth } from '../components/agent/hooks';
import { StatusBar } from '../components/agent/StatusBar';
import type { AgentStats, HistoryItem, HistoryStats } from '../components/agent/types';
import { shortDate } from '../lib/format';
import '../components/agent/agent.css';

/** /admin - operational view: service status, agent counters and everyone's recent sends. */
export function Admin() {
  const { health, refresh: refreshHealth } = useAgentHealth();
  const [stats, setStats] = useState<AgentStats | null>(null);
  const [totals, setTotals] = useState<HistoryStats | null>(null);
  const [recent, setRecent] = useState<HistoryItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, t, r] = await Promise.all([
        agentApi.stats(),
        agentApi.messageStats(true),
        agentApi.messages({ all: true, limit: 10 }),
      ]);
      setStats(s);
      setTotals(t);
      setRecent(r);
      setError(null);
    } catch {
      setError('Could not load admin data.');
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="assistant-page">
      <div className="assistant-head">
        <div>
          <h2>Admin</h2>
          <p className="muted">Service status and activity across all users</p>
        </div>
        <StatusBar health={health} onRefresh={() => { void refreshHealth(); void load(); }} />
      </div>
      {error && <div className="form-error" role="alert">{error}</div>}

      <div className="admin-grid">
        <div className="card">
          <div className="card-title">Messages</div>
          <p><strong>{totals?.total ?? 0}</strong> total</p>
          <p className="muted">
            {totals?.by_status.sent ?? 0} sent · {totals?.by_status.failed ?? 0} failed · {totals?.by_status.dry_run ?? 0} dry run
          </p>
          <p className="muted">
            Email {totals?.by_channel.email ?? 0} · WhatsApp {totals?.by_channel.whatsapp ?? 0}
          </p>
        </div>
        <div className="card">
          <div className="card-title">Agents</div>
          <p><strong>{stats?.total_requests ?? 0}</strong> chat requests · {stats?.active_sessions ?? 0} open conversations</p>
          {stats?.agent_state &&
            Object.entries(stats.agent_state).map(([name, a]) => (
              <p key={name} className="muted">
                {name}: {a.execution_count} runs · {a.failure_count} failed{a.last_error ? ` · last error: ${a.last_error}` : ''}
              </p>
            ))}
        </div>
      </div>

      <h3>Recent messages (all users)</h3>
      {recent.length === 0 ? (
        <p className="muted">Nothing sent yet.</p>
      ) : (
        <div className="history-table-wrap">
          <table className="history-table">
            <thead>
              <tr><th>When</th><th>Channel</th><th>To</th><th>Status</th></tr>
            </thead>
            <tbody>
              {recent.map((m) => (
                <tr key={m.id}>
                  <td>{m.created_at ? shortDate(new Date(`${m.created_at}Z`)) : ''}</td>
                  <td><span className="chip">{m.channel}</span></td>
                  <td className="cell-to">{m.recipients.join(', ')}</td>
                  <td><span className={`chip status-chip status-${m.status}`}>{m.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
