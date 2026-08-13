import { useRef, useState } from 'react';
import { api } from '../api/client';
import type { Apk } from '../api/types';
import {
  Badge,
  Empty,
  ErrorBanner,
  InfoBanner,
  Modal,
  ProgressBar,
  Spinner,
  formatBytes,
  formatDate,
} from '../components/ui';
import { useAsync } from '../hooks/useAsync';

export default function ApkPage() {
  const [search, setSearch] = useState('');
  const [notes, setNotes] = useState('');
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [details, setDetails] = useState<Apk | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const { data, loading, error: loadError, reload } = useAsync(
    () => api.apks({ search: search || undefined }),
    [search],
  );

  const upload = async (file: File) => {
    setError(null);
    setInfo(null);
    setUploadPercent(0);
    try {
      const result = await api.uploadApk(file, notes, setUploadPercent);
      setInfo(
        result.deduplicated
          ? `Такий файл уже був у репозиторії: ${result.package_name} ${result.display_version}`
          : `Додано ${result.package_name} ${result.display_version} (${result.architecture})`,
      );
      setNotes('');
      await reload();
    } catch (uploadError) {
      setError(uploadError);
    } finally {
      setUploadPercent(null);
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  const remove = async (apk: Apk) => {
    if (!confirm(`Видалити ${apk.filename} з репозиторію?`)) return;
    setError(null);
    try {
      await api.deleteApk(apk.id);
      setInfo('APK видалено з репозиторію');
      await reload();
    } catch (deleteError) {
      setError(deleteError);
    }
  };

  return (
    <>
      <header className="page-header">
        <h1>Репозиторій APK</h1>
        <input
          type="search"
          placeholder="Пошук за пакетом або файлом"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </header>

      <section className="card">
        <h2>Завантажити APK</h2>
        <p className="muted small">
          Сервер сам обчислює SHA-256 і читає AndroidManifest.xml: пакет, версію, мінімальний
          SDK, архітектуру та перелік дозволів. Split-APK не приймаються.
        </p>
        <div className="row">
          <input
            ref={fileInput}
            type="file"
            accept=".apk,application/vnd.android.package-archive"
            disabled={uploadPercent !== null}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void upload(file);
            }}
          />
          <input
            type="text"
            placeholder="Примітка (необовʼязково)"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
        </div>
        {uploadPercent !== null ? (
          <div className="row">
            <ProgressBar percent={uploadPercent} />
            <span className="small">{uploadPercent}%</span>
          </div>
        ) : null}
      </section>

      <ErrorBanner error={error ?? loadError} />
      <InfoBanner message={info} />

      {loading && !data ? <Spinner /> : null}
      {data && data.items.length === 0 ? <Empty>Репозиторій порожній.</Empty> : null}

      {data && data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Пакет</th>
              <th>Версія</th>
              <th>Архітектура</th>
              <th>SDK</th>
              <th>Розмір</th>
              <th>Додано</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.items.map((apk) => (
              <tr key={apk.id}>
                <td>
                  <strong>{apk.package_name ?? '—'}</strong>
                  <div className="muted small">{apk.filename}</div>
                </td>
                <td>{apk.display_version}</td>
                <td className="small">{apk.architecture}</td>
                <td className="small">
                  min {apk.min_sdk ?? '—'} / target {apk.target_sdk ?? '—'}
                </td>
                <td className="small">{formatBytes(apk.size_bytes)}</td>
                <td className="small">{formatDate(apk.created_at)}</td>
                <td className="actions">
                  <button type="button" className="link" onClick={() => setDetails(apk)}>
                    Деталі
                  </button>
                  <button
                    type="button"
                    className="link"
                    onClick={() => void api.downloadApk(apk.id, apk.filename)}
                  >
                    Завантажити
                  </button>
                  <button type="button" className="link danger" onClick={() => void remove(apk)}>
                    Видалити
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {details ? (
        <Modal title={details.filename} onClose={() => setDetails(null)}>
          <dl className="details">
            <dt>Пакет</dt>
            <dd>{details.package_name ?? '—'}</dd>
            <dt>Версія</dt>
            <dd>{details.display_version}</dd>
            <dt>SHA-256</dt>
            <dd className="mono small">{details.sha256}</dd>
            <dt>Розмір</dt>
            <dd>{formatBytes(details.size_bytes)}</dd>
            <dt>Архітектура</dt>
            <dd>{details.architecture}</dd>
            <dt>SDK</dt>
            <dd>
              min {details.min_sdk ?? '—'}, target {details.target_sdk ?? '—'}
            </dd>
            <dt>Підпис</dt>
            <dd>
              {details.signature_schemes?.length
                ? details.signature_schemes.join(', ')
                : 'не виявлено'}
            </dd>
            <dt>Дозволи</dt>
            <dd>
              {details.permissions?.length ? (
                <ul className="permissions">
                  {details.permissions.map((permission) => (
                    <li key={permission}>{permission}</li>
                  ))}
                </ul>
              ) : (
                'не запитує'
              )}
            </dd>
            <dt>Примітка</dt>
            <dd>{details.notes ?? '—'}</dd>
          </dl>
          {details.is_split ? <Badge tone="warn">split-APK</Badge> : null}
        </Modal>
      ) : null}
    </>
  );
}
