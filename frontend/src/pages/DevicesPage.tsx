import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { DeviceHealth } from '../api/types';
import { Badge, Empty, ErrorBanner, Spinner, relativeTime } from '../components/ui';
import { useAsync } from '../hooks/useAsync';

export default function DevicesPage() {
  const [search, setSearch] = useState('');
  const { data, loading, error, reload } = useAsync(
    () => api.devices({ search: search || undefined }),
    [search],
    { refreshMs: 20000 },
  );
  // Показники стану приходять окремим запитом, щоб список лишався швидким.
  const health = useAsync(() => api.deviceHealth(), [], { refreshMs: 20000 });
  const healthById = useMemo(
    () => new Map((health.data ?? []).map((item) => [item.device_id, item])),
    [health.data],
  );

  return (
    <>
      <header className="page-header">
        <h1>Пристрої</h1>
        <div className="row">
          <input
            type="search"
            placeholder="Пошук за назвою або ANDROID_ID"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <button type="button" onClick={() => void reload()}>
            Оновити
          </button>
        </div>
      </header>

      <ErrorBanner error={error} />
      {loading && !data ? <Spinner /> : null}

      {data && data.items.length === 0 ? (
        <Empty>
          Ще немає зареєстрованих пристроїв. Створіть enrollment-токен і введіть його в агенті.
        </Empty>
      ) : null}

      {data && data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Пристрій</th>
              <th>Стан</th>
              <th>Режим</th>
              <th>Android</th>
              <th>Батарея</th>
              <th>Ресурси</th>
              <th>Сповіщення</th>
              <th>Останній звʼязок</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((device) => (
              <tr key={device.id}>
                <td>
                  <Link to={`/devices/${device.id}`}>{device.name}</Link>
                  <div className="muted small">{device.android_id ?? device.id.slice(0, 8)}</div>
                </td>
                <td>
                  <Badge tone={device.online ? 'ok' : 'muted'}>
                    {device.online ? 'онлайн' : 'офлайн'}
                  </Badge>
                  {device.status !== 'active' ? (
                    <Badge tone="warn">{device.status}</Badge>
                  ) : null}
                </td>
                <td className="small">{describeMode(device.management_mode)}</td>
                <td className="small">
                  {device.android_version ?? '—'}
                  <div className="muted small">{device.model ?? ''}</div>
                </td>
                <td className="small">
                  {device.battery_level !== null && device.battery_level !== undefined
                    ? `${device.battery_level}%`
                    : '—'}
                  {healthById.get(device.id)?.charging ? (
                    <span className="muted"> ⚡</span>
                  ) : null}
                </td>
                <td className="small">
                  <ResourceCell health={healthById.get(device.id)} />
                </td>
                <td>
                  <AlertCell health={healthById.get(device.id)} />
                </td>
                <td className="small">{relativeTime(device.last_seen_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </>
  );
}


function ResourceCell({ health }: { health?: DeviceHealth }) {
  if (!health) return <span className="muted">—</span>;
  const parts: string[] = [];
  if (health.storage_free_percent !== null) parts.push(`диск ${health.storage_free_percent}%`);
  if (health.ram_free_percent !== null) parts.push(`RAM ${health.ram_free_percent}%`);
  if (health.cpu_load_percent !== null) parts.push(`CPU ${Math.round(health.cpu_load_percent)}%`);
  return parts.length ? <span>{parts.join(' · ')}</span> : <span className="muted">—</span>;
}

function AlertCell({ health }: { health?: DeviceHealth }) {
  if (!health || health.active_alerts === 0) {
    return <Badge tone="ok">немає</Badge>;
  }
  const tone = health.worst_severity === 'critical' ? 'bad' : 'warn';
  return <Badge tone={tone}>{health.active_alerts}</Badge>;
}

export function describeMode(mode: string): string {
  switch (mode) {
    case 'device_owner':
      return 'Device Owner';
    case 'profile_owner':
      return 'Profile Owner';
    default:
      return 'без прав адміністратора';
  }
}
