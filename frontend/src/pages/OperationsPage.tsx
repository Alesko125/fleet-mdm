import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import {
  Empty,
  ErrorBanner,
  ProgressBar,
  Spinner,
  StatusBadge,
  formatDate,
} from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { formatMs } from './CommandsPage';

// Перелік типів беремо з реєстру, а не зі списку в коді: інакше він
// розходився б із тим, що система насправді вміє.

const STATUSES = ['pending', 'dispatched', 'acked', 'succeeded', 'failed', 'cancelled', 'expired'];

export default function OperationsPage() {
  const [type, setType] = useState('');
  const [status, setStatus] = useState('');

  const registry = useAsync(() => api.commandRegistry(), []);
  const types = (registry.data ?? []).map((spec) => spec.type);
  const titles = new Map((registry.data ?? []).map((spec) => [spec.type, spec.title]));

  const { data, loading, error, reload } = useAsync(
    () => api.commands({ type: type || undefined, status: status || undefined }),
    [type, status],
    { refreshMs: 5000 },
  );

  return (
    <>
      <header className="page-header">
        <h1>Історія операцій</h1>
        <div className="row">
          <select value={type} onChange={(event) => setType(event.target.value)}>
            <option value="">усі типи</option>
            {types.map((item) => (
              <option key={item} value={item}>
                {titles.get(item) ?? item}
              </option>
            ))}
          </select>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">усі стани</option>
            {STATUSES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <button type="button" onClick={() => void reload()}>
            Оновити
          </button>
        </div>
      </header>

      <ErrorBanner error={error} />
      {loading && !data ? <Spinner /> : null}
      {data && data.items.length === 0 ? <Empty>Операцій за фільтром немає.</Empty> : null}

      {data && data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Операція</th>
              <th>Пристрій</th>
              <th>Пакет</th>
              <th>Стан</th>
              <th>Прогрес</th>
              <th>У черзі</th>
              <th>Виконання</th>
              <th>Створено</th>
              <th>Завершено</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((command) => (
              <tr key={command.id}>
                <td>
                  <strong>{titles.get(command.type) ?? command.type}</strong>
                  <div className="muted small mono">{command.id.slice(0, 8)}</div>
                </td>
                <td className="small">
                  <Link to={`/devices/${command.device_id}`}>
                    {command.device_id.slice(0, 8)}
                  </Link>
                </td>
                <td className="small mono">
                  {(command.payload?.package_name as string) ?? '—'}
                </td>
                <td>
                  <StatusBadge status={command.status} />
                  {command.error ? <div className="small danger-text">{command.error}</div> : null}
                </td>
                <td style={{ minWidth: 120 }}>
                  <ProgressBar percent={command.progress_percent} />
                </td>
                <td className="small">{formatMs(command.queue_ms)}</td>
                <td className="small">{formatMs(command.duration_ms)}</td>
                <td className="small">{formatDate(command.created_at)}</td>
                <td className="small">{formatDate(command.completed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </>
  );
}
