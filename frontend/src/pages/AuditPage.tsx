import { useState } from 'react';
import { api } from '../api/client';
import { Badge, Empty, ErrorBanner, Spinner, formatDate } from '../components/ui';
import { useAsync } from '../hooks/useAsync';

const APP_ACTIONS = [
  'APK_UPLOADED',
  'APK_DOWNLOADED',
  'APK_DELETED',
  'APP_INSTALL',
  'APP_UPDATE',
  'APP_UNINSTALL',
  'APP_LAUNCH',
  'APP_INVENTORY_REPORTED',
  'COMMAND_COMPLETED',
  'COMMAND_CANCELLED',
];

export default function AuditPage() {
  const [action, setAction] = useState('');
  const [result, setResult] = useState('');

  const { data, loading, error, reload } = useAsync(
    () => api.audit({ action: action || undefined, result: result || undefined }),
    [action, result],
  );

  return (
    <>
      <header className="page-header">
        <h1>Аудит</h1>
        <div className="row">
          <select value={action} onChange={(event) => setAction(event.target.value)}>
            <option value="">усі дії</option>
            <optgroup label="Застосунки та APK">
              {APP_ACTIONS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </optgroup>
            <optgroup label="Інше">
              <option value="AUTH_LOGIN">AUTH_LOGIN</option>
              <option value="AUTH_LOGIN_FAILED">AUTH_LOGIN_FAILED</option>
              <option value="DEVICE_ENROLLED">DEVICE_ENROLLED</option>
              <option value="ACCESS_DENIED">ACCESS_DENIED</option>
            </optgroup>
          </select>
          <select value={result} onChange={(event) => setResult(event.target.value)}>
            <option value="">усі результати</option>
            <option value="success">success</option>
            <option value="failure">failure</option>
            <option value="denied">denied</option>
          </select>
          <button type="button" onClick={() => void reload()}>
            Оновити
          </button>
        </div>
      </header>

      <ErrorBanner error={error} />
      {loading && !data ? <Spinner /> : null}
      {data && data.items.length === 0 ? <Empty>Записів за фільтром немає.</Empty> : null}

      {data && data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Час</th>
              <th>Дія</th>
              <th>Актор</th>
              <th>Обʼєкт</th>
              <th>Результат</th>
              <th>Деталі</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((entry) => (
              <tr key={entry.id}>
                <td className="small">{formatDate(entry.occurred_at)}</td>
                <td className="small mono">{entry.action}</td>
                <td className="small">
                  {entry.actor_label ?? '—'}
                  <div className="muted small">{entry.actor_type}</div>
                </td>
                <td className="small mono">
                  {entry.object_type ?? '—'}
                  {entry.object_id ? (
                    <div className="muted small">{entry.object_id.slice(0, 8)}</div>
                  ) : null}
                </td>
                <td>
                  <Badge
                    tone={
                      entry.result === 'success'
                        ? 'ok'
                        : entry.result === 'denied'
                          ? 'warn'
                          : 'bad'
                    }
                  >
                    {entry.result}
                  </Badge>
                </td>
                <td className="small">
                  {entry.details ? (
                    <details>
                      <summary className="muted">показати</summary>
                      <pre className="json">{JSON.stringify(entry.details, null, 2)}</pre>
                    </details>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </>
  );
}
