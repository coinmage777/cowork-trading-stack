interface SectionHeaderProps {
  title: string;
  descriptor?: string;
  version?: string;
  right?: React.ReactNode;
  className?: string;
}

export function SectionHeader({
  title,
  descriptor,
  version,
  right,
  className = '',
}: SectionHeaderProps) {
  return (
    <div
      className={`flex items-center justify-between gap-4 py-2 ${className}`}
    >
      <div className="flex items-baseline gap-2 min-w-0">
        <span className="text-[11px] font-bold tracking-[0.18em] uppercase text-ink">
          {title}
          <sup className="text-[8px] ml-0.5 text-ink-faint">TM</sup>
        </span>
        {descriptor && (
          <>
            <span className="text-ink-faint">·</span>
            <span className="text-[10px] uppercase tracking-[0.14em] text-ink-mute truncate">
              {descriptor}
            </span>
          </>
        )}
        {version && (
          <>
            <span className="text-ink-faint tracking-widest">///</span>
            <span className="text-[10px] uppercase tracking-[0.14em] text-ink-faint">
              {version}
            </span>
          </>
        )}
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  );
}
