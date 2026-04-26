import type { ExchangeInfo } from '../types';
import { NetworkInfo } from './NetworkInfo';
import {
  formatGap,
  formatUSD,
  getGapColorClass,
  getGapHighlight,
} from '../utils/format';
import { venueColor } from './snatch/VenueBadge';

type DisplayMode = 'gap' | 'usd_price';

interface ExchangeCardProps {
  exchangeName: string;
  data: ExchangeInfo;
  minSpotGap: number | null;
  minFuturesGap: number | null;
  displayMode?: DisplayMode;
  isMuted?: boolean;
  onToggleMute?: (exchange: string, nextMuted: boolean) => void;
  onNetworkClick?: (exchange: string, network: string) => void;
  onEnterHedge?: (exchange: string) => void;
}

function GapBadge({
  label,
  gap,
  isMin,
  supported,
}: {
  label: string;
  gap: number | null | undefined;
  isMin: boolean;
  supported: boolean;
}) {
  const highlightClass = getGapHighlight(gap, isMin);
  return (
    <div className={`px-2 py-1.5 ${highlightClass}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="whitespace-nowrap text-[9px] uppercase tracking-[0.16em] text-ink-mute">
          {label}
        </span>
        {!supported ? (
          <span className="whitespace-nowrap text-[9px] uppercase tracking-[0.14em] text-ink-faint">
            N/A
          </span>
        ) : (
          <span
            className={`whitespace-nowrap text-[14px] font-extrabold tracking-tight ${
              isMin ? 'text-ink' : getGapColorClass(gap)
            }`}
          >
            {formatGap(gap)}
          </span>
        )}
      </div>
    </div>
  );
}

function PriceBadge({
  label,
  bid,
  ask,
  supported,
}: {
  label: string;
  bid: number | null | undefined;
  ask: number | null | undefined;
  supported: boolean;
}) {
  const value =
    bid != null && ask != null ? (bid + ask) / 2 : bid ?? ask ?? null;
  return (
    <div className="px-2 py-1.5 bg-oatmeal border border-rule">
      <div className="flex items-center justify-between gap-2">
        <span className="whitespace-nowrap text-[9px] uppercase tracking-[0.16em] text-ink-mute">
          {label}
        </span>
        {!supported ? (
          <span className="whitespace-nowrap text-[9px] uppercase tracking-[0.14em] text-ink-faint">
            N/A
          </span>
        ) : (
          <span className="whitespace-nowrap text-[13px] font-extrabold text-ink">
            {formatUSD(value)}
          </span>
        )}
      </div>
    </div>
  );
}

function SupportChip({
  feature,
  supported,
}: {
  feature: 'MGN' | 'LOAN';
  supported: boolean | null | undefined;
}) {
  let cls = 'border-rule bg-oatmeal text-ink-faint';
  let label = `${feature} ?`;
  if (supported === true) {
    cls = 'border-gain-green/60 bg-gain-green/15 text-gain-green';
    label = feature;
  } else if (supported === false) {
    cls = 'border-rule bg-oatmeal text-ink-faint';
    label = `${feature}·X`;
  }
  return (
    <span
      className={`whitespace-nowrap border px-1 py-0 text-[8px] font-semibold uppercase tracking-[0.14em] ${cls}`}
    >
      {label}
    </span>
  );
}

export function ExchangeCard({
  exchangeName,
  data,
  minSpotGap,
  minFuturesGap,
  displayMode = 'gap',
  isMuted = false,
  onToggleMute,
  onNetworkClick,
  onEnterHedge,
}: ExchangeCardProps) {
  const displayName = exchangeName.toUpperCase();
  const isUnsupported = !data.spot.supported && !data.futures.supported;
  const isPriceMode = displayMode === 'usd_price';
  const dotColor = venueColor(exchangeName);

  if (isUnsupported) {
    return (
      <div className="border border-rule bg-oatmeal/40 p-2">
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 min-w-0">
            <span
              className="w-2 h-2 rounded-full shrink-0"
              style={{ backgroundColor: dotColor }}
            />
            <h3 className="max-w-[120px] truncate text-[11px] font-bold uppercase tracking-[0.14em] text-ink">
              {displayName}
            </h3>
          </div>
          <span className="border border-rule bg-cream px-1.5 py-0 text-[9px] uppercase tracking-[0.14em] text-ink-faint">
            UNSUPPORTED
          </span>
        </div>
        <div className="flex h-[72px] items-center justify-center border border-dashed border-rule bg-cream">
          <p className="text-[9px] uppercase tracking-[0.16em] text-ink-faint">—</p>
        </div>
      </div>
    );
  }

  const isMinSpot =
    data.spot.gap != null && minSpotGap != null && data.spot.gap === minSpotGap;
  const isMinFutures =
    data.futures.gap != null &&
    minFuturesGap != null &&
    data.futures.gap === minFuturesGap;

  return (
    <div
      className={`border bg-cream p-2 transition-colors ${
        isMuted ? 'border-accent-orange bg-accent-orange/10' : 'border-rule hover:border-ink'
      }`}
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span
            className="w-2 h-2 rounded-full shrink-0"
            style={{ backgroundColor: dotColor }}
          />
          <h3 className="max-w-[120px] truncate text-[11px] font-bold uppercase tracking-[0.14em] text-ink">
            {displayName}
          </h3>
        </div>
        <div className="flex items-center gap-1 flex-wrap justify-end">
          <button
            type="button"
            onClick={() => onToggleMute?.(exchangeName, !isMuted)}
            className={`whitespace-nowrap border px-1 py-0 text-[9px] uppercase tracking-[0.14em] transition-colors ${
              isMuted
                ? 'border-accent-orange bg-accent-orange/25 text-ink hover:bg-accent-orange/40'
                : 'border-rule bg-oatmeal text-ink-mute hover:bg-ink hover:text-cream'
            }`}
            title={isMuted ? 'Unmute' : 'Mute alarm'}
          >
            {isMuted ? 'MUTED' : 'MUTE'}
          </button>
          {data.spot.supported && (
            <span className="whitespace-nowrap border border-accent-cyan/60 bg-accent-cyan/20 px-1 py-0 text-[9px] uppercase tracking-[0.14em] text-ink font-semibold">
              SPOT
            </span>
          )}
          {data.futures.supported && (
            <span className="whitespace-nowrap border border-accent-purple/60 bg-accent-purple/20 px-1 py-0 text-[9px] uppercase tracking-[0.14em] text-ink font-semibold">
              PERP
            </span>
          )}
          <SupportChip feature="MGN" supported={data.margin?.supported} />
          <SupportChip feature="LOAN" supported={data.loan?.supported} />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-1">
        {isPriceMode ? (
          <PriceBadge
            label="SPOT $"
            bid={data.spot.bid}
            ask={data.spot.ask}
            supported={data.spot.supported}
          />
        ) : (
          <GapBadge
            label="SPOT GAP"
            gap={data.spot.gap}
            isMin={isMinSpot}
            supported={data.spot.supported}
          />
        )}
        {isPriceMode ? (
          <PriceBadge
            label="PERP $"
            bid={data.futures.bid}
            ask={data.futures.ask}
            supported={data.futures.supported}
          />
        ) : (
          <GapBadge
            label="PERP GAP"
            gap={data.futures.gap}
            isMin={isMinFutures}
            supported={data.futures.supported}
          />
        )}
      </div>

      {data.networks && data.networks.length > 0 && (
        <div className="mt-1.5">
          <p className="mb-0.5 text-[9px] uppercase tracking-[0.16em] text-ink-faint">
            NETWORKS ///
          </p>
          <NetworkInfo
            networks={data.networks}
            onNetworkClick={(network) => onNetworkClick?.(exchangeName, network)}
          />
        </div>
      )}

      {!isPriceMode && data.futures.supported && (
        <div className="mt-1.5 flex justify-end">
          <button
            type="button"
            onClick={() => onEnterHedge?.(exchangeName)}
            className="whitespace-nowrap border border-ink bg-accent-yellow px-2 py-0.5 text-[9px] uppercase tracking-[0.18em] font-bold text-ink transition-colors hover:bg-ink hover:text-accent-yellow"
            title="Open spot/perp hedge"
          >
            OPEN HEDGE ↗
          </button>
        </div>
      )}
    </div>
  );
}
