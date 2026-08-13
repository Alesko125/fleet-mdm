import { useState } from 'react';
import { api } from '../api/client';
import type { LocationFix, PermissionState } from '../api/types';
import MapView from '../components/MapView';
import {
  Badge,
  Empty,
  ErrorBanner,
  InfoBanner,
  Spinner,
  formatDate,
  relativeTime,
} from '../components/ui';
import { useAsync } from '../hooks/useAsync';

const INTERVALS = [
  { value: 60, label: '1 хвилина' },
  { value: 300, label: '5 хвилин' },
  { value: 900, label: '15 хвилин' },
  { value: 1800, label: '30 хвилин' },
  { value: 3600, label: '1 година' },
  { value: 21600, label: '6 годин' },
];

const DISTANCES = [
  { value: 0, label: 'без фільтра' },
  { value: 25, label: '25 м' },
  { value: 50, label: '50 м' },
  { value: 100, label: '100 м' },
  { value: 500, label: '500 м' },
];

const PERMISSION_LABELS: Record<PermissionState, string> = {
  unknown: 'невідомо',
  granted_fine: 'точна геолокація',
  granted_coarse: 'приблизна геолокація',
  denied: 'відмовлено',
  restricted: 'заблоковано політикою',
};

const SOURCE_LABELS: Record<LocationFix['source'], string> = {
  periodic: 'за розкладом',
  on_demand: 'на запит',
  offline_queue: 'з офлайн-черги',
};

export default function LocationTab({ deviceId }: { deviceId: string }) {
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const overview = useAsync(() => api.locationOverview(deviceId), [deviceId], {
    refreshMs: 30000,
  });
  const history = useAsync(() => api.locationHistory(deviceId, { limit: 500 }), [deviceId]);

  const settings = overview.data?.settings;

  const change = async (patch: Parameters<typeof api.updateLocationSettings>[1], message: string) => {
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      await api.updateLocationSettings(deviceId, patch);
      setInfo(message);
      await overview.reload(true);
    } catch (updateError) {
      setError(updateError);
    } finally {
      setBusy(false);
    }
  };

  const requestNow = async () => {
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      await api.requestLocationNow(deviceId);
      setInfo('Запит позиції надіслано — пристрій відповість найближчим часом');
    } catch (requestError) {
      setError(requestError);
    } finally {
      setBusy(false);
    }
  };

  const purge = async () => {
    if (!confirm('Видалити всю історію переміщень цього пристрою? Дію буде записано в аудит.')) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.purgeLocationHistory(deviceId);
      setInfo(result.message);
      await Promise.all([overview.reload(true), history.reload(true)]);
    } catch (purgeError) {
      setError(purgeError);
    } finally {
      setBusy(false);
    }
  };

  if (overview.loading && !overview.data) return <Spinner />;
  if (!overview.data || !settings) return <ErrorBanner error={overview.error} />;

  const fixes = history.data?.items ?? [];

  return (
    <>
      <ErrorBanner error={error ?? overview.error} />
      <InfoBanner message={info} />

      {/* --------------------------------------------------- конфігурація */}
      <section className="card">
        <div className="row space-between">
          <h2>Модуль геолокації</h2>
          <Badge tone={settings.enabled ? 'ok' : 'muted'}>
            {settings.enabled ? 'увімкнено' : 'вимкнено'}
          </Badge>
        </div>

        <p className="muted small">
          Модуль opt-in: доки він вимкнений, пристрій не збирає координати, а сервер відхиляє
          будь-які спроби їх надіслати. Кожна зміна тут потрапляє в аудит.
        </p>

        <div className="row">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={settings.enabled}
              disabled={busy}
              onChange={(event) =>
                void change(
                  { enabled: event.target.checked },
                  event.target.checked
                    ? 'Геолокацію увімкнено — конфігурацію надіслано на пристрій'
                    : 'Геолокацію вимкнено',
                )
              }
            />
            збирати геолокацію
          </label>

          <label>
            Інтервал звітування
            <select
              value={settings.interval_seconds}
              disabled={busy || !settings.enabled}
              onChange={(event) =>
                void change(
                  { interval_seconds: Number(event.target.value) },
                  'Інтервал оновлено',
                )
              }
            >
              {INTERVALS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>

          <label>
            Мінімальне зміщення
            <select
              value={settings.min_distance_meters}
              disabled={busy || !settings.enabled}
              onChange={(event) =>
                void change(
                  { min_distance_meters: Number(event.target.value) },
                  'Фільтр зміщення оновлено',
                )
              }
            >
              {DISTANCES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>

          <label className="checkbox">
            <input
              type="checkbox"
              checked={settings.high_accuracy}
              disabled={busy || !settings.enabled}
              onChange={(event) =>
                void change(
                  { high_accuracy: event.target.checked },
                  'Режим точності оновлено',
                )
              }
            />
            висока точність (GPS)
          </label>
        </div>

        <div className="row">
          <button type="button" onClick={requestNow} disabled={busy || !settings.enabled}>
            Запитати позицію зараз
          </button>
          <button type="button" className="link danger" onClick={purge} disabled={busy}>
            Видалити історію
          </button>
        </div>
      </section>

      {/* ------------------------------------------------------- статуси */}
      <section className="card">
        <dl className="details">
          <dt>Стан звітування</dt>
          <dd>
            {overview.data.reporting_active ? (
              <Badge tone="ok">активне</Badge>
            ) : (
              <Badge tone="warn">неактивне</Badge>
            )}
            <span className="muted small">
              {' '}
              {settings.enabled
                ? 'модуль увімкнено'
                : 'модуль вимкнено адміністратором'}
              {settings.enabled && !isGranted(settings.permission_state)
                ? ' · користувач не надав дозвіл'
                : ''}
            </span>
          </dd>

          <dt>Дозвіл на пристрої</dt>
          <dd>
            <Badge tone={isGranted(settings.permission_state) ? 'ok' : 'bad'}>
              {PERMISSION_LABELS[settings.permission_state]}
            </Badge>
            <span className="muted small">
              {' '}
              фоновий: {settings.background_permission ? 'надано' : 'ні'} · повідомлено{' '}
              {relativeTime(settings.permission_reported_at)}
            </span>
          </dd>

          <dt>Останній вимір</dt>
          <dd>
            {overview.data.current
              ? `${formatDate(overview.data.current.recorded_at)} (${relativeTime(
                  overview.data.current.recorded_at,
                )})`
              : 'ще не надходив'}
          </dd>

          <dt>Записів в історії</dt>
          <dd>
            {overview.data.fix_count}
            <span className="muted small">
              {' '}
              найстаріший: {formatDate(overview.data.oldest_fix_at)} · зберігаються{' '}
              {overview.data.retention_days} днів
            </span>
          </dd>

          <dt>Згоду зафіксовано</dt>
          <dd>{formatDate(settings.consent_recorded_at)}</dd>
        </dl>
      </section>

      {/* ---------------------------------------------------------- карта */}
      <section className="card">
        <div className="row space-between">
          <h2>Карта переміщень</h2>
          <button type="button" onClick={() => void history.reload()} disabled={history.loading}>
            Оновити
          </button>
        </div>

        {history.loading && !history.data ? <Spinner /> : null}
        {fixes.length === 0 ? (
          <Empty>
            Координат ще немає. {settings.enabled
              ? 'Пристрій надішле їх за розкладом.'
              : 'Увімкніть модуль, щоб почати збір.'}
          </Empty>
        ) : (
          <MapView fixes={fixes} selectedId={selectedId} onSelect={(fix) => setSelectedId(fix.id)} />
        )}
      </section>

      {/* ------------------------------------------------------ таймлайн */}
      {fixes.length > 0 ? (
        <section className="card">
          <h2>Таймлайн</h2>
          <ol className="timeline">
            {fixes.map((fix) => (
              <li
                key={fix.id}
                className={selectedId === fix.id ? 'timeline-item selected' : 'timeline-item'}
                onClick={() => setSelectedId(fix.id)}
                role="presentation"
              >
                <div className="timeline-time">{formatDate(fix.recorded_at)}</div>
                <div className="timeline-body">
                  <span className="mono">
                    {fix.latitude.toFixed(5)}, {fix.longitude.toFixed(5)}
                  </span>
                  <span className="muted small">
                    {fix.accuracy_m ? ` ±${Math.round(fix.accuracy_m)} м` : ''}
                    {fix.provider ? ` · ${fix.provider}` : ''}
                    {` · ${SOURCE_LABELS[fix.source]}`}
                    {fix.battery_level !== null ? ` · батарея ${fix.battery_level}%` : ''}
                  </span>
                  {fix.is_mock ? <Badge tone="warn">підроблена локація</Badge> : null}
                </div>
              </li>
            ))}
          </ol>
        </section>
      ) : null}
    </>
  );
}

function isGranted(state: PermissionState): boolean {
  return state === 'granted_fine' || state === 'granted_coarse';
}
