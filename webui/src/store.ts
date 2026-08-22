import { reactive } from "vue";
import { api } from "./api";
import type { Account, Health, ProxySummary, UsageSummary } from "./types";

interface Toast {
  id: number;
  kind: "ok" | "error" | "info";
  text: string;
}

let toastSeq = 0;

export const store = reactive({
  connected: false,
  checking: false,
  health: null as Health | null,
  accounts: [] as Account[],
  proxies: null as ProxySummary | null,
  usage: null as UsageSummary | null,
  toasts: [] as Toast[],
});

export function toast(text: string, kind: Toast["kind"] = "info"): void {
  const id = ++toastSeq;
  store.toasts.push({ id, kind, text });
  setTimeout(() => {
    const index = store.toasts.findIndex((item) => item.id === id);
    if (index >= 0) store.toasts.splice(index, 1);
  }, kind === "error" ? 6000 : 3500);
}

export async function loadAccounts(): Promise<void> {
  store.accounts = (await api<{ accounts: Account[] }>("/accounts")).accounts;
}

export async function loadProxies(): Promise<void> {
  store.proxies = await api<ProxySummary>("/proxies");
}

export async function loadUsage(hours = 24): Promise<void> {
  store.usage = await api<UsageSummary>(`/api/usage?hours=${hours}`);
}

/** Verify the key and hydrate every dashboard panel. */
export async function connect(): Promise<boolean> {
  store.checking = true;
  try {
    store.health = await api<Health>("/health");
    store.connected = true;
    await Promise.all([
      loadAccounts().catch(() => undefined),
      loadProxies().catch(() => undefined),
      loadUsage().catch(() => undefined),
    ]);
    return true;
  } catch {
    store.connected = false;
    return false;
  } finally {
    store.checking = false;
  }
}
