interface LiveChipProps {
  connected: boolean;
  time?: string;
  className?: string;
}

export function LiveChip({ connected, time, className = '' }: LiveChipProps) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold uppercase tracking-[0.18em] ${
        connected ? 'bg-accent-yellow text-ink' : 'bg-loss-red text-cream'
      } ${className}`}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${
          connected ? 'bg-ink animate-pulse' : 'bg-cream'
        }`}
      />
      {connected ? 'LIVE' : 'OFFLINE'}
      {time && <span className="text-ink-mute font-normal ml-1">· {time} KST</span>}
    </span>
  );
}
