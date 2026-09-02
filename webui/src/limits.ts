// Per-window usage derivation, ported from the glassgauge usage widget.
// The "pace line" (匀速线) is the percentage you would have consumed if you
// spent the window's budget at an even rate — the reference for whether an
// account is ahead of or behind average usage.

import type { AccountLimits, LimitWindow, WindowModelUsage } from "./types";

export interface DerivedWindow {
  name: string;
  label: string;
  used: number;
  budget: number;
  remaining: number;
  usedPct: number;
  remPct: number;
  pacePct: number;
  delta: number;
  ahead: boolean;
  deltaText: string;
  resetText: string;
  /** Per-model split of a shared window (7d_fable), highest spend first. */
  models: WindowModelUsage[];
}

function round1(x: number): number {
  return Math.round(x * 10) / 10;
}

/** Seconds -> countdown text: days+hours, hours+minutes, else minutes. */
export function resetText(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时后重置`;
  if (h > 0) return `${h} 小时 ${m} 分后重置`;
  return `${m} 分后重置`;
}

/** One window -> display values. nowSec is Unix seconds. */
export function deriveWindow(w: LimitWindow, nowSec: number): DerivedWindow | null {
  if (!(w.budget > 0)) return null;
  const usedPct = (w.used / w.budget) * 100;
  const remaining = w.reset_at ? Math.max(0, w.reset_at - nowSec) : 0;
  const len = w.length ?? 0;
  const pacePct = len > 0 ? Math.min(100, Math.max(0, ((len - remaining) / len) * 100)) : 0;
  const delta = usedPct - pacePct;
  return {
    name: w.name,
    label: w.label,
    used: w.used,
    budget: w.budget,
    remaining: Math.max(0, w.budget - w.used),
    usedPct: round1(usedPct),
    remPct: Math.max(0, Math.round(100 - usedPct)),
    pacePct: round1(pacePct),
    delta: round1(delta),
    ahead: delta >= 0,
    deltaText: len > 0
      ? `匀速线 ${round1(pacePct)}% · ${delta >= 0 ? "超前" : "落后"} ${Math.abs(round1(delta))}%`
      : "",
    resetText: w.reset_at ? resetText(remaining) : "无重置时间",
    // Sorted so the model that consumed the shared window reads first; a
    // model with no spend this window still shows, as a 0 row.
    models: [...(w.models ?? [])].sort((a, b) => b.total_tokens - a.total_tokens),
  };
}

/** Full response -> ordered derived windows. Uses the server's fetch clock. */
export function deriveAll(limits: AccountLimits): DerivedWindow[] {
  const nowSec = limits.fetched_epoch || Date.now() / 1000;
  return (limits.windows ?? [])
    .map((w) => deriveWindow(w, nowSec))
    .filter((w): w is DerivedWindow => w !== null);
}

/** Tightest window = highest used%. Null for an empty list. */
export function tightest(windows: DerivedWindow[]): DerivedWindow | null {
  return windows.reduce<DerivedWindow | null>(
    (a, b) => (a == null || b.usedPct > a.usedPct ? b : a), null);
}

/** Compact number for large credit counts (12.3k, 1.2M). */
export function compact(n: number): string {
  if (!Number.isFinite(n)) return "–";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return round1(n / 1_000_000) + "M";
  if (abs >= 1_000) return round1(n / 1_000) + "k";
  return Math.round(n).toString();
}
