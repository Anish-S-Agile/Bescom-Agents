import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { useToast } from '../../context/ToastContext';
import { Icon } from '../ui';
import { agentApi } from './api';
import { errorText } from './hooks';
import type { Contact } from './types';

const EMPTY = { name: '', email: '', phone: '', organization: '' };

/** Address book: the orchestrator resolves names like "email Alice" through these contacts. */
export function ContactsPanel() {
  const toast = useToast();
  const [items, setItems] = useState<Contact[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY);
  const [editing, setEditing] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [filter, setFilter] = useState('');

  const load = useCallback(async () => {
    try {
      setItems(await agentApi.contacts());
      setLoadError(null);
    } catch (err) {
      setLoadError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return (items ?? []).filter(
      (c) => !q || [c.name, c.email, c.phone, c.organization].some((v) => (v ?? '').toLowerCase().includes(q)),
    );
  }, [items, filter]);

  function startEdit(c: Contact) {
    setEditing(c.id);
    setForm({ name: c.name, email: c.email ?? '', phone: c.phone ?? '', organization: c.organization ?? '' });
  }

  function cancelEdit() {
    setEditing(null);
    setForm(EMPTY);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (saving) return;
    if (!form.name.trim()) return toast('Name is required', 'error');
    if (!form.email.trim() && !form.phone.trim()) return toast('Add an email address or a phone number', 'error');
    setSaving(true);
    try {
      const body = { name: form.name.trim(), email: form.email.trim() || null, phone: form.phone.trim() || null,
        organization: form.organization.trim() || null };
      if (editing !== null) await agentApi.updateContact(editing, body);
      else await agentApi.createContact(body);
      toast(editing !== null ? 'Contact updated' : 'Contact added', 'success');
      cancelEdit();
      await load();
    } catch (err) {
      toast(errorText(err), 'error');
    } finally {
      setSaving(false);
    }
  }

  async function remove(c: Contact) {
    if (!window.confirm(`Delete ${c.name}?`)) return;
    try {
      await agentApi.deleteContact(c.id);
      toast('Contact deleted', 'success');
      if (editing === c.id) cancelEdit();
      await load();
    } catch (err) {
      toast(errorText(err), 'error');
    }
  }

  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <div className="contacts-panel">
      <form className="contact-form card" onSubmit={submit} aria-label={editing !== null ? 'Edit contact' : 'Add contact'}>
        <div className="card-title">
          <Icon name={editing !== null ? 'edit' : 'person_add'} />
          {editing !== null ? 'Edit contact' : 'Add contact'}
        </div>
        <div className="form-grid">
          <label className="field">
            <span className="field-label">Name</span>
            <input className="text-input" value={form.name} onChange={set('name')} maxLength={120} />
          </label>
          <label className="field">
            <span className="field-label">Email</span>
            <input className="text-input" type="email" value={form.email} onChange={set('email')} />
          </label>
          <label className="field">
            <span className="field-label">WhatsApp / phone</span>
            <input className="text-input" value={form.phone} onChange={set('phone')} placeholder="+919876543210" />
          </label>
          <label className="field">
            <span className="field-label">Organization</span>
            <input className="text-input" value={form.organization} onChange={set('organization')} maxLength={160} />
          </label>
        </div>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {editing !== null ? 'Save changes' : 'Add contact'}
          </button>
          {editing !== null && (
            <button type="button" className="btn btn-stroked" onClick={cancelEdit}>
              Cancel
            </button>
          )}
        </div>
      </form>

      <div className="contacts-list">
        <input className="text-input" placeholder="Search contacts" aria-label="Search contacts" value={filter}
          onChange={(e) => setFilter(e.target.value)} />
        {loadError && <div className="form-error" role="alert">{loadError}</div>}
        {items === null && !loadError && <p className="muted">Loading…</p>}
        {items !== null && visible.length === 0 && (
          <p className="muted">{items.length === 0 ? 'No contacts yet. Add one to email or WhatsApp people by name.' : 'No matches.'}</p>
        )}
        <ul className="contact-items">
          {visible.map((c) => (
            <li key={c.id} className="contact-item">
              <div className="contact-main">
                <strong>{c.name}</strong>
                {c.organization && <span className="muted"> · {c.organization}</span>}
                <div className="contact-lines">
                  {c.email && <span><Icon name="mail" /> {c.email}</span>}
                  {c.phone && <span><Icon name="chat" /> {c.phone}</span>}
                </div>
              </div>
              <div className="contact-actions">
                <button type="button" className="icon-btn" aria-label={`Edit ${c.name}`} onClick={() => startEdit(c)}>
                  <Icon name="edit" />
                </button>
                <button type="button" className="icon-btn" aria-label={`Delete ${c.name}`} onClick={() => void remove(c)}>
                  <Icon name="delete" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
