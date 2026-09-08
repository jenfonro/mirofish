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

/** Recorded upstream refusal that parks an account (401/503). */
export interface AccountHealth {
  /** "error" = 上游拒绝，可能自愈；"suspended" = 上游封号，只能客服解除。 */
  state?: "error" | "suspended" | string;
  status?: number;
  kind?: string;
  message?: string;
  at?: string;
  /** Epoch after which automatic selection retries it; null = never. */
  retry_at?: number | null;
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
  healthy?: boolean;
  health?: AccountHealth;
  health_retry_in?: number | null;
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

/** 一个手动维护的代理节点。凭据只在已认证的管理接口里流转。 */
export interface ProxyNode {
  id: string;
  name: string;
  scheme: string;
  host: string;
  port: number;
  username?: string;
  password?: string;
  active: boolean;
  assigned: number;
  failure_count: number;
  last_error: string | null;
  last_checked: string | null;
}

export interface ProxySummary {
  configured: boolean;
  active: number;
  total: number;
  assigned: number;
  nodes: ProxyNode[];
}

export interface Health {
  ok: boolean;
  accounts: number;
  version: string;
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
