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
            <th>别名</th><th>邮箱</th><th>套餐</th><th>代理节点</th>
            <th>7 天配额</th><th class="num">活跃会话</th><th class="num">最近用量</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="account in store.accounts" :key="account.alias">
            <td class="mono">{{ account.alias }}</td>
            <td>{{ account.email }}</td>
            <td>
              <span class="badge">{{ account.plan || "未知" }}</span>
              <span v-if="account.profile_pending" class="badge"
                    title="验证码登录已完成；套餐和租户资料可稍后刷新">资料待刷新</span>
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
.quota-cell { display: flex; align-items: center; gap: 8px; }
.actions { white-space: nowrap; text-align: right; }
.actions button { margin-left: 6px; }
</style>
