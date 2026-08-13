import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import type { Apk, DeviceApp } from '../api/types';
import {
  Badge,
  Empty,
  ErrorBanner,
  InfoBanner,
  Modal,
  ProgressBar,
  Spinner,
  StatusBadge,
  formatBytes,
  formatDate,
  relativeTime,
} from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import CommsTab from './CommsTab';
import DeviceActions from '../components/DeviceActions';
import { describeMode } from './DevicesPage';
import FilesTab from './FilesTab';
import LocationTab from './LocationTab';
import SupportTab from './SupportTab';

type Tab = 'overview' | 'apps' | 'files' | 'location' | 'support' | 'operations' | 'comms';

export default function DeviceDetailPage() {
  const { deviceId = '' } = useParams();
  const [tab, setTab] = useState<Tab>('apps');

  const device = useAsync(() => api.device(deviceId), [deviceId], { refreshMs: 20000 });

  if (device.loading && !device.data) return <Spinner />;
  if (device.error) return <ErrorBanner error={device.error} />;
  if (!device.data) return null;

  const info = device.data;

  return (
    <>
      <header className="page-header">
        <div>
          <Link className="link small" to="/devices">
            ← до списку
          </Link>
          <h1>{info.name}</h1>
          <div className="row small muted">
            <Badge tone={info.online ? 'ok' : 'muted'}>
              {info.online ? 'онлайн' : 'офлайн'}
            </Badge>
            <span>{describeMode(info.management_mode)}</span>
            <span>агент {info.agent_version ?? '—'}</span>
            <span>останній звʼязок: {relativeTime(info.last_seen_at)}</span>
          </div>
        </div>
      </header>

      <nav className="tabs">
        <button type="button" className={tab === 'apps' ? 'active' : ''} onClick={() => setTab('apps')}>
          Застосунки
        </button>
        <button
          type="button"
          className={tab === 'files' ? 'active' : ''}
          onClick={() => setTab('files')}
        >
          Файли
        </button>
        <button
          type="button"
          className={tab === 'location' ? 'active' : ''}
          onClick={() => setTab('location')}
        >
          Геолокація
        </button>
        <button
          type="button"
          className={tab === 'support' ? 'active' : ''}
          onClick={() => setTab('support')}
        >
          Підтримка
        </button>
        <button
          type="button"
          className={tab === 'comms' ? 'active' : ''}
          onClick={() => setTab('comms')}
        >
          Повідомлення
        </button>
        <button
          type="button"
          className={tab === 'operations' ? 'active' : ''}
          onClick={() => setTab('operations')}
        >
          Операції
        </button>
        <button
          type="button"
          className={tab === 'overview' ? 'active' : ''}
          onClick={() => setTab('overview')}
        >
          Телеметрія
        </button>
      </nav>

      {tab === 'apps' ? <AppsTab deviceId={deviceId} /> : null}
      {tab === 'files' ? <FilesTab deviceId={deviceId} /> : null}
      {tab === 'location' ? <LocationTab deviceId={deviceId} /> : null}
      {tab === 'support' ? <SupportTab deviceId={deviceId} /> : null}
      {tab === 'comms' ? <CommsTab deviceId={deviceId} /> : null}
      {tab === 'operations' ? <OperationsTab deviceId={deviceId} /> : null}
      {tab === 'overview' ? (
        <>
          <DeviceActions deviceId={deviceId} managementMode={info.management_mode} />
        <section className="card">
          <dl className="details">
            <dt>Виробник / модель</dt>
            <dd>
              {info.telemetry?.manufacturer ?? '—'} {info.telemetry?.model ?? ''}
            </dd>
            <dt>Android</dt>
            <dd>
              {info.telemetry?.android_version ?? '—'} (SDK {info.telemetry?.sdk_int ?? '—'})
            </dd>
            <dt>Ядро</dt>
            <dd className="small mono">{info.telemetry?.kernel_version ?? '—'}</dd>
            <dt>Батарея</dt>
            <dd>
              {info.telemetry?.battery_level !== null && info.telemetry?.battery_level !== undefined
                ? `${info.telemetry.battery_level}%`
                : '—'}
              {info.telemetry?.charging ? ' (заряджається)' : ''}
            </dd>
            <dt>Памʼять</dt>
            <dd>
              {formatBytes(info.telemetry?.ram_available_bytes)} вільно з{' '}
              {formatBytes(info.telemetry?.ram_total_bytes)}
            </dd>
            <dt>Сховище</dt>
            <dd>
              {formatBytes(info.telemetry?.storage_free_bytes)} вільно з{' '}
              {formatBytes(info.telemetry?.storage_total_bytes)}
            </dd>
            <dt>Мережа</dt>
            <dd>{info.telemetry?.network_type ?? '—'}</dd>
            <dt>Відбиток ключа</dt>
            <dd className="mono small">{info.key_fingerprint}</dd>
            <dt>Зареєстровано</dt>
            <dd>{formatDate(info.enrolled_at)}</dd>
          </dl>
          <p className="muted small">
            Повна телеметрія збирається агентом у наступному мілстоуні (M2) — тут показано те,
            що надійшло під час реєстрації.
          </p>
        </section>
        </>
      ) : null}
    </>
  );
}

// --------------------------------------------------------------- застосунки

function AppsTab({ deviceId }: { deviceId: string }) {
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [installFor, setInstallFor] = useState<DeviceApp | 'new' | null>(null);
  const [showSystem, setShowSystem] = useState(false);

  const apps = useAsync(() => api.deviceApps(deviceId), [deviceId], { refreshMs: 15000 });
  const repository = useAsync(() => api.apks(), []);

  const visible = useMemo(() => {
    const items = apps.data ?? [];
    return showSystem ? items : items.filter((app) => !app.is_system);
  }, [apps.data, showSystem]);

  const updatable = visible.filter((app) => app.update_available);

  const act = async (action: () => Promise<unknown>, message: string) => {
    setError(null);
    setInfo(null);
    try {
      await action();
      setInfo(message);
      await apps.reload(true);
    } catch (actionError) {
      setError(actionError);
    }
  };

  return (
    <>
      <div className="row toolbar">
        <button
          type="button"
          onClick={() =>
            void act(() => api.refreshApps(deviceId), 'Запит переліку застосунків надіслано')
          }
        >
          Оновити перелік із пристрою
        </button>
        <button type="button" onClick={() => setInstallFor('new')}>
          Встановити APK
        </button>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={showSystem}
            onChange={(event) => setShowSystem(event.target.checked)}
          />
          показувати системні
        </label>
        {updatable.length > 0 ? (
          <Badge tone="warn">доступні оновлення: {updatable.length}</Badge>
        ) : null}
      </div>

      <ErrorBanner error={error ?? apps.error} />
      <InfoBanner message={info} />

      {apps.loading && !apps.data ? <Spinner /> : null}
      {apps.data && visible.length === 0 ? (
        <Empty>
          Перелік застосунків ще не надходив. Натисніть «Оновити перелік із пристрою».
        </Empty>
      ) : null}

      {visible.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Застосунок</th>
              <th>Встановлена версія</th>
              <th>У репозиторії</th>
              <th>Стан</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {visible.map((app) => (
              <tr key={app.package_name}>
                <td>
                  <strong>{app.app_name ?? app.package_name}</strong>
                  <div className="muted small mono">{app.package_name}</div>
                </td>
                <td className="small">
                  {app.version_name ?? '—'}
                  <span className="muted"> ({app.version_code ?? '—'})</span>
                </td>
                <td className="small">
                  {app.in_repository ? (
                    <>
                      {app.repository_version_name ?? '—'}
                      <span className="muted"> ({app.repository_version_code ?? '—'})</span>
                    </>
                  ) : (
                    <span className="muted">немає</span>
                  )}
                </td>
                <td>
                  {app.update_available ? (
                    <Badge tone="warn">є оновлення</Badge>
                  ) : app.in_repository ? (
                    <Badge tone="ok">актуальна</Badge>
                  ) : (
                    <Badge tone="muted">поза репозиторієм</Badge>
                  )}
                  {app.is_system ? <Badge tone="info">системний</Badge> : null}
                </td>
                <td className="actions">
                  {app.update_available && app.repository_apk_id ? (
                    <button
                      type="button"
                      className="link"
                      onClick={() =>
                        void act(
                          () => api.installApp(deviceId, app.repository_apk_id as string),
                          `Оновлення ${app.package_name} поставлено в чергу`,
                        )
                      }
                    >
                      Оновити
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="link"
                    onClick={() =>
                      void act(
                        () => api.launchApp(deviceId, app.package_name),
                        `Запуск ${app.package_name} надіслано`,
                      )
                    }
                  >
                    Запустити
                  </button>
                  {!app.is_system ? (
                    <button
                      type="button"
                      className="link danger"
                      onClick={() => {
                        if (!confirm(`Видалити ${app.package_name} з пристрою?`)) return;
                        void act(
                          () => api.uninstallApp(deviceId, app.package_name),
                          `Видалення ${app.package_name} поставлено в чергу`,
                        );
                      }}
                    >
                      Видалити
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {installFor ? (
        <InstallModal
          deviceId={deviceId}
          apks={repository.data?.items ?? []}
          onClose={() => setInstallFor(null)}
          onInstalled={async (message) => {
            setInstallFor(null);
            setInfo(message);
            await apps.reload(true);
          }}
        />
      ) : null}
    </>
  );
}

function InstallModal({
  deviceId,
  apks,
  onClose,
  onInstalled,
}: {
  deviceId: string;
  apks: Apk[];
  onClose: () => void;
  onInstalled: (message: string) => Promise<void>;
}) {
  const [selected, setSelected] = useState('');
  const [allowDowngrade, setAllowDowngrade] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const command = await api.installApp(deviceId, selected, allowDowngrade);
      await onInstalled(`Операція ${command.type} поставлена в чергу`);
    } catch (installError) {
      setError(installError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Встановити APK з репозиторію" onClose={onClose}>
      <ErrorBanner error={error} />
      {apks.length === 0 ? (
        <Empty>Репозиторій порожній — спершу завантажте APK.</Empty>
      ) : (
        <>
          <label>
            APK
            <select value={selected} onChange={(event) => setSelected(event.target.value)}>
              <option value="">— оберіть —</option>
              {apks.map((apk) => (
                <option key={apk.id} value={apk.id}>
                  {apk.package_name} · {apk.display_version} · {formatBytes(apk.size_bytes)}
                </option>
              ))}
            </select>
          </label>

          <label className="checkbox">
            <input
              type="checkbox"
              checked={allowDowngrade}
              onChange={(event) => setAllowDowngrade(event.target.checked)}
            />
            дозволити пониження версії
          </label>

          <p className="muted small">
            Пристрій перевірить SHA-256 і назву пакета перед встановленням. Без режиму Device
            Owner Android покаже користувачу діалог підтвердження.
          </p>

          <button type="button" className="primary" disabled={!selected || busy} onClick={submit}>
            {busy ? 'Ставимо в чергу…' : 'Встановити'}
          </button>
        </>
      )}
    </Modal>
  );
}

// ----------------------------------------------------------------- операції

function OperationsTab({ deviceId }: { deviceId: string }) {
  const { data, loading, error, reload } = useAsync(
    () => api.commands({ deviceId }),
    [deviceId],
    { refreshMs: 5000 },
  );
  const [actionError, setActionError] = useState<unknown>(null);

  const cancel = async (id: string) => {
    setActionError(null);
    try {
      await api.cancelCommand(id);
      await reload(true);
    } catch (cancelError) {
      setActionError(cancelError);
    }
  };

  return (
    <>
      <ErrorBanner error={error ?? actionError} />
      {loading && !data ? <Spinner /> : null}
      {data && data.items.length === 0 ? <Empty>Операцій ще не було.</Empty> : null}

      {data && data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Операція</th>
              <th>Пакет</th>
              <th>Стан</th>
              <th>Прогрес</th>
              <th>Створено</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.items.map((command) => (
              <tr key={command.id}>
                <td>
                  <strong>{command.type}</strong>
                  <div className="muted small mono">{command.id.slice(0, 8)}</div>
                </td>
                <td className="small mono">
                  {(command.payload?.package_name as string) ?? '—'}
                  {command.payload?.version_code ? (
                    <span className="muted"> ({String(command.payload.version_code)})</span>
                  ) : null}
                </td>
                <td>
                  <StatusBadge status={command.status} />
                  {command.error ? (
                    <div className="small danger-text">{command.error}</div>
                  ) : null}
                  {command.result?.mechanism ? (
                    <div className="muted small">{String(command.result.mechanism)}</div>
                  ) : null}
                </td>
                <td style={{ minWidth: 140 }}>
                  <ProgressBar percent={command.progress_percent} />
                  <div className="muted small">{command.status_message ?? ''}</div>
                </td>
                <td className="small">{formatDate(command.created_at)}</td>
                <td className="actions">
                  {['pending', 'dispatched', 'acked'].includes(command.status) ? (
                    <button type="button" className="link danger" onClick={() => void cancel(command.id)}>
                      Скасувати
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </>
  );
}
