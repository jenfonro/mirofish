<script setup lang="ts">
import { computed, ref } from "vue";
import { loadUsage, store, toast } from "../store";

const SERIES_VARS = ["--series-1", "--series-2", "--series-3",
                     "--series-4", "--series-5", "--series-6"];
const MAX_SERIES = 5; // sixth slot is reserved for "其他"

const metric = ref<"tokens" | "requests">("tokens");
const showTable = ref(false);
const hoverIndex = ref<number | null>(null);

const PLOT = { width: 720, height: 190, top: 8, bottom: 22, left: 44, right: 8 };

function hourKeys(count: number): string[] {
  const keys: string[] = [];
  const now = new Date();
  for (let index = count - 1; index >= 0; index -= 1) {
    const at = new Date(now.getTime() - index * 3600_000);
    keys.push(at.toISOString().slice(0, 13) + ":00Z");
  }
  return keys;
}

function bucketValue(bucket: { requests: number; input_tokens: number; output_tokens: number }): number {
  return metric.value === "requests"
    ? bucket.requests
    : bucket.input_tokens + bucket.output_tokens;
}

/** Aliases in fixed first-seen-sorted order so colors follow the entity. */
const seriesNames = computed<string[]>(() => {
  const totals = new Map<string, number>();
  for (const bucket of store.usage?.buckets ?? []) {
    totals.set(bucket.alias, (totals.get(bucket.alias) ?? 0)
      + bucket.input_tokens + bucket.output_tokens + bucket.requests);
  }
  const sorted = [...totals.keys()].sort();
  if (sorted.length <= MAX_SERIES + 1) return sorted;
  const byVolume = [...totals.entries()].sort((a, b) => b[1] - a[1])
    .slice(0, MAX_SERIES).map(([alias]) => alias).sort();
  return [...byVolume, "其他"];
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
  const slotOf = new Map(named.map((name, index) => [name, index]));
  const grid: Map<string, number[]> = new Map(keys.map((key) => [key, named.map(() => 0)]));
  for (const bucket of store.usage?.buckets ?? []) {
    const row = grid.get(bucket.hour);
    if (!row) continue;
    const slot = slotOf.has(bucket.alias) ? slotOf.get(bucket.alias)!
      : hasOther ? named.length - 1 : -1;
    if (slot >= 0) row[slot] += bucketValue(bucket);
  }
  return keys.map((key) => {
    const row = grid.get(key)!;
    return {
      key,
      label: key.slice(11, 13) + ":00",
      total: row.reduce((sum, value) => sum + value, 0),
      segments: named.map((alias, index) => ({
        alias,
        value: row[index],
        color: `var(${SERIES_VARS[index % SERIES_VARS.length]})`,
      })).filter((segment) => segment.value > 0),
    };
  });
});

const maxTotal = computed(() => Math.max(1, ...columns.value.map((column) => column.total)));

const yTicks = computed<number[]>(() => {
  const top = maxTotal.value;
  const step = Math.pow(10, Math.floor(Math.log10(top)));
  const unit = top / step > 5 ? step * 2 : top / step > 2 ? step : step / 2;
  const ticks: number[] = [];
  for (let value = unit; value <= top; value += unit) ticks.push(value);
  return ticks.slice(0, 5);
});

function y(value: number): number {
  const inner = PLOT.height - PLOT.top - PLOT.bottom;
  return PLOT.height - PLOT.bottom - (value / maxTotal.value) * inner;
}

const barSlot = computed(() => (PLOT.width - PLOT.left - PLOT.right) / 24);
const barWidth = computed(() => Math.max(4, barSlot.value - 2)); // 2px gap between bars

function formatNumber(value: number): string {
  if (value >= 1_000_000) return (value / 1_000_000).toFixed(1) + "M";
  if (value >= 1_000) return (value / 1_000).toFixed(1) + "k";
  return String(value);
}

const accountTotals = computed(() => {
  const named = seriesNames.value;
  const hasOther = named[named.length - 1] === "其他";
  const slotOf = new Map(named.map((name, index) => [name, index]));
  const totals = named.map((alias, index) => ({
    alias,
    color: `var(${SERIES_VARS[index % SERIES_VARS.length]})`,
    requests: 0, input: 0, output: 0,
  }));
  for (const bucket of store.usage?.buckets ?? []) {
    const slot = slotOf.has(bucket.alias) ? slotOf.get(bucket.alias)!
      : hasOther ? named.length - 1 : -1;
    if (slot < 0) continue;
    totals[slot].requests += bucket.requests;
    totals[slot].input += bucket.input_tokens;
    totals[slot].output += bucket.output_tokens;
  }
  return totals;
});

async function reload() {
  try {
    await loadUsage();
  } catch (error: any) {
    toast(`加载用量失败：${error.message}`, "error");
  }
}
</script>

<template>
  <section class="card">
    <h2>
      近 24 小时用量
      <span class="spacer"></span>
      <div class="seg">
        <button class="ghost small" :class="{ active: metric === 'tokens' }"
                @click="metric = 'tokens'">Token</button>
        <button class="ghost small" :class="{ active: metric === 'requests' }"
                @click="metric = 'requests'">请求数</button>
      </div>
      <button class="ghost small" @click="showTable = !showTable">
        {{ showTable ? "看图表" : "看表格" }}
      </button>
      <button class="ghost small" @click="reload">刷新</button>
    </h2>

    <p v-if="store.usage" class="muted totals-line">
      共 {{ store.usage.totals.requests }} 次请求 ·
      输入 {{ formatNumber(store.usage.totals.input_tokens) }} /
      输出 {{ formatNumber(store.usage.totals.output_tokens) }} tokens
    </p>

    <p v-if="!store.usage || !store.usage.totals.requests" class="muted">
      这段时间还没有请求记录。
    </p>

    <table v-else-if="showTable">
      <thead>
        <tr><th>账号</th><th class="num">请求</th><th class="num">输入 tokens</th>
            <th class="num">输出 tokens</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in accountTotals" :key="row.alias">
          <td><span class="swatch" :style="{ background: row.color }"></span>{{ row.alias }}</td>
          <td class="num">{{ row.requests }}</td>
          <td class="num">{{ row.input.toLocaleString() }}</td>
          <td class="num">{{ row.output.toLocaleString() }}</td>
        </tr>
      </tbody>
    </table>

    <div v-else class="chart-wrap" @mouseleave="hoverIndex = null">
      <svg :viewBox="`0 0 ${PLOT.width} ${PLOT.height}`" class="chart" role="img"
           aria-label="按小时和账号堆叠的用量柱状图">
        <g v-for="tick in yTicks" :key="tick">
          <line :x1="PLOT.left" :x2="PLOT.width - PLOT.right" :y1="y(tick)" :y2="y(tick)"
                class="gridline" />
          <text :x="PLOT.left - 6" :y="y(tick) + 3" class="tick" text-anchor="end">
            {{ formatNumber(tick) }}
          </text>
        </g>
        <line :x1="PLOT.left" :x2="PLOT.width - PLOT.right"
              :y1="PLOT.height - PLOT.bottom" :y2="PLOT.height - PLOT.bottom"
              class="axisline" />
        <g v-for="(column, index) in columns" :key="column.key">
          <template v-if="column.total > 0">
            <template v-for="(segment, si) in column.segments" :key="segment.alias">
              <rect
                :x="PLOT.left + index * barSlot + 1"
                :y="y(column.segments.slice(0, si + 1).reduce((s, item) => s + item.value, 0))"
                :width="barWidth"
                :height="Math.max(1, y(0) - y(segment.value))"
                :fill="segment.color"
                :rx="column.segments.length === 1 ? 3 : 0"
                class="bar-segment" />
            </template>
          </template>
          <text v-if="index % 4 === 0" :x="PLOT.left + index * barSlot + barSlot / 2"
                :y="PLOT.height - 6" class="tick" text-anchor="middle">
            {{ column.label }}
          </text>
          <rect :x="PLOT.left + index * barSlot" y="0" :width="barSlot" :height="PLOT.height"
                fill="transparent" @mouseenter="hoverIndex = index" />
        </g>
      </svg>
      <div v-if="hoverIndex !== null && columns[hoverIndex]" class="tooltip"
           :style="{ left: (8 + (hoverIndex / 24) * 88) + '%' }">
        <div class="tooltip-title">{{ columns[hoverIndex].label }}（本地时区 UTC 小时）</div>
        <div v-if="!columns[hoverIndex].total" class="muted">无请求</div>
        <div v-for="segment in [...columns[hoverIndex].segments].reverse()" :key="segment.alias"
             class="tooltip-row">
          <span class="swatch" :style="{ background: segment.color }"></span>
          <span class="tooltip-alias">{{ segment.alias }}</span>
          <span class="tooltip-value">{{ segment.value.toLocaleString() }}</span>
        </div>
      </div>
    </div>

    <div v-if="!showTable && seriesNames.length >= 2 && store.usage?.totals.requests"
         class="legend">
      <span v-for="(alias, index) in seriesNames" :key="alias" class="legend-item">
        <span class="swatch" :style="{ background: `var(${SERIES_VARS[index % SERIES_VARS.length]})` }"></span>
        {{ alias }}
      </span>
    </div>
  </section>
</template>

<style scoped>
.seg { display: inline-flex; gap: 0; }
.seg button { border-radius: 0; }
.seg button:first-child { border-radius: 8px 0 0 8px; }
.seg button:last-child { border-radius: 0 8px 8px 0; border-left: none; }
.seg button.active { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
.totals-line { margin: 0 0 10px; }
.chart-wrap { position: relative; }
.chart { width: 100%; height: auto; display: block; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.axisline { stroke: var(--baseline); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 9.5px; }
.bar-segment { stroke: var(--surface); stroke-width: 1; }
.tooltip {
  position: absolute;
  top: 4px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 12px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.14);
  pointer-events: none;
  min-width: 130px;
  z-index: 5;
}
.tooltip-title { color: var(--ink-2); margin-bottom: 4px; }
.tooltip-row { display: flex; align-items: center; gap: 6px; }
.tooltip-alias { color: var(--ink); }
.tooltip-value { margin-left: auto; font-variant-numeric: tabular-nums; }
.swatch {
  display: inline-block; width: 9px; height: 9px; border-radius: 2px;
  margin-right: 6px; vertical-align: baseline; flex: none;
}
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; }
.legend-item { display: inline-flex; align-items: center; font-size: 12px; color: var(--ink-2); }
</style>
