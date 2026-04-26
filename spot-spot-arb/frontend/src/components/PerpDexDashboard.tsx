import { useEffect, useMemo, useState } from 'react';
import { SectionHeader } from './snatch/SectionHeader';
import { KPIBlock } from './snatch/KPIBlock';
import { Divider } from './snatch/Divider';
import { VenueBadge } from './snatch/VenueBadge';

// VPS-hosted mpdex dashboard API. Override via VITE_MPDEX_API_URL in frontend/.env
const MPDEX_API =
  (import.meta.env.VITE_MPDEX_API_URL as string | undefined) ??
  'http://<VPS_IP>:38743';

type ExchangeRow = {
  name: string;
  balance: number;
  disabled: boolean;
  disabled_reason: string | null;
  status: 'live' | 'idle' | 'off';
  hip3: boolean;
};
type ExchangesResp = { exchanges: ExchangeRow[]; total: number; stamp: string | null };

type PnlRow = {
  name: string;
  start_balance: number;
  end_balance: number;
  pnl_usd: number;
  pnl_pct: number;
  trades: number;
  deposits: number;
  source: 'equity' | 'db';
};
type PnlResp = {
  rows: PnlRow[];
  total_pnl: number;
  winners: number;
  losers: number;
  date: string;
};

type FundingRow = {
  symbol: string;
  max_ex: string;
  min_ex: string;
  max_rate_8h: number;
  min_rate_8h: number;
  spread_8h: number;
  spread_pct: number;
};
type FundingResp = { spreads: FundingRow[]; latest_ts: string | null };

type Position = {
  id: number;
  exchange: string;
  direction: string;
  coin1: string;
  coin2: string;
  entry_time: string;
  age_min: number | null;
  entries: number;
  pnl_percent: number;
  pnl_usd: number;
};
type PositionsResp = { open: Position[]; count: number };

type VolumeRow = {
  exchange: string;
  trades: number;
  entries: number;
  pnl_usd: number;
};
type VolumeResp = {
  rows: VolumeRow[];
  total_trades: number;
  total_entries: number;
  date: string;
};

async function fetchJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${MPDEX_API}${path}`, { cache: 'no-store' });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

function fmtUsd(v: number | null | undefined, signed = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const sign = signed && v > 0 ? '+' : '';
  return `${sign}$${v.toFixed(2)}`;
}

function fmtPct(v: number | null | undefined, signed = true): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const sign = signed && v > 0 ? '+' : '';
  return `${sign}${v.toFixed(2)}%`;
}

function toneFor(v: number): 'gain' | 'loss' | 'muted' {
  if (v > 0.5) return 'gain';
  if (v < -0.5) return 'loss';
  return 'muted';
}

export function PerpDexDashboard() {
  const [exchanges, setExchanges] = useState<ExchangesResp | null>(null);
  const [pnl, setPnl] = useState<PnlResp | null>(null);
  const [funding, setFunding] = useState<FundingResp | null>(null);
  const [positions, setPositions] = useState<PositionsResp | null>(null);
  const [volume, setVolume] = useState<VolumeResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [lastFetch, setLastFetch] = useState<number>(0);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      const [e, p, f, pos, v] = await Promise.all([
        fetchJson<ExchangesResp>('/exchanges'),
        fetchJson<PnlResp>('/today_pnl'),
        fetchJson<FundingResp>('/funding'),
        fetchJson<PositionsResp>('/positions'),
        fetchJson<VolumeResp>('/volume_farming'),
      ]);
      if (!alive) return;
      if (!e && !p && !f) {
        setErr(`mpdex api unreachable @ ${MPDEX_API}`);
      } else {
        setErr(null);
      }
      if (e) setExchanges(e);
      if (p) setPnl(p);
      if (f) setFunding(f);
      if (pos) setPositions(pos);
      if (v) setVolume(v);
      setLastFetch(Date.now());
    };
    pull();
    const t = window.setInterval(pull, 30_000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, []);

  const venueCount = exchanges?.exchanges.length ?? 0;
  const liveCount = useMemo(
    () => exchanges?.exchanges.filter((r) => r.status === 'live').length ?? 0,
    [exchanges],
  );
  const topLosers = useMemo(
    () => (pnl ? [...pnl.rows].filter((r) => r.pnl_usd < 0).slice(0, 3) : []),
    [pnl],
  );
  const topWinners = useMemo(
    () => (pnl ? [...pnl.rows].sort((a, b) => b.pnl_usd - a.pnl_usd).slice(0, 3) : []),
    [pnl],
  );

  return (
    <div className="h-full overflow-auto bg-cream text-ink">
      <div className="px-5 py-4 space-y-5">
        {/* Header */}
        <SectionHeader
          title="PERP DEX LIVE"
          descriptor={`${liveCount} LIVE /// ${venueCount} TRACKED`}
          version="V01"
          right={
            <div className="flex items-center gap-3 text-[10px] uppercase tracking-[0.16em] text-ink-mute">
              <span>
                API · <span className="text-ink">{MPDEX_API.replace(/^https?:\/\//, '')}</span>
              </span>
              {err ? (
                <span className="bg-loss-red text-cream px-2 py-0.5 font-bold">
                  OFFLINE
                </span>
              ) : (
                <span className="bg-accent-yellow text-ink px-2 py-0.5 font-bold">
                  {lastFetch ? `SYNC ${Math.round((Date.now() - lastFetch) / 1000)}s AGO` : 'LOADING'}
                </span>
              )}
            </div>
          }
        />

        {/* Venue badge strip */}
        <div className="border border-rule bg-oatmeal/40">
          <div className="px-4 py-3 flex flex-wrap gap-1.5">
            {(exchanges?.exchanges ?? []).map((row) => (
              <VenueBadge
                key={row.name}
                name={row.name}
                status={row.status}
                size="sm"
              />
            ))}
            {exchanges?.exchanges.length === 0 && (
              <span className="text-[11px] text-ink-faint uppercase tracking-[0.16em]">
                no exchanges reported
              </span>
            )}
          </div>
        </div>

        {/* KPI Row */}
        <div className="grid grid-cols-2 md:grid-cols-4 border border-rule bg-cream">
          <KPIBlock
            label="TODAY PnL (UTC)"
            value={pnl ? fmtUsd(pnl.total_pnl, true) : '—'}
            sublabel={pnl ? `${pnl.date} · ${pnl.rows.length} venues` : ''}
            tone={toneFor(pnl?.total_pnl ?? 0)}
            size="lg"
          />
          <KPIBlock
            label="WINNERS / LOSERS"
            value={pnl ? `${pnl.winners} / ${pnl.losers}` : '—'}
            sublabel={pnl ? `net ${pnl.winners - pnl.losers}` : ''}
            tone={pnl && pnl.winners > pnl.losers ? 'gain' : 'loss'}
            size="lg"
          />
          <KPIBlock
            label="TOTAL EQUITY"
            value={exchanges ? fmtUsd(exchanges.total) : '—'}
            sublabel={exchanges?.stamp ? exchanges.stamp.slice(11, 16) + ' UTC' : ''}
            size="lg"
          />
          <KPIBlock
            label="VOLUME FARMING"
            value={volume ? `${volume.total_entries}` : '—'}
            sublabel={volume ? `entries today · ${volume.total_trades} trades` : ''}
            tone="accent"
            size="lg"
            suffix={<span>legs</span>}
          />
        </div>

        {/* Second KPI row: open positions + funding */}
        <div className="grid grid-cols-2 md:grid-cols-4 border border-rule bg-cream">
          <KPIBlock
            label="OPEN POSITIONS"
            value={positions ? `${positions.count}` : '—'}
            sublabel="live trades"
            tone={(positions?.count ?? 0) > 0 ? 'accent' : 'muted'}
            size="md"
          />
          <KPIBlock
            label="FUNDING SPREADS"
            value={funding ? funding.spreads.length : '—'}
            sublabel={funding?.latest_ts ? funding.latest_ts.slice(11, 16) + ' UTC' : ''}
            size="md"
          />
          <KPIBlock
            label="TOP LOSER"
            value={topLosers[0] ? topLosers[0].name : '—'}
            sublabel={topLosers[0] ? fmtUsd(topLosers[0].pnl_usd, true) : ''}
            tone={topLosers[0] ? 'loss' : 'muted'}
            size="md"
          />
          <KPIBlock
            label="TOP WINNER"
            value={topWinners[0] && topWinners[0].pnl_usd > 0 ? topWinners[0].name : '—'}
            sublabel={
              topWinners[0] && topWinners[0].pnl_usd > 0 ? fmtUsd(topWinners[0].pnl_usd, true) : ''
            }
            tone={topWinners[0] && topWinners[0].pnl_usd > 0 ? 'gain' : 'muted'}
            size="md"
          />
        </div>

        {/* Exchange detail table */}
        <div className="border border-rule">
          <div className="flex items-center justify-between px-4 py-2 border-b border-rule bg-oatmeal/40">
            <span className="text-[11px] font-bold uppercase tracking-[0.18em]">
              PER-EXCHANGE /// TODAY
            </span>
            <Divider variant="slash" />
            <span className="text-[10px] text-ink-mute uppercase tracking-[0.14em]">
              UTC {pnl?.date ?? ''}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-[9px] uppercase tracking-[0.16em] text-ink-faint border-b border-rule">
                  <th className="text-left px-3 py-2">VENUE</th>
                  <th className="text-left px-3 py-2">STATUS</th>
                  <th className="text-right px-3 py-2">BALANCE</th>
                  <th className="text-right px-3 py-2">TODAY PnL</th>
                  <th className="text-right px-3 py-2">PnL %</th>
                  <th className="text-right px-3 py-2">TRADES</th>
                  <th className="text-right px-3 py-2">DEPOSITS</th>
                  <th className="text-left px-3 py-2">SRC</th>
                </tr>
              </thead>
              <tbody>
                {(() => {
                  const map = new Map<string, ExchangeRow>();
                  (exchanges?.exchanges ?? []).forEach((r) => map.set(r.name, r));
                  const rows = (pnl?.rows ?? []).slice().sort(
                    (a, b) => a.pnl_usd - b.pnl_usd,
                  );
                  // also add exchanges with no pnl row
                  const seen = new Set(rows.map((r) => r.name));
                  (exchanges?.exchanges ?? []).forEach((e) => {
                    if (!seen.has(e.name)) {
                      rows.push({
                        name: e.name,
                        start_balance: e.balance,
                        end_balance: e.balance,
                        pnl_usd: 0,
                        pnl_pct: 0,
                        trades: 0,
                        deposits: 0,
                        source: 'equity',
                      });
                    }
                  });
                  return rows.map((row) => {
                    const x = map.get(row.name);
                    const statusColor =
                      row.pnl_usd > 0.5
                        ? 'text-gain-green'
                        : row.pnl_usd < -0.5
                          ? 'text-loss-red'
                          : 'text-ink-mute';
                    const bg =
                      row.pnl_usd < -5 ? 'bg-loss-red/10' : row.pnl_usd > 5 ? 'bg-accent-yellow/30' : '';
                    return (
                      <tr
                        key={row.name}
                        className={`border-b border-rule/50 ${bg}`}
                      >
                        <td className="px-3 py-1.5 font-semibold">
                          <div className="flex items-center gap-2">
                            {row.name}
                            {x?.hip3 && (
                              <span className="text-[8px] bg-ink text-cream px-1 py-0.5 uppercase tracking-wider">
                                HIP3
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-1.5">
                          {x?.disabled ? (
                            <span className="text-loss-red font-bold">OFF</span>
                          ) : x?.status === 'idle' ? (
                            <span className="text-ink-faint">IDLE</span>
                          ) : (
                            <span className="text-gain-green">LIVE</span>
                          )}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums">
                          {fmtUsd(row.end_balance)}
                        </td>
                        <td
                          className={`px-3 py-1.5 text-right tabular-nums font-bold ${statusColor}`}
                        >
                          {fmtUsd(row.pnl_usd, true)}
                        </td>
                        <td
                          className={`px-3 py-1.5 text-right tabular-nums ${statusColor}`}
                        >
                          {fmtPct(row.pnl_pct)}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums">
                          {row.trades}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-ink-faint">
                          {row.deposits !== 0 ? fmtUsd(row.deposits, true) : '—'}
                        </td>
                        <td className="px-3 py-1.5 text-[9px] text-ink-faint uppercase tracking-wider">
                          {row.source}
                        </td>
                      </tr>
                    );
                  });
                })()}
              </tbody>
            </table>
          </div>
        </div>

        {/* Bottom: open positions + funding side-by-side */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Open positions */}
          <div className="border border-rule">
            <div className="px-4 py-2 border-b border-rule bg-oatmeal/40 flex items-center justify-between">
              <span className="text-[11px] font-bold uppercase tracking-[0.18em]">
                OPEN POSITIONS /// {positions?.count ?? 0}
              </span>
              <Divider variant="slash" />
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-[9px] uppercase tracking-[0.16em] text-ink-faint border-b border-rule">
                    <th className="text-left px-3 py-2">VENUE</th>
                    <th className="text-left px-3 py-2">DIR</th>
                    <th className="text-left px-3 py-2">PAIR</th>
                    <th className="text-right px-3 py-2">DCA</th>
                    <th className="text-right px-3 py-2">AGE</th>
                    <th className="text-right px-3 py-2">uPnL</th>
                  </tr>
                </thead>
                <tbody>
                  {(positions?.open ?? []).map((p) => (
                    <tr key={p.id} className="border-b border-rule/50">
                      <td className="px-3 py-1.5 font-semibold">{p.exchange}</td>
                      <td className="px-3 py-1.5 text-[10px] uppercase tracking-wider">
                        {p.direction}
                      </td>
                      <td className="px-3 py-1.5 text-ink-mute">
                        {p.coin1}/{p.coin2}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{p.entries}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-ink-faint">
                        {p.age_min !== null ? `${p.age_min}m` : '—'}
                      </td>
                      <td
                        className={`px-3 py-1.5 text-right tabular-nums font-bold ${
                          p.pnl_usd > 0
                            ? 'text-gain-green'
                            : p.pnl_usd < 0
                              ? 'text-loss-red'
                              : 'text-ink-mute'
                        }`}
                      >
                        {fmtUsd(p.pnl_usd, true)}
                      </td>
                    </tr>
                  ))}
                  {(!positions || positions.open.length === 0) && (
                    <tr>
                      <td
                        colSpan={6}
                        className="px-3 py-4 text-center text-[11px] uppercase tracking-[0.14em] text-ink-faint"
                      >
                        NO OPEN POSITIONS
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* Funding spreads */}
          <div className="border border-rule">
            <div className="px-4 py-2 border-b border-rule bg-oatmeal/40 flex items-center justify-between">
              <span className="text-[11px] font-bold uppercase tracking-[0.18em]">
                FUNDING SPREAD /// TOP
              </span>
              <span className="text-[10px] text-ink-mute">
                {funding?.latest_ts ? funding.latest_ts.slice(11, 16) + ' UTC' : ''}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-[9px] uppercase tracking-[0.16em] text-ink-faint border-b border-rule">
                    <th className="text-left px-3 py-2">COIN</th>
                    <th className="text-left px-3 py-2">LONG (MIN)</th>
                    <th className="text-left px-3 py-2">SHORT (MAX)</th>
                    <th className="text-right px-3 py-2">SPREAD 8H</th>
                    <th className="text-right px-3 py-2">ΔAPR</th>
                  </tr>
                </thead>
                <tbody>
                  {(funding?.spreads ?? []).map((s) => {
                    const apr = s.spread_8h * 3 * 365;
                    return (
                      <tr
                        key={`${s.symbol}-${s.max_ex}-${s.min_ex}`}
                        className="border-b border-rule/50"
                      >
                        <td className="px-3 py-1.5 font-bold">{s.symbol}</td>
                        <td className="px-3 py-1.5 text-[10px] text-ink-mute uppercase tracking-wider">
                          {s.min_ex}
                        </td>
                        <td className="px-3 py-1.5 text-[10px] text-ink-mute uppercase tracking-wider">
                          {s.max_ex}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums">
                          {fmtPct(s.spread_pct * 100, false)}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-gain-green font-bold">
                          {fmtPct(apr * 100, false)}
                        </td>
                      </tr>
                    );
                  })}
                  {(!funding || funding.spreads.length === 0) && (
                    <tr>
                      <td
                        colSpan={5}
                        className="px-3 py-4 text-center text-[11px] uppercase tracking-[0.14em] text-ink-faint"
                      >
                        NO FUNDING DATA
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* Volume farming */}
        <div className="border border-rule">
          <div className="px-4 py-2 border-b border-rule bg-oatmeal/40 flex items-center justify-between">
            <span className="text-[11px] font-bold uppercase tracking-[0.18em]">
              VOLUME FARMING /// TODAY
            </span>
            <span className="text-[10px] text-ink-mute">
              {volume ? `${volume.total_entries} entries · ${volume.total_trades} trades` : ''}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-[9px] uppercase tracking-[0.16em] text-ink-faint border-b border-rule">
                  <th className="text-left px-3 py-2">VENUE</th>
                  <th className="text-right px-3 py-2">TRADES</th>
                  <th className="text-right px-3 py-2">ENTRIES</th>
                  <th className="text-right px-3 py-2">PnL</th>
                </tr>
              </thead>
              <tbody>
                {(volume?.rows ?? []).map((r) => (
                  <tr key={r.exchange} className="border-b border-rule/50">
                    <td className="px-3 py-1.5 font-semibold">{r.exchange}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{r.trades}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{r.entries}</td>
                    <td
                      className={`px-3 py-1.5 text-right tabular-nums ${
                        r.pnl_usd > 0 ? 'text-gain-green' : r.pnl_usd < 0 ? 'text-loss-red' : 'text-ink-mute'
                      }`}
                    >
                      {fmtUsd(r.pnl_usd, true)}
                    </td>
                  </tr>
                ))}
                {(!volume || volume.rows.length === 0) && (
                  <tr>
                    <td
                      colSpan={4}
                      className="px-3 py-4 text-center text-[11px] uppercase tracking-[0.14em] text-ink-faint"
                    >
                      NO TRADE ACTIVITY TODAY
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {err && (
          <div className="border border-loss-red bg-loss-red/10 px-4 py-3 text-[11px] uppercase tracking-[0.16em] text-loss-red font-semibold">
            {err} /// check uvicorn perp_dashboard_api on <VPS_IP>:38743
          </div>
        )}
      </div>
    </div>
  );
}
