<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import { loadSchedule, store, toast } from "../store";

const busy = ref(false);
type Mode = "balanced" | "reset_first" | "fable_first";

const MODES: { id: Mode; label: string; desc: string }[] = [
  {
    id: "balanced",
    label: "均衡分配",
    desc: "把新会话交给活跃会话最少的账号，各账号用量平均。",
  },
  {
    id: "reset_first",
    label: "优先重置窗口",
    desc: "同样按活跃会话数均分，给 48 小时内要重置的账号一点提前量（最多相当于 2 个会话），把快清零的额度先花掉。",
  },
  {
    id: "fable_first",
    label: "优先重置 + Fable 已用最高",
    desc: "在「优先重置窗口」基础上，对非 fable 模型的请求再看一层：48 小时内要重置的账号中，7 天 Fable 窗口已用越高的越先用。fable 请求本身仍按「优先重置窗口」分配。",
  },
];

const mode = ref<Mode>("balanced");
const ceiling = ref(0.98);

const dirty = computed(() =>
  !!store.schedule &&
  (store.schedule.mode !== mode.value ||
    Math.abs(store.schedule.max_utilization - ceiling.value) > 1e-9));

function adopt() {
  if (!store.schedule) return;
  mode.value = store.schedule.mode;
  ceiling.value = store.schedule.max_utilization;
}

async function save() {
  busy.value = true;
  try {
    store.schedule = await api("/api/schedule", {
      method: "POST",
      body: JSON.stringify({ mode: mode.value, max_utilization: ceiling.value }),
    });
    adopt();
    toast("调度设置已保存", "ok");
  } catch (e: any) {
    toast(`保存失败：${e.message}`, "error");
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  try {
    await loadSchedule();
    adopt();
  } catch (e: any) {
    toast(`读取调度设置失败：${e.message}`, "error");
  }
});
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <h3>账号调度</h3>
      <span v-if="store.schedule" class="chip accent">
        {{ MODES.find((m) => m.id === store.schedule!.mode)?.label || store.schedule.mode }}
      </span>
      <span v-if="store.schedule" class="chip">
        用量上限 {{ (store.schedule.max_utilization * 100).toFixed(0) }}%
      </span>
    </div>
    <div class="panel-body">
      <p class="muted mb-2" style="margin-bottom:14px">
        决定新会话分配给哪个账号。已经开始的对话仍固定在原账号上，不会中途切换。
      </p>

      <div style="display:grid;gap:8px">
        <label
          v-for="m in MODES"
          :key="m.id"
          style="display:flex;gap:10px;align-items:flex-start;padding:12px 14px;
                 border:1px solid var(--border-2);border-radius:var(--r);cursor:pointer;
                 margin:0;font-size:13px;color:inherit;transition:border-color .12s,background .12s"
          :style="{
            borderColor: mode === m.id ? 'var(--accent)' : 'var(--border-2)',
            background: mode === m.id ? 'var(--accent-soft)' : 'var(--surface)',
          }"
        >
          <input v-model="mode" type="radio" :value="m.id"
                 style="width:auto;flex:none;margin:3px 0 0" />
          <span>
            <b style="display:block;font-weight:600">{{ m.label }}</b>
            <small style="display:block;color:var(--ink-3);margin-top:3px;line-height:1.5">
              {{ m.desc }}
            </small>
          </span>
        </label>
      </div>

      <div style="margin-top:18px">
        <label style="margin-bottom:6px">
          用量上限：<b class="tnum">{{ (ceiling * 100).toFixed(0) }}%</b>
        </label>
        <input v-model.number="ceiling" type="range" min="0.5" max="1.2" step="0.01"
               style="padding:0;border:none;background:none;accent-color:var(--accent)" />
        <p class="muted mt-2">
          所有模式都生效：账号用量超过此值后排到所有有余量的账号之后；
          窗口用满（约 100%）的账号会被自动分配直接跳过，避免烧超额度，
          直到所有账号都用满才继续兜底服务。claude-fable-5 另有独立的
          7 天窗口（7d_fable），取两者中更满的一个来判断。
        </p>
      </div>

      <div class="row" style="margin-top:18px">
        <button class="btn" :disabled="busy || !dirty" @click="save">保存</button>
        <button class="btn ghost" :disabled="busy || !dirty" @click="adopt">撤销</button>
      </div>

      <p class="muted mt-3">
        额度数据每 5 分钟在后台刷新一次（零模型开销），不会拖慢请求。
        判断偏差最多让某次请求多试一个账号：上游返回 429 时会自动换号重试。
      </p>
    </div>
  </div>
</template>
