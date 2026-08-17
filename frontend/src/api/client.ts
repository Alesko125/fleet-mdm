import type {
  Apk,
  AuditEntry,
  Command,
  CreatedEnrollmentToken,
  Device,
  DeviceApp,
  Alert,
  AlertRule,
  CommandDetail,
  CommandSpec,
  CallLogEntry,
  CommandStats,
  CommsSettings,
  CommsSettingsUpdate,
  ConnectionStatus,
  CreatedSupportSession,
  DeviceCredentials,
  DeviceHealth,
  DeviceKeyHistoryEntry,
  DeviceKeyOverview,
  EnrollmentToken,
  FileEntry,
  FileRoot,
  FileTransfer,
  LocationFix,
  LocationOverview,
  LocationSettings,
  LocationSettingsUpdate,
  MonitoringSummary,
  Page,
  RuleSchema,
  SecretRotationStatus,
  SessionAuditEntry,
  SmsMessage,
  SupportSession,
  TokenPair,
  User,
  ViewerTicket,
} from './types';

const API = '/api/v1';
const CSRF_COOKIE = 'fleet_csrf';
const CSRF_HEADER = 'X-CSRF-Token';
/** Позначка «сесія була» — самого токена тут немає й бути не може. */
const SESSION_FLAG = 'fleet.session';

/** Читає значення cookie, доступної скриптам (CSRF — саме така). */
function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) return decodeURIComponent(trimmed.slice(prefix.length));
  }
  return null;
}

/**
 * Помилка API у форматі бекенду: {"error": {code, message, details}}.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

type Listener = (authenticated: boolean) => void;

class ApiClient {
  /**
   * Access-токен живе лише в памʼяті вкладки.
   *
   * Refresh-токен сюди не потрапляє взагалі: сервер віддає його у cookie
   * `HttpOnly; Secure; SameSite=strict`, тож ані цей код, ані впроваджений
   * XSS-скрипт прочитати чи вивезти його не можуть. У localStorage лишається
   * тільки прапорець «сесія колись була» — щоб після перезавантаження
   * сторінки одразу спробувати оновлення, а не показувати форму входу.
   */
  private accessToken: string | null = null;
  private refreshPromise: Promise<void> | null = null;
  private listeners = new Set<Listener>();
  private permissions = new Set<string>();

  private get hasSession(): boolean {
    return localStorage.getItem(SESSION_FLAG) === '1';
  }

  get isAuthenticated(): boolean {
    return this.accessToken !== null || this.hasSession;
  }

  /** Дозволи поточної ролі — панель ховає недоступні дії. */
  can(permission: string): boolean {
    return this.permissions.has(permission);
  }

  grantedPermissions(): string[] {
    return [...this.permissions];
  }

  onAuthChange(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit() {
    for (const listener of this.listeners) listener(this.isAuthenticated);
  }

  private storeTokens(pair: TokenPair) {
    this.accessToken = pair.access_token;
    this.permissions = new Set(pair.permissions ?? []);
    localStorage.setItem(SESSION_FLAG, '1');
    this.emit();
  }

  clearTokens() {
    this.accessToken = null;
    this.permissions = new Set();
    localStorage.removeItem(SESSION_FLAG);
    this.emit();
  }

  // --------------------------------------------------------- транспорт ---

  private async parse<T>(response: Response): Promise<T> {
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    const body = text ? JSON.parse(text) : {};
    if (!response.ok) {
      const error = body?.error ?? {};
      throw new ApiError(
        response.status,
        error.code ?? 'error',
        error.message ?? `Помилка ${response.status}`,
        error.details ?? {},
      );
    }
    return body as T;
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    retry = true,
  ): Promise<T> {
    const headers = new Headers(init.headers);
    if (this.accessToken) headers.set('Authorization', `Bearer ${this.accessToken}`);
    if (init.body && !(init.body instanceof FormData)) {
      headers.set('Content-Type', 'application/json');
    }
    // Double-submit: cookie браузер надсилає сам, заголовок — лише наш код,
    // тож стороння сторінка запит підробити не зможе.
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers.set(CSRF_HEADER, csrf);

    const response = await fetch(`${API}${path}`, {
      ...init,
      headers,
      // Cookie сесії має летіти з запитом; крос-доменних викликів у панелі немає.
      credentials: 'same-origin',
    });

    if (response.status === 401 && retry && this.hasSession) {
      await this.refreshSession();
      return this.request<T>(path, init, false);
    }
    return this.parse<T>(response);
  }

  /**
   * Завантаження файла під автентифікацією.
   *
   * Звичайне `<a href>` пішло б без заголовка Authorization і отримало 401,
   * тож файл забираємо fetch-ом і віддаємо браузеру як blob. Токен у
   * посиланні (варіант, до якого тут легко скотитися) осідав би в історії
   * браузера й логах проксі — саме тому так не робимо.
   */
  async downloadFile(path: string, filename: string): Promise<void> {
    const headers = new Headers();
    if (!this.accessToken) await this.refreshSession();
    if (this.accessToken) headers.set('Authorization', `Bearer ${this.accessToken}`);

    let response = await fetch(`${API}${path}`, { headers, credentials: 'same-origin' });
    if (response.status === 401 && this.hasSession) {
      await this.refreshSession();
      headers.set('Authorization', `Bearer ${this.accessToken}`);
      response = await fetch(`${API}${path}`, { headers, credentials: 'same-origin' });
    }
    if (!response.ok) {
      throw new ApiError(response.status, 'download_failed', `Не вдалося завантажити (${response.status})`);
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    try {
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  private get<T>(path: string) {
    return this.request<T>(path);
  }

  private post<T>(path: string, body?: unknown) {
    return this.request<T>(path, {
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  // ------------------------------------------------------ автентифікація --

  async login(email: string, password: string): Promise<TokenPair> {
    const pair = await this.request<TokenPair>(
      '/auth/login',
      { method: 'POST', body: JSON.stringify({ email, password }) },
      false,
    );
    this.storeTokens(pair);
    return pair;
  }

  /**
   * Оновлення пари токенів; паралельні виклики чекають на один запит.
   *
   * Refresh-токен у тілі не передається — його надсилає браузер cookie-ю.
   */
  async refreshSession(): Promise<void> {
    if (this.refreshPromise) return this.refreshPromise;
    if (!this.hasSession) {
      this.clearTokens();
      throw new ApiError(401, 'unauthenticated', 'Сесія завершена');
    }

    this.refreshPromise = (async () => {
      try {
        const pair = await this.request<TokenPair>('/auth/refresh', { method: 'POST' }, false);
        this.storeTokens(pair);
      } catch (error) {
        this.clearTokens();
        throw error;
      } finally {
        this.refreshPromise = null;
      }
    })();

    return this.refreshPromise;
  }

  async logout(): Promise<void> {
    try {
      await this.post('/auth/logout');
    } catch {
      // Навіть якщо сервер не відповів — локальний стан прибираємо.
    } finally {
      this.clearTokens();
    }
  }

  /** Завершує сесії на всіх пристроях. */
  async logoutEverywhere(): Promise<void> {
    try {
      await this.post('/auth/logout-all');
    } finally {
      this.clearTokens();
    }
  }

  async changePassword(currentPassword: string, newPassword: string) {
    return this.post<{ message: string }>('/auth/password', {
      current_password: currentPassword,
      new_password: newPassword,
    });
  }

  async me(): Promise<User> {
    const user = await this.get<User>('/auth/me');
    if (user.permissions) this.permissions = new Set(user.permissions);
    return user;
  }

  // ------------------------------------------------------------ пристрої --

  devices(params: { search?: string; limit?: number } = {}) {
    const query = new URLSearchParams();
    if (params.search) query.set('search', params.search);
    query.set('limit', String(params.limit ?? 100));
    return this.get<Page<Device>>(`/devices?${query}`);
  }

  device(id: string) {
    return this.get<Device>(`/devices/${id}`);
  }

  // ------------------------------------------------------ репозиторій APK -

  apks(params: { search?: string; package?: string } = {}) {
    const query = new URLSearchParams();
    if (params.search) query.set('search', params.search);
    if (params.package) query.set('package', params.package);
    query.set('limit', '200');
    return this.get<Page<Apk>>(`/apks?${query}`);
  }

  /** Вивантаження APK із відстеженням прогресу (XHR — fetch його не дає). */
  uploadApk(file: File, notes: string, onProgress?: (percent: number) => void): Promise<Apk> {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append('file', file);
      if (notes) form.append('notes', notes);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API}/apks`);
      if (this.accessToken) {
        xhr.setRequestHeader('Authorization', `Bearer ${this.accessToken}`);
      }
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(Math.round((event.loaded / event.total) * 100));
        }
      };
      xhr.onload = () => {
        const body = xhr.responseText ? JSON.parse(xhr.responseText) : {};
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(body as Apk);
        } else {
          const error = body?.error ?? {};
          reject(
            new ApiError(
              xhr.status,
              error.code ?? 'error',
              error.message ?? `Помилка ${xhr.status}`,
              error.details ?? {},
            ),
          );
        }
      };
      xhr.onerror = () => reject(new ApiError(0, 'network', 'Мережева помилка'));
      xhr.send(form);
    });
  }

  deleteApk(id: string) {
    return this.request<{ message: string }>(`/apks/${id}`, { method: 'DELETE' });
  }

  updateApk(id: string, body: { notes?: string; label?: string }) {
    return this.request<Apk>(`/apks/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
  }

  /** Завантажує APK із заголовком Authorization (не через голий href). */
  downloadApk(id: string, filename: string) {
    return this.downloadFile(`/apks/${id}/download`, filename);
  }

  // ------------------------------------------------ застосунки пристрою ---

  deviceApps(deviceId: string) {
    return this.get<DeviceApp[]>(`/devices/${deviceId}/apps`);
  }

  refreshApps(deviceId: string) {
    return this.post<Command>(`/devices/${deviceId}/apps/refresh`);
  }

  installApp(deviceId: string, apkId: string, allowDowngrade = false) {
    return this.post<Command>(`/devices/${deviceId}/apps/install`, {
      apk_id: apkId,
      allow_downgrade: allowDowngrade,
    });
  }

  uninstallApp(deviceId: string, packageName: string) {
    return this.post<Command>(`/devices/${deviceId}/apps/uninstall`, {
      package_name: packageName,
    });
  }

  launchApp(deviceId: string, packageName: string) {
    return this.post<Command>(`/devices/${deviceId}/apps/launch`, {
      package_name: packageName,
    });
  }

  // --------------------------------------------------------- геолокація ---

  locationOverview(deviceId: string) {
    return this.get<LocationOverview>(`/devices/${deviceId}/location`);
  }

  updateLocationSettings(deviceId: string, body: LocationSettingsUpdate) {
    return this.request<LocationSettings>(`/devices/${deviceId}/location/settings`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  locationHistory(deviceId: string, params: { since?: string; limit?: number } = {}) {
    const query = new URLSearchParams();
    if (params.since) query.set('since', params.since);
    query.set('limit', String(params.limit ?? 500));
    return this.get<Page<LocationFix>>(`/devices/${deviceId}/location/history?${query}`);
  }

  requestLocationNow(deviceId: string) {
    return this.post<Command>(`/devices/${deviceId}/location/request`);
  }

  purgeLocationHistory(deviceId: string) {
    return this.request<{ message: string }>(`/devices/${deviceId}/location/history`, {
      method: 'DELETE',
    });
  }

  // -------------------------------------------------------------- файли ---

  fileRoots(deviceId: string) {
    return this.get<FileRoot[]>(`/devices/${deviceId}/files/roots`);
  }

  addFileRoot(
    deviceId: string,
    body: {
      path: string;
      label?: string | null;
      writable: boolean;
      max_file_size_mb?: number | null;
      allow_privileged: boolean;
    },
  ) {
    return this.post<FileRoot>(`/devices/${deviceId}/files/roots`, body);
  }

  deleteFileRoot(deviceId: string, rootId: string) {
    return this.request<{ message: string }>(`/devices/${deviceId}/files/roots/${rootId}`, {
      method: 'DELETE',
    });
  }

  /**
   * Перелік каталогу виконує пристрій, тому запит асинхронний: створюємо
   * команду й чекаємо на її результат.
   */
  async listDirectory(deviceId: string, path: string, timeoutMs = 45000): Promise<FileEntry[]> {
    const command = await this.post<Command>(`/devices/${deviceId}/files/list`, { path });
    const deadline = Date.now() + timeoutMs;

    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1200));
      const current = await this.get<Command>(`/commands/${command.id}`);
      if (current.status === 'succeeded') {
        return ((current.result?.entries as FileEntry[]) ?? []).slice();
      }
      if (['failed', 'expired', 'cancelled'].includes(current.status)) {
        throw new ApiError(409, 'command_failed', current.error ?? 'Пристрій не виконав запит');
      }
    }
    throw new ApiError(504, 'timeout', 'Пристрій не відповів вчасно — спробуйте ще раз');
  }

  makeDirectory(deviceId: string, path: string, name: string) {
    return this.post<Command>(`/devices/${deviceId}/files/mkdir`, { path, name });
  }

  renameEntry(deviceId: string, path: string, name: string) {
    return this.post<Command>(`/devices/${deviceId}/files/rename`, { path, name });
  }

  deleteEntry(deviceId: string, path: string, recursive: boolean) {
    return this.post<Command>(`/devices/${deviceId}/files/delete`, { path, recursive });
  }

  requestFileDownload(deviceId: string, path: string) {
    return this.post<FileTransfer>(`/devices/${deviceId}/files/download`, { path });
  }

  fileTransfers(deviceId: string) {
    return this.get<Page<FileTransfer>>(`/devices/${deviceId}/files/transfers?limit=50`);
  }

  downloadTransfer(deviceId: string, transferId: string, filename: string) {
    return this.downloadFile(
      `/devices/${deviceId}/files/transfers/${transferId}/content`,
      filename,
    );
  }

  /** Вивантаження файла на пристрій із відстеженням прогресу. */
  uploadFileToDevice(
    deviceId: string,
    file: File,
    targetDir: string,
    onProgress?: (percent: number) => void,
  ): Promise<FileTransfer> {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append('file', file);
      form.append('target_dir', targetDir);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API}/devices/${deviceId}/files/upload`);
      if (this.accessToken) {
        xhr.setRequestHeader('Authorization', `Bearer ${this.accessToken}`);
      }
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(Math.round((event.loaded / event.total) * 100));
        }
      };
      xhr.onload = () => {
        const body = xhr.responseText ? JSON.parse(xhr.responseText) : {};
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(body as FileTransfer);
        } else {
          const error = body?.error ?? {};
          reject(
            new ApiError(
              xhr.status,
              error.code ?? 'error',
              error.message ?? `Помилка ${xhr.status}`,
              error.details ?? {},
            ),
          );
        }
      };
      xhr.onerror = () => reject(new ApiError(0, 'network', 'Мережева помилка'));
      xhr.send(form);
    });
  }

  // ---------------------------------------------------------- моніторинг ---

  monitoringSummary() {
    return this.get<MonitoringSummary>('/monitoring/summary');
  }

  deviceHealth() {
    return this.get<DeviceHealth[]>('/monitoring/devices');
  }

  alerts(params: { state?: string; severity?: string; type?: string; deviceId?: string } = {}) {
    const query = new URLSearchParams();
    if (params.state) query.set('state', params.state);
    if (params.severity) query.set('severity', params.severity);
    if (params.type) query.set('type', params.type);
    if (params.deviceId) query.set('device_id', params.deviceId);
    query.set('limit', '200');
    return this.get<Page<Alert>>(`/alerts?${query}`);
  }

  acknowledgeAlert(id: string) {
    return this.post<Alert>(`/alerts/${id}/acknowledge`);
  }

  resolveAlert(id: string) {
    return this.post<Alert>(`/alerts/${id}/resolve`);
  }

  alertRules() {
    return this.get<AlertRule[]>('/alert-rules');
  }

  ruleSchema() {
    return this.get<RuleSchema[]>('/alert-rules/schema');
  }

  createRule(body: Partial<AlertRule>) {
    return this.post<AlertRule>('/alert-rules', body);
  }

  updateRule(id: string, body: Partial<AlertRule>) {
    return this.request<AlertRule>(`/alert-rules/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  deleteRule(id: string) {
    return this.request<{ message: string }>(`/alert-rules/${id}`, { method: 'DELETE' });
  }

  evaluateRules() {
    return this.post<{ devices: number; rules: number; raised: number; resolved: number }>(
      '/monitoring/evaluate',
    );
  }

  /**
   * URL WebSocket-каналу панелі. Токен передається параметром, бо браузерний
   * WebSocket не дозволяє задати заголовок Authorization; канал — лише для
   * читання, тож дій в обхід RBAC через нього виконати не можна.
   */
  async adminSocketUrl(): Promise<string> {
    if (!this.accessToken) await this.refreshSession();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/ws/admin?token=${this.accessToken}`;
  }

  // -------------------------------------------------- віддалена підтримка --

  createSupportSession(
    deviceId: string,
    body: { reason: string; input_enabled: boolean; max_duration_minutes: number },
  ) {
    return this.post<CreatedSupportSession>(`/devices/${deviceId}/support-sessions`, body);
  }

  supportSessions(params: { deviceId?: string; state?: string } = {}) {
    const query = new URLSearchParams();
    if (params.deviceId) query.set('device_id', params.deviceId);
    if (params.state) query.set('state', params.state);
    query.set('limit', '50');
    return this.get<Page<SupportSession>>(`/support-sessions?${query}`);
  }

  supportSession(id: string) {
    return this.get<SupportSession>(`/support-sessions/${id}`);
  }

  stopSupportSession(id: string, reason = 'stopped_by_admin') {
    return this.post<SupportSession>(`/support-sessions/${id}/stop`, { reason });
  }

  /**
   * Одноразовий квиток на приєднання до перегляду.
   *
   * Саме він, а не довгоживучий `join_secret`, використовується для
   * підключення: квиток живе секунди й гасне при першому використанні, тож
   * перепідключення та перезавантаження вкладки не потребують зберігати
   * нічого чутливого в браузері.
   */
  viewerTicket(sessionId: string) {
    return this.post<ViewerTicket>(`/support-sessions/${sessionId}/viewer-ticket`);
  }

  supportConnection(sessionId: string) {
    return this.get<ConnectionStatus>(`/support-sessions/${sessionId}/connection`);
  }

  supportSessionAudit(sessionId: string) {
    return this.get<SessionAuditEntry[]>(`/support-sessions/${sessionId}/audit`);
  }

  /** URL каналу перегляду — щоразу з новим квитком. */
  async supportSocketUrl(sessionId: string): Promise<string> {
    if (!this.accessToken) await this.refreshSession();
    const { ticket } = await this.viewerTicket(sessionId);
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams({
      role: 'viewer',
      token: this.accessToken ?? '',
      ticket,
    });
    return `${protocol}//${window.location.host}/ws/support/${sessionId}?${params}`;
  }

  // ------------------------------------------- облікові дані пристрою -----

  deviceCredentials(deviceId: string) {
    return this.get<DeviceCredentials>(`/devices/${deviceId}/credentials`);
  }

  deviceKeyHistory(deviceId: string) {
    return this.get<DeviceKeyHistoryEntry[]>(`/devices/${deviceId}/credentials/history`);
  }

  /** Відкликає токени пристрою: агент перевипустить ключ при підключенні. */
  rotateDeviceCredentials(deviceId: string) {
    return this.post<{ message: string }>(`/devices/${deviceId}/credentials/rotate`);
  }

  secretRotationStatus() {
    return this.get<SecretRotationStatus>('/maintenance/secrets');
  }

  deviceKeyOverview() {
    return this.get<DeviceKeyOverview>('/maintenance/device-keys');
  }

  // ------------------------------------ комунікаційний модуль (SMS/дзвінки) -

  commsSettings(deviceId: string) {
    return this.get<CommsSettings>(`/devices/${deviceId}/comms`);
  }

  updateCommsSettings(deviceId: string, body: CommsSettingsUpdate) {
    return this.request<CommsSettings>(`/devices/${deviceId}/comms`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  purgeComms(deviceId: string) {
    return this.post<{ message: string }>(`/devices/${deviceId}/comms/purge`);
  }

  commsMessages(deviceId: string, params: { limit?: number; offset?: number } = {}) {
    const query = new URLSearchParams();
    query.set('limit', String(params.limit ?? 50));
    query.set('offset', String(params.offset ?? 0));
    return this.get<Page<SmsMessage>>(`/devices/${deviceId}/comms/messages?${query}`);
  }

  commsCalls(deviceId: string, params: { limit?: number; offset?: number } = {}) {
    const query = new URLSearchParams();
    query.set('limit', String(params.limit ?? 50));
    query.set('offset', String(params.offset ?? 0));
    return this.get<Page<CallLogEntry>>(`/devices/${deviceId}/comms/calls?${query}`);
  }

  // ------------------------------------------------------------ команди ---

  commands(params: { deviceId?: string; type?: string; status?: string; limit?: number } = {}) {
    const query = new URLSearchParams();
    if (params.type) query.set('type', params.type);
    if (params.status) query.set('status', params.status);
    query.set('limit', String(params.limit ?? 100));
    const base = params.deviceId ? `/devices/${params.deviceId}/commands` : '/commands';
    return this.get<Page<Command>>(`${base}?${query}`);
  }

  cancelCommand(id: string) {
    return this.post<Command>(`/commands/${id}/cancel`);
  }

  command(id: string) {
    return this.get<CommandDetail>(`/commands/${id}`);
  }

  commandStats(deviceId?: string) {
    const query = deviceId ? `?device_id=${deviceId}` : '';
    return this.get<CommandStats[]>(`/commands/stats${query}`);
  }

  // ------------------------------------------------------ реєстр команд ---

  /** Повний перелік команд, які взагалі існують у системі. */
  commandRegistry() {
    return this.get<CommandSpec[]>('/commands/registry');
  }

  /**
   * Видає команду з реєстру.
   *
   * Довільного рядка для виконання на пристрої тут немає: `type` має бути
   * в реєстрі, `params` — відповідати схемі саме цього типу.
   */
  issueCommand(deviceId: string, type: string, params?: Record<string, unknown>) {
    return this.post<Command>(`/devices/${deviceId}/commands`, { type, params });
  }

  setCommandPermission(
    type: string,
    body: { required_permission?: string; enabled?: boolean },
  ) {
    return this.request<CommandSpec>(`/commands/registry/${type}/permission`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  resetCommandPermission(type: string) {
    return this.request<CommandSpec>(`/commands/registry/${type}/permission`, {
      method: 'DELETE',
    });
  }

  // -------------------------------------------------------------- аудит ---

  audit(params: { action?: string; result?: string; limit?: number } = {}) {
    const query = new URLSearchParams();
    if (params.action) query.set('action', params.action);
    if (params.result) query.set('result', params.result);
    query.set('limit', String(params.limit ?? 100));
    return this.get<Page<AuditEntry>>(`/audit?${query}`);
  }

  // -------------------------------------------------------- enrollment ----

  enrollmentTokens() {
    return this.get<Page<EnrollmentToken>>('/enrollment/tokens?limit=100');
  }

  createEnrollmentToken(params: { label?: string; maxUses?: number; ttlSeconds?: number }) {
    return this.post<CreatedEnrollmentToken>('/enrollment/tokens', {
      label: params.label || null,
      max_uses: params.maxUses ?? 1,
      ttl_seconds: params.ttlSeconds ?? null,
    });
  }

  revokeEnrollmentToken(id: string) {
    return this.post<EnrollmentToken>(`/enrollment/tokens/${id}/revoke`);
  }
}

export const api = new ApiClient();
