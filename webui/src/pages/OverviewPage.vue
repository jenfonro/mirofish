<script setup lang="ts">
import { computed, onMounted } from "vue";
import { loadAccounts, loadUsage, store } from "../store";
import { accountName, accountStatus } from "../accounts";
import { proxyLabel } from "../proxies";
import AccountStatus from "../components/AccountStatus.vue";
import type { PageId } from "../main";

const emit = defineEmits<{ navigate: [page: PageId] }>();

const kpis = computed(() => {
  const accounts = store.accounts;
  const active = accounts.filter((a) => accountStatus(a).label === "正常").length;
  const cooling = accounts.filter((a) => (a.shared_quota_cooldown ?? 0) > 0).length;
  const sessions = accounts.reduce((sum, a) => sum + (a.active_sessions || 0), 0);

  // Tightest utilization across accounts that have limits data
  let maxUtil = 0;
  for (const a of accounts) {
    const u = Number(a.quota?.["7d_utilization"]);
    if (Number.isFinite(u) && u > maxUtil) maxUtil = u;
  }

  const req24 = store.usage?.totals.requests ?? 0;
  const inTok = store.usage?.totals.input_tokens ?? 0;
  const outTok = store.usage?.totals.output_tokens ?? 0;

  return [
    {
      label: "账号",
      value: `${active}`,
      unit: `/ ${accounts.length}`,
      hint: cooling ? `${cooling} 个窗口冷却中` : `正常 ${active} / ${accounts.length}`,
    },
    {
      label: "活跃会话",
      value: `${sessions}`,
      unit: "",
      hint: "当前固定在账号上的对话",
    },
    {
      label: "最高 7 天用量",
      value: `${(maxUtil * 100).toFixed(0)}`,
      unit: "%",
      hint: maxUtil >= 0.9 ? "接近上限" : "有余量",
    },
    {
      label: "24h 请求",
      value: formatCompact(req24),
      unit: "",
      hint: `入 ${formatCompact(inTok)} / 出 ${formatCompact(outTok)}`,
    },
  ];
});

function formatCompact(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

/** Account health rows for the overview list. */
const healthRows = computed(() => {
  return store.accounts.map((a) => {
    const raw = a.quota?.["7d_utilization"];
    const util = raw == null || raw === "" ? NaN : Number(raw);
    const utilPct = Number.isFinite(util) ? util * 100 : null;
    return {
      account: a,
      alias: a.alias,
      name: accountName(a),
      email: a.email,
      plan: a.plan || "未知",
      utilPct,
      sessions: a.active_sessions || 0,
      proxy: a.proxy ? proxyLabel(a.proxy) : "无代理",
    };
  });
});

const scheduleHint = computed(() => {
  const s = store.schedule;
  if (!s) return null;
  return {
    mode: "重置优先 + Fable 使用偏好",
    ceiling: `${(s.max_utilization * 100).toFixed(0)}%`,
  };
});

function utilColor(pct: number | null): string {
  if (pct === null) return "var(--ink-4)";
  if (pct >= 90) return "var(--danger)";
  if (pct >= 70) return "var(--warn)";
  return "var(--ok)";
}

onMounted(async () => {
  // store.connect already hydrates; this is a soft refresh
  loadAccounts().catch(() => undefined);
  loadUsage().catch(() => undefined);
});
</script>

<template>
  <!-- KPI strip -->
  <div class="kpi-row">
    <div v-for="k in kpis" :key="k.label" class="kpi">
      <div class="kpi-label">{{ k.label }}</div>
      <div class="kpi-value">
        {{ k.value }}<span v-if="k.unit" class="unit">{{ k.unit }}</span>
      </div>
      <div class="kpi-hint">{{ k.hint }}</div>
    </div>
  </div>

  <!-- Quick actions + schedule hint -->
  <div class="grid-2">
    <div class="panel">
      <div class="panel-head">
        <h3>快速操作</h3>
      </div>
      <div class="panel-body row">
        <button class="btn" @click="emit('navigate', 'accounts')">
          管理账号
        </button>
        <button class="btn ghost" @click="emit('navigate', 'playground')">
          测试模型
        </button>
        <button class="btn ghost" @click="emit('navigate', 'network')">
          代理池
        </button>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">
        <h3>调度策略</h3>
        <span class="spacer"></span>
        <button class="btn ghost sm" @click="emit('navigate', 'schedule')">修改</button>
      </div>
      <div class="panel-body">
        <template v-if="scheduleHint">
          <div class="row">
            <span class="chip accent">{{ scheduleHint.mode }}</span>
            <span class="chip">硬阈值 {{ scheduleHint.ceiling }}</span>
          </div>
          <p class="muted mt-2">新会话按此策略分配；已开始的对话不会中途切换。</p>
        </template>
        <p v-else class="muted">读取中…</p>
      </div>
    </div>
  </div>

  <!-- Account health -->
  <div class="panel">
    <div class="panel-head">
      <h3>账号健康</h3>
      <span class="spacer"></span>
      <button class="btn ghost sm" @click="emit('navigate', 'accounts')">全部账号</button>
    </div>
    <div v-if="!healthRows.length" class="empty">
      <div class="empty-title">还没有账号</div>
      <div class="empty-hint">到「账号」页用邮箱验证码添加第一个账号。</div>
      <button class="btn sm mt-2" @click="emit('navigate', 'accounts')">去添加</button>
    </div>
    <div v-else class="table-wrap">
      <table class="data">
        <thead>
          <tr>
            <th>账号名称</th>
            <th>状态</th>
            <th>套餐</th>
            <th>7 天用量</th>
            <th class="num">会话</th>
            <th>出口</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in healthRows" :key="row.alias" class="clickable"
              @click="emit('navigate', 'accounts')">
            <td>
              <div :title="row.alias">{{ row.name }}</div>
              <div class="muted">{{ row.email }}</div>
            </td>
            <td>
              <AccountStatus :account="row.account" />
            </td>
            <td>{{ row.plan }}</td>
            <td style="min-width: 120px">
              <div class="row" style="gap:8px">
                <div class="meter" style="flex:1; min-width:60px">
                  <div class="meter-fill"
                       :style="{ width: Math.min(100, row.utilPct ?? 0) + '%', background: utilColor(row.utilPct) }"></div>
                </div>
                <span class="tnum muted" style="min-width:40px; text-align:right">
                  {{ row.utilPct !== null ? row.utilPct.toFixed(0) + '%' : '—' }}
                </span>
              </div>
            </td>
            <td class="num tnum">{{ row.sessions }}</td>
            <td class="muted">{{ row.proxy }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
