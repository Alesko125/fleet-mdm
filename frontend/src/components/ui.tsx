import type { ReactNode } from 'react';

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('uk-UA', { dateStyle: 'short', timeStyle: 'short' });
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return 'ніколи';
  const diffMs = Date.now() - new Date(value).getTime();
  if (Number.isNaN(diffMs)) return 'ніколи';
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return 'щойно';
  if (minutes < 60) return `${minutes} хв тому`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} год тому`;
  return `${Math.floor(hours / 24)} дн тому`;
}

type Tone = 'ok' | 'warn' | 'bad' | 'muted' | 'info';

export function Badge({ tone = 'muted', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

const COMMAND_TONES: Record<string, Tone> = {
  succeeded: 'ok',
  failed: 'bad',
  expired: 'bad',
  cancelled: 'muted',
  pending: 'info',
  dispatched: 'info',
  acked: 'warn',
};

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={COMMAND_TONES[status] ?? 'muted'}>{status}</Badge>;
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return <div className="banner banner-error">{message}</div>;
}

export function InfoBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return <div className="banner banner-info">{message}</div>;
}

export function Spinner({ label = 'Завантаження…' }: { label?: string }) {
  return <div className="spinner">{label}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ProgressBar({ percent }: { percent: number | null }) {
  if (percent === null) return <span className="muted">—</span>;
  return (
    <div className="progress" title={`${percent}%`}>
      <div className="progress-fill" style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div className="modal" onClick={(event) => event.stopPropagation()} role="dialog">
        <header className="modal-header">
          <h3>{title}</h3>
          <button type="button" className="link" onClick={onClose}>
            Закрити
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}
