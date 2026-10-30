import { useId, useState, type CSSProperties, type ReactNode } from 'react';

/** Material Icons ligature (font is loaded in index.html). */
export function Icon({ name, className = '' }: { name: string; className?: string }) {
  return (
    <span className={`material-icons ${className}`} aria-hidden="true">
      {name}
    </span>
  );
}

export interface TabDef {
  id: string;
  label: string;
  content: ReactNode;
}

/** Minimal accessible tab group (Only the active tab's content is mounted, like mat-tab-group). */
export function Tabs({ tabs }: { tabs: TabDef[] }) {
  const [active, setActive] = useState(tabs[0]?.id);
  const [direction, setDirection] = useState<'forward' | 'backward'>('forward');
  const base = useId();
  const current = tabs.find((t) => t.id === active) ?? tabs[0];
  const index = Math.max(0, tabs.indexOf(current));

  function select(id: string) {
    const next = tabs.findIndex((t) => t.id === id);
    setDirection(next < index ? 'backward' : 'forward'); // content slides in from the side the tab lies on
    setActive(id);
  }

  return (
    <div className="tab-group">
      <div
        className="tab-header"
        role="tablist"
        style={{ '--i': index, '--n': tabs.length } as CSSProperties}
      >
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`${base}-tab-${t.id}`}
            aria-selected={t.id === current.id}
            aria-controls={`${base}-panel-${t.id}`}
            className={`tab-label${t.id === current.id ? ' active' : ''}`}
            onClick={() => select(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div
        key={current.id}
        className="tab-body"
        data-direction={direction}
        role="tabpanel"
        id={`${base}-panel-${current.id}`}
        aria-labelledby={`${base}-tab-${current.id}`}
      >
        {current.content}
      </div>
    </div>
  );
}
