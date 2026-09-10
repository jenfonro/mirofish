<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import { compact, deriveAll } from "../limits";
import { loadAccounts, store, toast } from "../store";
import type { Account } from "../types";
import { iconSvg } from "../icons";

const busy = ref("");
const search = ref("");
const filter = ref<"all" | "active" | "off" | "cool">("all");
const drawer = ref<Account | null>(null);
const showAdd = ref(false);

// Add-account form
const addAlias = ref("");
const addEmail = ref("");
const addCode = ref("");
const addStage = ref<"start" | "verify">("start");
const addBusy = ref(false);

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase();
  return store.accounts.filter((a) => {
    if (filter.value === "active" && a.disabled) return false;
    if (filter.value === "off" && !a.disabled) return false;
    if (filter.value === "cool" && !((a.shared_quota_cooldown ?? 0) > 0)) return false;
    if (!q) return true;
    return (
      a.alias.toLowerCase().includes(q) ||
      (a.email || "").toLowerCase().includes(q) ||
      (a.plan || "").toLowerCase().includes(q)
    );
  });
});

function utilization(a: Account): number | null {
  const raw = a.quota?.["7d_utilization"];
  if (raw === null || raw === undefined || raw === "") return null;
  const v = Number(raw);
  return Number.isFinite(v) ? v : null;
}

function utilColor(v: number): string {
  if (v >= 0.9) return "var(--danger)";
  if (v >= 0.7) return "var(--warn)";
  return "var(--ok)";
}

function planExpiry(a: Account) {
  const epoch = a.profile?.plan_expires_epoch;
  if (!epoch || !Number.isFinite(epoch)) return null;
  const d = new Date(epoch * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    days: Math.ceil((epoch * 1000 - Date.now()) / 86_400_000),
  };
}

function expiryColor(days: number): string {
  if (days <= 3) return "var(--danger)";
  if (days <= 7) return "var(--warn)";
  return "var(--ok)";
}

function planClass(plan?: string | null): string {
  const p = (plan || "").toLowerCase();
  if (p.startsWith("max")) return "warn";
  if (p === "pro" || p === "plus") return "accent";
  return "";
}

function cooldownLabel(s: number): string {
  return s >= 90 ? `${Math.ceil(s / 60)} 分钟` : `${s} 秒`;
}

async function refreshStatus(alias: string, probe = false) {
  busy.value = alias;
  try {
    await api(`/accounts/${alias}/status${probe ? "?probe=1" : ""}`);
    await loadAccounts();
    if (drawer.value?.alias === alias) {
      drawer.value = store.accounts.find((a) => a.alias === alias) || drawer.value;
    }
    toast(probe ? `已刷新 ${alias} 资料与额度` : `已刷新 ${alias}`, "ok");
  } catch (error: any) {
    toast(`刷新失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

async function toggleEnabled(a: Account) {
  busy.value = a.alias;
  try {
    const enabled = !!a.disabled;
    await api(`/api/accounts/${a.alias}/enabled`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    await loadAccounts();
    toast(enabled ? `已启用 ${a.alias}` : `已停用 ${a.alias}`, "ok");
  } catch (error: any) {
    toast(`切换失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

async function removeAccount(alias: string) {
  if (!confirm(`删除账号 ${alias} 的本地凭证？（不会注销远端账号）`)) return;
  try {
    await api(`/api/accounts/${alias}`, { method: "DELETE" });
    drawer.value = null;
    await loadAccounts();
    toast(`已删除 ${alias}`, "ok");
  } catch (error: any) {
    toast(`删除失败：${error.message}`, "error");
  }
}

async function resetDevice(a: Account) {
  if (!confirm(`重置 ${a.alias} 的设备身份？将生成新的设备密钥。`)) return;
  busy.value = a.alias;
  try {
    await api(`/api/accounts/${a.alias}/reset-device`, { method: "POST" });
    await loadAccounts();
    if (drawer.value?.alias === a.alias) {
      drawer.value = store.accounts.find((x) => x.alias === a.alias) || drawer.value;
    }
    toast(`已重置 ${a.alias} 设备身份`, "ok");
  } catch (error: any) {
    toast(`重置失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

async function sendCode() {
  addBusy.value = true;
  try {
    await api("/api/login/start", {
      method: "POST",
      body: JSON.stringify({ alias: addAlias.value.trim(), email: addEmail.value.trim() }),
    });
    addStage.value = "verify";
    toast("验证码已发送，请查收邮箱", "ok");
  } catch (error: any) {
    toast(`发送失败：${error.message}`, "error");
  } finally {
    addBusy.value = false;
  }
}

async function verify() {
  addBusy.value = true;
  try {
    const result = await api<{ alias: string; plan?: string; profile_pending?: boolean }>(
      "/api/login/finish",
      { method: "POST", body: JSON.stringify({ alias: addAlias.value.trim(), code: addCode.value.trim() }) },
    );
    toast(result.profile_pending
      ? `登录成功：${result.alias}（资料可稍后刷新）`
      : `登录成功：${result.alias}（${result.plan || "未知套餐"}）`, "ok");
    addAlias.value = addEmail.value = addCode.value = "";
    addStage.value = "start";
    showAdd.value = false;
    await loadAccounts();
  } catch (error: any) {
    toast(`登录失败：${error.message}`, "error");
  } finally {
    addBusy.value = false;
  }
}

function openDrawer(a: Account) {
  drawer.value = a;
}

const drawerWindows = computed(() => {
  if (!drawer.value?.limits) return [];
  return deriveAll(drawer.value.limits);
});

onMounted(() => {
  loadAccounts().catch(() => toast("加载账号失败", "error"));
});
</script>

<template>
  <!-- Toolbar -->
  <div class="panel">
    <div class="panel-head">
      <h3>账号</h3>
      <span class="spacer"></span>
      <div class="row" style="gap:6px">
        <div class="seg">
          <button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部</button>
          <button :class="{ active: filter === 'active' }" @click="filter = 'active'">启用</button>
          <button :class="{ active: filter === 'cool' }" @click="filter = 'cool'">冷却</button>
          <button :class="{ active: filter === 'off' }" @click="filter = 'off'">停用</button>
        </div>
        <input
          v-model="search"
          placeholder="搜索别名 / 邮箱…"
          style="width:160px"
        />
        <button class="btn ghost sm" @click="loadAccounts().catch(() => undefined)">
          <span style="width:12px;height:12px;display:inline-flex" v-html="iconSvg('refresh')"></span>
          刷新
        </button>
        <button class="btn sm" @click="showAdd = !showAdd">
          <span style="width:12px;height:12px;display:inline-flex" v-html="iconSvg('plus')"></span>
          添加
        </button>
      </div>
    </div>

    <!-- Add form -->
    <div v-if="showAdd" class="panel-body" style="border-bottom:1px solid var(--border); background:var(--bg-subtle)">
      <div class="field-row">
        <div class="grow">
          <label>别名</label>
          <input v-model="addAlias" placeholder="work" :disabled="addStage === 'verify'" />
        </div>
        <div class="grow">
          <label>邮箱</label>
          <input v-model="addEmail" type="email" placeholder="you@example.com"
                 :disabled="addStage === 'verify'" />
        </div>
        <div v-if="addStage === 'verify'" class="grow" style="max-width:140px">
          <label>验证码</label>
          <input v-model="addCode" maxlength="6" inputmode="numeric" placeholder="123456"
                 @keyup.enter="verify" />
        </div>
        <template v-if="addStage === 'start'">
          <button class="btn" :disabled="addBusy || !addAlias.trim() || !addEmail.trim()"
                  @click="sendCode">
            {{ addBusy ? "发送中…" : "发送验证码" }}
          </button>
        </template>
        <template v-else>
          <button class="btn" :disabled="addBusy || addCode.trim().length !== 6" @click="verify">
            {{ addBusy ? "验证中…" : "完成登录" }}
          </button>
          <button class="btn ghost" :disabled="addBusy" @click="addStage = 'start'">重来</button>
        </template>
      </div>
      <p class="muted mt-2">邮箱验证码登录；凭证只写入本机加密存储。</p>
    </div>

    <!-- Empty -->
    <div v-if="!filtered.length" class="empty">
      <div class="empty-title">
        {{ store.accounts.length ? "没有匹配的账号" : "还没有账号" }}
      </div>
      <div class="empty-hint">
        {{ store.accounts.length ? "换个筛选或关键词试试。" : "点右上「添加」用邮箱验证码登录第一个账号。" }}
      </div>
      <button v-if="!store.accounts.length" class="btn sm mt-2" @click="showAdd = true">添加账号</button>
    </div>

    <!-- Table -->
    <div v-else class="table-wrap">
      <table class="data">
        <thead>
          <tr>
            <th style="width:48px">启用</th>
            <th>别名</th>
            <th>邮箱</th>
            <th>套餐</th>
            <th>到期</th>
            <th>出口</th>
            <th style="min-width:100px">7 天用量</th>
            <th class="num">会话</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in filtered" :key="a.alias" class="clickable" @click="openDrawer(a)">
            <td @click.stop>
              <button
                class="toggle"
                :class="{ on: !a.disabled }"
                :disabled="busy === a.alias"
                :title="a.disabled ? '点击启用' : '点击停用'"
                @click="toggleEnabled(a)"
              >
                <span class="knob"></span>
              </button>
            </td>
            <td class="mono">{{ a.alias }}</td>
            <td>
              {{ a.email }}
              <div v-if="a.profile?.name" class="muted">{{ a.profile.name }}</div>
            </td>
            <td>
              <span class="chip" :class="planClass(a.plan)">{{ a.plan || "未知" }}</span>
              <span v-if="a.shared_quota_cooldown" class="chip warn" style="margin-left:4px">
                冷却 {{ cooldownLabel(a.shared_quota_cooldown) }}
              </span>
            </td>
            <td>
              <template v-if="planExpiry(a)">
                <div class="mono tnum">{{ planExpiry(a)!.date }}</div>
                <div class="muted" :style="{ color: expiryColor(planExpiry(a)!.days) }">
                  {{ planExpiry(a)!.days < 0 ? "已到期"
                    : planExpiry(a)!.days === 0 ? "今天到期"
                    : `剩 ${planExpiry(a)!.days} 天` }}
                </div>
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td>
              <span v-if="a.proxy" class="muted">
                <span class="dot" :class="a.proxy.active ? 'ok' : 'bad'"
                      style="display:inline-block;margin-right:4px"></span>
                {{ a.proxy.name || a.proxy.id }}
              </span>
              <span v-else class="muted">直连</span>
            </td>
            <td>
              <template v-if="utilization(a) !== null">
                <div class="row" style="gap:6px">
                  <div class="meter" style="width:60px">
                    <div class="meter-fill"
                         :style="{ width: Math.min(100, utilization(a)! * 100) + '%',
                                   background: utilColor(utilization(a)!) }"></div>
                  </div>
                  <span class="tnum muted">{{ (utilization(a)! * 100).toFixed(0) }}%</span>
                </div>
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td class="num tnum">{{ a.active_sessions || 0 }}</td>
            <td @click.stop>
              <button class="btn ghost sm" :disabled="busy === a.alias"
                      @click="refreshStatus(a.alias)">刷新</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Drawer -->
  <template v-if="drawer">
    <div class="drawer-scrim" @click="drawer = null"></div>
    <aside class="drawer">
      <div class="drawer-head">
        <h3 class="mono">{{ drawer.alias }}</h3>
        <span class="chip" :class="planClass(drawer.plan)">{{ drawer.plan || "未知" }}</span>
        <button class="btn ghost icon-only sm" @click="drawer = null">
          <span style="width:14px;height:14px;display:inline-flex" v-html="iconSvg('close')"></span>
        </button>
      </div>
      <div class="drawer-body">
        <!-- Basic -->
        <div class="drawer-section">
          <h4>基本信息</h4>
          <dl class="kv">
            <dt>邮箱</dt><dd>{{ drawer.email }}</dd>
            <dt v-if="drawer.profile?.name">姓名</dt>
            <dd v-if="drawer.profile?.name">{{ drawer.profile.name }}</dd>
            <dt v-if="drawer.user_id">用户 ID</dt>
            <dd v-if="drawer.user_id" class="mono">{{ drawer.user_id }}</dd>
            <dt v-if="drawer.tenant">租户</dt>
            <dd v-if="drawer.tenant" class="mono">{{ drawer.tenant }}</dd>
            <dt v-if="drawer.device_id">设备 ID</dt>
            <dd v-if="drawer.device_id" class="mono" style="font-size:11px">{{ drawer.device_id }}</dd>
            <dt>状态</dt>
            <dd>
              <span class="chip" :class="drawer.disabled ? '' : 'ok'">
                {{ drawer.disabled ? "已停用" : "已启用" }}
              </span>
              <span v-if="drawer.shared_quota_cooldown" class="chip warn" style="margin-left:4px">
                冷却 {{ cooldownLabel(drawer.shared_quota_cooldown) }}
              </span>
            </dd>
            <dt>出口</dt>
            <dd>
              <template v-if="drawer.proxy">
                {{ drawer.proxy.name || drawer.proxy.id }}
                <span class="muted"> · {{ drawer.proxy.host }}:{{ drawer.proxy.port }}</span>
              </template>
              <span v-else class="muted">直连</span>
            </dd>
          </dl>
        </div>

        <!-- Expiry -->
        <div v-if="planExpiry(drawer)" class="drawer-section">
          <h4>套餐到期</h4>
          <div class="row">
            <span class="mono tnum" style="font-size:16px;font-weight:600">
              {{ planExpiry(drawer)!.date }}
            </span>
            <span class="chip" :class="planExpiry(drawer)!.days <= 3 ? 'danger' : planExpiry(drawer)!.days <= 7 ? 'warn' : 'ok'">
              {{ planExpiry(drawer)!.days < 0 ? "已到期"
                : planExpiry(drawer)!.days === 0 ? "今天到期"
                : `剩 ${planExpiry(drawer)!.days} 天` }}
            </span>
          </div>
        </div>

        <!-- Limits windows -->
        <div v-if="drawerWindows.length" class="drawer-section">
          <h4>用量窗口</h4>
          <div v-for="w in drawerWindows" :key="w.name" class="mb-2">
            <div class="row" style="justify-content:space-between; margin-bottom:4px">
              <span style="font-size:12.5px">{{ w.label }}</span>
              <span class="tnum" style="font-weight:600"
                    :style="{ color: w.usedPct >= 90 ? 'var(--danger)' : w.usedPct >= 70 ? 'var(--warn)' : 'var(--ok)' }">
                {{ w.usedPct }}%
              </span>
            </div>
            <div class="meter pace" :title="`匀速线 ${w.pacePct}%`">
              <div class="meter-fill"
                   :style="{ width: Math.min(100, w.usedPct) + '%',
                             background: w.usedPct >= 90 ? 'var(--danger)' : w.usedPct >= 70 ? 'var(--warn)' : 'var(--ok)' }"></div>
              <div v-if="w.pacePct > 0" class="pace-mark" :style="{ left: w.pacePct + '%' }"></div>
            </div>
            <div class="row muted mt-2" style="font-size:11px">
              <span>剩 {{ compact(w.remaining) }}</span>
              <span class="spacer"></span>
              <span>{{ w.resetText }}</span>
            </div>
            <div v-if="w.models.length" class="mt-2">
              <div v-for="m in w.models" :key="m.model" class="row muted"
                   style="font-size:11px; justify-content:space-between">
                <span class="mono">{{ m.model.replace("claude-", "") }}</span>
                <span class="tnum">{{ m.requests }} 次 · {{ compact(m.total_tokens) }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Referral -->
        <div v-if="drawer.referral && (drawer.referral.threshold || 0) > 0" class="drawer-section">
          <h4>邀请进度</h4>
          <p class="muted">
            {{ drawer.referral.redeemed ?? 0 }} / {{ drawer.referral.threshold }}
            <template v-if="drawer.referral.next_plan || drawer.profile?.next_plan">
              （满额升级 {{ drawer.referral.next_plan || drawer.profile?.next_plan }}）
            </template>
          </p>
        </div>

        <!-- Actions -->
        <div class="drawer-section">
          <h4>操作</h4>
          <div class="row">
            <button class="btn ghost sm" :disabled="busy === drawer.alias"
                    @click="refreshStatus(drawer.alias)">刷新状态</button>
            <button class="btn ghost sm" :disabled="busy === drawer.alias"
                    title="通过 /v1/limits 刷新，不产生模型调用"
                    @click="refreshStatus(drawer.alias, true)">资料+额度</button>
            <button class="btn ghost sm" :disabled="busy === drawer.alias"
                    @click="resetDevice(drawer)">重置设备</button>
            <button class="btn danger sm" @click="removeAccount(drawer.alias)">删除账号</button>
          </div>
        </div>
      </div>
    </aside>
  </template>
</template>
