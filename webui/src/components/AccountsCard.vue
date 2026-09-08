<script setup lang="ts">
import { ref } from "vue";
import { api } from "../api";
import { compact } from "../limits";
import { loadAccounts, store, toast } from "../store";
import type { Account } from "../types";

const busy = ref<string>("");

function utilization(account: Account): number | null {
  const raw = account.quota?.["7d_utilization"];
  if (raw === null || raw === undefined || raw === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

function quotaColor(value: number): string {
  if (value >= 0.9) return "var(--critical)";
  if (value >= 0.7) return "var(--warning)";
  return "var(--good)";
}

interface Expiry {
  date: string;
  days: number;
}

/** Plan expiry from the stored profile; free accounts simply have none. */
function planExpiry(account: Account): Expiry | null {
  const epoch = account.profile?.plan_expires_epoch;
  if (!epoch || !Number.isFinite(epoch)) return null;
  const d = new Date(epoch * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    days: Math.ceil((epoch * 1000 - Date.now()) / 86_400_000),
  };
}

function expiryColor(days: number): string {
  if (days <= 3) return "var(--critical)";
  if (days <= 7) return "var(--warning)";
  return "var(--good-text)";
}

function planClass(plan?: string | null): string {
  const p = (plan || "").toLowerCase();
  if (p.startsWith("max")) return "plan-max";
  if (p === "pro" || p === "plus") return "plan-pro";
  if (p === "free") return "plan-free";
  return "";
}

/** Hover detail for the plan badge: holder, tenant, referral ladder, budgets. */
function profileTitle(account: Account): string {
  const parts: string[] = [];
  if (account.profile?.name) parts.push(`姓名：${account.profile.name}`);
  if (account.user_id) parts.push(`用户 ID：${account.user_id}`);
  if (account.tenant) parts.push(`租户：${account.tenant}`);
  const roles = account.profile?.roles;
  if (roles?.length && roles.join() !== "user") parts.push(`角色：${roles.join("、")}`);
  const ref = account.referral;
  if (ref && typeof ref.threshold === "number" && ref.threshold > 0) {
    const target = ref.next_plan || account.profile?.next_plan;
    parts.push(`邀请进度：${ref.redeemed ?? 0}/${ref.threshold}`
      + (target ? `（满额升级 ${target}）` : ""));
  }
  for (const w of account.limits?.windows ?? []) {
    if (w.budget > 0) parts.push(`${w.label}预算：${compact(w.budget)}`);
  }
  return parts.join("\n") || "点「资料」获取更多账号资料";
}

// 资料与额度分属两个数据源：资料是 mirasim 账号数据（套餐、到期、持有人），
// 额度是 relay 的计量窗口。分开刷新，一个失败不影响另一个——额度用满的账号
// 照样有套餐可看，而资料正常、额度被 403 拒绝本身就是一个可区分的状态。
async function refreshProfile(alias: string) {
  busy.value = alias;
  try {
    await api(`/accounts/${alias}/status`);
    await loadAccounts();
    toast(`已刷新 ${alias} 的资料`, "ok");
  } catch (error: any) {
    toast(`刷新 ${alias} 资料失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

async function refreshLimits(alias: string) {
  busy.value = alias;
  try {
    await api(`/accounts/${alias}/limits`);
    await loadAccounts();
    toast(`已刷新 ${alias} 的额度`, "ok");
  } catch (error: any) {
    toast(`刷新 ${alias} 额度失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

async function toggleEnabled(account: Account) {
  busy.value = account.alias;
  try {
    const enabled = !!account.disabled;
    await api(`/api/accounts/${account.alias}/enabled`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    await loadAccounts();
    toast(enabled ? `已启用 ${account.alias}` : `已停用 ${account.alias}：不再参与自动分配`, "ok");
  } catch (error: any) {
    toast(`切换 ${account.alias} 失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

function cooldownLabel(seconds: number): string {
  return seconds >= 90 ? `${Math.ceil(seconds / 60)} 分钟` : `${seconds} 秒`;
}

// 停调时长可达一天，分钟数会长得没法读。
function retryLabel(seconds: number): string {
  if (seconds >= 7200) return `${Math.round(seconds / 3600)} 小时`;
  return cooldownLabel(seconds);
}

// 状态列：上游 401、403 限流封停、503 overloaded_error 会标记为「异常」并停止调度；
// 上游直接封号（403 contact support）是「封停」，只能由客服解除，不会自动重试。
// 其他 503（边缘错误页、relay 自身的 503）是共享故障，不会标记任何账号。
function healthState(account: Account): "disabled" | "suspended" | "error" | "ok" {
  if (account.disabled) return "disabled";
  if (account.health?.state === "suspended") return "suspended";
  return account.healthy === false ? "error" : "ok";
}

function healthLabel(account: Account): string {
  const state = healthState(account);
  if (state === "disabled") return "已停用";
  if (state === "suspended") return "封停";
  if (state !== "error") return "正常";
  // The reason belongs in the status column, not next to the plan: what is
  // wrong and what to do about it are the same question.
  if (account.health?.status === 401) return "需重新登录";
  if (typeof account.health_retry_in === "number") {
    return `异常 · 停调 ${retryLabel(account.health_retry_in)}`;
  }
  return "异常";
}

function healthTitle(account: Account): string {
  if (account.disabled) return "已停用：不参与自动分配，点击开关可启用";
  if (healthState(account) === "suspended") {
    const at = account.health?.at
      ? `（${account.health.at.slice(0, 19).replace("T", " ")} UTC）`
      : "";
    // 没有重试期限：只有客服能解除，任何定时重试都只是白撞上游。
    return `上游已封停该账号${at}：${account.health?.message || "account suspended"}`
      + "；已永久移出调度，需联系官方客服解除。解除后在测试台指定该账号成功发送一次请求即可恢复";
  }
  if (account.healthy === false) {
    const code = account.health?.status ? `上游 ${account.health.status}：` : "";
    const reason = account.health?.message || "上游拒绝了这个账号";
    const at = account.health?.at
      ? `（${account.health.at.slice(0, 19).replace("T", " ")} UTC）`
      : "";
    // 401 需要重新登录；403 封停与 503 容量问题都会自己好，到点自动重试。
    const recovery = account.health?.status === 401
      ? "已暂停调度；凭证已失效，需要重新登录该账号，或在测试台指定它成功发送一次请求"
      : typeof account.health_retry_in === "number"
        ? `已暂停调度，${retryLabel(account.health_retry_in)}后自动重试；`
          + "也可在测试台指定该账号发送一次请求立即恢复"
        : "已暂停调度；在测试台指定该账号发送一次请求即可恢复";
    return `${code}${reason}${at}；${recovery}`;
  }
  return "正常：参与自动分配";
}

async function removeAccount(alias: string) {
  if (!confirm(`删除账号 ${alias} 的本地凭证？（不会注销远端账号）`)) return;
  try {
    await api(`/api/accounts/${alias}`, { method: "DELETE" });
    await loadAccounts();
    toast(`已删除 ${alias}`, "ok");
  } catch (error: any) {
    toast(`删除失败：${error.message}`, "error");
  }
}
</script>

<template>
  <section class="card">
    <h2>
      账号
      <span class="spacer"></span>
      <button class="ghost small" @click="loadAccounts().catch(() => toast('加载账号失败', 'error'))">
        刷新列表
      </button>
    </h2>
    <p v-if="!store.accounts.length" class="muted">
      还没有账号。在右侧「添加账号」用邮箱验证码登录第一个账号。
    </p>
    <div v-else class="scroll-x">
      <table>
        <thead>
          <tr>
            <th>启用</th><th>状态</th><th>账号名称</th><th>套餐</th><th>到期</th><th>代理节点</th>
            <th>7 天配额</th><th class="num">活跃会话</th><th class="num">最近用量</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="account in store.accounts" :key="account.alias"
              :class="{ off: account.disabled, unhealthy: healthState(account) === 'error' }">
            <td>
              <button class="switch" :class="{ on: !account.disabled }"
                      :disabled="busy === account.alias"
                      :title="account.disabled
                        ? '已停用：不参与自动分配，点击启用'
                        : '已启用：点击停用后不再参与自动分配（凭证保留）'"
                      @click="toggleEnabled(account)">
                <span class="knob"></span>
              </button>
            </td>
            <td>
              <span class="badge" :title="healthTitle(account)">
                <span class="dot" :class="healthState(account) === 'ok' ? 'ok'
                  : healthState(account) === 'suspended' ? 'off' : 'bad'"></span>
                {{ healthLabel(account) }}
              </span>
              <!-- A cooldown is not an abnormal account: the quota is spent
                   and returns on its own, so it qualifies the row rather than
                   replacing its state. -->
              <span v-if="account.shared_quota_cooldown" class="badge"
                    :title="'上游额度拒绝了这个账号；自动分配会先避开它，'
                      + cooldownLabel(account.shared_quota_cooldown) + '后自动重试'">
                冷却 {{ cooldownLabel(account.shared_quota_cooldown) }}
              </span>
            </td>
            <td class="identity">
              <div class="mono">{{ account.alias }}</div>
              <div class="muted">{{ account.email }}</div>
              <div v-if="account.profile?.name && account.profile.name !== account.alias"
                   class="muted">{{ account.profile.name }}</div>
            </td>
            <td>
              <span class="badge" :class="planClass(account.plan)"
                    :title="profileTitle(account)">{{ account.plan || "未知" }}</span>
            </td>
            <td>
              <template v-if="planExpiry(account)">
                <div class="mono expiry-date">{{ planExpiry(account)!.date }}</div>
                <div v-if="planExpiry(account)!.days < 0" class="muted"
                     :style="{ color: 'var(--critical)' }">已到期</div>
                <div v-else class="muted"
                     :style="{ color: expiryColor(planExpiry(account)!.days) }">
                  {{ planExpiry(account)!.days === 0 ? "今天到期" : `剩 ${planExpiry(account)!.days} 天` }}
                </div>
              </template>
              <span v-else class="muted"
                    :title="account.plan ? '该套餐无到期时间' : '点「资料」获取账号资料'">—</span>
            </td>
            <td>
              <template v-if="account.proxy">
                <span class="badge">
                  <span class="dot" :class="account.proxy.active ? 'ok' : 'bad'"></span>
                  {{ account.proxy.name || account.proxy.id }}
                </span>
                <div class="muted mono">{{ account.proxy.host }}:{{ account.proxy.port }}</div>
              </template>
              <span v-else class="muted">直连</span>
            </td>
            <td>
              <template v-if="utilization(account) !== null">
                <div class="quota-cell">
                  <div class="quota-track">
                    <div class="quota-fill"
                         :style="{ width: Math.min(100, utilization(account)! * 100) + '%',
                                   background: quotaColor(utilization(account)!) }"></div>
                  </div>
                  <span class="muted">{{ (utilization(account)! * 100).toFixed(1) }}%</span>
                </div>
              </template>
              <span v-else class="muted">未知</span>
            </td>
            <td class="num">
              <span v-if="account.active_sessions" class="badge">{{ account.active_sessions }}</span>
              <span v-else class="muted">0</span>
            </td>
            <td class="num muted">
              入 {{ account.last_usage?.input_tokens ?? "–" }} /
              出 {{ account.last_usage?.output_tokens ?? "–" }}
            </td>
            <td class="actions">
              <button class="ghost small" :disabled="busy === account.alias"
                      title="读取 /v1/limits 刷新用量窗口；不产生模型调用"
                      @click="refreshLimits(account.alias)">额度</button>
              <button class="ghost small" :disabled="busy === account.alias"
                      title="读取 mirasim 账号资料（套餐、到期、持有人）；不产生模型调用"
                      @click="refreshProfile(account.alias)">资料</button>
              <button class="danger small" @click="removeAccount(account.alias)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.scroll-x { overflow-x: auto; }
tr.off td:not(:first-child) { opacity: 0.55; }
tr.unhealthy td:nth-child(2) { color: var(--critical); }
/* Alias over email in one cell: they identify the same account, so a separate
   column only widened the table. */
.identity { line-height: 1.35; }
.identity .muted { font-size: 12px; }
.badge.plan-free { color: var(--muted); }
.badge.plan-pro {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.badge.plan-max {
  color: color-mix(in srgb, var(--warning) 70%, var(--ink));
  border-color: color-mix(in srgb, var(--warning) 60%, transparent);
  background: color-mix(in srgb, var(--warning) 14%, transparent);
}
.expiry-date { font-variant-numeric: tabular-nums; white-space: nowrap; }
.switch {
  width: 34px; height: 18px; border-radius: 9px; padding: 0; position: relative;
  border: 1px solid var(--border, rgba(128, 128, 128, 0.5));
  background: var(--critical); cursor: pointer;
}
.switch.on { background: var(--good); }
.switch:disabled { opacity: 0.5; cursor: default; }
.switch .knob {
  position: absolute; top: 1px; left: 1px; width: 14px; height: 14px;
  border-radius: 50%; background: #fff; transition: left 0.15s ease;
}
.switch.on .knob { left: 17px; }
.quota-cell { display: flex; align-items: center; gap: 8px; }
.actions { white-space: nowrap; text-align: right; }
.actions button { margin-left: 6px; }
</style>
