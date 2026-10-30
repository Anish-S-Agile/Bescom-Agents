import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { Icon } from '../ui';
import { PreviewCard } from './PreviewCard';
import { RichText } from './richText';
import type { ChatEntry } from './types';
import type { useAgentChat } from './hooks';

type Chat = ReturnType<typeof useAgentChat>;

const SUGGESTIONS = [
  'Email alice@example.com that the meeting moved to 4pm',
  'WhatsApp +919876543210 that the delivery is delayed by a day',
  'Send an email to ops@example.com about tonight\'s maintenance window',
  'Email Alice and WhatsApp Bob about the Friday town hall',
];

function ResultList({ entry }: { entry: ChatEntry }) {
  if (!entry.results?.length) return null;
  return (
    <ul className="send-results" aria-label="Send results">
      {entry.results.map((r, i) => (
        <li key={i} className={r.success ? 'ok' : 'bad'}>
          <Icon name={r.success ? (r.dry_run ? 'science' : 'check_circle') : 'error'} />
          <span className="chip">{r.channel}</span>
          {r.success ? r.message : r.error}
        </li>
      ))}
    </ul>
  );
}

function Bubble({ entry, isLast, loading, send }: { entry: ChatEntry; isLast: boolean; loading: boolean; send: Chat['send'] }) {
  const awaiting = entry.role === 'bot' && isLast && entry.meta?.awaiting_confirmation;
  return (
    <div className={`agent-msg agent-msg-${entry.role}${entry.failed ? ' agent-msg-failed' : ''}`}>
      <div className="agent-bubble">
        {entry.role === 'bot' ? <RichText text={entry.text} /> : <p>{entry.text}</p>}
        {entry.role === 'bot' && awaiting && entry.meta?.preview && <PreviewCard preview={entry.meta.preview} />}
        <ResultList entry={entry} />
        {awaiting && (
          <div className="quick-replies" role="group" aria-label="Confirm">
            <button type="button" className="btn btn-primary" disabled={loading} onClick={() => void send('yes')}>
              <Icon name="send" /> Yes, send it
            </button>
            <button type="button" className="btn btn-stroked" disabled={loading} onClick={() => void send('cancel')}>
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function AgentChat({ chat }: { chat: Chat }) {
  const { entries, loading, send, reset } = chat;
  const [text, setText] = useState('');
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' });
  }, [entries, loading]);

  function submit(e?: FormEvent) {
    e?.preventDefault();
    if (!text.trim() || loading) return;
    const value = text;
    setText('');
    void send(value);
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="agent-chat">
      <div className="agent-chat-toolbar">
        <span className="agent-chat-hint">
          Ask in plain English. The assistant (local Mistral) asks for anything missing and shows a preview before it sends.
        </span>
        <button type="button" className="btn btn-stroked" onClick={() => void reset()} disabled={loading || entries.length === 0}>
          <Icon name="restart_alt" /> New conversation
        </button>
      </div>

      <div className="agent-messages" role="log" aria-live="polite" aria-label="Conversation">
        {entries.length === 0 && (
          <div className="agent-empty">
            <Icon name="forum" className="agent-empty-icon" />
            <h3>What would you like to send?</h3>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" className="chip chip-button" onClick={() => setText(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {entries.map((e, i) => (
          <Bubble key={e.id} entry={e} isLast={i === entries.length - 1} loading={loading} send={send} />
        ))}
        {loading && (
          <div className="agent-msg agent-msg-bot" role="status">
            <div className="agent-bubble agent-typing">
              <span className="typing-dot" />
              <span className="typing-dot" />
              <span className="typing-dot" />
              <span className="sr-only">Assistant is thinking</span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="agent-input" onSubmit={submit}>
        <textarea
          className="text-input"
          rows={2}
          value={text}
          placeholder="e.g. Email alice@example.com that the meeting moved to 4pm"
          aria-label="Message"
          maxLength={8000}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
        />
        <button type="submit" className="btn btn-primary" disabled={loading || !text.trim()}>
          <Icon name="send" /> Send
        </button>
      </form>
    </div>
  );
}
