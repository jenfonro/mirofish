export interface ProxyInfo {
  id: string;
  name?: string;
  scheme?: string;
  host?: string;
  port?: number;
  active: boolean;
  failure_count?: number;
  last_error?: string | null;
}

export interface WindowModelUsage {
  model: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
}

export interface LimitWindow {
  name: string;
  label: string;
  length: number | null;
  used: number;
  budget: number;
  reset_at: number | null;
  /** Present on 7d_fable only: the shared window split per fable model. */
  models?: WindowModelUsage[];
}

export interface AccountLimits {
  subject?: string | null;
  suspended: boolean;
  degraded: boolean;
  unmetered: boolean;
  windows: LimitWindow[];
  fetched_epoch: number;
}

/** Normalized subscription profile from upstream /auth/me + /auth/referral. */
export interface AccountProfile {
  name?: string | null;
  roles?: string[];
  plan_expires_epoch?: number | null;
  next_plan?: string | null;
}

/** Raw upstream /auth/referral body: progress toward the next plan tier. */
export interface ReferralInfo {
  code?: string | null;
  redeemed?: number;
  threshold?: number;
  remaining?: number;
  reached?: boolean;
  max_redemptions?: number;
  current_plan?: string | null;
  plan_expires_at?: string | null;
  next_plan?: string | null;
}

export interface Account {
  alias: string;
  email: string;
  user_id?: string;
  plan?: string | null;
  tenant?: string | null;
  profile?: AccountProfile;
  referral?: ReferralInfo;
  quota: { "7d_utilization"?: string | null; "7d_reset_epoch"?: string | null };
  last_usage: { input_tokens?: number; output_tokens?: number };
  last_model?: string | null;
  limits?: AccountLimits | null;
  profile_pending?: boolean;
  disabled?: boolean;
  shared_quota_cooldown?: number;
  active_sessions?: number;
  checked_at?: string | null;
  proxy?: ProxyInfo | null;
}

export interface AccountLimitsResult {
  alias: string;
  ok: boolean;
  limits?: AccountLimits;
  error?: string;
  status?: number;
}

export interface LimitsSummary {
  accounts: AccountLimitsResult[];
}

export interface ScheduleSettings {
  mode: "balanced" | "reset_first" | "fable_first";
  max_utilization: number;
}

export interface ProxySummary {
  configured: boolean;
  backend: "mihomo" | "direct";
  active: number;
  total: number;
  assigned: number;
  last_refresh: string | null;
  last_error: string | null;
  skipped_nodes: number;
  nodes: ProxyInfo[] & { assigned?: number }[];
}

export interface Health {
  ok: boolean;
  accounts: number;
  version: string;
  proxy_backend: string;
  default_account: string | null;
}

export interface UsageBucket {
  hour: string;
  alias: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
}

export interface UsageSummary {
  hours: number;
  totals: { requests: number; input_tokens: number; output_tokens: number };
  buckets: UsageBucket[];
}
