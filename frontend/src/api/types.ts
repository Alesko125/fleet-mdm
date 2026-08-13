export type Role = 'superadmin' | 'admin' | 'operator' | 'auditor';

export interface TokenPair {
  access_token: string;
  token_type: string;
  expires_in: number;
  role: Role;
  /** Дозволи ролі — панель ховає недоступні дії. */
  permissions: string[];
  /** Значення для заголовка X-CSRF-Token (дублює cookie). */
  csrf_token: string | null;
}

export interface User {
  id: string;
  email: string;
  full_name: string | null;
  role: Role;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
  permissions?: string[];
}

// --------------------------------------------- облікові дані пристрою ----

export interface DeviceCredentials {
  device_id: string;
  key_algorithm: string;
  key_fingerprint: string;
  key_generation: number;
  key_created_at: string;
  key_rotated_at: string | null;
  key_age_days: number;
  rotation_due: boolean;
  max_age_days: number;
}

export interface DeviceKeyHistoryEntry {
  id: string;
  key_fingerprint: string;
  key_algorithm: string;
  generation: number;
  reason: string;
  retired_at: string;
}

export interface DeviceKeyOverview {
  total: number;
  rotation_due: number;
  max_age_days: number;
  devices: Array<{
    id: string;
    name: string;
    key_age_days: number;
    key_generation: number;
  }>;
}

export interface SecretRotationStatus {
  jwt: {
    algorithm: string;
    previous_secret_configured: boolean;
    access_token_ttl_seconds: number;
    refresh_token_ttl_seconds: number;
    safe_to_drop_previous_after_seconds: number;
  };
  token_hash: {
    previous_secret_configured: boolean;
    safe_to_drop_previous_after_seconds: number;
  };
  token_epoch: string | null;
  device_keys: {
    max_age_days: number;
    daily_rotation_limit: number;
    history_retention_days: number;
  };
  procedure: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Telemetry {
  manufacturer: string | null;
  model: string | null;
  android_version: string | null;
  sdk_int: number | null;
  kernel_version: string | null;
  uptime_seconds: number | null;
  battery_level: number | null;
  charging: boolean | null;
  ram_total_bytes: number | null;
  ram_available_bytes: number | null;
  storage_total_bytes: number | null;
  storage_free_bytes: number | null;
  network_type: string | null;
  agent_version: string | null;
  collected_at: string | null;
}

export interface Device {
  id: string;
  name: string;
  status: string;
  management_mode: string;
  agent_version: string | null;
  android_id: string | null;
  serial_number: string | null;
  key_fingerprint: string;
  enrolled_at: string;
  last_seen_at: string | null;
  last_ip: string | null;
  notes: string | null;
  online: boolean;
  battery_level?: number | null;
  model?: string | null;
  android_version?: string | null;
  telemetry?: Telemetry | null;
}

export interface Apk {
  id: string;
  filename: string;
  label: string | null;
  package_name: string | null;
  version_name: string | null;
  version_code: number | null;
  size_bytes: number;
  sha256: string;
  signature_digest: string | null;
  signature_schemes: string[] | null;
  min_sdk: number | null;
  target_sdk: number | null;
  compile_sdk: number | null;
  abis: string[] | null;
  permissions: string[] | null;
  is_split: boolean;
  notes: string | null;
  uploaded_by_id: string | null;
  created_at: string;
  architecture: string;
  display_version: string;
  deduplicated?: boolean;
}

export interface DeviceApp {
  package_name: string;
  app_name: string | null;
  version_name: string | null;
  version_code: number | null;
  is_system: boolean;
  is_enabled: boolean;
  installer_package: string | null;
  first_installed_at: string | null;
  last_updated_at: string | null;
  last_seen_at: string | null;
  repository_apk_id: string | null;
  repository_version_name: string | null;
  repository_version_code: number | null;
  update_available: boolean;
  in_repository: boolean;
}

export type CommandStatus =
  | 'pending'
  | 'dispatched'
  | 'acked'
  | 'succeeded'
  | 'failed'
  | 'expired'
  | 'cancelled';

export interface Command {
  id: string;
  device_id: string;
  type: string;
  status: CommandStatus;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error: string | null;
  progress_percent: number | null;
  status_message: string | null;
  attempts: number;
  created_by_id: string | null;
  created_at: string;
  dispatched_at: string | null;
  acked_at: string | null;
  completed_at: string | null;
  expires_at: string | null;
  /** Скільки команда чекала в черзі й скільки виконувалася на пристрої. */
  queue_ms: number | null;
  duration_ms: number | null;
  is_terminal: boolean;
}

export interface AuditEntry {
  id: string;
  occurred_at: string;
  actor_type: 'user' | 'device' | 'system';
  actor_label: string | null;
  action: string;
  object_type: string | null;
  object_id: string | null;
  result: 'success' | 'failure' | 'denied';
  ip: string | null;
  details: Record<string, unknown> | null;
}

export type PermissionState =
  | 'unknown'
  | 'granted_fine'
  | 'granted_coarse'
  | 'denied'
  | 'restricted';

export interface LocationSettings {
  device_id: string;
  enabled: boolean;
  interval_seconds: number;
  min_distance_meters: number;
  high_accuracy: boolean;
  permission_state: PermissionState;
  background_permission: boolean;
  permission_reported_at: string | null;
  last_fix_at: string | null;
  consent_recorded_at: string | null;
  updated_by_id: string | null;
  updated_at: string;
}

export interface LocationFix {
  id: string;
  device_id: string;
  latitude: number;
  longitude: number;
  accuracy_m: number | null;
  altitude_m: number | null;
  speed_mps: number | null;
  bearing_deg: number | null;
  provider: string | null;
  source: 'periodic' | 'on_demand' | 'offline_queue';
  is_mock: boolean;
  battery_level: number | null;
  recorded_at: string;
  received_at: string;
}

export interface LocationOverview {
  settings: LocationSettings;
  current: LocationFix | null;
  fix_count: number;
  oldest_fix_at: string | null;
  retention_days: number;
  reporting_active: boolean;
}

export interface LocationSettingsUpdate {
  enabled?: boolean;
  interval_seconds?: number;
  min_distance_meters?: number;
  high_accuracy?: boolean;
}

export interface FileRoot {
  id: string;
  device_id: string | null;
  path: string;
  label: string | null;
  writable: boolean;
  max_file_size_mb: number | null;
  allow_privileged: boolean;
  created_by_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size_bytes: number | null;
  modified_at: string | null;
  permissions: string | null;
  is_symlink: boolean;
  readable: boolean;
  writable: boolean;
}

export interface FileTransfer {
  id: string;
  device_id: string;
  command_id: string | null;
  direction: 'upload' | 'download';
  remote_path: string;
  filename: string;
  size_bytes: number | null;
  received_bytes: number;
  chunk_size: number;
  sha256: string | null;
  checksum_verified: boolean;
  status: 'pending' | 'in_progress' | 'completed' | 'failed';
  error: string | null;
  created_at: string;
  completed_at: string | null;
  progress_percent: number | null;
}

export type RuleType =
  | 'DEVICE_OFFLINE'
  | 'LOW_BATTERY'
  | 'LOW_STORAGE'
  | 'LOW_MEMORY'
  | 'HIGH_CPU'
  | 'OUTDATED_AGENT'
  | 'OUTDATED_ANDROID'
  | 'OUTDATED_APP'
  | 'LOCATION_REPORTING_STOPPED'
  | 'COMMAND_FAILURES'
  | 'ENROLLMENT_STATUS';

export type Severity = 'info' | 'warning' | 'critical';
export type AlertState = 'firing' | 'acknowledged' | 'resolved';

export interface AlertRule {
  id: string;
  name: string;
  type: RuleType;
  severity: Severity;
  enabled: boolean;
  thresholds: Record<string, unknown>;
  device_id: string | null;
  cooldown_seconds: number;
  description: string | null;
  created_by_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface Alert {
  id: string;
  rule_id: string | null;
  device_id: string;
  device_name?: string | null;
  type: RuleType;
  severity: Severity;
  state: AlertState;
  dedup_key: string;
  message: string;
  details: Record<string, unknown> | null;
  first_seen_at: string;
  last_seen_at: string;
  acknowledged_at: string | null;
  acknowledged_by_id: string | null;
  resolved_at: string | null;
}

export interface RuleSchema {
  type: RuleType;
  keys: Record<string, string>;
  defaults: Record<string, unknown>;
}

export interface DeviceHealth {
  device_id: string;
  online: boolean;
  battery_level: number | null;
  charging: boolean | null;
  storage_free_percent: number | null;
  ram_free_percent: number | null;
  cpu_load_percent: number | null;
  android_version: string | null;
  agent_version: string | null;
  location_reporting: 'disabled' | 'active' | 'blocked';
  enrollment_status: string;
  active_alerts: number;
  worst_severity: Severity | null;
  last_seen_at: string | null;
  collected_at: string | null;
}

export interface MonitoringSummary {
  devices_total: number;
  devices_online: number;
  devices_offline: number;
  alerts_firing: number;
  alerts_by_severity: Record<string, number>;
  alerts_by_type: Record<string, number>;
  rules_enabled: number;
}

export type SupportSessionState =
  | 'requested'
  | 'awaiting_consent'
  | 'active'
  | 'denied'
  | 'ended'
  | 'expired';

export interface SupportSession {
  id: string;
  device_id: string;
  device_name?: string | null;
  requested_by_id: string | null;
  requested_by_email?: string | null;
  state: SupportSessionState;
  transport: string;
  reason: string | null;
  input_enabled: boolean;
  input_available: boolean;
  consent_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  end_reason: string | null;
  expires_at: string | null;
  last_frame_at: string | null;
  frames_sent: number;
  bytes_sent: number;
  /** Роздільність кадру пристрою — база для перерахунку координат кліку. */
  frame_width: number | null;
  frame_height: number | null;
  input_events: number;
  viewer_joins: number;
  last_viewer_at: string | null;
  stats: Record<string, number> | null;
  created_at: string;
  duration_seconds: number | null;
  is_open: boolean;
  /** Керування дозволене, підтримується пристроєм і має відому роздільність. */
  input_ready?: boolean;
  connection?: ConnectionStatus | null;
}

/** Живий стан каналу — з сервера, а не з здогадок клієнта. */
export interface ConnectionStatus {
  device_online: boolean;
  viewers: number;
  streaming: boolean;
  seconds_since_frame: number | null;
  frame_width: number | null;
  frame_height: number | null;
}

/** Одноразовий квиток на приєднання до перегляду. */
export interface ViewerTicket {
  session_id: string;
  ticket: string;
  expires_in: number;
  viewer_url: string;
}

export interface SessionAuditEntry {
  id: string;
  action: string;
  result: string;
  actor_type: string;
  actor_label: string | null;
  ip: string | null;
  details: Record<string, unknown> | null;
  occurred_at: string;
}

export interface CreatedSupportSession extends SupportSession {
  join_secret: string;
  viewer_url: string;
}

// ----------------------------------------------------------- команди ----

export interface CommandDetail extends Command {
  title: string | null;
  created_by_email: string | null;
  device_name: string | null;
  disruptive: boolean;
  requires_device_owner: boolean;
}

export interface CommandParamSpec {
  name: string;
  type: string;
  required: boolean;
  default: unknown;
  description: string | null;
}

/** Опис типу команди з реєстру. */
export interface CommandSpec {
  type: string;
  title: string;
  description: string;
  /** Що побачить власник пристрою, коли команда виконається. */
  user_visible: string;
  required_permission: string;
  enabled: boolean;
  /** Правило змінено адміністратором, а не взяте з реєстру. */
  overridden: boolean;
  roles: string[];
  allowed_for_me: boolean;
  disruptive: boolean;
  requires_device_owner: boolean;
  ttl_seconds: number;
  params: CommandParamSpec[];
}

export interface CommandStats {
  type: string;
  total: number;
  succeeded: number;
  failed: number;
  expired: number;
  cancelled: number;
  pending: number;
  avg_duration_ms: number | null;
  max_duration_ms: number | null;
}

export type CommsPermission = 'unknown' | 'granted' | 'denied' | 'not_requested' | 'unavailable';

export interface CommsSettings {
  device_id: string;
  enabled: boolean;
  sms_enabled: boolean;
  call_log_enabled: boolean;
  metadata_only: boolean;
  enabled_at: string | null;
  enabled_by_id: string | null;
  enabled_by_email?: string | null;
  disabled_at: string | null;
  retention_days: number;
  sms_permission: CommsPermission;
  call_log_permission: CommsPermission;
  permission_reported_at: string | null;
  last_sync_at: string | null;
  updated_at: string;
  encryption_ready: boolean;
  retention_max_days: number;
}

export interface CommsSettingsUpdate {
  enabled?: boolean;
  sms_enabled?: boolean;
  call_log_enabled?: boolean;
  metadata_only?: boolean;
  retention_days?: number;
}

export interface SmsMessage {
  id: string;
  direction: string;
  address: string | null;
  body: string | null;
  body_length: number | null;
  sent_at: string | null;
  collected_at: string;
}

export interface CallLogEntry {
  id: string;
  direction: string;
  number: string | null;
  contact: string | null;
  duration_seconds: number | null;
  occurred_at: string | null;
  collected_at: string;
}

export interface EnrollmentToken {
  id: string;
  token_prefix: string;
  label: string | null;
  notes: string | null;
  max_uses: number;
  use_count: number;
  expires_at: string;
  revoked_at: string | null;
  created_at: string;
}

export interface CreatedEnrollmentToken extends EnrollmentToken {
  token: string;
  enroll_url: string;
  qr_payload: string;
}
