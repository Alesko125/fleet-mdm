import { useState } from 'react';
import { api } from '../api/client';
import type { CreatedEnrollmentToken } from '../api/types';
import { QrCode } from '../components/QrCode';
import { Badge, Empty, ErrorBanner, Spinner, formatDate, relativeTime } from '../components/ui';
import { useAsync } from '../hooks/useAsync';

// Термін дії у формі. Значення в межах, які приймає сервер (5 хв … 30 діб).
const TTL_OPTIONS = [
  { label: '1 година', seconds: 3600 },
  { label: '24 години', seconds: 86400 },
  { label: '7 днів', seconds: 7 * 86400 },
  { label: '30 днів', seconds: 30 * 86400 },
];

export default function EnrollmentPage() {
  const canManage = api.can('enrollment:manage');
  const [label, setLabel] = useState('');
  const [maxUses, setMaxUses] = useState(1);
  const [ttl, setTtl] = useState(86400);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<unknown>(null);
  // Відкритий токен сервер віддає рівно один раз — тримаємо його лише в стані
  // сторінки. Перезавантаження вкладки його втрачає, і це навмисно.
  const [created, setCreated] = useState<CreatedEnrollmentToken | null>(null);

  const list = useAsync(() => api.enrollmentTokens(), [], { refreshMs: 30000 });

  const create = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      const token = await api.createEnrollmentToken({
        label: label.trim(),
        maxUses,
        ttlSeconds: ttl,
      });
      setCreated(token);
      setLabel('');
      await list.reload();
    } catch (error) {
      setCreateError(error);
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (id: string) => {
    await api.revokeEnrollmentToken(id);
    await list.reload();
  };

  if (!canManage) {
    return (
      <>
        <header className="page-header">
          <h1>Реєстрація пристроїв</h1>
        </header>
        <Empty>Ваша роль не має дозволу enrollment:manage.</Empty>
      </>
    );
  }

  return (
    <>
      <header className="page-header">
        <h1>Реєстрація пристроїв</h1>
        <button type="button" onClick={() => void list.reload()}>
          Оновити
        </button>
      </header>

      <div className="card">
        <h3>Новий токен</h3>
        <div className="row">
          <input
            type="text"
            placeholder="Назва (для кого цей токен)"
            value={label}
            maxLength={200}
            onChange={(event) => setLabel(event.target.value)}
          />
          <label className="small muted">
            використань
            <input
              type="number"
              min={1}
              max={1000}
              value={maxUses}
              onChange={(event) => setMaxUses(Math.max(1, Number(event.target.value) || 1))}
              style={{ width: '5rem', marginLeft: '0.5rem' }}
            />
          </label>
          <select value={ttl} onChange={(event) => setTtl(Number(event.target.value))}>
            {TTL_OPTIONS.map((option) => (
              <option key={option.seconds} value={option.seconds}>
                діє {option.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="primary"
            disabled={creating}
            onClick={() => void create()}
          >
            {creating ? 'Створюємо…' : 'Створити токен'}
          </button>
        </div>
        <ErrorBanner error={createError} />
      </div>

      {created ? <CreatedToken token={created} onHide={() => setCreated(null)} /> : null}

      <h3>Видані токени</h3>
      <ErrorBanner error={list.error} />
      {list.loading && !list.data ? <Spinner /> : null}
      {list.data && list.data.items.length === 0 ? <Empty>Токенів ще немає.</Empty> : null}

      {list.data && list.data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Токен</th>
              <th>Назва</th>
              <th>Використань</th>
              <th>Діє до</th>
              <th>Стан</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.data.items.map((token) => {
              const expired = new Date(token.expires_at).getTime() < Date.now();
              const exhausted = token.use_count >= token.max_uses;
              const active = !token.revoked_at && !expired && !exhausted;
              return (
                <tr key={token.id}>
                  <td className="mono small">{token.token_prefix}…</td>
                  <td className="small">{token.label ?? '—'}</td>
                  <td className="small">
                    {token.use_count} / {token.max_uses}
                  </td>
                  <td className="small">
                    {formatDate(token.expires_at)}
                    <div className="muted small">{relativeTime(token.expires_at)}</div>
                  </td>
                  <td>
                    {token.revoked_at ? (
                      <Badge tone="bad">відкликано</Badge>
                    ) : expired ? (
                      <Badge tone="muted">прострочено</Badge>
                    ) : exhausted ? (
                      <Badge tone="muted">вичерпано</Badge>
                    ) : (
                      <Badge tone="ok">активний</Badge>
                    )}
                  </td>
                  <td>
                    {active ? (
                      <button
                        type="button"
                        className="link danger"
                        onClick={() => void revoke(token.id)}
                      >
                        Відкликати
                      </button>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : null}
    </>
  );
}

function CreatedToken({ token, onHide }: { token: CreatedEnrollmentToken; onHide: () => void }) {
  const [copied, setCopied] = useState<string | null>(null);
  const origin = new URL(token.enroll_url).origin;

  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
    } catch {
      // Без HTTPS браузер не дає доступу до буфера — лишається виділити вручну.
      setCopied(null);
    }
  };

  return (
    <div className="card enroll-created">
      <div className="enroll-qr">
        <QrCode value={token.qr_payload} />
        <div className="muted small">В агенті: «Сканувати QR»</div>
      </div>

      <div className="enroll-details">
        <h3>Токен створено</h3>
        <p className="danger-text small">
          Відкритий токен показується один раз — у базі лишається тільки його хеш.
          Відскануйте або скопіюйте зараз.
        </p>

        <dl className="details">
          <dt>Токен</dt>
          <dd>
            <span className="mono">{token.token}</span>{' '}
            <button type="button" className="link" onClick={() => void copy(token.token, 'токен')}>
              копіювати
            </button>
          </dd>
          <dt>Адреса сервера</dt>
          <dd>
            <span className="mono">{origin}</span>{' '}
            <button type="button" className="link" onClick={() => void copy(origin, 'адресу')}>
              копіювати
            </button>
          </dd>
          <dt>Діє до</dt>
          <dd className="small">{formatDate(token.expires_at)}</dd>
        </dl>

        {copied ? <div className="muted small">Скопійовано: {copied}.</div> : null}

        <p className="muted small">
          Агент приймає лише HTTPS. Виняток — <span className="mono">localhost</span> і{' '}
          <span className="mono">10.0.2.2</span> (емулятор): обмеження навмисне, щоб токен не
          пішов мережею відкритим текстом. Адреса в QR береться з{' '}
          <span className="mono">PUBLIC_BASE_URL</span>.
        </p>

        <button type="button" onClick={onHide}>
          Сховати
        </button>
      </div>
    </div>
  );
}
