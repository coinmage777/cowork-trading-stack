import { useCallback, useEffect, useMemo, useState } from 'react';
import type { GapUpdate, HedgeJob } from '../types';
import { ExchangeCard } from './ExchangeCard';
import { HedgePnlPanel } from './HedgePnlPanel';
import { WithdrawPanel } from './WithdrawPanel';
import { formatCoin, formatKRW, formatNumber, orDash, DASH } from '../utils/format';
import { SectionHeader } from './snatch/SectionHeader';
import { KPIBlock } from './snatch/KPIBlock';
import { Divider } from './snatch/Divider';

interface TickerDashboardProps {
  ticker: string;
  data: GapUpdate;
  mutedPairs?: ReadonlySet<string>;
  onTogglePriceMute?: (
    exchange: string,
    ticker: string,
    nextMuted: boolean,
  ) => void;
  onNetworkClick?: (exchange: string, ticker: string, network: string) => void;
}

const DOMESTIC_EXCHANGES = new Set(['upbit', 'coinone']);

function formatTimestamp(ts: number): string {
  const date = new Date(ts * 1000);
  return date.toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

async function fetchJson<T>(url: string): Promise<T | null> {
  try {
    const response = await fetch(url);
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export function TickerDashboard({
  ticker,
  data,
  mutedPairs,
  onTogglePriceMute,
  onNetworkClick,
}: TickerDashboardProps) {
  const { bithumb, exchanges, timestamp } = data;
  const hasBithumbData = bithumb.ask != null && bithumb.ask > 0;
  const priceOnlyMode = !hasBithumbData;

  const [hedgeLoading, setHedgeLoading] = useState(false);
  const [hedgeRefreshLoading, setHedgeRefreshLoading] = useState(false);
  const [hedgeMessage, setHedgeMessage] = useState<string | null>(null);
  const [hedgeJobs, setHedgeJobs] = useState<HedgeJob[]>([]);
  const [currentHedgeJob, setCurrentHedgeJob] = useState<HedgeJob | null>(null);
  const [exitSpotExchange, setExitSpotExchange] = useState('');
  const [exitFuturesExchange, setExitFuturesExchange] = useState('');

  const activeHedgeJob = useMemo(() => {
    if (!currentHedgeJob) {
      return null;
    }
    const status = String(currentHedgeJob.status || '').toLowerCase();
    if (status === 'closed' || Boolean(currentHedgeJob.finalized_at)) {
      return null;
    }
    return currentHedgeJob;
  }, [currentHedgeJob]);

  let minSpotGap: number | null = null;
  let minFuturesGap: number | null = null;
  for (const [, info] of Object.entries(exchanges)) {
    if (info.spot.gap != null && (minSpotGap === null || info.spot.gap < minSpotGap)) {
      minSpotGap = info.spot.gap;
    }
    if (info.futures.gap != null && (minFuturesGap === null || info.futures.gap < minFuturesGap)) {
      minFuturesGap = info.futures.gap;
    }
  }

  const foreignExchanges: [string, typeof exchanges[string]][] = [];
  const domesticExchanges: [string, typeof exchanges[string]][] = [];
  for (const [name, info] of Object.entries(exchanges)) {
    if (DOMESTIC_EXCHANGES.has(name)) {
      domesticExchanges.push([name, info]);
      continue;
    }
    foreignExchanges.push([name, info]);
  }

  const foreignExchangesToRender = priceOnlyMode
    ? foreignExchanges.filter(([, info]) => info.spot.supported || info.futures.supported)
    : foreignExchanges;

  const futuresExchangeCandidates = useMemo(() => {
    return Object.entries(exchanges)
      .filter(([, info]) => info.futures.supported)
      .map(([name]) => name);
  }, [exchanges]);

  const loadHedgeJobs = useCallback(async () => {
    const payload = await fetchJson<{ items?: HedgeJob[] }>(
      `/api/hedge/jobs?ticker=${encodeURIComponent(ticker)}&limit=50`,
    );
    setHedgeJobs(payload?.items ?? []);
  }, [ticker]);

  const loadLatestHedgeJob = useCallback(async () => {
    const payload = await fetchJson<{ ok?: boolean; job?: HedgeJob }>(
      `/api/hedge/latest?ticker=${encodeURIComponent(ticker)}`,
    );
    if (payload?.ok && payload.job) {
      setCurrentHedgeJob(payload.job);
      const nextSpotExchange =
        payload.job.exit_spot_exchange ?? payload.job.futures_exchange;
      if (nextSpotExchange) {
        setExitSpotExchange(nextSpotExchange);
      }
      const nextFuturesExchange =
        payload.job.exit_futures_exchange ?? payload.job.futures_exchange;
      if (nextFuturesExchange) {
        setExitFuturesExchange(nextFuturesExchange);
      }
      return;
    }
    setCurrentHedgeJob(null);
  }, [ticker]);

  const handleHedgeJobUpdated = useCallback(
    (job: HedgeJob) => {
      setCurrentHedgeJob(job);
      if (job.exit_spot_exchange) {
        setExitSpotExchange(job.exit_spot_exchange);
      }
      if (job.exit_futures_exchange) {
        setExitFuturesExchange(job.exit_futures_exchange);
      }
      loadHedgeJobs();
    },
    [loadHedgeJobs],
  );

  const refreshTrackedHedgeJob = useCallback(
    async (
      nextSpotExchange?: string,
      nextFuturesExchange?: string,
    ): Promise<{ ok: boolean; job: HedgeJob | null }> => {
      try {
        const response = await fetch('/api/hedge/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticker,
            exit_spot_exchange:
              nextSpotExchange ??
              exitSpotExchange ??
              currentHedgeJob?.exit_spot_exchange ??
              currentHedgeJob?.futures_exchange,
            exit_futures_exchange:
              currentHedgeJob?.futures_exchange ??
              currentHedgeJob?.exit_futures_exchange ??
              nextFuturesExchange ??
              exitFuturesExchange ??
              currentHedgeJob?.futures_exchange,
          }),
        });
        const refreshed = (await response.json()) as { ok?: boolean; job?: HedgeJob | null };
        if (refreshed.ok) {
          if (refreshed.job) {
            handleHedgeJobUpdated(refreshed.job);
            return { ok: true, job: refreshed.job };
          }

          setCurrentHedgeJob(null);
          await loadHedgeJobs();
          return { ok: true, job: null };
        }
        return { ok: false, job: null };
      } catch {
        return { ok: false, job: null };
      }
    },
    [
      ticker,
      exitSpotExchange,
      exitFuturesExchange,
      currentHedgeJob?.exit_spot_exchange,
      currentHedgeJob?.exit_futures_exchange,
      currentHedgeJob?.futures_exchange,
      handleHedgeJobUpdated,
      loadHedgeJobs,
    ],
  );

  const handleManualRefresh = useCallback(async () => {
    setHedgeRefreshLoading(true);
    setHedgeMessage(null);
    try {
      const refreshed = await refreshTrackedHedgeJob(exitSpotExchange, exitFuturesExchange);
      if (!refreshed.ok) {
        setHedgeMessage('포지션 새로고침 실패');
        return;
      }
      if (refreshed.job) {
        setHedgeMessage('현재 포지션을 기준으로 새로고침했습니다');
        return;
      }
      setHedgeMessage('현재 열린 포지션이 없어 결과 기록만 유지합니다');
    } finally {
      setHedgeRefreshLoading(false);
    }
  }, [exitSpotExchange, exitFuturesExchange, refreshTrackedHedgeJob]);

  useEffect(() => {
    setHedgeMessage(null);
    loadHedgeJobs();
    loadLatestHedgeJob();
  }, [ticker, loadHedgeJobs, loadLatestHedgeJob]);

  useEffect(() => {
    if (!currentHedgeJob || currentHedgeJob.finalized_at) {
      return;
    }
    const status = String(currentHedgeJob.status || '').toLowerCase();
    if (status === 'closed') {
      return;
    }
    if (!exitSpotExchange || !exitFuturesExchange) {
      return;
    }
    void refreshTrackedHedgeJob(exitSpotExchange, exitFuturesExchange);
  }, [
    currentHedgeJob?.job_id,
    currentHedgeJob?.finalized_at,
    exitSpotExchange,
    exitFuturesExchange,
    refreshTrackedHedgeJob,
  ]);

  useEffect(() => {
    if (!currentHedgeJob || currentHedgeJob.finalized_at) {
      return;
    }
    const status = String(currentHedgeJob.status || '').toLowerCase();
    if (status === 'closed') {
      return;
    }
    if (status !== 'hedged' && status !== 'partial_hedged') {
      return;
    }

    const timer = window.setInterval(() => {
      void refreshTrackedHedgeJob();
    }, 4000);

    return () => window.clearInterval(timer);
  }, [
    currentHedgeJob?.job_id,
    currentHedgeJob?.status,
    currentHedgeJob?.finalized_at,
    refreshTrackedHedgeJob,
  ]);

  useEffect(() => {
    if (!exitFuturesExchange || !futuresExchangeCandidates.includes(exitFuturesExchange)) {
      const fallback =
        currentHedgeJob?.exit_futures_exchange ??
        currentHedgeJob?.futures_exchange ??
        futuresExchangeCandidates[0] ??
        '';
      setExitFuturesExchange(fallback);
    }
  }, [futuresExchangeCandidates, exitFuturesExchange, currentHedgeJob]);

  useEffect(() => {
    if (exitSpotExchange && exchanges[exitSpotExchange]) {
      return;
    }
    const fallback =
      currentHedgeJob?.exit_spot_exchange ??
      currentHedgeJob?.futures_exchange ??
      '';
    if (fallback && exchanges[fallback]) {
      setExitSpotExchange(fallback);
    }
  }, [exitSpotExchange, currentHedgeJob, exchanges]);

  const handleEnterHedge = useCallback(
    async (futuresExchange: string) => {
      setHedgeLoading(true);
      setHedgeMessage(null);
      try {
        const response = await fetch('/api/hedge/enter', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticker,
            futures_exchange: futuresExchange,
            leverage: 4,
          }),
        });
        const payload = (await response.json()) as {
          ok?: boolean;
          status?: string;
          code?: string;
          message?: string;
          job?: HedgeJob;
        };

        if (payload.job) {
          setCurrentHedgeJob(payload.job);
          setExitFuturesExchange(
            payload.job.exit_futures_exchange ??
              payload.job.futures_exchange ??
              futuresExchange,
          );
        }

        if (payload.ok) {
          const status = payload.status ?? payload.job?.status ?? 'hedged';
          setHedgeMessage(`현선갭 진입 완료 (${status})`);
        } else {
          setHedgeMessage(
            payload.message ??
              `${payload.code ?? 'HEDGE_ERROR'}: 현선갭 진입 실패`,
          );
        }
      } catch {
        setHedgeMessage('현선갭 요청 실패 (네트워크 에러)');
      } finally {
        setHedgeLoading(false);
        loadHedgeJobs();
      }
    },
    [ticker, loadHedgeJobs],
  );

  const bestForeignGap = useMemo(() => {
    let best: { name: string; gap: number } | null = null;
    for (const [name, info] of Object.entries(exchanges)) {
      if (DOMESTIC_EXCHANGES.has(name)) continue;
      if (info.spot.gap != null && (best === null || info.spot.gap < best.gap)) {
        best = { name, gap: info.spot.gap };
      }
    }
    return best;
  }, [exchanges]);

  const foreignVenueCount = useMemo(
    () =>
      Object.entries(exchanges).filter(
        ([name, info]) =>
          !DOMESTIC_EXCHANGES.has(name) && (info.spot.supported || info.futures.supported),
      ).length,
    [exchanges],
  );

  return (
    <div className="h-full overflow-y-auto bg-cream">
      <div
        className={`grid min-h-full grid-cols-1 gap-0 ${
          priceOnlyMode ? '' : 'xl:grid-cols-12 xl:grid-rows-[minmax(0,1fr)_auto]'
        }`}
      >
        <section className={`min-h-0 ${priceOnlyMode ? '' : 'xl:col-span-9'}`}>
          <div className="flex h-full min-h-0 flex-col">
            {/* SNATCH-style brand header */}
            <div className="px-5 pt-4 pb-2 border-b border-rule">
              <SectionHeader
                title={`${ticker} LEDGER`}
                descriptor={priceOnlyMode ? 'USD PRICE MODE' : 'KRW/USDT GAP'}
                version="V01"
                right={
                  <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.16em] text-ink-faint">
                    <span>LAST TICK</span>
                    <span className="text-ink">{formatTimestamp(timestamp)}</span>
                  </div>
                }
              />
              <div className="flex flex-wrap items-baseline gap-3 mt-1">
                <h1 className="text-[64px] md:text-[84px] leading-[0.9] font-extrabold tracking-tight">
                  {ticker}
                </h1>
                {priceOnlyMode ? (
                  <span className="px-2 py-0.5 border border-accent-cyan bg-accent-cyan/20 text-ink text-[10px] font-bold uppercase tracking-[0.2em]">
                    USD ONLY
                  </span>
                ) : (
                  <div className="flex items-baseline gap-2 text-[11px] text-ink-mute uppercase tracking-[0.16em]">
                    <span>BITHUMB ASK</span>
                    <span className="text-ink font-bold text-[14px]">
                      {orDash(bithumb.ask != null ? formatKRW(bithumb.ask) : null)}
                    </span>
                  </div>
                )}
              </div>
            </div>

            {/* KPI row */}
            {!priceOnlyMode && (
              <div className="grid grid-cols-2 md:grid-cols-4 border-b border-rule">
                <KPIBlock
                  label="BITHUMB ASK"
                  value={bithumb.ask != null ? formatKRW(bithumb.ask) : DASH}
                  size="md"
                />
                <KPIBlock
                  label="USDT/KRW"
                  value={
                    bithumb.usdt_krw_last != null
                      ? formatNumber(bithumb.usdt_krw_last, 2)
                      : DASH
                  }
                  size="md"
                />
                <KPIBlock
                  label="BEST FOREIGN GAP"
                  value={bestForeignGap ? formatNumber(bestForeignGap.gap, 2) : DASH}
                  sublabel={bestForeignGap?.name.toUpperCase() ?? DASH}
                  tone={
                    bestForeignGap && bestForeignGap.gap <= 9500
                      ? 'loss'
                      : bestForeignGap && bestForeignGap.gap <= 9800
                      ? 'default'
                      : 'default'
                  }
                  highlight={
                    !!bestForeignGap && bestForeignGap.gap <= 9500
                  }
                  size="md"
                />
                <KPIBlock
                  label="VENUES LIVE"
                  value={String(foreignVenueCount).padStart(2, '0')}
                  sublabel={`${Object.keys(exchanges).length} TOTAL`}
                  size="md"
                  suffix={<span>/ EXT</span>}
                />
              </div>
            )}

            {/* Withdraw-limit mini row (compact, chip-style) */}
            {!priceOnlyMode && (
              <div className="px-5 py-2 border-b border-rule flex flex-wrap items-center gap-x-6 gap-y-1 text-[10px] uppercase tracking-[0.14em]">
                <span className="text-ink-faint">WD LIMIT ///</span>
                <span>
                  <span className="text-ink-faint">1X </span>
                  <span className="text-ink font-semibold">
                    {bithumb.withdrawal_limit
                      ? formatCoin(bithumb.withdrawal_limit.onetime_coin)
                      : DASH}
                  </span>
                </span>
                <span>
                  <span className="text-ink-faint">24H </span>
                  <span className="text-ink font-semibold">
                    {bithumb.withdrawal_limit
                      ? formatCoin(bithumb.withdrawal_limit.daily_coin)
                      : DASH}
                  </span>
                </span>
                <span>
                  <span className="text-ink-faint">REM </span>
                  <span className="text-ink font-semibold">
                    {bithumb.withdrawal_limit
                      ? formatCoin(bithumb.withdrawal_limit.remaining_daily_coin)
                      : DASH}
                  </span>
                </span>
                <span className="text-ink-faint ml-auto">
                  {bithumb.withdrawal_limit?.remaining_daily_krw != null
                    ? `REM KRW ${formatKRW(bithumb.withdrawal_limit.remaining_daily_krw)}`
                    : ''}
                </span>
              </div>
            )}

            {/* Networks row */}
            {!priceOnlyMode && bithumb.networks && bithumb.networks.length > 0 && (
              <div className="px-5 py-2 border-b border-rule">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-[9px] uppercase tracking-[0.2em] text-ink-faint">
                    NETWORKS
                  </span>
                  <Divider variant="slash" />
                </div>
                <div className="flex flex-wrap gap-1">
                  {bithumb.networks.map((net) => {
                    const isOk = net.deposit && net.withdraw;
                    const depositOnly = net.deposit && !net.withdraw;
                    const withdrawOnly = !net.deposit && net.withdraw;
                    let cls = 'border-loss-red/60 bg-loss-red/15 text-loss-red';
                    let label = 'OFF';
                    if (isOk) {
                      cls = 'border-gain-green/60 bg-gain-green/15 text-gain-green';
                      label = 'OK';
                    } else if (depositOnly) {
                      cls = 'border-accent-orange/60 bg-accent-orange/15 text-accent-orange';
                      label = 'DEP';
                    } else if (withdrawOnly) {
                      cls = 'border-accent-cyan/60 bg-accent-cyan/20 text-ink';
                      label = 'WD';
                    }
                    return (
                      <button
                        key={net.network}
                        className={`px-1.5 py-0.5 border text-[9px] uppercase tracking-[0.14em] font-semibold hover:ring-1 hover:ring-ink transition-all ${cls}`}
                        title="Click to watch"
                        onClick={() => onNetworkClick?.('bithumb', ticker, net.network)}
                      >
                        {net.network} · {label}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {priceOnlyMode && (
              <div className="px-5 py-2 border-b border-rule">
                <p className="text-[10px] uppercase tracking-[0.16em] text-ink-mute">
                  BITHUMB UNSUPPORTED /// SHOWING FOREIGN USD ONLY · LAST{' '}
                  {formatTimestamp(timestamp)}
                </p>
              </div>
            )}

            <div className="flex-1 min-h-0 overflow-y-auto px-5 py-3">
              {foreignExchangesToRender.length > 0 && (
                <div className="mb-4">
                  <SectionHeader
                    title="FOREIGN VENUES"
                    descriptor={priceOnlyMode ? 'USD PRICE' : 'GAP DETAIL'}
                    version={`${foreignExchangesToRender.length} ACTIVE`}
                  />
                  <Divider />
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 2xl:grid-cols-3 mt-2">
                    {foreignExchangesToRender.map(([name, info]) => {
                      const isMuted =
                        mutedPairs?.has(`${name.toLowerCase()}:${ticker.toUpperCase()}`) ??
                        false;
                      return (
                        <ExchangeCard
                          key={name}
                          exchangeName={name}
                          data={info}
                          minSpotGap={minSpotGap}
                          minFuturesGap={minFuturesGap}
                          displayMode={priceOnlyMode ? 'usd_price' : 'gap'}
                          isMuted={isMuted}
                          onToggleMute={(exchange, nextMuted) =>
                            onTogglePriceMute?.(exchange, ticker, nextMuted)
                          }
                          onNetworkClick={(exchange, network) =>
                            onNetworkClick?.(exchange, ticker, network)
                          }
                          onEnterHedge={priceOnlyMode ? undefined : handleEnterHedge}
                        />
                      );
                    })}
                  </div>
                </div>
              )}

              {priceOnlyMode && foreignExchangesToRender.length === 0 && (
                <div className="border border-rule bg-oatmeal p-3 text-[11px] uppercase tracking-[0.16em] text-ink-mute">
                  NO FOREIGN SPOT/FUTURES PRICES AVAILABLE.
                </div>
              )}

              {!priceOnlyMode && domesticExchanges.length > 0 && (
                <div className="mb-3">
                  <SectionHeader
                    title="DOMESTIC VENUES"
                    descriptor="KRW GAP"
                    version={`${domesticExchanges.length} ACTIVE`}
                  />
                  <Divider />
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 2xl:grid-cols-3 mt-2">
                    {domesticExchanges.map(([name, info]) => {
                      const isMuted =
                        mutedPairs?.has(`${name.toLowerCase()}:${ticker.toUpperCase()}`) ??
                        false;
                      return (
                        <ExchangeCard
                          key={name}
                          exchangeName={name}
                          data={info}
                          minSpotGap={minSpotGap}
                          minFuturesGap={minFuturesGap}
                          displayMode="gap"
                          isMuted={isMuted}
                          onToggleMute={(exchange, nextMuted) =>
                            onTogglePriceMute?.(exchange, ticker, nextMuted)
                          }
                          onNetworkClick={(exchange, network) =>
                            onNetworkClick?.(exchange, ticker, network)
                          }
                        />
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          </div>
        </section>

        {!priceOnlyMode && (
          <aside className="min-h-0 xl:col-span-3 border-l border-rule bg-oatmeal/30">
            <div className="px-4 pt-3 pb-1 border-b border-rule">
              <SectionHeader
                title="WITHDRAW"
                descriptor="NET TRANSFER"
                version="V01"
              />
            </div>
            <WithdrawPanel
              ticker={ticker}
              data={data}
              compact
              className="h-full overflow-y-auto"
              onTargetExchangeChange={setExitSpotExchange}
            />
          </aside>
        )}

        {!priceOnlyMode && (
          <section className="min-h-0 xl:col-span-12 border-t border-rule bg-oatmeal/20">
            <div className="px-5 pt-3 pb-1 border-b border-rule">
              <SectionHeader
                title="HEDGE LEDGER"
                descriptor="SPOT/PERP PNL"
                version="V01"
              />
            </div>
            <HedgePnlPanel
              ticker={ticker}
              data={data}
              currentJob={activeHedgeJob}
              jobs={hedgeJobs}
              exitSpotExchange={exitSpotExchange}
              exitFuturesExchange={exitFuturesExchange}
              loading={hedgeLoading}
              refreshing={hedgeRefreshLoading}
              onRefresh={activeHedgeJob ? handleManualRefresh : undefined}
              actionMessage={hedgeMessage}
              horizontal
              className="mt-0 h-full"
            />
          </section>
        )}
      </div>
    </div>
  );
}
