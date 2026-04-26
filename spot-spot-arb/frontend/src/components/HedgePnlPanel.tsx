import { useMemo } from 'react';
import type { GapUpdate, HedgeJob } from '../types';
import { formatCoin, formatGap, formatNumber } from '../utils/format';

interface HedgePnlPanelProps {
  ticker: string;
  data: GapUpdate;
  currentJob: HedgeJob | null;
  jobs: HedgeJob[];
  exitSpotExchange: string;
  exitFuturesExchange: string;
  loading?: boolean;
  refreshing?: boolean;
  onRefresh?: () => void;
  actionMessage?: string | null;
  className?: string;
  horizontal?: boolean;
}

const DOMESTIC_EXCHANGES = new Set(['upbit', 'coinone']);
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

const isDomesticExchange = (exchange: string | undefined): boolean =>
  DOMESTIC_EXCHANGES.has(String(exchange || '').toLowerCase());

const formatKrw = (value: number | null | undefined): string => {
  if (value == null) return '-';
  return `₩${value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`;
};

const formatUsdt = (value: number | null | undefined, decimals = 4): string => {
  if (value == null) return '-';
  return `${value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals,
  })} USDT`;
};

const formatUsd = (value: number | null | undefined): string => {
  if (value == null) return '-';
  return `$${value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}`;
};

const formatUsdtWithKrw = (
  valueUsdt: number | null | undefined,
  usdtKrw: number | null | undefined,
): string => {
  if (valueUsdt == null) return '-';
  const usdtText = formatUsdt(valueUsdt, 6);
  if (usdtKrw == null || usdtKrw <= 0) return usdtText;
  return `${usdtText} (${formatKrw(valueUsdt * usdtKrw)})`;
};

const formatTimestamp = (timestamp: number | undefined): string => {
  if (!timestamp) return '-';
  const date = new Date(timestamp * 1000);
  return date.toLocaleString('ko-KR', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
};

const statusClass = (status: string): string => {
  const value = status.toLowerCase();
  if (value === 'closed') return 'text-emerald-300';
  if (value === 'hedged') return 'text-green-400';
  if (value === 'partial_hedged') return 'text-amber-400';
  if (value === 'rolled_back') return 'text-cyan-400';
  if (value === 'rollback_failed' || value === 'failed') return 'text-red-400';
  return 'text-gray-400';
};

const pnlClass = (value: number | null | undefined): string =>
  value != null && value < 0 ? 'text-red-400' : 'text-green-400';

const getPrimaryFillQty = (job: HedgeJob, market: 'spot' | 'futures'): number => {
  const aggregateQty =
    market === 'spot' ? job.entry_qty_spot ?? null : job.entry_qty_futures ?? null;
  if (aggregateQty != null && aggregateQty > 0) {
    return aggregateQty;
  }
  const legs = market === 'spot' ? job.legs?.spot : job.legs?.futures;
  if (!legs || legs.length === 0) return 0;
  return legs[0]?.filled_qty ?? 0;
};

const normalizeOrderError = (error: string): string => {
  const text = String(error || '').trim();
  if (!text) return '-';

  const lowered = text.toLowerCase();
  if (lowered.includes('"code":-2019') || lowered.includes('margin is insufficient')) {
    return '선물 주문 증거금 부족 (Binance -2019)';
  }

  return text.replace(/^(binance|bithumb)\s+/i, '');
};

const getRollbackReason = (job: HedgeJob): string | null => {
  const status = String(job.status || '').toLowerCase();
  if (status !== 'rollback_failed' && status !== 'rolled_back') {
    return null;
  }

  const futuresLeg = job.legs?.futures?.[0];
  const spotEntryLeg = job.legs?.spot?.[0];
  const spotRollbackLeg = job.legs?.spot?.[1];

  if (futuresLeg?.error) {
    const baseReason = `선물 주문 실패: ${normalizeOrderError(futuresLeg.error)}`;
    const spotBuyFilled = spotEntryLeg?.filled_qty ?? 0;
    const spotRollbackFilled = spotRollbackLeg?.filled_qty ?? 0;
    const dustQty = spotBuyFilled - spotRollbackFilled;
    if (status === 'rollback_failed' && dustQty > 0) {
      return `${baseReason} / 롤백 잔량 ${formatCoin(dustQty, 8)}`;
    }
    return baseReason;
  }

  if (spotRollbackLeg?.error) {
    return `스팟 롤백 주문 실패: ${normalizeOrderError(spotRollbackLeg.error)}`;
  }

  if (job.message) {
    return job.message;
  }

  return null;
};

interface HedgeLogEntry {
  id: string;
  timestamp: number;
  status: string;
  spotQty: number;
  futuresQty: number;
  spotAvgKrw: number | null;
  futuresAvgUsdt: number | null;
  entryUsdtKrw: number | null;
  closedAt?: number | null;
  finalizedAt?: number | null;
  finalPnlUsdt?: number | null;
  finalPnlKrw?: number | null;
  rollbackReason: string | null;
}

const toFiniteNumber = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return null;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null;

const buildBaseLogEntry = (job: HedgeJob): HedgeLogEntry => ({
  id: job.job_id,
  timestamp: job.created_at,
  status: job.status,
  spotQty: getPrimaryFillQty(job, 'spot'),
  futuresQty: getPrimaryFillQty(job, 'futures'),
  spotAvgKrw: job.entry_avg_spot_krw ?? null,
  futuresAvgUsdt: job.entry_avg_futures_usdt ?? null,
  entryUsdtKrw: job.entry_usdt_krw ?? null,
  closedAt: job.closed_at ?? null,
  finalizedAt: job.finalized_at ?? null,
  finalPnlUsdt: job.final_pnl_usdt ?? null,
  finalPnlKrw: job.final_pnl_krw ?? null,
  rollbackReason: getRollbackReason(job),
});

const buildJobLogEntries = (job: HedgeJob): HedgeLogEntry[] => {
  const baseStatus = String(job.status || '').toLowerCase();
  if (baseStatus === 'closed' || Boolean(job.finalized_at)) {
    return [buildBaseLogEntry(job)];
  }

  const attemptEvents = Array.isArray(job.events)
    ? job.events.filter((event): event is Record<string, unknown> => {
        if (!isRecord(event)) return false;
        const type = String(event.type || '').toLowerCase();
        return type === 'entry' || type === 'scale_in';
      })
    : [];

  if (attemptEvents.length === 0) {
    return [buildBaseLogEntry(job)];
  }

  return attemptEvents
    .map((event, index) => {
      const status = String(event.status || job.status || '').trim() || job.status;
      const loweredStatus = status.toLowerCase();
      const eventMessage = String(event.message || '').trim();

      return {
        id: `${job.job_id}:event:${index}`,
        timestamp: toFiniteNumber(event.timestamp) ?? job.created_at,
        status,
        spotQty: toFiniteNumber(event.filled_qty_spot) ?? 0,
        futuresQty: toFiniteNumber(event.filled_qty_futures) ?? 0,
        spotAvgKrw: toFiniteNumber(event.entry_avg_spot_krw),
        futuresAvgUsdt: toFiniteNumber(event.entry_avg_futures_usdt),
        entryUsdtKrw: job.entry_usdt_krw ?? null,
        rollbackReason:
          loweredStatus === 'rolled_back' ||
          loweredStatus === 'rollback_failed' ||
          loweredStatus === 'failed'
            ? eventMessage || job.message || null
            : null,
      };
    })
    .sort((left, right) => right.timestamp - left.timestamp);
};

const buildLogEntries = (jobs: HedgeJob[]): HedgeLogEntry[] =>
  jobs
    .flatMap((job) => buildJobLogEntries(job))
    .sort((left, right) => right.timestamp - left.timestamp);

const formatFixedSpotAvg = (job: HedgeJob): string => {
  if (job.close_avg_spot_price != null) {
    if (String(job.close_avg_spot_quote || '').toUpperCase() === 'KRW') {
      return formatKrw(job.close_avg_spot_price);
    }
    return formatUsdtWithKrw(
      job.close_avg_spot_price,
      job.close_usdt_krw ?? job.exit_usdt_krw ?? null,
    );
  }

  if (isDomesticExchange(job.exit_spot_exchange)) {
    return formatKrw(job.exit_avg_spot_krw ?? null);
  }
  return formatUsdtWithKrw(
    job.exit_avg_spot_usdt ?? null,
    job.exit_usdt_krw ?? null,
  );
};

const formatFixedFuturesAvg = (job: HedgeJob): string =>
  formatUsdtWithKrw(
    job.close_avg_futures_usdt ?? job.exit_avg_futures_usdt ?? null,
    job.close_usdt_krw ?? job.exit_usdt_krw ?? null,
  );

export function HedgePnlPanel({
  ticker,
  data,
  currentJob,
  jobs,
  exitSpotExchange,
  exitFuturesExchange,
  loading = false,
  refreshing = false,
  onRefresh,
  actionMessage = null,
  className = 'mt-3',
  horizontal = false,
}: HedgePnlPanelProps) {
  const hasAutoClosedExit = Boolean(
    currentJob?.status === 'closed' &&
      currentJob?.closed_at &&
      currentJob?.final_pnl_usdt != null,
  );
  const hasManualFinalizedExit = Boolean(
    !hasAutoClosedExit && currentJob?.finalized_at && currentJob?.final_pnl_usdt != null,
  );
  const hasFixedExit = hasAutoClosedExit || hasManualFinalizedExit;
  const fixedExitTimestamp = hasAutoClosedExit
    ? currentJob?.closed_at ?? undefined
    : currentJob?.finalized_at ?? undefined;
  const fixedUsdtKrw = currentJob?.close_usdt_krw ?? currentJob?.exit_usdt_krw ?? null;
  const resolvedExitSpotExchange = hasFixedExit
    ? currentJob?.exit_spot_exchange ?? exitSpotExchange
    : exitSpotExchange;
  const resolvedExitFuturesExchange =
    currentJob?.futures_exchange ??
    currentJob?.exit_futures_exchange ??
    exitFuturesExchange;

  const usdtKrwNow = data.bithumb.usdt_krw_last;
  const entryUsdtKrw = currentJob?.entry_usdt_krw ?? null;
  const entryQtySpot = currentJob?.entry_qty_spot ?? 0;
  const entryAvgSpotKrw = currentJob?.entry_avg_spot_krw ?? null;
  const entryAvgFuturesUsdt = currentJob?.entry_avg_futures_usdt ?? null;

  const spotExchangeInfo = resolvedExitSpotExchange
    ? data.exchanges[resolvedExitSpotExchange]
    : undefined;
  const futuresExchangeInfo = resolvedExitFuturesExchange
    ? data.exchanges[resolvedExitFuturesExchange]
    : undefined;

  const exitSpotBid = spotExchangeInfo?.spot.bid ?? null;
  const exitFuturesAsk = futuresExchangeInfo?.futures.ask ?? null;

  const exitSpotBidUsdt = useMemo(() => {
    if (exitSpotBid == null) return null;
    if (isDomesticExchange(resolvedExitSpotExchange)) {
      if (!usdtKrwNow || usdtKrwNow <= 0) return null;
      return exitSpotBid / usdtKrwNow;
    }
    return exitSpotBid;
  }, [exitSpotBid, resolvedExitSpotExchange, usdtKrwNow]);

  const entryGap = useMemo(() => {
    if (!entryAvgSpotKrw || !entryAvgFuturesUsdt || !entryUsdtKrw || entryUsdtKrw <= 0) {
      return null;
    }
    return (entryAvgSpotKrw / (entryAvgFuturesUsdt * entryUsdtKrw)) * 10_000;
  }, [entryAvgSpotKrw, entryAvgFuturesUsdt, entryUsdtKrw]);

  const liveExitGap = useMemo(() => {
    if (!exitSpotBidUsdt || !exitFuturesAsk || exitFuturesAsk <= 0) return null;
    return (exitSpotBidUsdt / exitFuturesAsk) * 10_000;
  }, [exitSpotBidUsdt, exitFuturesAsk]);

  const entrySpread = useMemo(() => {
    if (!entryAvgSpotKrw || !entryAvgFuturesUsdt || !entryUsdtKrw || entryUsdtKrw <= 0) {
      return null;
    }
    return entryAvgFuturesUsdt - (entryAvgSpotKrw / entryUsdtKrw);
  }, [entryAvgSpotKrw, entryAvgFuturesUsdt, entryUsdtKrw]);

  const liveExitSpread = useMemo(() => {
    if (!exitSpotBidUsdt || !exitFuturesAsk) return null;
    return exitSpotBidUsdt - exitFuturesAsk;
  }, [exitSpotBidUsdt, exitFuturesAsk]);

  const livePnlUsdt = useMemo(() => {
    if (entrySpread == null || liveExitSpread == null || !entryQtySpot) return null;
    return (entrySpread + liveExitSpread) * entryQtySpot;
  }, [entrySpread, liveExitSpread, entryQtySpot]);

  const livePnlKrw = useMemo(() => {
    if (livePnlUsdt == null || !usdtKrwNow || usdtKrwNow <= 0) return null;
    return livePnlUsdt * usdtKrwNow;
  }, [livePnlUsdt, usdtKrwNow]);

  const fixedExitGap = hasAutoClosedExit
    ? currentJob?.close_gap ?? currentJob?.exit_gap ?? null
    : currentJob?.exit_gap ?? null;
  const fixedExitSpread = hasAutoClosedExit
    ? currentJob?.close_spread_usdt ?? currentJob?.exit_spread_usdt ?? null
    : currentJob?.exit_spread_usdt ?? null;
  const pnlBasisQty = hasAutoClosedExit
    ? currentJob?.closed_qty ?? entryQtySpot
    : entryQtySpot;
  const exitGap = hasFixedExit ? fixedExitGap : liveExitGap;
  const exitSpread = hasFixedExit ? fixedExitSpread : liveExitSpread;
  const pnlUsdt = hasFixedExit ? currentJob?.final_pnl_usdt ?? null : livePnlUsdt;
  const pnlKrw = hasFixedExit ? currentJob?.final_pnl_krw ?? null : livePnlKrw;
  const historyJobs = useMemo(() => {
    return [...jobs]
      .filter((job) => {
        const status = String(job.status || '').toLowerCase();
        return (status === 'closed' || Boolean(job.finalized_at)) && job.final_pnl_usdt != null;
      })
      .sort((left, right) => {
        const leftTs = left.closed_at ?? left.finalized_at ?? left.created_at ?? 0;
        const rightTs = right.closed_at ?? right.finalized_at ?? right.created_at ?? 0;
        return rightTs - leftTs;
      });
  }, [jobs]);
  const logEntries = useMemo(() => buildLogEntries(historyJobs), [historyJobs]);

  const panelClass = `snatch-panel bg-cream border border-rule p-3 ${className}`;
  const entryCardClass = 'rounded-lg border border-gray-800 bg-gray-900/70 px-2 py-1.5';
  const exitCardClass = 'rounded-lg border border-cyan-900/50 bg-cyan-950/25 px-2 py-1.5';
  const entryLabelClass = 'text-[9px] uppercase tracking-wide text-gray-500';
  const exitLabelClass = 'text-[9px] uppercase tracking-wide text-cyan-300';
  const liveUpdatedAt = formatTimestamp(data.timestamp);

  if (!horizontal) {
    return (
      <div className={panelClass}>
        <h3 className="mb-2.5 text-[10px] font-bold uppercase tracking-[0.2em] text-ink">REALTIME PNL CALC ///</h3>
        <div className="grid grid-cols-1 gap-2">
          <div>
            <p className="mb-1 text-[11px] text-gray-500">Spot Exit 거래소 (자동 출금 연동)</p>
            <div className="rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-white">
              {EXCHANGE_DISPLAY[resolvedExitSpotExchange] ?? resolvedExitSpotExchange ?? '-'}
            </div>
          </div>
          <div>
            <p className="mb-1 text-[11px] text-gray-500">Futures Exit 거래소</p>
            <div className="rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-white">
              {EXCHANGE_DISPLAY[resolvedExitFuturesExchange] ?? resolvedExitFuturesExchange ?? '-'}
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`${panelClass} overflow-hidden`}>
      <h3 className="mb-1 text-[10px] font-bold uppercase tracking-[0.2em] text-ink">REALTIME PNL CALC ///</h3>

      <div className="grid h-[calc(100%-1.25rem)] grid-cols-1 gap-1.5 xl:grid-cols-12">
        <div className="rounded border border-gray-800 bg-gray-950/70 p-1.5 xl:col-span-3">
          <div className="space-y-1.5">
            <div>
              <p className="mb-0.5 text-[10px] text-gray-500">Spot Exit 거래소 (자동 출금 연동)</p>
              <div className="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-white">
                {EXCHANGE_DISPLAY[resolvedExitSpotExchange] ?? resolvedExitSpotExchange ?? '-'}
              </div>
            </div>
            <div>
              <p className="mb-0.5 text-[10px] text-gray-500">Futures Exit 거래소</p>
              <div className="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-white">
                {EXCHANGE_DISPLAY[resolvedExitFuturesExchange] ?? resolvedExitFuturesExchange ?? '-'}
              </div>
              <p className="mt-0.5 text-[9px] text-gray-500">
                futures close 감지는 hedge open에 사용한 선물 거래소로 자동 고정됩니다.
              </p>
            </div>
            <button
              type="button"
              onClick={onRefresh}
              disabled={loading || refreshing || !onRefresh}
              className="rounded border border-cyan-800/70 bg-cyan-950/30 px-2 py-1 text-[10px] text-cyan-200 transition-colors enabled:hover:bg-cyan-950/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {refreshing ? 'Refreshing...' : 'Refresh'}
            </button>
            {(loading || refreshing || actionMessage) && (
              <p className="text-[10px] text-cyan-300">
                {loading ? '현선갭 주문 실행 중...' : actionMessage}
              </p>
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-col rounded border border-gray-800 bg-gray-950/70 p-1.5 xl:col-span-6">
          {currentJob ? (
            <div className="min-h-0 space-y-1 overflow-y-auto pr-1 text-[10px] text-gray-300">
              <div className="rounded border border-cyan-900/50 bg-cyan-950/20 px-2 py-1 text-[9px] text-cyan-200">
                {hasAutoClosedExit
                  ? `EXIT values are fixed from detected close fills (${formatTimestamp(
                      fixedExitTimestamp,
                    )})`
                  : hasManualFinalizedExit
                    ? `EXIT values are fixed from close averages (${formatTimestamp(
                        fixedExitTimestamp,
                      )})`
                  : `EXIT values are live (updated ${liveUpdatedAt})`}
              </div>

              <div className="grid grid-cols-1 gap-1 sm:grid-cols-4">
                <div className={entryCardClass}>
                  <p className={entryLabelClass}>Position (Entry)</p>
                  <p className={`mt-0.5 text-[12px] font-semibold ${statusClass(currentJob.status)}`}>
                    {hasAutoClosedExit ? 'closed' : currentJob.status}
                  </p>
                </div>
                <div className={entryCardClass}>
                  <p className={entryLabelClass}>Entry Time (Fixed)</p>
                  <p className="mt-0.5 text-[12px] font-medium text-gray-100">
                    {formatTimestamp(currentJob.created_at)}
                  </p>
                </div>
                <div className={entryCardClass}>
                  <p className={entryLabelClass}>Entry Qty (Fixed)</p>
                  <p className="mt-0.5 text-[12px] font-medium text-gray-100">
                    {formatCoin(entryQtySpot, 8)}
                  </p>
                </div>
                <div className={entryCardClass}>
                  <p className={entryLabelClass}>Entry Nominal (Fixed)</p>
                  <p className="mt-0.5 text-[12px] font-medium text-gray-100">
                    {formatUsd(currentJob.nominal_usd ?? null)}
                  </p>
                </div>
              </div>

              {hasFixedExit && (
                <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                  <div className={exitCardClass}>
                    <p className={exitLabelClass}>Exit Spot Avg (Fixed)</p>
                    <p className="mt-0.5 text-[12px] font-medium text-cyan-100">
                      {formatFixedSpotAvg(currentJob)}
                    </p>
                  </div>
                  <div className={exitCardClass}>
                    <p className={exitLabelClass}>Exit Futures Avg (Fixed)</p>
                    <p className="mt-0.5 text-[12px] font-medium text-cyan-100">
                      {formatFixedFuturesAvg(currentJob)}
                    </p>
                  </div>
                </div>
              )}

              <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                <div className={entryCardClass}>
                  <p className={entryLabelClass}>Entry Gap (Fixed)</p>
                  <p className="mt-0.5 text-[12px] font-medium text-gray-100">{formatGap(entryGap)}</p>
                </div>
                <div className={exitCardClass}>
                  <p className={exitLabelClass}>{hasFixedExit ? 'Exit Gap (Fixed)' : 'Exit Gap (Live)'}</p>
                  <p className="mt-0.5 text-[12px] font-medium text-cyan-100">{formatGap(exitGap)}</p>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                <div className={entryCardClass}>
                  <p className={entryLabelClass}>Entry Spread (Fixed)</p>
                  <p className="mt-0.5 text-[12px] font-medium text-gray-100">{formatUsdt(entrySpread, 6)}</p>
                </div>
                <div className={exitCardClass}>
                  <p className={exitLabelClass}>
                    {hasFixedExit ? 'Exit Spread (Fixed)' : 'Exit Spread (Live)'}
                  </p>
                  <p className="mt-0.5 text-[12px] font-medium text-cyan-100">
                    {formatUsdt(exitSpread, 6)}
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                <div className={exitCardClass}>
                  <p className={exitLabelClass}>
                    {hasFixedExit ? 'Final PnL (Fixed)' : 'Realtime PnL (Live)'}
                  </p>
                  <p
                    className={`mt-0.5 text-[12px] font-semibold ${pnlClass(pnlUsdt)}`}
                  >
                    {formatUsdt(pnlUsdt, 6)}
                  </p>
                </div>
                <div className={exitCardClass}>
                  <p className={exitLabelClass}>
                    {hasFixedExit ? 'Final PnL(KRW) (Fixed)' : 'Realtime PnL(KRW) (Live)'}
                  </p>
                  <p
                    className={`mt-0.5 text-[12px] font-semibold ${pnlClass(pnlKrw)}`}
                  >
                    {formatKrw(pnlKrw)}
                  </p>
                </div>
              </div>

              <p className="text-[9px] text-gray-500">
                Basis: (Entry Spread + Exit Spread) * Bithumb Filled Qty / Entry=
                {formatNumber(entryUsdtKrw, 2)} KRW
                {hasFixedExit
                  ? ` / Exit=${formatNumber(fixedUsdtKrw, 2)} KRW / Qty=${formatCoin(pnlBasisQty, 8)}`
                  : ''}
              </p>
            </div>
          ) : (
            <>
            <div className="hidden"><p className="text-[11px] text-gray-500">
              {ticker} 기준 활성 진입 로그가 없습니다. 거래소 카드의 현선갭 버튼으로 진입하면 계산이 시작됩니다.
            </p></div>
            <div className="flex min-h-0 flex-1 items-center justify-center rounded border border-dashed border-gray-800 bg-gray-900/40 px-3 text-center">
              <p className="text-[11px] text-gray-500">
                현재 열린 포지션이 없습니다. 새 open이 체결되면 이 영역에 live PnL을 표시합니다.
              </p>
            </div>
            </>
          )}
        </div>

        <div className="flex min-h-0 flex-col rounded border border-gray-800 bg-gray-950/70 p-1.5 xl:col-span-3">
          <p className="mb-1 text-[10px] font-semibold text-gray-300">체결 로그</p>
          {logEntries.length === 0 ? (
            <p className="text-[10px] text-gray-500">로그 없음</p>
          ) : (
            <div className="min-h-0 space-y-1 overflow-y-auto pr-1">
              {logEntries.slice(0, 5).map((entry) => {
                const job = {
                  job_id: entry.id,
                  created_at: entry.timestamp,
                  updated_at: entry.timestamp,
                  ticker,
                  status: entry.status,
                  futures_exchange: currentJob?.futures_exchange ?? '',
                  nominal_usd: 0,
                  leverage: 0,
                  requested_qty: 0,
                  entry_usdt_krw: entry.entryUsdtKrw,
                  entry_qty_spot: entry.spotQty,
                  entry_qty_futures: entry.futuresQty,
                  entry_avg_spot_krw: entry.spotAvgKrw,
                  entry_avg_futures_usdt: entry.futuresAvgUsdt,
                  finalized_at: entry.finalizedAt,
                  closed_at: entry.closedAt,
                  final_pnl_usdt: entry.finalPnlUsdt,
                  final_pnl_krw: entry.finalPnlKrw,
                } as HedgeJob;
                const rollbackReason = entry.rollbackReason;
                return (
                  <div key={entry.id} className="rounded border border-gray-800 bg-gray-900/60 p-1">
                    <p className="text-[10px] text-gray-300">
                      {formatTimestamp(entry.timestamp)} /{' '}
                      <span className={statusClass(entry.status)}>{entry.status}</span>
                    </p>
                    <p className="text-[10px] text-gray-400">
                      Spot 체결: {formatCoin(getPrimaryFillQty(job, 'spot'), 8)} / Futures 체결:{' '}
                      {formatCoin(getPrimaryFillQty(job, 'futures'), 8)}
                    </p>
                    <p className="text-[9px] text-gray-500">
                      Spot 평단: {formatKrw(job.entry_avg_spot_krw ?? null)} / Futures 평단:{' '}
                      {formatUsdtWithKrw(
                        job.entry_avg_futures_usdt ?? null,
                        job.entry_usdt_krw ?? usdtKrwNow,
                      )}
                    </p>
                    {job.status === 'closed' || job.finalized_at ? (
                      <p className={`text-[9px] ${pnlClass(job.final_pnl_usdt ?? job.final_pnl_krw ?? null)}`}>
                        Close {formatTimestamp(job.closed_at ?? job.finalized_at ?? undefined)} / Final PnL:{' '}
                        {formatUsdt(job.final_pnl_usdt ?? null, 6)} / {formatKrw(job.final_pnl_krw ?? null)}
                      </p>
                    ) : null}
                    {rollbackReason ? (
                      <p className="text-[9px] text-rose-300">롤백 원인: {rollbackReason}</p>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
