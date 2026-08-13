import { useState } from 'react';
import { api } from '../api/client';
import type { CommsPermission, CommsSettings } from '../api/types';
import { Badge, Empty, ErrorBanner, InfoBanner, Spinner, formatDate } from '../components/ui';
import { useAsync } from '../hooks/useAsync';

/**
 * Комунікаційний модуль: SMS та журнал дзвінків.
 *
 * Модуль опційний і **вимкнений за замовчуванням**. Дані читаються лише з
 * системних content provider'ів за наданими на пристрої дозволами; жодного
 * перехоплення чужих застосунків, сповіщень чи OTP тут немає. Перегляд
 * зібраного вимагає окремого дозволу й пишеться в аудит.
 */

const PERM_LABELS: Record<CommsPermission, { text: string; tone: 'ok' | 'bad' | 'warn' | 'muted' }> = {
  granted: { text: 'надано', tone: 'ok' },
  denied: { text: 'відхилено', tone: 'bad' },
  not_requested: { text: 'не запитано', tone: 'warn' },
  unavailable: { text: 'недоступно на пристрої', tone: 'muted' },
  unknown: { text: 'невідомо', tone: 'muted' },
};

const DIRECTION_LABELS: Record<string, string> = {
  inbox: 'вхідне',
  sent: 'надіслане',
  draft: 'чернетка',
  outbox: 'вихідне',
  incoming: 'вхідний',
  outgoing: 'вихідний',
  missed: 'пропущений',
  other: 'інше',
};

export default function CommsTab({ deviceId }: { deviceId: string }) {
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<'sms' | 'calls'>('sms');

  const settings = useAsync(() => api.commsSettings(deviceId), [deviceId], { refreshMs: 8000 });

  const save = async (patch: Partial<CommsSettings>) => {
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      await api.updateCommsSettings(deviceId, patch);
      setInfo('Налаштування збережено; пристрій отримає їх найближчим часом');
      await settings.reload(true);
    } catch (saveError) {
      setError(saveError);
    } finally {
      setBusy(false);
    }
  };

  const purge = async () => {
    if (!window.confirm('Видалити всі зібрані SMS та дзвінки цього пристрою?')) return;
    setBusy(true);
    try {
      await api.purgeComms(deviceId);
      setInfo('Зібрані дані видалено');
    } catch (purgeError) {
      setError(purgeError);
    } finally {
      setBusy(false);
    }
  };

  const s = settings.data;

  return (
    <>
      <ErrorBanner error={error ?? settings.error} />
      <InfoBanner message={info} />

      <section className="card">
        <h2>Комунікаційний модуль</h2>
        <p className="muted small">
          Опційний модуль для явно зареєстрованих пристроїв. Читає SMS та журнал
          дзвінків <strong>лише</strong> із системних постачальників даних Android і
          <strong> лише</strong> за наданими на пристрої дозволами. Власник пристрою
          бачить стан модуля на екрані агента. Перехоплення чужих застосунків,
          сповіщень, OTP чи кейлогінгу немає — таких можливостей Android застосунку
          не надає.
        </p>

        {settings.loading && !s ? <Spinner /> : null}

        {s ? (
          <>
            {!s.encryption_ready ? (
              <ErrorBanner
                error={
                  new Error(
                    'На сервері не налаштовано шифрування at-rest (COMMS_ENCRYPTION_KEY) — ' +
                      'модуль неможливо ввімкнути.',
                  )
                }
              />
            ) : null}

            <div className="row space-between">
              <div className="row">
                <Badge tone={s.enabled ? 'ok' : 'muted'}>
                  {s.enabled ? 'модуль увімкнено' : 'вимкнено'}
                </Badge>
                {s.metadata_only ? <Badge tone="info">лише метадані</Badge> : null}
              </div>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={s.enabled}
                  disabled={busy || (!s.enabled && !s.encryption_ready)}
                  onChange={(event) => void save({ enabled: event.target.checked })}
                />
                модуль активний
              </label>
            </div>

            <dl className="details">
              <dt>Увімкнено</dt>
              <dd>
                {s.enabled_at ? (
                  <>
                    {formatDate(s.enabled_at)}
                    {s.enabled_by_email ? ` · ${s.enabled_by_email}` : ''}
                  </>
                ) : (
                  '—'
                )}
              </dd>
              {s.disabled_at ? (
                <>
                  <dt>Вимкнено</dt>
                  <dd>{formatDate(s.disabled_at)}</dd>
                </>
              ) : null}
              <dt>Дозвіл на SMS</dt>
              <dd>
                <Badge tone={PERM_LABELS[s.sms_permission].tone}>
                  {PERM_LABELS[s.sms_permission].text}
                </Badge>
              </dd>
              <dt>Дозвіл на дзвінки</dt>
              <dd>
                <Badge tone={PERM_LABELS[s.call_log_permission].tone}>
                  {PERM_LABELS[s.call_log_permission].text}
                </Badge>
              </dd>
              <dt>Останній звіт дозволів</dt>
              <dd>{s.permission_reported_at ? formatDate(s.permission_reported_at) : '—'}</dd>
              <dt>Остання синхронізація</dt>
              <dd>{s.last_sync_at ? formatDate(s.last_sync_at) : '—'}</dd>
            </dl>

            <div className="row">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={s.sms_enabled}
                  disabled={busy || !s.enabled}
                  onChange={(event) => void save({ sms_enabled: event.target.checked })}
                />
                збирати SMS
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={s.call_log_enabled}
                  disabled={busy || !s.enabled}
                  onChange={(event) => void save({ call_log_enabled: event.target.checked })}
                />
                збирати журнал дзвінків
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={s.metadata_only}
                  disabled={busy || !s.enabled}
                  onChange={(event) => void save({ metadata_only: event.target.checked })}
                />
                лише метадані (без тексту SMS)
              </label>
            </div>

            <div className="row">
              <label>
                Зберігати днів (макс. {s.retention_max_days})
                <input
                  type="number"
                  min={1}
                  max={s.retention_max_days}
                  defaultValue={s.retention_days}
                  disabled={busy}
                  onBlur={(event) => {
                    const value = Number(event.target.value);
                    if (value && value !== s.retention_days) void save({ retention_days: value });
                  }}
                />
              </label>
              <button type="button" className="link danger" disabled={busy} onClick={() => void purge()}>
                Видалити зібрані дані
              </button>
            </div>
          </>
        ) : null}
      </section>

      {s?.enabled ? <CommsData deviceId={deviceId} view={view} onView={setView} /> : null}
    </>
  );
}

/** Перегляд зібраних SMS / дзвінків (потребує дозволу comms:read). */
function CommsData({
  deviceId,
  view,
  onView,
}: {
  deviceId: string;
  view: 'sms' | 'calls';
  onView: (v: 'sms' | 'calls') => void;
}) {
  const messages = useAsync(
    () => (view === 'sms' ? api.commsMessages(deviceId) : Promise.resolve(null)),
    [deviceId, view],
  );
  const calls = useAsync(
    () => (view === 'calls' ? api.commsCalls(deviceId) : Promise.resolve(null)),
    [deviceId, view],
  );

  return (
    <section className="card">
      <div className="row space-between">
        <h2>Зібрані дані</h2>
        <div className="tabs">
          <button type="button" className={view === 'sms' ? 'active' : ''} onClick={() => onView('sms')}>
            SMS
          </button>
          <button
            type="button"
            className={view === 'calls' ? 'active' : ''}
            onClick={() => onView('calls')}
          >
            Дзвінки
          </button>
        </div>
      </div>

      <p className="muted small">
        Перегляд вмісту доступний лише з дозволом <code>comms:read</code> і фіксується
        в аудиті. Дані зберігаються зашифрованими; тут вони показані розшифрованими.
      </p>

      <ErrorBanner error={messages.error ?? calls.error} />

      {view === 'sms' ? (
        messages.loading && !messages.data ? (
          <Spinner />
        ) : !messages.data || messages.data.items.length === 0 ? (
          <Empty>Повідомлень ще не зібрано.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Напрямок</th>
                <th>Номер</th>
                <th>Текст</th>
                <th>Час</th>
              </tr>
            </thead>
            <tbody>
              {messages.data.items.map((m) => (
                <tr key={m.id}>
                  <td className="small">{DIRECTION_LABELS[m.direction] ?? m.direction}</td>
                  <td className="small mono">{m.address ?? '—'}</td>
                  <td className="small">
                    {m.body ?? (m.body_length !== null ? `(${m.body_length} символів)` : '—')}
                  </td>
                  <td className="small">{formatDate(m.sent_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      ) : calls.loading && !calls.data ? (
        <Spinner />
      ) : !calls.data || calls.data.items.length === 0 ? (
        <Empty>Дзвінків ще не зібрано.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Напрямок</th>
              <th>Номер</th>
              <th>Контакт</th>
              <th>Тривалість</th>
              <th>Час</th>
            </tr>
          </thead>
          <tbody>
            {calls.data.items.map((c) => (
              <tr key={c.id}>
                <td className="small">{DIRECTION_LABELS[c.direction] ?? c.direction}</td>
                <td className="small mono">{c.number ?? '—'}</td>
                <td className="small">{c.contact ?? '—'}</td>
                <td className="small">
                  {c.duration_seconds !== null ? `${c.duration_seconds} с` : '—'}
                </td>
                <td className="small">{formatDate(c.occurred_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
