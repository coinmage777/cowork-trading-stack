export function formatNumber(
  value: number | null | undefined,
  decimals = 0,
): string {
  if (value == null) return '-';
  return value.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatGap(value: number | null | undefined): string {
  if (value == null) return '-';
  return value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

export function formatKRW(value: number | null | undefined): string {
  if (value == null) return '-';
  return `₩${value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`;
}

export function formatCoin(
  value: number | null | undefined,
  decimals = 8,
): string {
  if (value == null) return '-';
  return value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals,
  });
}

export function formatUSD(
  value: number | null | undefined,
  decimals = 6,
): string {
  if (value == null) return '-';
  return `$${value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals,
  })}`;
}

// <=9500 = deep discount, <=9800 = moderate discount, <10000 = mild discount.
export function getGapColorClass(value: number | null | undefined): string {
  if (value == null) return 'text-ink-faint';
  if (value <= 9500) return 'text-loss-red';
  if (value <= 9800) return 'text-accent-orange';
  if (value < 10000) return 'text-accent-magenta';
  return 'text-ink-mute';
}

export function getGapHighlight(
  value: number | null | undefined,
  isMin: boolean,
): string {
  if (!isMin || value == null) return 'bg-oatmeal border border-rule';
  if (value <= 9500) return 'bg-accent-yellow border border-ink text-ink';
  if (value <= 9800) return 'bg-accent-orange/25 border border-accent-orange text-ink';
  return 'bg-accent-cyan/25 border border-accent-cyan text-ink';
}

// Zero-state dash for SNATCH aesthetic
export const DASH = '—';

export function orDash(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === '' || v === '-' || Number.isNaN(v)) {
    return DASH;
  }
  return String(v);
}
