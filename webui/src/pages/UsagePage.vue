<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { compact, deriveAll } from "../limits";
import { api } from "../api";
import { accountName } from "../accounts";
import { loadAccounts, refreshLimits, loadUsage, store, toast } from "../store";
import AccountStatus from "../components/AccountStatus.vue";
import WindowTokens from "../components/WindowTokens.vue";

const metric = ref<"tokens" | "requests">("tokens");
const showTable = ref(false);
const hoverIndex = ref<number | null>(null);
const hours = ref(24);
const limitsBusy = ref("");
const limitsErrors = ref<Record<string, string>>({});

const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"];
const MAX_SERIES = 5;

const PLOT = { width: 720, height: 200, top: 10, bottom: 24, left: 48, right: 10 };

function hourKeys(count: number): string[] {
  const keys: string[] = [];
  const now = new Date();
  for (let i = count - 1; i >= 0; i -= 1) {
    keys.push(new Date(now.getTime() - i * 3600_000).toISOString().slice(0, 13) + ":00Z");
  }
  return keys;
}

function bucketValue(b: { requests: number; input_tokens: number; output_tokens: number }): number {
  return metric.value === "requests" ? b.requests : b.input_tokens + b.output_tokens;
}

const seriesNames = computed<string[]>(() => {
  const totals = new Map<string, number>();
  for (const b of store.usage?.buckets ?? []) {
    totals.set(b.alias, (totals.get(b.alias) ?? 0) + b.input_tokens + b.output_tokens + b.requests);
  }
  const sorted = [...totals.keys()].sort();
  if (sorted.length <= MAX_SERIES + 1) return sorted;
  const byVol = [...totals.entries()].sort((a, b) => b[1] - a[1])
    .slice(0, MAX_SERIES).map(([a]) => a).sort();
  return [...byVol, "其他"];
});

interface Column {
  key: string;
  label: string;
  total: number;
  segments: { alias: string; value: number; color: string }[];
}

const columns = computed<Column[]>(() => {
  const keys = hourKeys(24);
  const named = seriesNames.value;
  const hasOther = named[named.length - 1] === "其他";
  const slotOf = new Map(named.map((n, i) => [n, i]));
  const grid = new Map(keys.map((k) => [k, named.map(() => 0)]));
  for (const b of store.usage?.buckets ?? []) {
    const row = grid.get(b.hour);
    if (!row) continue;
    const slot = slotOf.has(b.alias) ? slotOf.get(b.alias)! : hasOther ? named.length - 1 : -1;
    if (slot >= 0) row[slot] += bucketValue(b);
  }
  return keys.map((key) => {
    const row = grid.get(key)!;
    return {
      key,
      label: key.slice(11, 13) + ":00",
      total: row.reduce((s, v) => s + v, 0),
      segments: named.map((alias, i) => ({
        alias, value: row[i],
        color: `var(${SERIES[i % SERIES.length]})`,
      })).filter((s) => s.value > 0),
    };
  });
});

const maxTotal = computed(() => Math.max(1, ...columns.value.map((c) => c.total)));

const yTicks = computed(() => {
  const top = maxTotal.value;
  const step = Math.pow(10, Math.floor(Math.log10(top)));
  const unit = top / step > 5 ? step * 2 : top / step > 2 ? step : step / 2;
  const ticks: number[] = [];
  for (let v = unit; v <= top; v += unit) ticks.push(v);
  return ticks.slice(0, 5);
});

function y(value: number): number {
  const inner = PLOT.height - PLOT.top - PLOT.bottom;
  return PLOT.height - PLOT.bottom - (value / maxTotal.value) * inner;
}

const barSlot = computed(() => (PLOT.width - PLOT.left - PLOT.right) / 24);
const barWidth = computed(() => Math.max(4, barSlot.value - 2));

function fmt(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

const accountTotals = computed(() => {
  const named = seriesNames.value;
  const hasOther = named[named.length - 1] === "其他";
  const slotOf = new Map(named.map((n, i) => [n, i]));
  const totals = named.map((alias, i) => ({
    alias, color: `var(${SERIES[i % SERIES.length]})`,
    requests: 0, input: 0, output: 0,
  }));
  for (const b of store.usage?.buckets ?? []) {
    const slot = slotOf.has(b.alias) ? slotOf.get(b.alias)! : hasOther ? named.length - 1 : -1;
    if (slot < 0) continue;
    totals[slot].requests += b.requests;
    totals[slot].input += b.input_tokens;
    totals[slot].output += b.output_tokens;
  }
  return totals;
});

const limitsViews = computed(() =>
  store.accounts.map((account) => {
    const flags: string[] = [];
    if (account.limits?.suspended) flags.push("已暂停");
    if (account.limits?.degraded) flags.push("降级中");
    if (account.limits?.unmetered) flags.push("不计量");
    return {
      account, flags, error: limitsErrors.value[account.alias],
      windows: account.limits ? deriveAll(account.limits) : [],
    };
  }));

function usedColor(pct: number): string {
  if (pct >= 90) return "var(--danger)";
  if (pct >= 70) return "var(--warn)";
  return "var(--ok)";
}

async function reloadUsage() {
  try {
    await loadUsage(hours.value);
  } catch (e: any) {
    toast(`加载用量失败：${e.message}`, "error");
  }
}

async function reloadLimits() {
  try {
    limitsErrors.value = {};
    await refreshLimits();
    for (const result of store.limits?.accounts ?? []) {
      if (!result.ok) limitsErrors.value[result.alias] = result.error || "额度查询失败";
    }
  } catch (e: any) {
    toast(`刷新额度失败：${e.message}`, "error");
  }
}

async function reloadAccountLimits(alias: string) {
  limitsBusy.value = alias;
  delete limitsErrors.value[alias];
  try {
    await api(`/accounts/${encodeURIComponent(alias)}/limits`);
    toast(`已刷新 ${alias} 的额度`, "ok");
  } catch (e: any) {
    limitsErrors.value[alias] = e.message;
    toast(`刷新额度失败：${e.message}`, "error");
  } finally {
    await loadAccounts().catch(() => toast("加载账号失败", "error"));
    limitsBusy.value = "";
  }
}

function setHours(h: number) {
  hours.value = h;
  loadUsage(h).catch(() => undefined);
}

onMounted(() => {
  loadUsage(hours.value).catch(() => undefined);
  loadAccounts().catch(() => toast("加载账号失败", "error"));
});
</script>

<template>
  <!-- Chart panel -->
  <div class="panel">
    <div class="panel-head">
      <h3>用量趋势</h3>
      <span class="spacer"></span>
      <div class="seg">
        <button :class="{ active: hours === 24 }" @click="setHours(24)">24h</button>
        <button :class="{ active: hours === 72 }" @click="setHours(72)">3天</button>
        <button :class="{ active: hours === 168 }" @click="setHours(168)">7天</button>
      </div>
      <div class="seg">
        <button :class="{ active: metric === 'tokens' }" @click="metric = 'tokens'">Token</button>
        <button :class="{ active: metric === 'requests' }" @click="metric = 'requests'">请求</button>
      </div>
      <button class="btn ghost sm" @click="showTable = !showTable">
        {{ showTable ? "图表" : "表格" }}
      </button>
      <button class="btn ghost sm" @click="reloadUsage">刷新</button>
    </div>
    <div class="panel-body">
      <p v-if="store.usage" class="muted mb-2">
        共 {{ store.usage.totals.requests }} 次 ·
        入 {{ fmt(store.usage.totals.input_tokens) }} / 出 {{ fmt(store.usage.totals.output_tokens) }}
      </p>
      <p v-if="!store.usage?.totals.requests" class="muted">这段时间还没有请求记录。</p>

      <table v-else-if="showTable" class="data">
        <thead>
          <tr>
            <th>账号</th><th class="num">请求</th>
            <th class="num">输入</th><th class="num">输出</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in accountTotals" :key="row.alias">
            <td>
              <span class="dot" :style="{ background: row.color, display:'inline-block', marginRight:'6px' }"></span>
              {{ row.alias }}
            </td>
            <td class="num tnum">{{ row.requests }}</td>
            <td class="num tnum">{{ row.input.toLocaleString() }}</td>
            <td class="num tnum">{{ row.output.toLocaleString() }}</td>
          </tr>
        </tbody>
      </table>

      <div v-else style="position:relative" @mouseleave="hoverIndex = null">
        <svg :viewBox="`0 0 ${PLOT.width} ${PLOT.height}`" style="width:100%;height:auto;display:block"
             role="img" aria-label="用量柱状图">
          <g v-for="tick in yTicks" :key="tick">
            <line :x1="PLOT.left" :x2="PLOT.width - PLOT.right" :y1="y(tick)" :y2="y(tick)"
                  stroke="var(--border)" stroke-width="1" />
            <text :x="PLOT.left - 6" :y="y(tick) + 3" fill="var(--ink-3)" font-size="9.5" text-anchor="end">
              {{ fmt(tick) }}
            </text>
          </g>
          <line :x1="PLOT.left" :x2="PLOT.width - PLOT.right"
                :y1="PLOT.height - PLOT.bottom" :y2="PLOT.height - PLOT.bottom"
                stroke="var(--border-2)" stroke-width="1" />
          <g v-for="(column, index) in columns" :key="column.key">
            <template v-if="column.total > 0">
              <template v-for="(seg, si) in column.segments" :key="seg.alias">
                <rect
                  :x="PLOT.left + index * barSlot + 1"
                  :y="y(column.segments.slice(0, si + 1).reduce((s, it) => s + it.value, 0))"
                  :width="barWidth"
                  :height="Math.max(1, y(0) - y(seg.value))"
                  :fill="seg.color"
                  :rx="column.segments.length === 1 ? 2 : 0" />
              </template>
            </template>
            <text v-if="index % 4 === 0" :x="PLOT.left + index * barSlot + barSlot / 2"
                  :y="PLOT.height - 6" fill="var(--ink-3)" font-size="9" text-anchor="middle">
              {{ column.label }}
            </text>
            <rect :x="PLOT.left + index * barSlot" y="0" :width="barSlot" :height="PLOT.height"
                  fill="transparent" @mouseenter="hoverIndex = index" />
          </g>
        </svg>
        <div v-if="hoverIndex !== null && columns[hoverIndex]"
             style="position:absolute;top:4px;background:var(--surface);border:1px solid var(--border-2);
                    border-radius:8px;padding:8px 12px;font-size:12px;box-shadow:var(--shadow-lg);
                    pointer-events:none;min-width:130px;z-index:5"
             :style="{ left: (8 + (hoverIndex / 24) * 88) + '%' }">
          <div class="muted mb-2">{{ columns[hoverIndex].label }}</div>
          <div v-if="!columns[hoverIndex].total" class="muted">无请求</div>
          <div v-for="seg in [...columns[hoverIndex].segments].reverse()" :key="seg.alias"
               class="row" style="gap:6px;justify-content:space-between">
            <span>
              <span class="dot" :style="{ background: seg.color, display:'inline-block', marginRight:'4px' }"></span>
              {{ seg.alias }}
            </span>
            <span class="tnum">{{ seg.value.toLocaleString() }}</span>
          </div>
        </div>
      </div>

      <div v-if="!showTable && seriesNames.length >= 2 && store.usage?.totals.requests"
           class="row mt-2" style="gap:14px">
        <span v-for="(alias, i) in seriesNames" :key="alias" class="muted row" style="gap:5px">
          <span class="dot" :style="{ background: `var(${SERIES[i % SERIES.length]})` }"></span>
          {{ alias }}
        </span>
      </div>
    </div>
  </div>

  <!-- Limits -->
  <div class="panel">
    <div class="panel-head">
      <h3>用量额度</h3>
      <span class="chip">缓存 · 手动刷新</span>
      <span class="spacer"></span>
      <button class="btn ghost sm" :disabled="store.limitsLoading || !!limitsBusy" @click="reloadLimits">
        {{ store.limitsLoading ? "读取中…" : "批量刷新额度" }}
      </button>
    </div>
    <div class="panel-body">
      <p class="muted mb-2">
        <b style="color:var(--ink-2)">匀速线</b> 是按窗口时长匀速消耗的参照；
        实际用量在它左侧为落后（省），右侧为超前（费）。
      </p>
      <p v-if="!limitsViews.length" class="muted">还没有账号。</p>

      <div v-for="view in limitsViews" :key="view.account.alias" class="mb-3"
           style="padding-top:12px;border-top:1px solid var(--border)">
        <div class="row mb-2">
          <span style="font-weight:600" :title="view.account.alias">{{ accountName(view.account) }}</span>
          <AccountStatus :account="view.account" />
          <span v-for="flag in view.flags" :key="flag" class="chip warn">{{ flag }}</span>
          <span v-if="view.error" class="chip danger" :title="view.error">读取失败</span>
          <span class="spacer"></span>
          <button class="btn ghost sm" :disabled="store.limitsLoading || !!limitsBusy"
                  @click="reloadAccountLimits(view.account.alias)">刷新额度</button>
        </div>
        <p v-if="!view.windows.length" class="muted">暂无窗口缓存；可手动刷新额度。</p>
        <div v-else class="limits-windows">
          <div v-for="w in view.windows" :key="w.name">
            <div class="row" style="justify-content:space-between; margin-bottom:5px">
              <span class="muted" style="font-size:12px">{{ w.label }}</span>
              <span class="tnum" style="font-size:16px;font-weight:600"
                    :style="{ color: usedColor(w.usedPct) }">{{ w.usedPct }}%</span>
            </div>
            <div class="meter pace" :title="`匀速线 ${w.pacePct}%`">
              <div class="meter-fill"
                   :style="{ width: Math.min(100, w.usedPct) + '%', background: usedColor(w.usedPct) }"></div>
              <div v-if="w.pacePct > 0" class="pace-mark" :style="{ left: w.pacePct + '%' }"></div>
            </div>
            <div class="row mt-2" style="font-size:11.5px; justify-content:space-between">
              <span v-if="w.deltaText" :style="{ color: w.ahead ? 'var(--warn)' : 'var(--ok)' }">
                {{ w.ahead ? "▲ 超前" : "▼ 落后" }} {{ Math.abs(w.delta) }}%
              </span>
              <span class="muted">剩 {{ compact(w.remaining) }}</span>
            </div>
            <div class="muted" style="font-size:11px;margin-top:2px">{{ w.resetText }}</div>
            <WindowTokens :models="w.models" />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.limits-windows {
  display: grid; grid-auto-flow: column; grid-auto-columns: minmax(210px, 1fr);
  gap: 14px; overflow-x: auto; padding-bottom: 8px;
}
</style>
