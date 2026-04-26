import type { NetworkInfo as NetworkInfoType } from '../types';

interface NetworkInfoProps {
  networks: NetworkInfoType[];
  onNetworkClick?: (network: string) => void;
}

export function NetworkInfo({ networks, onNetworkClick }: NetworkInfoProps) {
  if (!networks || networks.length === 0) {
    return (
      <p className="text-[9px] uppercase tracking-[0.16em] text-ink-faint">
        —
      </p>
    );
  }

  return (
    <div className="flex flex-wrap gap-1">
      {networks.map((net) => {
        const active = net.deposit && net.withdraw;
        return (
          <button
            key={net.network}
            type="button"
            title={`DEP: ${net.deposit ? 'OK' : 'X'} / WD: ${
              net.withdraw ? 'OK' : 'X'
            }\nClick to watch`}
            className={`inline-flex items-center gap-1 border px-1.5 py-0 text-[9px] font-semibold uppercase tracking-[0.14em] cursor-pointer transition-all hover:ring-1 hover:ring-ink ${
              active
                ? 'bg-gain-green/15 border-gain-green/60 text-gain-green'
                : 'bg-loss-red/15 border-loss-red/60 text-loss-red'
            }`}
            onClick={() => onNetworkClick?.(net.network)}
          >
            <span
              className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${
                active ? 'bg-gain-green' : 'bg-loss-red'
              }`}
            />
            <span>{net.network}</span>
          </button>
        );
      })}
    </div>
  );
}
