import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type {
  ConnectionStatus,
  SessionAuditEntry,
  SupportSession,
  SupportSessionState,
} from '../api/types';
import {
  Badge,
  Empty,
  ErrorBanner,
  InfoBanner,
  Spinner,
  formatBytes,
  formatDate,
} from '../components/ui';
import { useAsync } from '../hooks/useAsync';

const STATE_LABELS: Record<SupportSessionState, string> = {
  requested: 'очікує згоди користувача',
  awaiting_consent: 'згоду надано, чекаємо на потік',
  active: 'триває',
  denied: 'користувач відмовив',
  ended: 'завершено',
  expired: 'протерміновано',
};

const STATE_TONES: Record<SupportSessionState, 'ok' | 'warn' | 'bad' | 'muted' | 'info'> = {
  requested: 'warn',
  awaiting_consent: 'warn',
  active: 'ok',
  denied: 'bad',
  ended: 'muted',
  expired: 'muted',
};

/** Людські назви подій журналу сеансу. */
const AUDIT_LABELS: Record<string, string> = {
  REMOTE_SESSION_REQUESTED: 'сеанс запитано',
  REMOTE_SESSION_CONSENT: 'відповідь користувача',
  REMOTE_SESSION_STARTED: 'трансляція почалася',
  REMOTE_SESSION_ENDED: 'сеанс завершено',
  REMOTE_SESSION_VIEWER_JOINED: 'оператор приєднався',
  REMOTE_SESSION_VIEWER_LEFT: 'оператор відʼєднався',
  REMOTE_SESSION_INPUT: 'дії керування',
};

export default function SupportTab({ deviceId }: { deviceId: string }) {
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState('');
  const [inputEnabled, setInputEnabled] = useState(false);
  const [duration, setDuration] = useState(30);

  const sessions = useAsync(() => api.supportSessions({ deviceId }), [deviceId], {
    refreshMs: 5000,
  });

  const open = (sessions.data?.items ?? []).find((item) => item.is_open) ?? null;

  const start = async () => {
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      await api.createSupportSession(deviceId, {
        reason,
        input_enabled: inputEnabled,
        max_duration_minutes: duration,
      });
      setInfo('Запит надіслано. Трансляція почнеться після згоди користувача.');
      setReason('');
      await sessions.reload(true);
    } catch (startError) {
      setError(startError);
    } finally {
      setBusy(false);
    }
  };

  const stop = async (session: SupportSession) => {
    setBusy(true);
    setError(null);
    try {
      await api.stopSupportSession(session.id);
      setInfo('Сеанс завершено');
      await sessions.reload(true);
    } catch (stopError) {
      setError(stopError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <ErrorBanner error={error ?? sessions.error} />
      <InfoBanner message={info} />

      <section className="card">
        <h2>Новий сеанс</h2>
        <p className="muted small">
          Користувач пристрою побачить запит із вашою поштою та причиною. Трансляція
          неможлива, доки він не погодиться і не підтвердить системний діалог Android.
        </p>

        <label>
          Причина (її побачить користувач)
          <input
            value={reason}
            disabled={busy || Boolean(open)}
            placeholder="Наприклад: допомогти налаштувати пошту"
            onChange={(event) => setReason(event.target.value)}
          />
        </label>

        <div className="row">
          <label>
            Максимальна тривалість, хв
            <input
              type="number"
              min={1}
              max={240}
              value={duration}
              disabled={busy || Boolean(open)}
              onChange={(event) => setDuration(Number(event.target.value))}
            />
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={inputEnabled}
              disabled={busy || Boolean(open)}
              onChange={(event) => setInputEnabled(event.target.checked)}
            />
            запросити віддалене керування
          </label>
        </div>

        <p className="muted small">
          Керування спрацює лише якщо користувач окремо ввімкнув службу доступності агента;
          інакше сеанс лишиться режимом «тільки перегляд».
        </p>

        <button
          type="button"
          className="primary"
          disabled={busy || reason.trim().length < 3 || Boolean(open)}
          onClick={start}
        >
          {busy ? 'Надсилаємо…' : 'Запросити сеанс'}
        </button>
        {open ? (
          <p className="muted small">
            Уже є відкритий сеанс ({STATE_LABELS[open.state]}) — завершіть його, щоб почати новий.
          </p>
        ) : null}
      </section>

      {open ? <LiveSession session={open} onStop={() => void stop(open)} busy={busy} /> : null}

      <section className="card">
        <h2>Історія сеансів</h2>
        {sessions.loading && !sessions.data ? <Spinner /> : null}
        {sessions.data && sessions.data.items.length === 0 ? (
          <Empty>Сеансів підтримки ще не було.</Empty>
        ) : null}

        {sessions.data && sessions.data.items.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>Стан</th>
                <th>Адміністратор</th>
                <th>Причина</th>
                <th>Тривалість</th>
                <th>Трафік</th>
                <th>Керування</th>
                <th>Створено</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sessions.data.items.map((session) => (
                <SessionRow key={session.id} session={session} />
              ))}
            </tbody>
          </table>
        ) : null}
      </section>
    </>
  );
}

/** Рядок історії з розкривним журналом аудиту. */
function SessionRow({ session }: { session: SupportSession }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <>
      <tr>
        <td>
          <Badge tone={STATE_TONES[session.state]}>{STATE_LABELS[session.state]}</Badge>
          {session.end_reason ? <div className="muted small">{session.end_reason}</div> : null}
        </td>
        <td className="small">{session.requested_by_email ?? '—'}</td>
        <td className="small">{session.reason ?? '—'}</td>
        <td className="small">
          {session.duration_seconds !== null ? formatDuration(session.duration_seconds) : '—'}
        </td>
        <td className="small">
          {session.frames_sent} кадрів
          <div className="muted small">{formatBytes(session.bytes_sent)}</div>
        </td>
        <td className="small">
          {session.input_events > 0 ? `${session.input_events} дій` : '—'}
        </td>
        <td className="small">{formatDate(session.created_at)}</td>
        <td className="actions">
          <button type="button" className="link" onClick={() => setExpanded(!expanded)}>
            {expanded ? 'Сховати журнал' : 'Журнал'}
          </button>
        </td>
      </tr>
      {expanded ? (
        <tr>
          <td colSpan={8}>
            <SessionAudit sessionId={session.id} />
          </td>
        </tr>
      ) : null}
    </>
  );
}

/** Журнал подій одного сеансу. */
function SessionAudit({ sessionId }: { sessionId: string }) {
  const trail = useAsync<SessionAuditEntry[]>(
    () => api.supportSessionAudit(sessionId),
    [sessionId],
  );

  if (trail.loading && !trail.data) return <Spinner />;
  if (trail.error) return <ErrorBanner error={trail.error} />;
  if (!trail.data || trail.data.length === 0) return <Empty>Подій немає.</Empty>;

  return (
    <table className="table nested">
      <thead>
        <tr>
          <th>Час</th>
          <th>Подія</th>
          <th>Хто</th>
          <th>Деталі</th>
        </tr>
      </thead>
      <tbody>
        {trail.data.map((entry) => (
          <tr key={entry.id}>
            <td className="small">{formatDate(entry.occurred_at)}</td>
            <td className="small">
              <Badge tone={entry.result === 'success' ? 'ok' : 'bad'}>
                {AUDIT_LABELS[entry.action] ?? entry.action}
              </Badge>
            </td>
            <td className="small">
              {entry.actor_label ?? entry.actor_type}
              {entry.ip ? <div className="muted small">{entry.ip}</div> : null}
            </td>
            <td className="small muted">{describeDetails(entry)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function describeDetails(entry: SessionAuditEntry): string {
  const details = entry.details ?? {};
  const parts: string[] = [];
  if (typeof details.reason === 'string') parts.push(details.reason);
  if (typeof details.granted === 'boolean') {
    parts.push(details.granted ? 'згоду надано' : 'відмовлено');
  }
  if (typeof details.events === 'number') parts.push(`${details.events} дій`);
  if (Array.isArray(details.actions)) parts.push((details.actions as string[]).join(', '));
  if (typeof details.duration_seconds === 'number') {
    parts.push(formatDuration(details.duration_seconds));
  }
  if (typeof details.phase === 'string') parts.push(details.phase);
  return parts.join(' · ') || '—';
}

// ------------------------------------------------------------ живий сеанс --

type Health = 'connecting' | 'live' | 'retrying' | 'closed';

/** Живий перегляд екрана, керування та індикатори стану. */
function LiveSession({
  session,
  onStop,
  busy,
}: {
  session: SupportSession;
  onStop: () => void;
  busy: boolean;
}) {
  const imageRef = useRef<HTMLImageElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const [health, setHealth] = useState<Health>('connecting');
  const [streaming, setStreaming] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [frameSize, setFrameSize] = useState<{ width: number; height: number } | null>(null);
  const [inputReady, setInputReady] = useState(false);
  const [socketError, setSocketError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  // Авторитетний стан каналу — з сервера, а не з того, що бачить ця вкладка.
  const connection = useAsync<ConnectionStatus>(
    () => api.supportConnection(session.id),
    [session.id],
    { refreshMs: 4000 },
  );

  // Лічильник тривалості рахуємо локально, щоб не смикати сервер щосекунди.
  useEffect(() => {
    if (!session.started_at) return;
    const started = new Date(session.started_at).getTime();
    const tick = () => setElapsed(Math.max(0, Math.floor((Date.now() - started) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [session.started_at]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let objectUrl: string | null = null;
    let retryTimer: number | undefined;
    let heartbeat: number | undefined;
    let closed = false;
    let retryMs = 2000;

    /**
     * Підключення до каналу перегляду.
     *
     * Щоразу береться свіжий одноразовий квиток, тож перепідключення після
     * обриву мережі або перезавантаження вкладки не потребує зберігати в
     * браузері нічого довгоживучого.
     */
    const connect = async () => {
      if (closed) return;
      try {
        const url = await api.supportSocketUrl(session.id);
        if (closed) return;

        socket = new WebSocket(url);
        socketRef.current = socket;
        socket.binaryType = 'blob';

        socket.onopen = () => {
          setHealth('live');
          setSocketError(null);
          retryMs = 2000;
          heartbeat = window.setInterval(() => {
            if (socket?.readyState === WebSocket.OPEN) {
              socket.send(JSON.stringify({ type: 'ping' }));
            }
          }, 5000);
        };

        socket.onclose = (event) => {
          window.clearInterval(heartbeat);
          setStreaming(false);
          socketRef.current = null;
          if (closed) return;
          // 4403/4409 — сеанс завершено чи права знято: повторювати марно.
          if (event.code === 4403 || event.code === 4409) {
            setHealth('closed');
            setSocketError('Сеанс закрито сервером');
            return;
          }
          setHealth('retrying');
          retryTimer = window.setTimeout(() => {
            setAttempt((value) => value + 1);
            void connect();
          }, retryMs);
          retryMs = Math.min(retryMs * 2, 20000);
        };

        socket.onerror = () => setSocketError('Помилка каналу перегляду');

        socket.onmessage = (event) => {
          if (typeof event.data === 'string') {
            const message = JSON.parse(event.data) as {
              type: string;
              streaming?: boolean;
              input_ready?: boolean;
              frame_width?: number | null;
              frame_height?: number | null;
              width?: number;
              height?: number;
              message?: string;
            };
            if (message.type === 'joined') {
              setStreaming(Boolean(message.streaming));
              setInputReady(Boolean(message.input_ready));
              if (message.frame_width && message.frame_height) {
                setFrameSize({ width: message.frame_width, height: message.frame_height });
              }
            }
            if (message.type === 'metrics' && message.width && message.height) {
              setFrameSize({ width: message.width, height: message.height });
              setInputReady(true);
            }
            if (message.type === 'stream_ended') setStreaming(false);
            if (message.type === 'error') setSocketError(describeSocketError(message.message));
            return;
          }
          // Бінарний кадр — показуємо як зображення.
          const next = URL.createObjectURL(event.data as Blob);
          if (imageRef.current) imageRef.current.src = next;
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          objectUrl = next;
          setStreaming(true);
        };
      } catch {
        if (closed) return;
        setHealth('retrying');
        setSocketError('Не вдалося підключитися до каналу');
        retryTimer = window.setTimeout(() => {
          setAttempt((value) => value + 1);
          void connect();
        }, retryMs);
        retryMs = Math.min(retryMs * 2, 20000);
      }
    };

    void connect();
    return () => {
      closed = true;
      window.clearTimeout(retryTimer);
      window.clearInterval(heartbeat);
      socket?.close();
      socketRef.current = null;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [session.id]);

  /** Перетворює клік по зображенню в координати пікселів пристрою. */
  const sendInput = useCallback(
    (payload: Record<string, unknown>) => {
      const socket = socketRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ type: 'input', ...payload }));
    },
    [],
  );

  const canControl = session.input_enabled && inputReady && Boolean(frameSize);

  const handleTap = (event: React.MouseEvent<HTMLImageElement>) => {
    if (!canControl || !frameSize) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    // Кадр масштабується під канал, тож переводимо позицію кліку в частку
    // ширини/висоти і множимо на справжню роздільність пристрою.
    const x = Math.round(((event.clientX - rect.left) / rect.width) * frameSize.width);
    const y = Math.round(((event.clientY - rect.top) / rect.height) * frameSize.height);
    sendInput({ action: 'tap', x, y });
  };

  const status = connection.data;

  return (
    <section className="card">
      <div className="row space-between">
        <h2>Активний сеанс</h2>
        <div className="row">
          <Badge tone={STATE_TONES[session.state]}>{STATE_LABELS[session.state]}</Badge>
          <Badge tone={healthTone(health)}>{HEALTH_LABELS[health]}</Badge>
          <Badge tone={status?.device_online ? 'ok' : 'muted'}>
            {status?.device_online ? 'пристрій у каналі' : 'пристрій не в каналі'}
          </Badge>
          {session.input_enabled ? (
            <Badge tone={canControl ? 'warn' : 'muted'}>
              {canControl ? 'керування активне' : 'керування недоступне'}
            </Badge>
          ) : null}
          <button type="button" className="link danger" onClick={onStop} disabled={busy}>
            Завершити сеанс
          </button>
        </div>
      </div>

      <dl className="details">
        <dt>Тривалість</dt>
        <dd>{session.started_at ? formatDuration(elapsed) : 'ще не почався'}</dd>
        <dt>Згода користувача</dt>
        <dd>{session.consent_at ? formatDate(session.consent_at) : 'очікуємо'}</dd>
        <dt>Кадрів / трафік</dt>
        <dd>
          {session.frames_sent} · {formatBytes(session.bytes_sent)}
          {session.stats?.kbps ? ` · ${session.stats.kbps} кбіт/с` : ''}
        </dd>
        <dt>Глядачів</dt>
        <dd>{status ? status.viewers : '—'}</dd>
        <dt>Останній кадр</dt>
        <dd>
          {status?.seconds_since_frame !== null && status?.seconds_since_frame !== undefined
            ? `${status.seconds_since_frame} с тому`
            : '—'}
        </dd>
        <dt>Роздільність</dt>
        <dd>{frameSize ? `${frameSize.width}×${frameSize.height}` : 'невідома'}</dd>
        <dt>Максимум до</dt>
        <dd>{formatDate(session.expires_at)}</dd>
        {attempt > 0 ? (
          <>
            <dt>Перепідключень</dt>
            <dd>{attempt}</dd>
          </>
        ) : null}
      </dl>

      {socketError ? <ErrorBanner error={new Error(socketError)} /> : null}

      <div className="screen-frame">
        {/* eslint-disable-next-line jsx-a11y/alt-text */}
        <img
          ref={imageRef}
          className="screen-image"
          alt="Екран пристрою"
          style={{ cursor: canControl ? 'crosshair' : 'default' }}
          onClick={handleTap}
        />
        {!streaming ? (
          <div className="screen-placeholder">
            {session.consent_at
              ? health === 'retrying'
                ? 'Зʼєднання втрачено — перепідключаємось…'
                : 'Очікуємо кадри з пристрою…'
              : 'Користувач ще не підтвердив показ екрана'}
          </div>
        ) : null}
      </div>

      {session.input_enabled ? (
        <div className="row">
          <button type="button" disabled={!canControl} onClick={() => sendInput({ action: 'back' })}>
            Назад
          </button>
          <button type="button" disabled={!canControl} onClick={() => sendInput({ action: 'home' })}>
            Головний екран
          </button>
          <button
            type="button"
            disabled={!canControl}
            onClick={() => sendInput({ action: 'recents' })}
          >
            Останні застосунки
          </button>
          <span className="muted small">
            {canControl
              ? 'Клік по зображенню = дотик на пристрої.'
              : 'Користувач не ввімкнув службу доступності — доступний лише перегляд.'}
          </span>
        </div>
      ) : null}
    </section>
  );
}

const HEALTH_LABELS: Record<Health, string> = {
  connecting: 'підключаємось',
  live: 'канал підключено',
  retrying: 'перепідключення…',
  closed: 'канал закрито',
};

function healthTone(health: Health): 'ok' | 'warn' | 'bad' | 'muted' {
  if (health === 'live') return 'ok';
  if (health === 'retrying') return 'warn';
  if (health === 'closed') return 'bad';
  return 'muted';
}

function describeSocketError(code: string | undefined): string {
  switch (code) {
    case 'input_disabled':
      return 'Керування вимкнено для цього сеансу';
    case 'input_unavailable':
      return 'Пристрій не підтверджує підтримку керування';
    case 'bad_input':
      return 'Дію відхилено сервером';
    case 'frame_too_large':
      return 'Повідомлення завелике';
    default:
      return 'Помилка каналу перегляду';
  }
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes === 0) return `${rest} с`;
  return `${minutes} хв ${String(rest).padStart(2, '0')} с`;
}
