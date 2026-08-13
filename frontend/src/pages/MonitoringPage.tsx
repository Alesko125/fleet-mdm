import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { AlertRule, RuleSchema, RuleType, Severity } from '../api/types';
import {
  Badge,
  Empty,
  ErrorBanner,
  InfoBanner,
  Modal,
  Spinner,
  formatDate,
  relativeTime,
} from '../components/ui';
import { useAdminSocket } from '../hooks/useAdminSocket';
import { useAsync } from '../hooks/useAsync';

const RULE_LABELS: Record<RuleType, string> = {
  DEVICE_OFFLINE: 'Пристрій офлайн',
  LOW_BATTERY: 'Низький заряд',
  LOW_STORAGE: 'Мало місця у сховищі',
  LOW_MEMORY: 'Мало оперативної памʼяті',
  HIGH_CPU: 'Високе навантаження CPU',
  OUTDATED_AGENT: 'Застаріла версія агента',
  OUTDATED_ANDROID: 'Застаріла версія Android',
  OUTDATED_APP: 'Застарілий застосунок',
  LOCATION_REPORTING_STOPPED: 'Геолокація не звітує',
  COMMAND_FAILURES: 'Повторні збої команд',
  ENROLLMENT_STATUS: 'Стан реєстрації',
};

const SEVERITY_TONES: Record<Severity, 'info' | 'warn' | 'bad'> = {
  info: 'info',
  warning: 'warn',
  critical: 'bad',
};

export default function MonitoringPage() {
  const [tab, setTab] = useState<'alerts' | 'rules'>('alerts');
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [state, setState] = useState('firing');
  const [showForm, setShowForm] = useState(false);

  const summary = useAsync(() => api.monitoringSummary(), [], { refreshMs: 30000 });
  const alerts = useAsync(() => api.alerts({ state: state || undefined }), [state], {
    refreshMs: 30000,
  });
  const rules = useAsync(() => api.alertRules(), []);
  const schema = useAsync(() => api.ruleSchema(), []);

  // Живі події: нове спрацювання одразу підтягує список.
  const connected = useAdminSocket(
    useCallback(
      (event) => {
        if (event.type === 'alert_raised' || event.type === 'alert_resolved') {
          void alerts.reload(true);
          void summary.reload(true);
        }
      },
      [alerts, summary],
    ),
  );

  const act = async (action: () => Promise<unknown>, message: string) => {
    setError(null);
    setInfo(null);
    try {
      await action();
      setInfo(message);
      await Promise.all([alerts.reload(true), summary.reload(true), rules.reload(true)]);
    } catch (actionError) {
      setError(actionError);
    }
  };

  const stats = summary.data;

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Моніторинг</h1>
          <div className="row small muted">
            <Badge tone={connected ? 'ok' : 'muted'}>
              {connected ? 'живі оновлення' : 'оновлення за таймером'}
            </Badge>
          </div>
        </div>
        <button
          type="button"
          onClick={() => void act(() => api.evaluateRules(), 'Правила переоцінено')}
        >
          Перевірити зараз
        </button>
      </header>

      <ErrorBanner error={error ?? summary.error} />
      <InfoBanner message={info} />

      {stats ? (
        <div className="tiles">
          <Tile label="Пристроїв" value={stats.devices_total} />
          <Tile label="Онлайн" value={stats.devices_online} tone="ok" />
          <Tile label="Офлайн" value={stats.devices_offline} tone={stats.devices_offline ? 'warn' : undefined} />
          <Tile
            label="Активних сповіщень"
            value={stats.alerts_firing}
            tone={stats.alerts_firing ? 'bad' : 'ok'}
          />
          <Tile label="Критичних" value={stats.alerts_by_severity.critical ?? 0} tone="bad" />
          <Tile label="Правил увімкнено" value={stats.rules_enabled} />
        </div>
      ) : null}

      <nav className="tabs">
        <button type="button" className={tab === 'alerts' ? 'active' : ''} onClick={() => setTab('alerts')}>
          Сповіщення
        </button>
        <button type="button" className={tab === 'rules' ? 'active' : ''} onClick={() => setTab('rules')}>
          Правила
        </button>
      </nav>

      {tab === 'alerts' ? (
        <>
          <div className="row toolbar">
            <select value={state} onChange={(event) => setState(event.target.value)}>
              <option value="firing">активні</option>
              <option value="acknowledged">підтверджені</option>
              <option value="resolved">закриті</option>
              <option value="">усі</option>
            </select>
          </div>

          <ErrorBanner error={alerts.error} />
          {alerts.loading && !alerts.data ? <Spinner /> : null}
          {alerts.data && alerts.data.items.length === 0 ? (
            <Empty>Сповіщень немає — усі пристрої в межах порогів.</Empty>
          ) : null}

          {alerts.data && alerts.data.items.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Правило</th>
                  <th>Пристрій</th>
                  <th>Повідомлення</th>
                  <th>Стан</th>
                  <th>Виникло</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {alerts.data.items.map((alert) => (
                  <tr key={alert.id}>
                    <td>
                      <Badge tone={SEVERITY_TONES[alert.severity]}>{alert.severity}</Badge>
                      <div className="small">{RULE_LABELS[alert.type] ?? alert.type}</div>
                    </td>
                    <td className="small">
                      <Link to={`/devices/${alert.device_id}`}>
                        {alert.device_name ?? alert.device_id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="small">{alert.message}</td>
                    <td>
                      <Badge
                        tone={
                          alert.state === 'firing'
                            ? 'bad'
                            : alert.state === 'acknowledged'
                              ? 'warn'
                              : 'ok'
                        }
                      >
                        {alert.state}
                      </Badge>
                    </td>
                    <td className="small">
                      {relativeTime(alert.first_seen_at)}
                      <div className="muted small">{formatDate(alert.first_seen_at)}</div>
                    </td>
                    <td className="actions">
                      {alert.state === 'firing' ? (
                        <button
                          type="button"
                          className="link"
                          onClick={() =>
                            void act(
                              () => api.acknowledgeAlert(alert.id),
                              'Сповіщення підтверджено',
                            )
                          }
                        >
                          Підтвердити
                        </button>
                      ) : null}
                      {alert.state !== 'resolved' ? (
                        <button
                          type="button"
                          className="link"
                          onClick={() =>
                            void act(() => api.resolveAlert(alert.id), 'Сповіщення закрито')
                          }
                        >
                          Закрити
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </>
      ) : null}

      {tab === 'rules' ? (
        <>
          <div className="row toolbar">
            <button type="button" onClick={() => setShowForm(true)}>
              Створити правило
            </button>
          </div>

          <ErrorBanner error={rules.error} />
          {rules.data && rules.data.length === 0 ? (
            <Empty>Правил ще немає. Створіть перше — наприклад, «пристрій офлайн».</Empty>
          ) : null}

          {rules.data && rules.data.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Назва</th>
                  <th>Тип</th>
                  <th>Пороги</th>
                  <th>Область</th>
                  <th>Стан</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rules.data.map((rule) => (
                  <tr key={rule.id}>
                    <td>
                      <strong>{rule.name}</strong>
                      <div className="muted small">{rule.description ?? ''}</div>
                    </td>
                    <td className="small">
                      <Badge tone={SEVERITY_TONES[rule.severity]}>{rule.severity}</Badge>
                      <div>{RULE_LABELS[rule.type] ?? rule.type}</div>
                    </td>
                    <td className="small mono">{JSON.stringify(rule.thresholds)}</td>
                    <td className="small">
                      {rule.device_id ? (
                        <Link to={`/devices/${rule.device_id}`}>один пристрій</Link>
                      ) : (
                        'усі пристрої'
                      )}
                      <div className="muted small">пауза {rule.cooldown_seconds} с</div>
                    </td>
                    <td>
                      <Badge tone={rule.enabled ? 'ok' : 'muted'}>
                        {rule.enabled ? 'увімкнено' : 'вимкнено'}
                      </Badge>
                    </td>
                    <td className="actions">
                      <button
                        type="button"
                        className="link"
                        onClick={() =>
                          void act(
                            () => api.updateRule(rule.id, { enabled: !rule.enabled }),
                            rule.enabled ? 'Правило вимкнено' : 'Правило увімкнено',
                          )
                        }
                      >
                        {rule.enabled ? 'Вимкнути' : 'Увімкнути'}
                      </button>
                      <button
                        type="button"
                        className="link danger"
                        onClick={() => {
                          if (!confirm(`Видалити правило «${rule.name}»?`)) return;
                          void act(() => api.deleteRule(rule.id), 'Правило видалено');
                        }}
                      >
                        Видалити
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </>
      ) : null}

      {showForm ? (
        <RuleForm
          schema={schema.data ?? []}
          onClose={() => setShowForm(false)}
          onCreated={async (name) => {
            setShowForm(false);
            setInfo(`Правило «${name}» створено`);
            await rules.reload(true);
            await summary.reload(true);
          }}
        />
      ) : null}
    </>
  );
}

function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: 'ok' | 'warn' | 'bad';
}) {
  return (
    <div className={`tile${tone ? ` tile-${tone}` : ''}`}>
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  );
}

function RuleForm({
  schema,
  onClose,
  onCreated,
}: {
  schema: RuleSchema[];
  onClose: () => void;
  onCreated: (name: string) => Promise<void>;
}) {
  const [type, setType] = useState<RuleType>('DEVICE_OFFLINE');
  const [name, setName] = useState('Пристрій офлайн');
  const [severity, setSeverity] = useState<Severity>('warning');
  const [cooldown, setCooldown] = useState(3600);
  const [thresholds, setThresholds] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const current = schema.find((item) => item.type === type);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      // Порожні поля не надсилаємо — на сервері діють значення за замовчуванням.
      const parsed: Record<string, unknown> = {};
      for (const [key, raw] of Object.entries(thresholds)) {
        if (raw === '') continue;
        const kind = current?.keys[key];
        if (kind === 'int') parsed[key] = Number.parseInt(raw, 10);
        else if (kind === 'float') parsed[key] = Number.parseFloat(raw);
        else if (kind === 'bool') parsed[key] = raw === 'true';
        else if (kind === 'list') parsed[key] = raw.split(',').map((item) => item.trim()).filter(Boolean);
        else parsed[key] = raw;
      }

      await api.createRule({
        name,
        type,
        severity,
        cooldown_seconds: cooldown,
        thresholds: parsed,
      } as Partial<AlertRule>);
      await onCreated(name);
    } catch (createError) {
      setError(createError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Нове правило моніторингу" onClose={onClose}>
      <ErrorBanner error={error} />

      <label>
        Тип
        <select
          value={type}
          onChange={(event) => {
            const next = event.target.value as RuleType;
            setType(next);
            setName(RULE_LABELS[next]);
            setThresholds({});
          }}
        >
          {schema.map((item) => (
            <option key={item.type} value={item.type}>
              {RULE_LABELS[item.type] ?? item.type}
            </option>
          ))}
        </select>
      </label>

      <label>
        Назва
        <input value={name} onChange={(event) => setName(event.target.value)} />
      </label>

      <label>
        Важливість
        <select value={severity} onChange={(event) => setSeverity(event.target.value as Severity)}>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="critical">critical</option>
        </select>
      </label>

      {current
        ? Object.entries(current.keys).map(([key, kind]) => (
            <label key={key}>
              {key} <span className="muted small">({kind})</span>
              {kind === 'bool' ? (
                <select
                  value={thresholds[key] ?? String(current.defaults[key] ?? 'true')}
                  onChange={(event) =>
                    setThresholds({ ...thresholds, [key]: event.target.value })
                  }
                >
                  <option value="true">так</option>
                  <option value="false">ні</option>
                </select>
              ) : (
                <input
                  value={thresholds[key] ?? ''}
                  placeholder={String(current.defaults[key] ?? '')}
                  onChange={(event) =>
                    setThresholds({ ...thresholds, [key]: event.target.value })
                  }
                />
              )}
            </label>
          ))
        : null}

      <label>
        Пауза між повтореннями, с
        <input
          type="number"
          min={0}
          value={cooldown}
          onChange={(event) => setCooldown(Number(event.target.value))}
        />
      </label>

      <button type="button" className="primary" disabled={busy || !name} onClick={submit}>
        {busy ? 'Створюємо…' : 'Створити'}
      </button>
    </Modal>
  );
}
