interface Tab {
  id: string;
  label: string;
  count?: number;
}

interface TabNavProps {
  tabs: Tab[];
  active: string;
  onChange: (id: string) => void;
  className?: string;
}

export function TabNav({ tabs, active, onChange, className = '' }: TabNavProps) {
  return (
    <nav className={`flex items-center gap-5 text-[11px] uppercase tracking-[0.18em] ${className}`}>
      {tabs.map((tab, idx) => {
        const isActive = tab.id === active;
        const num = String(idx + 1).padStart(2, '0');
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            className={`flex items-center gap-1.5 py-1 transition-colors ${
              isActive
                ? 'text-ink border-b-2 border-ink'
                : 'text-ink-faint hover:text-ink-mute border-b-2 border-transparent'
            }`}
          >
            <span className="text-[9px] text-ink-faint">{num}</span>
            <span className="font-semibold">{tab.label}</span>
            {typeof tab.count === 'number' && (
              <span className="text-[9px] text-ink-faint">· {tab.count}</span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
