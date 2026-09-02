<script setup lang="ts">
import { computed } from "vue";
import { loadLimits, store, toast } from "../store";
import { compact, deriveAll, type DerivedWindow } from "../limits";
import type { AccountLimitsResult } from "../types";

interface AccountView {
  alias: string;
  ok: boolean;
  error?: string;
  status?: number;
  flags: string[];
  windows: DerivedWindow[];
}

const views = computed<AccountView[]>(() =>
  (store.limits?.accounts ?? []).map((entry: AccountLimitsResult) => {
    const flags: string[] = [];
    if (entry.limits?.suspended) flags.push("已暂停");
    if (entry.limits?.degraded) flags.push("降级中");
    if (entry.limits?.unmetered) flags.push("不计量");
    return {
      alias: entry.alias,
      ok: entry.ok,
      error: entry.error,
      status: entry.status,
      flags,
      windows: entry.limits ? deriveAll(entry.limits) : [],
    };
  }));

const hasAny = computed(() => views.value.some((v) => v.ok && v.windows.length));

function usedColor(pct: number): string {
  if (pct >= 90) return "var(--critical)";
  if (pct >= 70) return "var(--warning)";
  return "var(--good)";
}

async function reload() {
  try {
    await loadLimits();
    toast("已刷新用量额度", "ok");
  } catch (error: any) {
    toast(`刷新额度失败：${error.message}`, "error");
  }
}
</script>

<template>
  <section class="card">
    <h2>
      用量额度
      <span class="spacer"></span>
      <button class="ghost small" :disabled="store.limitsLoading" @click="reload">
        {{ store.limitsLoading ? "读取中…" : "刷新" }}
      </button>
    </h2>

    <p class="muted intro">
      来自上游 <span class="mono">/v1/limits</span>，与官方用量挂件同源，不消耗额度。
      <b>匀速线</b> 是按窗口时长匀速消耗到当前应达到的百分比（站内平均参照）；
      实际用量在它<b>左侧为落后</b>（省），<b>右侧为超前</b>（费）。
    </p>

    <p v-if="!store.limits" class="muted">尚未读取。点「刷新」获取各账号额度。</p>
    <p v-else-if="!views.length" class="muted">还没有账号。</p>
    <p v-else-if="!hasAny && !views.some((v) => !v.ok)" class="muted">
      上游未返回任何窗口数据。
    </p>

    <div v-for="view in views" :key="view.alias" class="acct">
      <div class="acct-head">
        <span class="mono acct-name">{{ view.alias }}</span>
        <span v-for="flag in view.flags" :key="flag" class="badge warn">{{ flag }}</span>
        <span v-if="!view.ok" class="badge bad">
          读取失败{{ view.status ? `（${view.status}）` : "" }}
        </span>
      </div>

      <p v-if="!view.ok" class="muted err">{{ view.error || "上游拒绝了额度查询" }}</p>
      <p v-else-if="!view.windows.length" class="muted">该账号无窗口数据。</p>

      <div v-else class="windows">
        <div v-for="w in view.windows" :key="w.name" class="win">
          <div class="win-top">
            <span class="win-label">{{ w.label }}</span>
            <span class="win-pct" :style="{ color: usedColor(w.usedPct) }">
              {{ w.usedPct }}%
            </span>
          </div>

          <div class="meter" :title="`匀速线 ${w.pacePct}%`">
            <div class="meter-fill"
                 :style="{ width: Math.min(100, w.usedPct) + '%', background: usedColor(w.usedPct) }"></div>
            <div v-if="w.pacePct > 0" class="pace" :style="{ left: w.pacePct + '%' }"></div>
          </div>

          <div class="win-meta">
            <span v-if="w.deltaText" class="delta"
                  :class="w.ahead ? 'ahead' : 'behind'">
              {{ w.ahead ? "▲ 超前" : "▼ 落后" }} {{ Math.abs(w.delta) }}%
            </span>
            <span class="spacer"></span>
            <span class="rem" :title="`剩余 ${Math.round(w.remaining).toLocaleString()} / ${Math.round(w.budget).toLocaleString()}`">
              剩 {{ compact(w.remaining) }}
            </span>
          </div>
          <div class="win-reset muted">{{ w.resetText }}</div>

          <!-- 7d_fable is one upstream number shared by every fable model;
               this is the local split of who spent it this window. -->
          <div v-if="w.models.length" class="split">
            <div v-for="m in w.models" :key="m.model" class="split-row"
                 :title="`${m.model}：${m.requests} 次请求 · `
                   + `输入 ${m.input_tokens.toLocaleString()} · 输出 ${m.output_tokens.toLocaleString()} · `
                   + `缓存读 ${m.cache_read_tokens.toLocaleString()} · 缓存写 ${m.cache_write_tokens.toLocaleString()}`">
              <span class="split-name mono">{{ m.model.replace("claude-", "") }}</span>
              <span class="split-req muted">{{ m.requests }} 次</span>
              <span class="split-tok">{{ compact(m.total_tokens) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.intro { margin: 0 0 14px; line-height: 1.55; }
.intro b { color: var(--ink-2); font-weight: 600; }
.acct { padding: 12px 0; border-top: 1px solid var(--border); }
.acct:first-of-type { border-top: none; }
.acct-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.acct-name { font-size: 14px; }
.badge.warn { background: color-mix(in srgb, var(--warning) 18%, transparent); }
.badge.bad { background: color-mix(in srgb, var(--critical) 16%, transparent); color: var(--critical); }
.err { margin: 0; }

.windows {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}
@media (max-width: 620px) { .windows { grid-template-columns: 1fr; } }

.win-top { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 6px; }
.win-label { font-size: 12px; color: var(--ink-2); }
.win-pct { font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; }

/* Per-model split of the shared fable window. */
.split { margin-top: 6px; display: grid; gap: 2px; }
.split-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 6px;
  align-items: baseline;
  font-size: 11px;
}
.split-name { color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; }
.split-tok { font-variant-numeric: tabular-nums; }

.meter {
  position: relative;
  height: 10px;
  background: var(--grid);
  border-radius: 6px;
  overflow: hidden;
}
.meter-fill { position: absolute; inset: 0 auto 0 0; border-radius: 6px 0 0 6px; }
/* Pace line (匀速线): the even-rate reference. overflow:hidden clips it to the track. */
.pace {
  position: absolute;
  top: -2px;
  bottom: -2px;
  width: 2px;
  background: var(--ink);
  opacity: 0.55;
  transform: translateX(-1px);
}

.win-meta {
  display: flex; align-items: center; gap: 6px;
  margin-top: 6px; font-size: 12px;
}
.delta { font-variant-numeric: tabular-nums; }
.delta.ahead { color: var(--warning); }
.delta.behind { color: var(--good-text); }
.rem { color: var(--ink-2); font-variant-numeric: tabular-nums; }
.win-reset { font-size: 11px; margin-top: 3px; }
</style>
