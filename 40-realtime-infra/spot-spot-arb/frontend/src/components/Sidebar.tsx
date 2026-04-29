import { useState, type KeyboardEvent } from 'react';
import { NetworkWatchList } from './NetworkWatchList';
import { PriceMuteList } from './PriceMuteList';
import type { NetworkWatchItem, PriceMuteItem } from '../types';
import { Divider } from './snatch/Divider';

interface SidebarProps {
  tickers: string[];
  onAddTicker: (t: string) => void;
  onRemoveTicker: (t: string) => void;
  selectedTicker: string | null;
  onSelectTicker: (t: string) => void;
  connected: boolean;
  muteItems: PriceMuteItem[];
  onRemoveMute: (item: PriceMuteItem) => void;
  watchItems: NetworkWatchItem[];
  onRemoveWatch: (item: NetworkWatchItem) => void;
}

export function Sidebar({
  tickers,
  onAddTicker,
  onRemoveTicker,
  selectedTicker,
  onSelectTicker,
  muteItems,
  onRemoveMute,
  watchItems,
  onRemoveWatch,
}: SidebarProps) {
  const [inputValue, setInputValue] = useState('');

  const handleAdd = () => {
    const ticker = inputValue.trim().toUpperCase();
    if (ticker && !tickers.includes(ticker)) {
      onAddTicker(ticker);
    }
    setInputValue('');
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleAdd();
  };

  return (
    <div className="flex h-full flex-col font-mono">
      {/* Section header */}
      <div className="px-3 pt-3 pb-2 border-b border-rule">
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-ink">
            TICKERS
          </span>
          <span className="text-[9px] uppercase tracking-[0.16em] text-ink-faint">
            {tickers.length.toString().padStart(2, '0')} / TRACKED
          </span>
        </div>
        <p className="mt-1 text-[9px] uppercase tracking-[0.14em] text-ink-faint">
          KRW/USDT REAL-TIME GAP MONITOR
        </p>
      </div>

      {/* Add ticker input */}
      <div className="p-3 border-b border-rule">
        <div className="flex gap-1.5">
          <input
            type="text"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value.toUpperCase())}
            onKeyDown={handleKeyDown}
            placeholder="ADD TICKER"
            className="min-w-0 flex-1 bg-cream border border-ink-faint px-2 py-1.5 text-[11px] font-semibold uppercase tracking-widest text-ink placeholder-ink-faint focus:outline-none focus:border-ink"
          />
          <button
            onClick={handleAdd}
            className="shrink-0 bg-ink text-cream hover:bg-accent-yellow hover:text-ink px-3 py-1.5 text-[10px] font-bold uppercase tracking-[0.16em] transition-colors"
          >
            ADD
          </button>
        </div>
      </div>

      {/* Ticker list */}
      <div className="flex-1 overflow-y-auto py-1">
        {tickers.length === 0 ? (
          <div className="p-4 text-center">
            <p className="text-ink-faint text-[10px] uppercase tracking-[0.18em]">
              NO TICKERS
            </p>
            <p className="mt-2 text-[9px] text-ink-faint tracking-wider">
              ADD A TICKER TO BEGIN
            </p>
          </div>
        ) : (
          <ul>
            {tickers.map((ticker, idx) => {
              const isActive = selectedTicker === ticker;
              const num = String(idx + 1).padStart(2, '0');
              return (
                <li key={ticker}>
                  <div
                    className={`group flex items-center justify-between px-3 py-1.5 cursor-pointer border-l-2 transition-colors ${
                      isActive
                        ? 'bg-accent-yellow border-ink text-ink'
                        : 'border-transparent text-ink hover:bg-oatmeal-deep/60 hover:border-ink-faint'
                    }`}
                    onClick={() => onSelectTicker(ticker)}
                  >
                    <div className="flex items-baseline gap-2 min-w-0">
                      <span className="text-[9px] text-ink-faint">{num}</span>
                      <span className="font-bold text-[12px] tracking-[0.08em] truncate">
                        {ticker}
                      </span>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onRemoveTicker(ticker);
                      }}
                      className={`text-[11px] px-1 transition-opacity ${
                        isActive
                          ? 'text-ink hover:text-loss-red'
                          : 'text-ink-faint hover:text-loss-red opacity-0 group-hover:opacity-100'
                      }`}
                      title="REMOVE"
                    >
                      ×
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <Divider variant="double" label="MUTED PAIRS" className="px-3 py-1 bg-oatmeal/50" />
      <PriceMuteList items={muteItems} onRemove={onRemoveMute} />

      <Divider variant="double" label="NETWORK WATCH" className="px-3 py-1 bg-oatmeal/50" />
      <NetworkWatchList items={watchItems} onRemove={onRemoveWatch} />
    </div>
  );
}
