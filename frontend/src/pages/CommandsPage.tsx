import { useState } from 'react';
import { api } from '../api/client';
import type { CommandSpec } from '../api/types';
import { Badge, Empty, ErrorBanner, InfoBanner, Spinner } from '../components/ui';
import { useAsync } from '../hooks/useAsync';

/**
 * Реєстр команд і матриця дозволів.
 *
 * Тут видно все, що система взагалі вміє робити з пристроєм: перелік
 * закритий, і кожен запис пояснює, що побачить власник пристрою. Довільної
 * команди — оболонки, скрипта — у переліку немає й бути не може.
 */

const ROLE_LABELS: Record<string, string> = {
  superadmin: 'суперадмін',
  admin: 'адмін',
  operator: 'оператор',
  auditor: 'аудитор',
};

/** Дозволи, які має сенс призначати команді. */
const ASSIGNABLE_PERMISSIONS = [
  { value: 'device:action:safe', label: 'безпечна дія (оператор і вище)' },
  { value: 'device:action:critical', label: 'критична дія (адмін і вище)' },
  { value: 'app:manage', label: 'керування застосунками (адмін і вище)' },
  { value: 'device:write', label: 'зміна пристрою (адмін і вище)' },
  { value: 'settings:manage', label: 'налаштування (лише суперадмін)' },
];

export default function CommandsPage() {
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const registry = useAsync(() => api.commandRegistry(), []);
  const stats = useAsync(() => api.commandStats(), [], { refreshMs: 15000 });

  const update = async (
    spec: CommandSpec,
    body: { required_permission?: string; enabled?: boolean },
  ) => {
    setBusy(spec.type);
    setError(null);
    setInfo(null);
    try {
      await api.setCommandPermission(spec.type, body);
      setInfo(`Правило для «${spec.title}» оновлено`);
      await registry.reload(true);
    } catch (updateError) {
      setError(updateError);
    } finally {
      setBusy(null);
    }
  };

  const reset = async (spec: CommandSpec) => {
    setBusy(spec.type);
    setError(null);
    try {
      await api.resetCommandPermission(spec.type);
      setInfo(`«${spec.title}» повернено до значення за замовчуванням`);
      await registry.reload(true);
    } catch (resetError) {
      setError(resetError);
    } finally {
      setBusy(null);
    }
  };

  const byType = new Map((stats.data ?? []).map((item) => [item.type, item]));

  return (
    <>
      <header className="page-header">
        <h1>Реєстр команд</h1>
      </header>

      <ErrorBanner error={error ?? registry.error} />
      <InfoBanner message={info} />

      <section className="card">
        <p className="muted small">
          Це повний перелік того, що система вміє робити з пристроєм. Виконати
          щось поза цим переліком неможливо: довільної команди, оболонки чи
          скрипта в реєстрі немає. Кожен запис пояснює, що саме побачить
          власник пристрою.
        </p>
      </section>

      {registry.loading && !registry.data ? <Spinner /> : null}
      {registry.data && registry.data.length === 0 ? <Empty>Реєстр порожній.</Empty> : null}

      {registry.data
        ? registry.data.map((spec) => {
            const usage = byType.get(spec.type);
            return (
              <section className="card" key={spec.type}>
                <div className="row space-between">
                  <div>
                    <h2>{spec.title}</h2>
                    <code className="muted small">{spec.type}</code>
                  </div>
                  <div className="row">
                    {spec.disruptive ? <Badge tone="warn">помітна дія</Badge> : null}
                    {spec.requires_device_owner ? (
                      <Badge tone="info">потребує Device Owner</Badge>
                    ) : null}
                    {spec.overridden ? <Badge tone="info">правило змінено</Badge> : null}
                    <Badge tone={spec.enabled ? 'ok' : 'bad'}>
                      {spec.enabled ? 'увімкнено' : 'вимкнено'}
                    </Badge>
                  </div>
                </div>

                <p className="small">{spec.description}</p>
                <p className="muted small">
                  <strong>Користувач пристрою побачить:</strong> {spec.user_visible}
                </p>

                {spec.params.length > 0 ? (
                  <p className="muted small">
                    <strong>Параметри:</strong>{' '}
                    {spec.params
                      .map(
                        (param) =>
                          `${param.name}: ${param.type}${param.required ? '' : ' (необовʼязковий)'}`,
                      )
                      .join(', ')}
                  </p>
                ) : (
                  <p className="muted small">Параметрів немає.</p>
                )}

                <div className="row">
                  <label>
                    Потрібний дозвіл
                    <select
                      value={spec.required_permission}
                      disabled={busy === spec.type}
                      onChange={(event) =>
                        void update(spec, { required_permission: event.target.value })
                      }
                    >
                      {ASSIGNABLE_PERMISSIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                      {ASSIGNABLE_PERMISSIONS.every(
                        (option) => option.value !== spec.required_permission,
                      ) ? (
                        <option value={spec.required_permission}>
                          {spec.required_permission}
                        </option>
                      ) : null}
                    </select>
                  </label>

                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={spec.enabled}
                      disabled={busy === spec.type}
                      onChange={(event) =>
                        void update(spec, { enabled: event.target.checked })
                      }
                    />
                    команда доступна
                  </label>

                  {spec.overridden ? (
                    <button
                      type="button"
                      className="link"
                      disabled={busy === spec.type}
                      onClick={() => void reset(spec)}
                    >
                      Повернути за замовчуванням
                    </button>
                  ) : null}
                </div>

                <p className="muted small">
                  Доступна ролям:{' '}
                  {spec.roles.length > 0
                    ? spec.roles.map((role) => ROLE_LABELS[role] ?? role).join(', ')
                    : '—'}
                  {' · '}термін дії команди: {Math.round(spec.ttl_seconds / 60)} хв
                </p>

                {usage ? (
                  <p className="muted small">
                    Виконано: {usage.succeeded} з {usage.total}
                    {usage.failed > 0 ? ` · невдало: ${usage.failed}` : ''}
                    {usage.expired > 0 ? ` · протерміновано: ${usage.expired}` : ''}
                    {usage.pending > 0 ? ` · у черзі: ${usage.pending}` : ''}
                    {usage.avg_duration_ms !== null
                      ? ` · середня тривалість: ${formatMs(usage.avg_duration_ms)}`
                      : ''}
                  </p>
                ) : null}
              </section>
            );
          })
        : null}
    </>
  );
}

export function formatMs(value: number | null): string {
  if (value === null) return '—';
  if (value < 1000) return `${value} мс`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} хв ${Math.round(seconds % 60)} с`;
}
