import type { NetworkWatchItem } from '../types';

const EXCHANGE_DISPLAY: Record<string, string> = {
  binance: 'Binance',
  bybit: 'Bybit',
  okx: 'OKX',
  bitget: 'Bitget',
  gate: 'Gate',
  htx: 'HTX',
  upbit: 'Upbit',
  coinone: 'Coinone',
  bithumb: 'Bithumb',
};

interface NetworkWatchListProps {
  items: NetworkWatchItem[];
  onRemove: (item: NetworkWatchItem) => void;
}

export function NetworkWatchList({ items, onRemove }: NetworkWatchListProps) {
  const grouped: Record<string, NetworkWatchItem[]> = {};
  for (const item of items) {
    if (!grouped[item.exchange]) {
      grouped[item.exchange] = [];
    }
    grouped[item.exchange].push(item);
  }

  return (
    <div className="flex flex-col">
      <div className="px-3 py-1.5 flex items-center justify-between">
        <h2 className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink">
          NETWORK WATCH
        </h2>
        <span className="text-[9px] text-ink-faint">
          {String(items.length).padStart(2, '0')}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-2 max-h-[220px]">
        {items.length === 0 ? (
          <p className="text-ink-faint text-[9px] uppercase tracking-[0.14em] text-center py-2">
            —
          </p>
        ) : (
          <div className="space-y-1.5">
            {Object.entries(grouped).map(([exchange, exchangeItems]) => (
              <div key={exchange}>
                <p className="text-[9px] font-semibold uppercase tracking-widest text-ink-mute px-1 mb-0.5">
                  {EXCHANGE_DISPLAY[exchange] ?? exchange}
                </p>
                <ul className="space-y-0">
                  {exchangeItems.map((item) => {
                    const key = `${item.exchange}:${item.ticker}:${item.network}`;
                    return (
                      <li
                        key={key}
                        className="flex items-center justify-between px-2 py-0.5 hover:bg-oatmeal-deep/60 group"
                      >
                        <span className="text-[10px] text-ink tracking-wider truncate pr-2">
                          {item.ticker}{' '}
                          <span className="text-ink-faint">({item.network})</span>
                        </span>
                        <button
                          onClick={() => onRemove(item)}
                          className="text-[11px] text-ink-faint hover:text-loss-red opacity-0 group-hover:opacity-100 px-1"
                          title="UNWATCH"
                        >
                          ×
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
