import { Icon } from '../ui';
import type { Preview } from './types';

/** Structured "about to send" card, shown while the orchestrator waits for the user's confirmation. */
export function PreviewCard({ preview }: { preview: Preview }) {
  const email = preview.channel === 'email' || preview.channel === 'both';
  const wa = preview.channel === 'whatsapp' || preview.channel === 'both';
  return (
    <div className="preview-card" aria-label="Message preview">
      {email && (
        <div className="preview-row">
          <Icon name="mail" />
          <div>
            <div className="preview-to">To: {preview.emails.join(', ')}</div>
            {preview.subject && <div className="preview-subject">{preview.subject}</div>}
          </div>
        </div>
      )}
      {wa && (
        <div className="preview-row">
          <Icon name="chat" />
          <div className="preview-to">WhatsApp: {preview.phones.join(', ')}</div>
        </div>
      )}
      {preview.body && <pre className="preview-body">{preview.body}</pre>}
      {preview.drafted_by_ai && (
        <div className="preview-ai-note">
          <Icon name="auto_awesome" /> Written by the AI assistant - please review before sending.
        </div>
      )}
    </div>
  );
}
