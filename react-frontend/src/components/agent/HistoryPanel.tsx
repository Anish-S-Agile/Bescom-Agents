import { useCallback, useEffect, useState } from 'react';
import { shortDate } from '../../lib/format';
import { Icon } from '../ui';
import { agentApi } from './api';
import { errorText } from './hooks';
import type { HistoryItem, HistoryStats, MessageStatus } from './types';

const PAGE = 25;
const STATUS_LABEL: Record<MessageStatus, string> = {
  sent: 'Sent',
  failed: 'Failed',
  dry_run: 'Dry run',
  duplicate: 'Duplicate',
};

/** Everything the agents have sent for this user (stored in PostgreSQL, or the fake Postgres in dev/tests). */
export function HistoryPanel() {
  const [items, setItems] = useState<HistoryItem[] | null>(null);
  const [stats, setStats] = useState<HistoryStats | null>(null);
  const [channel, setChannel] = useState<'' | 'email' | 'whatsapp'>('');
  const [status, setStatus] = useState<'' | MessageStatus>('');
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [rows, s] = await Promise.all([agentApi.messages({ channel, status, limit: PAGE }), agentApi.messageStats()]);
      setItems(rows);
      setStats(s);
      setMore(rows.length === PAGE);
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [channel, status]);

  useEffect(() => {
    void load();
  }, [load]);

  async function loadMore() {
    try {
      const rows = await agentApi.messages({ channel, status, limit: PAGE, offset: items?.length ?? 0 });
      setItems((prev) => [...(prev ?? []), ...rows]);
      setMore(rows.length === PAGE);
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <div className="history-panel">
      <div className="history-toolbar">
        {stats && (
          <span className="muted" aria-label="Totals">
            {stats.total} total · {stats.by_status.sent ?? 0} sent · {stats.by_status.failed ?? 0} failed
          </span>
        )}
        <span className="spacer" />
        <select className="text-input select" aria-label="Channel" value={channel} onChange={(e) => setChannel(e.target.value as typeof channel)}>
          <option value="">All channels</option>
          <option value="email">Email</option>
          <option value="whatsapp">WhatsApp</option>
        </select>
        <select className="text-input select" aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
          <option value="">All statuses</option>
          <option value="sent">Sent</option>
          <option value="failed">Failed</option>
          <option value="dry_run">Dry run</option>
        </select>
        <button type="button" className="icon-btn" aria-label="Refresh history" onClick={() => void load()}>
          <Icon name="refresh" />
        </button>
      </div>

      {error && <div className="form-error" role="alert">{error}</div>}
      {items === null && !error && <p className="muted">Loading…</p>}
      {items !== null && items.length === 0 && <p className="muted">Nothing sent yet.</p>}

      {items !== null && items.length > 0 && (
        <div className="history-table-wrap">
          <table className="history-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Channel</th>
                <th>To</th>
                <th>Message</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map((m) => (
                <tr key={m.id}>
                  <td>{m.created_at ? shortDate(new Date(`${m.created_at}Z`)) : ''}</td>
                  <td><span className="chip">{m.channel}</span></td>
                  <td className="cell-to">{m.recipients.join(', ')}</td>
                  <td className="cell-msg">
                    {m.subject && <strong>{m.subject}</strong>}
                    <div className="snippet">{(m.body ?? '').slice(0, 140)}</div>
                    {m.error && <div className="form-error small">{m.error}</div>}
                  </td>
                  <td><span className={`chip status-chip status-${m.status}`}>{STATUS_LABEL[m.status] ?? m.status}</span>
                    {m.drafted_by_ai && <span className="chip" title="Drafted by the AI assistant">AI</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {more && (
        <button type="button" className="btn btn-stroked" onClick={() => void loadMore()}>
          Load more
        </button>
      )}
    </div>
  );
}
