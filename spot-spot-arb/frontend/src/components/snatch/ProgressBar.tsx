interface ProgressBarProps {
  value: number; // 0..1 (can be signed for pos/neg visualization)
  tone?: 'default' | 'loss' | 'gain' | 'accent';
  height?: number;
  className?: string;
}

export function ProgressBar({
  value,
  tone = 'default',
  height = 6,
  className = '',
}: ProgressBarProps) {
  const pct = Math.max(0, Math.min(1, Math.abs(value))) * 100;
  const bg =
    tone === 'loss'
      ? 'var(--color-loss-red)'
      : tone === 'gain'
      ? 'var(--color-gain-green)'
      : tone === 'accent'
      ? 'var(--color-accent-yellow)'
      : 'var(--color-ink)';
  return (
    <div
      className={`w-full bg-rule/60 overflow-hidden ${className}`}
      style={{ height }}
    >
      <div
        style={{
          width: `${pct}%`,
          height: '100%',
          background: bg,
        }}
      />
    </div>
  );
}
