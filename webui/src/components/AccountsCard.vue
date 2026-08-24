<script setup lang="ts">
import { ref } from "vue";
import { api } from "../api";
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

async function refreshStatus(alias: string, probe = false) {
  busy.value = alias;
  try {
    await api(`/accounts/${alias}/status${probe ? "?probe=1" : ""}`);
    await loadAccounts();
    toast(probe ? `已刷新 ${alias} 的资料与额度（零模型调用）` : `已刷新 ${alias}`, "ok");
  } catch (error: any) {
    toast(`刷新 ${alias} 失败：${error.message}`, "error");
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
            <th>启用</th><th>别名</th><th>邮箱</th><th>套餐</th><th>代理节点</th>
            <th>7 天配额</th><th class="num">活跃会话</th><th class="num">最近用量</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="account in store.accounts" :key="account.alias"
              :class="{ off: account.disabled }">
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
            <td class="mono">{{ account.alias }}</td>
            <td>{{ account.email }}</td>
            <td>
              <span class="badge">{{ account.plan || "未知" }}</span>
              <span v-if="account.profile_pending" class="badge"
                    title="验证码登录已完成；套餐和租户资料可稍后刷新">资料待刷新</span>
              <span v-if="account.disabled" class="badge">已停用</span>
              <span v-else-if="account.shared_quota_cooldown" class="badge"
                    :title="'上游共享额度拒绝了这个账号；自动分配会先避开它，'
                      + cooldownLabel(account.shared_quota_cooldown) + '后自动重试'">
                额度冷却 {{ cooldownLabel(account.shared_quota_cooldown) }}
              </span>
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
                      @click="refreshStatus(account.alias)">刷新</button>
              <button class="ghost small" :disabled="busy === account.alias"
                      title="通过 /v1/limits 刷新账号资料与额度；不产生模型调用"
                      @click="refreshStatus(account.alias, true)">资料+额度</button>
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
