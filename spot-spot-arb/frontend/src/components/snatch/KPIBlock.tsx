import type { ReactNode } from 'react';

type Tone = 'default' | 'accent' | 'loss' | 'gain' | 'muted';

interface KPIBlockProps {
  label: string;
  value: ReactNode;
  sublabel?: ReactNode;
  tone?: Tone;
  highlight?: boolean;
  sparkline?: number[];
  size?: 'sm' | 'md' | 'lg' | 'xl';
  suffix?: ReactNode;
  className?: string;
}

const toneText: Record<Tone, string> = {
  default: 'text-ink',
  accent: 'text-ink',
  loss: 'text-loss-red',
  gain: 'text-gain-green',
  muted: 'text-ink-faint',
};

const sizeCls: Record<NonNullable<KPIBlockProps['size']>, string> = {
  sm: 'text-2xl',
  md: 'text-4xl',
  lg: 'text-5xl',
  xl: 'text-6xl',
};

export function KPIBlock({
  label,
  value,
  sublabel,
  tone = 'default',
  highlight = false,
  sparkline,
  size = 'lg',
  suffix,
  className = '',
}: KPIBlockProps) {
  return (
    <div className={`flex flex-col justify-between min-h-[120px] px-4 py-3 border-r border-rule last:border-r-0 ${className}`}>
      <div className="text-[10px] uppercase tracking-[0.16em] text-ink-mute mb-2">
        {label}
      </div>
      <div className="flex items-end justify-between gap-3">
        <div className="min-w-0 flex items-baseline gap-2">
          <span
            className={`${sizeCls[size]} font-bold leading-none tracking-tight ${toneText[tone]} ${
              highlight ? 'bg-accent-yellow px-1.5 py-0.5' : ''
            }`}
          >
            {value}
          </span>
          {suffix && (
            <span className="text-xs text-ink-mute whitespace-nowrap">{suffix}</span>
          )}
        </div>
        {sparkline && sparkline.length > 1 && (
          <Sparkline values={sparkline} tone={tone} />
        )}
      </div>
      {sublabel && (
        <div className="text-[10px] text-ink-faint mt-1.5 uppercase tracking-wider">
          {sublabel}
        </div>
      )}
    </div>
  );
}

function Sparkline({ values, tone }: { values: number[]; tone: Tone }) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const w = 80;
  const h = 24;
  const step = w / (values.length - 1);
  const points = values
    .map((v, i) => `${i * step},${h - ((v - min) / range) * h}`)
    .join(' ');

  const stroke =
    tone === 'loss'
      ? 'var(--color-loss-red)'
      : tone === 'gain'
      ? 'var(--color-gain-green)'
      : 'var(--color-ink)';

  return (
    <svg width={w} height={h} className="shrink-0">
      <polyline
        points={points}
        fill="none"
        stroke={stroke}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
