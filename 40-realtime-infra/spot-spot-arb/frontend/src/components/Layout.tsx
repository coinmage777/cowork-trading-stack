import type { ReactNode } from 'react';
import { TabNav } from './snatch/TabNav';
import { LiveChip } from './snatch/LiveChip';

interface LayoutProps {
  sidebar: ReactNode;
  children: ReactNode;
  connected?: boolean;
  activeTab?: string;
  onTabChange?: (id: string) => void;
  venueCount?: number;
  volume24hLabel?: string;
  timeLabel?: string;
}

const TABS = [
  { id: 'overview', label: 'OVERVIEW' },
  { id: 'venues', label: 'VENUES' },
  { id: 'networks', label: 'NETWORKS' },
  { id: 'hedges', label: 'HEDGES' },
  { id: 'backtests', label: 'BACKTESTS' },
  { id: 'perpdex', label: 'PERP DEX' },
];

export function Layout({
  sidebar,
  children,
  connected = false,
  activeTab = 'overview',
  onTabChange,
  venueCount,
  volume24hLabel,
  timeLabel,
}: LayoutProps) {
  return (
    <div className="flex h-screen flex-col bg-cream text-ink overflow-hidden">
      {/* Top brand bar */}
      <header className="flex items-center justify-between border-b border-rule px-4 py-2 bg-cream">
        <div className="flex items-center gap-6 min-w-0">
          <div className="flex items-baseline gap-1.5 shrink-0">
            <span className="text-[13px] font-extrabold tracking-[0.22em] uppercase">
              BITHUMB ARB
              <sup className="text-[9px] ml-0.5 text-ink-faint">TM</sup>
            </span>
            <span className="text-ink-faint text-[11px]">· PERP HEDGE</span>
            <span className="text-ink-faint tracking-widest text-[11px]">///</span>
            <span className="text-[10px] text-ink-faint uppercase tracking-[0.16em]">
              V01
            </span>
          </div>
          <TabNav
            tabs={TABS}
            active={activeTab}
            onChange={(id) => onTabChange?.(id)}
            className="hidden md:flex"
          />
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <div className="hidden sm:flex flex-col items-end text-[10px] uppercase tracking-[0.14em] text-ink-mute">
            <span>
              {venueCount ?? '—'} VENUES · {volume24hLabel ?? '$0.00M TOTAL'}
            </span>
          </div>
          <LiveChip connected={connected} time={timeLabel} />
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* Sidebar */}
        <aside className="w-[240px] min-w-[240px] max-w-[240px] flex-shrink-0 border-r border-rule bg-oatmeal/40 flex flex-col overflow-hidden">
          {sidebar}
        </aside>

        {/* Main content */}
        <main className="flex-1 h-full overflow-hidden bg-cream">
          <div className="h-full">{children}</div>
        </main>
      </div>
    </div>
  );
}
