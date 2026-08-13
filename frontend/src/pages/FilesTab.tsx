import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import type { FileEntry, FileRoot } from '../api/types';
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
} from '../components/ui';
import { useAsync } from '../hooks/useAsync';

export default function FilesTab({ deviceId }: { deviceId: string }) {
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [currentPath, setCurrentPath] = useState<string | null>(null);
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [loadingEntries, setLoadingEntries] = useState(false);
  const [showRootForm, setShowRootForm] = useState(false);
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const roots = useAsync(() => api.fileRoots(deviceId), [deviceId]);
  const transfers = useAsync(() => api.fileTransfers(deviceId), [deviceId], {
    refreshMs: 4000,
  });

  const activeRoot: FileRoot | undefined = useMemo(() => {
    if (!currentPath) return undefined;
    return (roots.data ?? []).find(
      (root) => currentPath === root.path || currentPath.startsWith(`${root.path}/`),
    );
  }, [currentPath, roots.data]);

  const browse = useCallback(
    async (path: string) => {
      setLoadingEntries(true);
      setError(null);
      try {
        const result = await api.listDirectory(deviceId, path);
        setEntries(result);
        setCurrentPath(path);
      } catch (browseError) {
        setError(browseError);
      } finally {
        setLoadingEntries(false);
      }
    },
    [deviceId],
  );

  // Перший дозволений каталог відкриваємо автоматично.
  useEffect(() => {
    const list = roots.data;
    if (!list || list.length === 0 || currentPath) return;
    void browse(list[0].path);
  }, [roots.data, currentPath, browse]);

  const act = async (action: () => Promise<unknown>, message: string) => {
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      await action();
      setInfo(message);
      await transfers.reload(true);
    } catch (actionError) {
      setError(actionError);
    } finally {
      setBusy(false);
    }
  };

  const download = (entry: FileEntry) =>
    act(
      () => api.requestFileDownload(deviceId, entry.path),
      `Запит на отримання ${entry.name} поставлено в чергу`,
    );

  const remove = (entry: FileEntry) => {
    const label = entry.is_dir ? 'каталог (з усім вмістом)' : 'файл';
    if (!confirm(`Видалити ${label} ${entry.path} на пристрої?`)) return;
    void act(
      () => api.deleteEntry(deviceId, entry.path, entry.is_dir),
      `Видалення ${entry.name} поставлено в чергу`,
    );
  };

  const rename = (entry: FileEntry) => {
    const name = prompt('Нова назва', entry.name);
    if (!name || name === entry.name) return;
    void act(
      () => api.renameEntry(deviceId, entry.path, name),
      `Перейменування ${entry.name} поставлено в чергу`,
    );
  };

  const mkdir = () => {
    if (!currentPath) return;
    const name = prompt('Назва нового каталогу');
    if (!name) return;
    void act(
      () => api.makeDirectory(deviceId, currentPath, name),
      `Створення каталогу ${name} поставлено в чергу`,
    );
  };

  const upload = async (file: File) => {
    if (!currentPath) return;
    setUploadPercent(0);
    setError(null);
    setInfo(null);
    try {
      const transfer = await api.uploadFileToDevice(
        deviceId,
        file,
        currentPath,
        setUploadPercent,
      );
      setInfo(`Файл прийнято сервером, пристрій запише його у ${transfer.remote_path}`);
      await transfers.reload(true);
    } catch (uploadError) {
      setError(uploadError);
    } finally {
      setUploadPercent(null);
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  const rootList = roots.data ?? [];
  const writable = activeRoot?.writable ?? false;

  return (
    <>
      <ErrorBanner error={error ?? roots.error} />
      <InfoBanner message={info} />

      {/* ------------------------------------------- дозволені каталоги */}
      <section className="card">
        <div className="row space-between">
          <h2>Дозволені каталоги</h2>
          <button type="button" onClick={() => setShowRootForm(true)}>
            Додати каталог
          </button>
        </div>
        <p className="muted small">
          Агент має доступ **лише** до цих каталогів. Порожній перелік означає, що файловий
          модуль для пристрою вимкнено. Кожна зміна потрапляє в аудит.
        </p>

        {roots.loading && !roots.data ? <Spinner /> : null}
        {rootList.length === 0 && !roots.loading ? (
          <Empty>Каталогів не налаштовано — файловий модуль вимкнено.</Empty>
        ) : null}

        {rootList.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>Шлях</th>
                <th>Режим</th>
                <th>Ліміт файла</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rootList.map((root) => (
                <tr key={root.id}>
                  <td>
                    <button type="button" className="link" onClick={() => void browse(root.path)}>
                      {root.path}
                    </button>
                    <div className="muted small">{root.label ?? ''}</div>
                  </td>
                  <td>
                    <Badge tone={root.writable ? 'warn' : 'ok'}>
                      {root.writable ? 'читання й запис' : 'лише читання'}
                    </Badge>
                    {root.allow_privileged ? <Badge tone="bad">root-доступ</Badge> : null}
                  </td>
                  <td className="small">
                    {root.max_file_size_mb ? `${root.max_file_size_mb} МБ` : 'за замовчуванням'}
                  </td>
                  <td className="actions">
                    <button
                      type="button"
                      className="link danger"
                      disabled={busy}
                      onClick={() => {
                        if (!confirm(`Прибрати ${root.path} з дозволених?`)) return;
                        void act(
                          () => api.deleteFileRoot(deviceId, root.id).then(() => roots.reload(true)),
                          'Каталог прибрано з дозволених',
                        );
                      }}
                    >
                      Прибрати
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>

      {/* --------------------------------------------------- браузер */}
      {currentPath ? (
        <section className="card">
          <div className="row space-between">
            <h2>Вміст каталогу</h2>
            <div className="row">
              <button type="button" onClick={() => void browse(currentPath)} disabled={loadingEntries}>
                Оновити
              </button>
              <button type="button" onClick={mkdir} disabled={busy || !writable}>
                Створити каталог
              </button>
              <input
                ref={fileInput}
                type="file"
                disabled={busy || !writable || uploadPercent !== null}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void upload(file);
                }}
              />
            </div>
          </div>

          <Breadcrumbs
            path={currentPath}
            root={activeRoot?.path ?? currentPath}
            onNavigate={(path) => void browse(path)}
          />

          {uploadPercent !== null ? (
            <div className="row">
              <ProgressBar percent={uploadPercent} />
              <span className="small">{uploadPercent}%</span>
            </div>
          ) : null}

          {!writable ? (
            <p className="muted small">Каталог доступний лише для читання — запис вимкнено.</p>
          ) : null}

          {loadingEntries ? <Spinner label="Пристрій формує перелік…" /> : null}
          {!loadingEntries && entries.length === 0 ? <Empty>Каталог порожній.</Empty> : null}

          {!loadingEntries && entries.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Імʼя</th>
                  <th>Розмір</th>
                  <th>Змінено</th>
                  <th>Права</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.path}>
                    <td>
                      {entry.is_dir ? (
                        <button
                          type="button"
                          className="link"
                          onClick={() => void browse(entry.path)}
                        >
                          {entry.name}/
                        </button>
                      ) : (
                        <span>{entry.name}</span>
                      )}
                      {entry.is_symlink ? <Badge tone="info">симлінк</Badge> : null}
                    </td>
                    <td className="small">{entry.is_dir ? '—' : formatBytes(entry.size_bytes)}</td>
                    <td className="small">{formatDate(entry.modified_at)}</td>
                    <td className="small mono">{entry.permissions ?? '—'}</td>
                    <td className="actions">
                      {!entry.is_dir ? (
                        <button
                          type="button"
                          className="link"
                          disabled={busy || !entry.readable}
                          onClick={() => void download(entry)}
                        >
                          Отримати
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className="link"
                        disabled={busy || !writable}
                        onClick={() => rename(entry)}
                      >
                        Перейменувати
                      </button>
                      <button
                        type="button"
                        className="link danger"
                        disabled={busy || !writable}
                        onClick={() => remove(entry)}
                      >
                        Видалити
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </section>
      ) : null}

      {/* --------------------------------------------------- передачі */}
      <section className="card">
        <h2>Передачі</h2>
        {transfers.data && transfers.data.items.length === 0 ? (
          <Empty>Передач ще не було.</Empty>
        ) : null}

        {transfers.data && transfers.data.items.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>Файл</th>
                <th>Напрям</th>
                <th>Стан</th>
                <th>Прогрес</th>
                <th>Контрольна сума</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {transfers.data.items.map((transfer) => (
                <tr key={transfer.id}>
                  <td>
                    <strong>{transfer.filename}</strong>
                    <div className="muted small mono">{transfer.remote_path}</div>
                  </td>
                  <td className="small">
                    {transfer.direction === 'download' ? 'з пристрою' : 'на пристрій'}
                  </td>
                  <td>
                    <StatusBadge status={transfer.status} />
                    {transfer.error ? (
                      <div className="small danger-text">{transfer.error}</div>
                    ) : null}
                  </td>
                  <td style={{ minWidth: 140 }}>
                    <ProgressBar percent={transfer.progress_percent} />
                    <div className="muted small">
                      {formatBytes(transfer.received_bytes)}
                      {transfer.size_bytes ? ` / ${formatBytes(transfer.size_bytes)}` : ''}
                    </div>
                  </td>
                  <td className="small">
                    {transfer.checksum_verified ? (
                      <Badge tone="ok">перевірено</Badge>
                    ) : (
                      <Badge tone="muted">—</Badge>
                    )}
                    <div className="muted small mono">
                      {transfer.sha256 ? `${transfer.sha256.slice(0, 12)}…` : ''}
                    </div>
                  </td>
                  <td className="actions">
                    {transfer.direction === 'download' && transfer.status === 'completed' ? (
                      <button
                        type="button"
                        className="link"
                        onClick={() =>
                          void api.downloadTransfer(deviceId, transfer.id, transfer.filename)
                        }
                      >
                        Завантажити
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>

      {showRootForm ? (
        <RootForm
          deviceId={deviceId}
          onClose={() => setShowRootForm(false)}
          onCreated={async (path) => {
            setShowRootForm(false);
            setInfo(`Каталог ${path} додано до дозволених`);
            await roots.reload(true);
          }}
        />
      ) : null}
    </>
  );
}

function Breadcrumbs({
  path,
  root,
  onNavigate,
}: {
  path: string;
  root: string;
  onNavigate: (path: string) => void;
}) {
  // Показуємо шлях лише в межах дозволеного каталогу — вище підніматися нікуди.
  const relative = path === root ? '' : path.slice(root.length + 1);
  const parts = relative ? relative.split('/') : [];

  return (
    <div className="row small breadcrumbs">
      <button type="button" className="link" onClick={() => onNavigate(root)}>
        {root}
      </button>
      {parts.map((part, index) => {
        const target = `${root}/${parts.slice(0, index + 1).join('/')}`;
        return (
          <span key={target}>
            {' / '}
            <button type="button" className="link" onClick={() => onNavigate(target)}>
              {part}
            </button>
          </span>
        );
      })}
    </div>
  );
}

function RootForm({
  deviceId,
  onClose,
  onCreated,
}: {
  deviceId: string;
  onClose: () => void;
  onCreated: (path: string) => Promise<void>;
}) {
  const [path, setPath] = useState('/sdcard/');
  const [label, setLabel] = useState('');
  const [writable, setWritable] = useState(false);
  const [privileged, setPrivileged] = useState(false);
  const [limit, setLimit] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.addFileRoot(deviceId, {
        path,
        label: label || null,
        writable,
        allow_privileged: privileged,
        max_file_size_mb: limit ? Number(limit) : null,
      });
      await onCreated(path);
    } catch (createError) {
      setError(createError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Додати дозволений каталог" onClose={onClose}>
      <ErrorBanner error={error} />

      <label>
        Абсолютний шлях на пристрої
        <input value={path} onChange={(event) => setPath(event.target.value)} />
      </label>

      <label>
        Опис (необовʼязково)
        <input value={label} onChange={(event) => setLabel(event.target.value)} />
      </label>

      <label>
        Максимальний розмір файла, МБ (порожньо — глобальний ліміт)
        <input
          type="number"
          min={1}
          value={limit}
          onChange={(event) => setLimit(event.target.value)}
        />
      </label>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={writable}
          onChange={(event) => setWritable(event.target.checked)}
        />
        дозволити запис (upload, rename, delete, mkdir)
      </label>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={privileged}
          onChange={(event) => setPrivileged(event.target.checked)}
        />
        дозволити привілейований доступ (root) для цього каталогу
      </label>

      <p className="muted small">
        Привілейований доступ використовується лише на рутованих пристроях і лише як запасний
        варіант, коли звичайних прав не вистачає. Механізм кожної операції фіксується в
        результаті команди.
      </p>

      <button type="button" className="primary" disabled={busy || !path} onClick={submit}>
        {busy ? 'Додаємо…' : 'Додати'}
      </button>
    </Modal>
  );
}
