import type { Account } from "./types";

export function accountName(account: Account): string {
  return account.display_name?.trim() || account.alias;
}

export function accountStatus(account: Account): { label: string; tone: string } {
  if (account.disabled) return { label: "disabled", tone: "" };
  if (account.health?.state === "suspended") return { label: "封停", tone: "danger" };
  if (account.healthy === false || account.health?.state === "error") {
    return { label: "异常", tone: "danger" };
  }
  if ((account.shared_quota_cooldown ?? 0) > 0) {
    return { label: "窗口冷却", tone: "warn" };
  }
  return { label: "正常", tone: "ok" };
}
