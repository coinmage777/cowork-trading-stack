interface VenueBadgeProps {
  name: string;
  color?: string;
  status?: 'live' | 'idle' | 'off' | 'warn';
  size?: 'xs' | 'sm' | 'md';
  onClick?: () => void;
  active?: boolean;
  className?: string;
}

const palette: Record<string, string> = {
  bithumb: '#FF9138',
  upbit: '#4DE0FF',
  coinone: '#B285FF',
  binance: '#E8FF3C',
  bybit: '#FF9138',
  okx: '#FF4DA6',
  gateio: '#B285FF',
  bitget: '#4DE0FF',
  bingx: '#2BC48A',
  mexc: '#FF6B6B',
  kucoin: '#B285FF',
  hyperliquid: '#4DE0FF',
  default: '#1A1A1A',
};

function colorFor(name: string) {
  const k = name.toLowerCase();
  return palette[k] ?? palette.default;
}

const sizeCls = {
  xs: 'text-[9px] px-1.5 py-0.5 gap-1',
  sm: 'text-[10px] px-2 py-0.5 gap-1.5',
  md: 'text-[11px] px-2.5 py-1 gap-2',
};
const dotCls = {
  xs: 'w-1.5 h-1.5',
  sm: 'w-2 h-2',
  md: 'w-2.5 h-2.5',
};

export function VenueBadge({
  name,
  color,
  status,
  size = 'sm',
  onClick,
  active = false,
  className = '',
}: VenueBadgeProps) {
  const dotColor = color ?? colorFor(name);
  const Tag = onClick ? 'button' : 'span';

  return (
    <Tag
      onClick={onClick}
      className={`inline-flex items-center ${sizeCls[size]} border uppercase tracking-[0.14em] font-semibold transition-colors ${
        active
          ? 'border-ink bg-ink text-cream'
          : 'border-rule bg-cream text-ink hover:border-ink'
      } ${onClick ? 'cursor-pointer' : ''} ${className}`}
    >
      <span
        className={`${dotCls[size]} rounded-full shrink-0`}
        style={{ backgroundColor: dotColor }}
      />
      <span className="truncate max-w-[120px]">{name}</span>
      {status && (
        <span
          className={`${sizeCls[size].includes('9px') ? 'text-[8px]' : 'text-[9px]'} ${
            status === 'live'
              ? 'text-gain-green'
              : status === 'warn'
              ? 'text-accent-orange'
              : status === 'off'
              ? 'text-loss-red'
              : 'text-ink-faint'
          }`}
        >
          {status.toUpperCase()}
        </span>
      )}
    </Tag>
  );
}

export { colorFor as venueColor };
