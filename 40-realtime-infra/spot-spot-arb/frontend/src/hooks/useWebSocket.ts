import { useCallback, useEffect, useRef, useState } from 'react';
import type { GapUpdate } from '../types';

const WS_URL = '/ws';
const RECONNECT_DELAY_MS = 3000;

interface UseWebSocketReturn {
  data: Record<string, GapUpdate>;
  connected: boolean;
}

export function useWebSocket(tickers: string[]): UseWebSocketReturn {
  const [data, setData] = useState<Record<string, GapUpdate>>({});
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tickersRef = useRef<string[]>(tickers);

  useEffect(() => {
    tickersRef.current = tickers;
  }, [tickers]);

  const sendSubscribe = useCallback((ws: WebSocket, symbols: string[]) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'subscribe', tickers: symbols }));
    }
  }, []);

  useEffect(() => {
    let disposed = false;

    const connect = () => {
      if (disposed) {
        return;
      }

      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }

      const wsUrl =
        typeof window !== 'undefined'
          ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}${WS_URL}`
          : `ws://localhost:8000${WS_URL}`;

      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        if (disposed) {
          return;
        }
        setConnected(true);
        sendSubscribe(ws, tickersRef.current);
      };

      ws.onmessage = (event: MessageEvent) => {
        try {
          const message = JSON.parse(event.data as string) as GapUpdate;
          if (message.type === 'gap_update' && message.ticker) {
            setData((prev) => ({ ...prev, [message.ticker]: message }));
          }
        } catch {
          // Ignore malformed websocket messages.
        }
      };

      ws.onerror = () => {
        // Intentionally empty: reconnect is handled in onclose.
      };

      ws.onclose = () => {
        if (disposed) {
          return;
        }

        setConnected(false);
        wsRef.current = null;
        reconnectTimerRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
      };
    };

    connect();

    return () => {
      disposed = true;

      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
      }

      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [sendSubscribe]);

  useEffect(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      sendSubscribe(wsRef.current, tickers);
    }
  }, [tickers, sendSubscribe]);

  return { data, connected };
}
