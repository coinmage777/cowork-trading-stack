interface DividerProps {
  variant?: 'slash' | 'line' | 'double';
  label?: string;
  className?: string;
}

export function Divider({ variant = 'line', label, className = '' }: DividerProps) {
  if (variant === 'slash') {
    return (
      <span className={`text-ink-faint tracking-widest ${className}`}>
        ///
      </span>
    );
  }
  if (variant === 'double') {
    return (
      <div className={`flex items-center gap-2 ${className}`}>
        <div className="h-px flex-1 bg-rule" />
        {label && (
          <span className="text-[10px] uppercase tracking-[0.18em] text-ink-faint">
            {label}
          </span>
        )}
        <div className="h-px flex-1 bg-rule" />
      </div>
    );
  }
  return <div className={`h-px w-full bg-rule ${className}`} />;
}
