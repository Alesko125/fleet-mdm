import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';

export interface AdminEvent {
  type: string;
  [key: string]: unknown;
}

/**
 * Живі оновлення панелі через `/ws/admin`.
 *
 * Канал доповнює, а не замінює звичайні запити: якщо сокет недоступний,
 * сторінки далі оновлюються за таймером. Перепідключення — з наростаючою
 * паузою, щоб не бомбардувати сервер.
 */
export function useAdminSocket(onEvent: (event: AdminEvent) => void) {
  const [connected, setConnected] = useState(false);
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry = 3000;
    let timer: number | undefined;
    let closed = false;

    const connect = async () => {
      if (closed) return;
      try {
        const url = await api.adminSocketUrl();
        socket = new WebSocket(url);

        socket.onopen = () => {
          setConnected(true);
          retry = 3000;
        };
        socket.onmessage = (event) => {
          try {
            handlerRef.current(JSON.parse(event.data) as AdminEvent);
          } catch {
            // Некоректне повідомлення просто ігноруємо.
          }
        };
        socket.onclose = () => {
          setConnected(false);
          if (!closed) {
            timer = window.setTimeout(connect, retry);
            retry = Math.min(retry * 2, 60000);
          }
        };
        socket.onerror = () => socket?.close();
      } catch {
        setConnected(false);
        if (!closed) {
          timer = window.setTimeout(connect, retry);
          retry = Math.min(retry * 2, 60000);
        }
      }
    };

    void connect();

    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return connected;
}
