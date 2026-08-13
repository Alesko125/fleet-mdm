import { useState } from 'react';
import { api } from '../api/client';
import type { CommandSpec } from '../api/types';
import { Badge, ErrorBanner, InfoBanner, Spinner } from '../components/ui';
import { useAsync } from '../hooks/useAsync';

/**
 * Дії над пристроєм, побудовані з реєстру команд.
 *
 * Кнопки не «зашиті» в код: перелік приходить із сервера разом із тим, хто
 * має право на кожну дію та що побачить власник пристрою. Якщо адміністратор
 * перенастроїть матрицю або вимкне команду, панель це одразу відобразить.
 *
 * Тут лише команди рівня пристрою; дії над застосунками живуть на вкладці
 * «Застосунки», бо потребують вибору пакета.
 */

/** Команди пристрою — у порядку від найбезпечнішої до найпомітнішої. */
const DEVICE_COMMANDS = [
  'REFRESH_TELEMETRY',
  'SYNC_CONFIG',
  'COLLECT_DIAGNOSTICS',
  'AGENT_RESTART',
  'REBOOT',
];

export default function DeviceActions({
  deviceId,
  managementMode,
}: {
  deviceId: string;
  managementMode: string;
}) {
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<CommandSpec | null>(null);

  const registry = useAsync(() => api.commandRegistry(), []);
  const specs = (registry.data ?? []).filter((spec) =>
    DEVICE_COMMANDS.includes(spec.type),
  );

  const run = async (spec: CommandSpec) => {
    setBusy(spec.type);
    setError(null);
    setInfo(null);
    setConfirming(null);
    try {
      const command = await api.issueCommand(deviceId, spec.type);
      setInfo(`«${spec.title}» надіслано (${command.id.slice(0, 8)})`);
    } catch (runError) {
      setError(runError);
    } finally {
      setBusy(null);
    }
  };

  /** Помітні дії просимо підтвердити — і показуємо наслідок для користувача. */
  const trigger = (spec: CommandSpec) => {
    if (spec.disruptive) {
      setConfirming(spec);
      return;
    }
    void run(spec);
  };

  if (registry.loading && !registry.data) return <Spinner />;

  return (
    <section className="card">
      <h2>Дії над пристроєм</h2>
      <p className="muted small">
        Перелік команд закритий: виконати щось поза ним неможливо. Кожна дія
        записується в аудит разом із тим, хто її видав.
      </p>

      <ErrorBanner error={error ?? registry.error} />
      <InfoBanner message={info} />

      <div className="row toolbar">
        {specs.map((spec) => {
          const blockedByMode =
            spec.requires_device_owner && managementMode !== 'device_owner';
          const disabled =
            busy !== null || !spec.allowed_for_me || !spec.enabled || blockedByMode;

          return (
            <button
              key={spec.type}
              type="button"
              className={spec.disruptive ? 'danger' : ''}
              disabled={disabled}
              title={reasonFor(spec, blockedByMode)}
              onClick={() => trigger(spec)}
            >
              {busy === spec.type ? 'Надсилаємо…' : spec.title}
            </button>
          );
        })}
      </div>

      {specs.some((spec) => spec.requires_device_owner) &&
      managementMode !== 'device_owner' ? (
        <p className="muted small">
          Пристрій у режимі <code>{managementMode}</code>: команди, що спираються
          на <code>DevicePolicyManager</code>, недоступні. Це обмеження Android,
          а не налаштування панелі.
        </p>
      ) : null}

      {confirming ? (
        <div className="card nested">
          <h3>Підтвердьте дію</h3>
          <p className="small">
            <strong>{confirming.title}</strong> — {confirming.description}
          </p>
          <p className="small">
            <Badge tone="warn">користувач це побачить</Badge> {confirming.user_visible}
          </p>
          <div className="row">
            <button type="button" className="danger" onClick={() => void run(confirming)}>
              Так, виконати
            </button>
            <button type="button" onClick={() => setConfirming(null)}>
              Скасувати
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function reasonFor(spec: CommandSpec, blockedByMode: boolean): string {
  if (!spec.enabled) return 'Команду вимкнено адміністратором';
  if (!spec.allowed_for_me) {
    return `Потрібен дозвіл ${spec.required_permission}`;
  }
  if (blockedByMode) return 'Потрібен режим Device Owner';
  return spec.description;
}
