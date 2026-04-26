import { useState, useEffect, useCallback, useMemo } from 'react';
import { Layout } from './components/Layout';
import { Sidebar } from './components/Sidebar';
import { TickerDashboard } from './components/TickerDashboard';
import { PerpDexDashboard } from './components/PerpDexDashboard';
import { useWebSocket } from './hooks/useWebSocket';
import type { NetworkWatchItem, PriceMuteItem } from './types';

const STORAGE_KEY = 'bithumb_arb_tickers';

function loadTickers(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed as string[];
    }
  } catch {
    // ignore
  }
  return [];
}

function saveTickers(tickers: string[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tickers));
  } catch {
    // ignore
  }
}

export default function App() {
  const [tickers, setTickers] = useState<string[]>(loadTickers);
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [muteItems, setMuteItems] = useState<PriceMuteItem[]>([]);
  const [watchItems, setWatchItems] = useState<NetworkWatchItem[]>([]);
  const [activeTab, setActiveTab] = useState<string>('overview');
  const [clockLabel, setClockLabel] = useState<string>('');
  const { data, connected } = useWebSocket(tickers);

  useEffect(() => {
    const tick = () => {
      const now = new Date();
      setClockLabel(
        now.toLocaleTimeString('ko-KR', {
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        }),
      );
    };
    tick();
    const t = window.setInterval(tick, 30_000);
    return () => window.clearInterval(t);
  }, []);

  // Persist tickers to localStorage whenever they change
  useEffect(() => {
    saveTickers(tickers);
  }, [tickers]);

  // Auto-select first ticker if nothing is selected but tickers exist
  useEffect(() => {
    if (!selectedTicker && tickers.length > 0) {
      setSelectedTicker(tickers[0]);
    }
  }, [tickers, selectedTicker]);

  // ?쒕쾭?먯꽌 媛먯떆 由ъ뒪??濡쒕뱶
  useEffect(() => {
    fetch('/api/network-watchlist')
      .then((res) => res.json())
      .then((data) => setWatchItems(data.items ?? []))
      .catch(() => {});

    fetch('/api/price-mute-list')
      .then((res) => res.json())
      .then((data) => setMuteItems(data.items ?? []))
      .catch(() => {});
  }, []);

  const handleAddTicker = (ticker: string) => {
    setTickers((prev) => {
      if (prev.includes(ticker)) return prev;
      return [...prev, ticker];
    });
    setSelectedTicker(ticker);
  };

  const handleRemoveTicker = (ticker: string) => {
    setTickers((prev) => {
      const next = prev.filter((t) => t !== ticker);
      return next;
    });
    setSelectedTicker((prev) => {
      if (prev === ticker) {
        const remaining = tickers.filter((t) => t !== ticker);
        return remaining.length > 0 ? remaining[0] : null;
      }
      return prev;
    });
  };

  const updatePriceMute = useCallback(
    (exchange: string, ticker: string, nextMuted: boolean) => {
      fetch('/api/price-mute-list', {
        method: nextMuted ? 'POST' : 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exchange, ticker }),
      })
        .then((res) => res.json())
        .then((data) => setMuteItems(data.items ?? []))
        .catch(() => {});
    },
    [],
  );

  const handleRemoveMute = useCallback(
    (item: PriceMuteItem) => {
      updatePriceMute(item.exchange, item.ticker, false);
    },
    [updatePriceMute],
  );

  const handleAddWatch = useCallback(
    (exchange: string, ticker: string, network: string) => {
      fetch('/api/network-watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exchange, ticker, network }),
      })
        .then((res) => res.json())
        .then((data) => setWatchItems(data.items ?? []))
        .catch(() => {});
    },
    [],
  );

  const handleRemoveWatch = useCallback((item: NetworkWatchItem) => {
    fetch('/api/network-watchlist', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        exchange: item.exchange,
        ticker: item.ticker,
        network: item.network,
      }),
    })
      .then((res) => res.json())
      .then((data) => setWatchItems(data.items ?? []))
      .catch(() => {});
  }, []);

  const mutedPairs = useMemo(() => {
    const pairs = new Set<string>();
    for (const item of muteItems) {
      pairs.add(`${item.exchange.toLowerCase()}:${item.ticker.toUpperCase()}`);
    }
    return pairs;
  }, [muteItems]);

  const selectedData = selectedTicker ? data[selectedTicker] : null;

  const venueCount = selectedData
    ? Object.values(selectedData.exchanges).filter(
        (e) => e.spot.supported || e.futures.supported,
      ).length
    : 0;

  return (
    <Layout
      connected={connected}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      venueCount={venueCount}
      volume24hLabel={`${tickers.length} TICKERS TRACKED`}
      timeLabel={clockLabel}
      sidebar={
        <Sidebar
          tickers={tickers}
          onAddTicker={handleAddTicker}
          onRemoveTicker={handleRemoveTicker}
          selectedTicker={selectedTicker}
          onSelectTicker={setSelectedTicker}
          connected={connected}
          muteItems={muteItems}
          onRemoveMute={handleRemoveMute}
          watchItems={watchItems}
          onRemoveWatch={handleRemoveWatch}
        />
      }
    >
      {activeTab === 'perpdex' ? (
        <PerpDexDashboard />
      ) : selectedTicker && selectedData ? (
        <TickerDashboard
          ticker={selectedTicker}
          data={selectedData}
          mutedPairs={mutedPairs}
          onTogglePriceMute={updatePriceMute}
          onNetworkClick={handleAddWatch}
        />
      ) : (
        <div className="flex h-full flex-col items-center justify-center bg-cream text-ink">
          {selectedTicker && !selectedData ? (
            <>
              <div className="mb-4 h-6 w-6 animate-spin border-2 border-rule border-t-ink" />
              <p className="text-[11px] font-bold uppercase tracking-[0.22em]">
                RECEIVING {selectedTicker} /// WAIT
              </p>
              <p className="mt-2 text-[10px] uppercase tracking-[0.18em] text-ink-faint">
                {connected ? 'CONNECTED' : 'AWAITING SERVER'}
              </p>
            </>
          ) : (
            <>
              <p className="text-[12px] font-bold uppercase tracking-[0.24em]">
                SELECT A TICKER
              </p>
              <p className="mt-2 text-[10px] uppercase tracking-[0.18em] text-ink-faint">
                ADD OR CHOOSE ONE FROM THE LEFT PANEL
              </p>
            </>
          )}
        </div>
      )}
    </Layout>
  );
}

