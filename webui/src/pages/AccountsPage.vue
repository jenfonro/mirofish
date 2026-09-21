<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import { accountName } from "../accounts";
import { compact, deriveAll } from "../limits";
import { proxyLabel, proxyStatus } from "../proxies";
import { loadAccounts, loadProxies, store, toast } from "../store";
import type { Account } from "../types";
import { iconSvg } from "../icons";
import AccountStatus from "../components/AccountStatus.vue";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import ProxyPicker from "../components/ProxyPicker.vue";
import WindowTokens from "../components/WindowTokens.vue";

const busy = ref("");
const search = ref("");
const filter = ref<"all" | "active" | "off" | "cool">("all");
const drawer = ref<Account | null>(null);
const editName = ref("");
const editProxy = ref("");
const deleting = ref<Account | null>(null);
const showAdd = ref(false);

// Add-account form
const addAlias = ref("");
const addEmail = ref("");
const addCode = ref("");
const addProxy = ref("");
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
      accountName(a).toLowerCase().includes(q) ||
      (a.email || "").toLowerCase().includes(q) ||
      (a.plan || "").toLowerCase().includes(q)
    );
  });
});

function accountWindows(a: Account) {
  return a.limits ? deriveAll(a.limits) : [];
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
    time: `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`,
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

async function refreshProfile(alias: string) {
  busy.value = alias;
  try {
    await api(`/accounts/${encodeURIComponent(alias)}/status`);
    toast(`已刷新 ${alias} 的资料`, "ok");
  } catch (error: any) {
    toast(`刷新资料失败：${error.message}`, "error");
  } finally {
    await loadAccounts().catch(() => toast("加载账号失败", "error"));
    if (drawer.value?.alias === alias) {
      drawer.value = store.accounts.find((a) => a.alias === alias) ?? null;
    }
    busy.value = "";
  }
}

async function toggleEnabled(a: Account) {
  busy.value = a.alias;
  try {
    const enabled = !!a.disabled;
    await api(`/api/accounts/${encodeURIComponent(a.alias)}/enabled`, {
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

async function removeAccount() {
  const account = deleting.value;
  if (!account) return;
  busy.value = account.alias;
  try {
    await api(`/api/accounts/${encodeURIComponent(account.alias)}`, { method: "DELETE" });
    if (drawer.value?.alias === account.alias) drawer.value = null;
    deleting.value = null;
    await loadAccounts();
    toast(`已删除 ${account.alias}`, "ok");
  } catch (error: any) {
    toast(`删除失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

const dirty = computed(() => drawer.value && (
  editName.value.trim() !== (drawer.value.display_name ?? "") ||
  editProxy.value !== (drawer.value.proxy?.id ?? "") ||
  drawer.value.proxy?.status === "unbound"
));

async function saveAccount() {
  const a = drawer.value;
  if (!a) return;
  busy.value = a.alias;
  try {
    await api(`/api/accounts/${encodeURIComponent(a.alias)}`, {
      method: "PATCH",
      body: JSON.stringify({ display_name: editName.value.trim(), proxy_id: editProxy.value }),
    });
    await loadAccounts();
    if (drawer.value?.alias === a.alias) {
      drawer.value = store.accounts.find((item) => item.alias === a.alias) ?? null;
    }
    toast(`已保存 ${a.alias}`, "ok");
  } catch (error: any) {
    toast(`保存失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

async function sendCode() {
  if (addBusy.value || !addAlias.value.trim() || !addEmail.value.trim()) return;
  addBusy.value = true;
  try {
    await api("/api/login/start", {
      method: "POST",
      body: JSON.stringify({
        alias: addAlias.value.trim(), email: addEmail.value.trim(), proxy_id: addProxy.value,
      }),
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
  if (addBusy.value || addCode.value.trim().length !== 6) return;
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
    addProxy.value = "";
    addStage.value = "start";
    showAdd.value = false;
  } catch (error: any) {
    toast(`登录失败：${error.message}`, "error");
  } finally {
    await loadAccounts().catch(() => toast("加载账号失败", "error"));
    addBusy.value = false;
  }
}

function openDrawer(a: Account) {
  if (busy.value) return;
  drawer.value = a;
  editName.value = a.display_name ?? "";
  editProxy.value = a.proxy?.id ?? "";
  loadProxies().catch(() => toast("读取代理列表失败", "error"));
}

function openAdd() {
  showAdd.value = !showAdd.value;
  if (showAdd.value) loadProxies().catch(() => toast("读取代理列表失败", "error"));
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
          placeholder="搜索名称 / 别名 / 邮箱…"
          style="width:160px"
        />
        <button class="btn ghost sm" @click="loadAccounts().catch(() => undefined)">
          <span style="width:12px;height:12px;display:inline-flex" v-html="iconSvg('refresh')"></span>
          刷新
        </button>
        <button class="btn sm" :disabled="addBusy" @click="openAdd">
          <span style="width:12px;height:12px;display:inline-flex" v-html="iconSvg('plus')"></span>
          添加
        </button>
      </div>
    </div>

    <!-- Add form -->
    <div v-if="showAdd" class="panel-body" style="border-bottom:1px solid var(--border); background:var(--bg-subtle)">
      <div class="field-row">
        <div class="grow">
          <label for="login-alias">本地别名（alias）</label>
          <input id="login-alias" v-model="addAlias" placeholder="work" :disabled="addBusy || addStage === 'verify'" />
        </div>
        <div class="grow">
          <label for="login-email">登录邮箱</label>
          <input id="login-email" v-model="addEmail" type="email" placeholder="you@example.com"
                 :disabled="addBusy || addStage === 'verify'" />
          <div class="field mt-3">
            <label>固定代理</label>
            <ProxyPicker v-model="addProxy" :nodes="store.proxies?.nodes ?? []"
                         :disabled="addBusy || addStage === 'verify'" />
          </div>
        </div>
        <div v-if="addStage === 'verify'" class="grow" style="max-width:140px">
          <label for="login-code">验证码</label>
          <input id="login-code" v-model="addCode" maxlength="6" inputmode="numeric" placeholder="123456"
                 :disabled="addBusy"
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
      <button v-if="!store.accounts.length && !showAdd" class="btn sm mt-2" @click="openAdd">添加账号</button>
    </div>

    <!-- Table -->
    <div v-else class="table-wrap">
      <table class="data">
        <thead>
          <tr>
            <th style="width:48px">启用</th>
            <th>状态</th>
            <th>账号名称</th>
            <th>套餐</th>
            <th>到期</th>
            <th>出口</th>
            <th>额度窗口（缓存）</th>
            <th class="num">会话</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in filtered" :key="a.alias" class="clickable" @click="openDrawer(a)">
            <td @click.stop>
              <button
                class="toggle"
                :class="{ on: !a.disabled }"
                :disabled="!!busy"
                role="switch"
                :aria-checked="!a.disabled"
                :aria-label="`${accountName(a)} 启用`"
                :title="a.disabled ? '点击启用' : '点击停用'"
                @click="toggleEnabled(a)"
              >
                <span class="knob"></span>
              </button>
            </td>
            <td><AccountStatus :account="a" /></td>
            <td class="identity">
              <div :title="a.alias">{{ accountName(a) }}</div>
              <div class="muted">{{ a.email }}</div>
            </td>
            <td>
              <span class="chip" :class="planClass(a.plan)">{{ a.plan || "未知" }}</span>
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
                <span class="dot" :class="proxyStatus(a.proxy).tone === 'ok' ? 'ok'
                      : proxyStatus(a.proxy).tone === 'danger' ? 'bad' : 'off'"
                      style="display:inline-block;margin-right:4px"></span>
                {{ proxyLabel(a.proxy) }}
              </span>
              <span v-else class="muted">无代理</span>
            </td>
            <td>
              <div v-if="accountWindows(a).length" class="account-windows">
                <div v-for="w in accountWindows(a)" :key="w.name"
                     :title="`${w.label} · ${w.resetText}`">
                  <div class="muted tnum">{{ w.name }} · {{ w.usedPct }}%</div>
                  <div class="meter mt-2">
                    <div class="meter-fill"
                         :style="{ width: Math.min(100, w.usedPct) + '%',
                                   background: utilColor(w.usedPct / 100) }"></div>
                  </div>
                </div>
              </div>
              <span v-else class="muted">暂无缓存</span>
            </td>
            <td class="num tnum">{{ a.active_sessions || 0 }}</td>
            <td @click.stop>
              <div class="row" style="flex-wrap:nowrap">
                <button class="btn ghost sm" :disabled="!!busy" @click="openDrawer(a)">修改</button>
                <button class="btn danger sm" :disabled="!!busy" @click="deleting = a">删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Drawer -->
  <template v-if="drawer">
    <div class="drawer-scrim" @click="!busy && (drawer = null)"></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="修改账号"
           @keydown.esc="!busy && (drawer = null)">
      <div class="drawer-head">
        <h3>{{ accountName(drawer) }}</h3>
        <span class="chip" :class="planClass(drawer.plan)">{{ drawer.plan || "未知" }}</span>
        <button class="btn ghost icon-only sm" :disabled="!!busy" aria-label="关闭" @click="drawer = null">
          <span style="width:14px;height:14px;display:inline-flex" v-html="iconSvg('close')"></span>
        </button>
      </div>
      <div class="drawer-body">
        <div class="drawer-section">
          <label for="account-display-name">账号显示名称</label>
          <input id="account-display-name" v-model="editName" maxlength="120"
                 :placeholder="drawer.alias" :disabled="!!busy" />
          <p class="muted mt-2">留空使用本地别名；不会修改 alias 或存储 key。</p>
        </div>
        <!-- Basic -->
        <div class="drawer-section">
          <h4>基本信息</h4>
          <dl class="kv">
            <dt>本地别名</dt><dd class="mono">{{ drawer.alias }}</dd>
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
              <AccountStatus :account="drawer" />
            </dd>
          </dl>
        </div>

        <div class="drawer-section">
          <label>固定代理</label>
          <ProxyPicker v-model="editProxy" :nodes="store.proxies?.nodes ?? []" :disabled="!!busy" />
          <p v-if="drawer.proxy?.status === 'unbound'" class="muted mt-2">
            当前未绑定；保存「无代理」才会显式使用直连。
          </p>
        </div>

        <!-- Expiry -->
        <div class="drawer-section">
          <h4>套餐到期（本地时间）</h4>
          <div v-if="planExpiry(drawer)" class="row">
            <span class="mono tnum" style="font-size:16px;font-weight:600">
              {{ planExpiry(drawer)!.date }} {{ planExpiry(drawer)!.time }}
            </span>
            <span class="chip" :class="planExpiry(drawer)!.days <= 3 ? 'danger' : planExpiry(drawer)!.days <= 7 ? 'warn' : 'ok'">
              {{ planExpiry(drawer)!.days < 0 ? "已到期"
                : planExpiry(drawer)!.days === 0 ? "今天到期"
                : `剩 ${planExpiry(drawer)!.days} 天` }}
            </span>
          </div>
          <span v-else class="muted">—</span>
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
            <WindowTokens :models="w.models" />
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
                    @click="refreshProfile(drawer.alias)">刷新资料</button>
            <button class="btn" :disabled="!!busy || !dirty" @click="saveAccount">保存修改</button>
          </div>
        </div>
      </div>
    </aside>
  </template>

  <ConfirmDialog v-if="deleting" title="删除账号"
                 :message="`删除 ${accountName(deleting)}（${deleting.alias}）的本地凭证？不会注销远端账号。`"
                 :busy="!!busy" @close="deleting = null" @confirm="removeAccount" />
</template>

<style scoped>
.identity { line-height: 1.4; }
.account-windows { display: flex; gap: 12px; }
.account-windows > div { flex: 1; min-width: 90px; }
</style>
