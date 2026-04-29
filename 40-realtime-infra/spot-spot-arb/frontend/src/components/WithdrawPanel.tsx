import { useEffect, useMemo, useState } from 'react';
import type { GapUpdate } from '../types';
import { formatCoin } from '../utils/format';

interface WithdrawPanelProps {
  ticker: string;
  data: GapUpdate;
  compact?: boolean;
  className?: string;
  onTargetExchangeChange?: (exchange: string) => void;
}

interface PreviewResult {
  ok: boolean;
  code?: string;
  message?: string;
  preview_token?: string;
  expires_at?: number;
  target_address_masked?: string;
  target_tag?: string | null;
  free_balance?: number;
  estimated_fee?: number;
  safety_buffer?: number;
  withdraw_amount?: number;
}

interface ExecuteResult {
  ok: boolean;
  code?: string;
  message?: string;
  job?: {
    job_id?: string;
    status?: string;
    error_code?: string;
    error_message?: string;
  };
}

const EXCHANGE_DISPLAY: Record<string, string> = {
  binance: 'Binance',
  bybit: 'Bybit',
  okx: 'OKX',
  bitget: 'Bitget',
  gate: 'Gate',
  htx: 'HTX',
  upbit: 'Upbit',
  coinone: 'Coinone',
};

const JOB_STATUS_DISPLAY: Record<string, string> = {
  requested: 'Requested',
  submitted: 'Submitted',
  done: 'Done',
  failed: 'Failed',
};

const uniqueNetworks = (items: { network: string }[]) => {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const item of items) {
    if (!item.network || seen.has(item.network)) {
      continue;
    }
    seen.add(item.network);
    result.push(item.network);
  }
  return result;
};

export function WithdrawPanel({
  ticker,
  data,
  compact = false,
  className = '',
  onTargetExchangeChange,
}: WithdrawPanelProps) {
  const [targetExchange, setTargetExchange] = useState('');
  const [withdrawNetwork, setWithdrawNetwork] = useState('');
  const [depositNetwork, setDepositNetwork] = useState('');
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [executeLoading, setExecuteLoading] = useState(false);
  const [executeResult, setExecuteResult] = useState<ExecuteResult | null>(null);

  const exchangeOptions = useMemo(() => {
    return Object.entries(data.exchanges)
      .filter(([name]) => name !== 'coinone')
      .filter(([, info]) => info.networks && info.networks.length > 0)
      .map(([name]) => name);
  }, [data.exchanges]);

  const selectedExchangeInfo = targetExchange
    ? data.exchanges[targetExchange]
    : undefined;

  const withdrawNetworks = useMemo(() => {
    return uniqueNetworks(
      data.bithumb.networks.filter((net) => net.withdraw).map((net) => ({
        network: net.network,
      })),
    );
  }, [data.bithumb.networks]);

  const depositNetworks = useMemo(() => {
    if (!selectedExchangeInfo) return [];
    return uniqueNetworks(
      selectedExchangeInfo.networks
        .filter((net) => net.deposit)
        .map((net) => ({ network: net.network })),
    );
  }, [selectedExchangeInfo]);

  useEffect(() => {
    // Reset selections only when the ticker changes.
    setTargetExchange(exchangeOptions[0] ?? '');
    setWithdrawNetwork(withdrawNetworks[0] ?? '');
    setDepositNetwork('');
    setPreview(null);
    setExecuteResult(null);
  }, [ticker]);

  useEffect(() => {
    // Keep user's exchange selection unless it became unavailable.
    setTargetExchange((prev) => {
      if (prev && exchangeOptions.includes(prev)) {
        return prev;
      }
      return exchangeOptions[0] ?? '';
    });
  }, [exchangeOptions]);

  useEffect(() => {
    // Keep user's withdraw network unless it became unavailable.
    setWithdrawNetwork((prev) => {
      if (prev && withdrawNetworks.includes(prev)) {
        return prev;
      }
      return withdrawNetworks[0] ?? '';
    });
  }, [withdrawNetworks]);

  useEffect(() => {
    const next = depositNetworks[0] ?? '';
    setDepositNetwork((prev) => {
      if (prev && depositNetworks.includes(prev)) {
        return prev;
      }
      return next;
    });
  }, [depositNetworks]);

  useEffect(() => {
    if (!targetExchange) {
      return;
    }
    onTargetExchangeChange?.(targetExchange);
  }, [targetExchange, onTargetExchangeChange]);

  const handlePreview = async () => {
    if (!targetExchange || !withdrawNetwork || !depositNetwork) {
      setPreview({
        ok: false,
        code: 'INVALID_INPUT',
        message: '출금/입금 네트워크와 대상 거래소를 모두 선택해 주세요.',
      });
      return;
    }

    setPreviewLoading(true);
    setPreview(null);
    setExecuteResult(null);
    try {
      const response = await fetch('/api/withdraw/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ticker,
          target_exchange: targetExchange,
          withdraw_network: withdrawNetwork,
          deposit_network: depositNetwork,
        }),
      });
      const payload = (await response.json()) as PreviewResult;
      setPreview(payload);
    } catch {
      setPreview({
        ok: false,
        code: 'NETWORK_ERROR',
        message: 'preview 요청에 실패했습니다.',
      });
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleExecute = async () => {
    const previewToken = preview?.preview_token;
    if (!previewToken) {
      return;
    }

    const confirmed = window.confirm(
      `출금 실행\n티커: ${ticker}\n출금 네트워크: ${withdrawNetwork}\n입금 네트워크: ${depositNetwork}`,
    );
    if (!confirmed) {
      return;
    }

    setExecuteLoading(true);
    setExecuteResult(null);
    try {
      const response = await fetch('/api/withdraw/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preview_token: previewToken }),
      });
      const payload = (await response.json()) as ExecuteResult;
      setExecuteResult(payload);
    } catch {
      setExecuteResult({
        ok: false,
        code: 'NETWORK_ERROR',
        message: 'execute 요청에 실패했습니다.',
      });
    } finally {
      setExecuteLoading(false);
    }
  };

  const containerClass = [
    'snatch-panel bg-cream border border-rule',
    compact ? 'p-3' : 'p-4 mb-6',
    className,
  ]
    .join(' ')
    .trim();

  const formGridClass = compact ? 'grid grid-cols-1 gap-2.5' : 'grid grid-cols-1 md:grid-cols-3 gap-3';
  const labelClass = compact ? 'mb-1 text-[11px] text-gray-500' : 'mb-1 text-xs text-gray-500';
  const selectClass = compact
    ? 'w-full rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-white focus:outline-none focus:ring-1 focus:ring-blue-500'
    : 'w-full rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-white focus:outline-none focus:ring-1 focus:ring-blue-500';
  const actionRowClass = compact ? 'mt-2.5 flex items-center gap-2' : 'mt-3 flex items-center gap-2';
  const actionButtonBase = compact
    ? 'flex-1 rounded px-2.5 py-1.5 text-xs font-medium transition-colors'
    : 'rounded px-3 py-1.5 text-sm font-medium transition-colors';
  const infoBoxClass = compact
    ? 'mt-2.5 rounded border border-gray-800 bg-gray-950/70 p-2.5'
    : 'mt-3 rounded border border-gray-800 bg-gray-950/70 p-3';
  const infoGridClass = compact
    ? 'grid grid-cols-1 gap-1.5 text-[11px] text-gray-300'
    : 'grid grid-cols-1 gap-2 text-xs text-gray-300 sm:grid-cols-2';

  return (
    <div className={containerClass}>
      <h3
        className={
          compact
            ? 'mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-ink'
            : 'mb-3 text-[11px] font-bold uppercase tracking-[0.2em] text-ink'
        }
      >
        AUTO WITHDRAW ///
      </h3>

      <div className={formGridClass}>
        <div>
          <p className={labelClass}>대상 거래소</p>
          <select
            value={targetExchange}
            onChange={(event) => setTargetExchange(event.target.value)}
            className={selectClass}
          >
            {exchangeOptions.map((exchange) => (
              <option key={exchange} value={exchange}>
                {EXCHANGE_DISPLAY[exchange] ?? exchange}
              </option>
            ))}
          </select>
        </div>

        <div>
          <p className={labelClass}>빗썸 출금 네트워크</p>
          <select
            value={withdrawNetwork}
            onChange={(event) => setWithdrawNetwork(event.target.value)}
            className={selectClass}
          >
            {withdrawNetworks.map((network) => (
              <option key={network} value={network}>
                {network}
              </option>
            ))}
          </select>
        </div>

        <div>
          <p className={labelClass}>대상 거래소 입금 네트워크</p>
          <select
            value={depositNetwork}
            onChange={(event) => setDepositNetwork(event.target.value)}
            className={selectClass}
          >
            {depositNetworks.map((network) => (
              <option key={network} value={network}>
                {network}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className={actionRowClass}>
        <button
          type="button"
          onClick={handlePreview}
          disabled={previewLoading || executeLoading}
          className={`bg-blue-600 text-white hover:bg-blue-500 disabled:bg-gray-700 ${actionButtonBase}`}
        >
          {previewLoading ? '검증 중...' : '미리보기'}
        </button>
        <button
          type="button"
          onClick={handleExecute}
          disabled={!preview?.ok || !preview?.preview_token || executeLoading}
          className={`bg-red-600 text-white hover:bg-red-500 disabled:bg-gray-700 ${actionButtonBase}`}
        >
          {executeLoading ? '출금 요청 중...' : '출금 실행'}
        </button>
      </div>

      {preview && (
        <div className={infoBoxClass}>
          {preview.ok ? (
            <>
              <p className={compact ? 'mb-1.5 text-[11px] text-green-400' : 'mb-2 text-xs text-green-400'}>
                검증 통과. 아래 값으로 출금을 요청합니다.
              </p>
              <div className={infoGridClass}>
                <p>티커: {ticker}</p>
                <p>목적지: {EXCHANGE_DISPLAY[targetExchange] ?? targetExchange}</p>
                <p>주소: {preview.target_address_masked}</p>
                <p>태그: {preview.target_tag || '-'}</p>
                <p>가용잔고: {formatCoin(preview.free_balance ?? null)}</p>
                <p>예상수수료: {formatCoin(preview.estimated_fee ?? null)}</p>
                <p>안전버퍼: {formatCoin(preview.safety_buffer ?? null)}</p>
                <p className="font-semibold text-white">
                  출금수량: {formatCoin(preview.withdraw_amount ?? null)}
                </p>
              </div>
            </>
          ) : (
            <p className={compact ? 'text-[11px] text-red-400' : 'text-xs text-red-400'}>
              {preview.code}: {preview.message}
            </p>
          )}
        </div>
      )}

      {executeResult && (
        <div className={infoBoxClass}>
          {executeResult.ok ? (
            <div className={compact ? 'space-y-1 text-[11px] text-green-400' : 'space-y-1 text-xs text-green-400'}>
              <p>출금 요청이 정상 접수되었습니다.</p>
              <p>
                작업 ID: <span className="font-mono text-green-300">{executeResult.job?.job_id ?? '-'}</span> / 상태:{' '}
                {JOB_STATUS_DISPLAY[executeResult.job?.status ?? ''] ?? executeResult.job?.status ?? '-'}
              </p>
              <p className="text-gray-400">최종 처리 결과는 작업 이력에서 확인해 주세요.</p>
            </div>
          ) : (
            <p className={compact ? 'text-[11px] text-red-400' : 'text-xs text-red-400'}>
              {executeResult.code}: {executeResult.message}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
