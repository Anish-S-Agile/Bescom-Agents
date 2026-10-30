import { AgentChat } from '../components/agent/AgentChat';
import { ContactsPanel } from '../components/agent/ContactsPanel';
import { HistoryPanel } from '../components/agent/HistoryPanel';
import { StatusBar } from '../components/agent/StatusBar';
import { useAgentChat, useAgentHealth } from '../components/agent/hooks';
import { Tabs } from '../components/ui';

/**
 * /assistant - the orchestrator UI. Chat, address book and send history in one place.
 * The chat hook is called here (not inside the tab) so a conversation survives tab switches.
 */
export function Assistant() {
  const chat = useAgentChat();
  const { health, refresh } = useAgentHealth();

  return (
    <div className="assistant-page">
      <div className="assistant-head">
        <div>
          <h2>Bescom Assistant</h2>
          <p className="muted">Orchestrator → Email service agent · WhatsApp agent</p>
        </div>
        <StatusBar health={health} onRefresh={() => void refresh()} />
      </div>
      <Tabs
        tabs={[
          { id: 'chat', label: 'Assistant', content: <AgentChat chat={chat} /> },
          { id: 'contacts', label: 'Contacts', content: <ContactsPanel /> },
          { id: 'history', label: 'History', content: <HistoryPanel /> },
        ]}
      />
    </div>
  );
}
