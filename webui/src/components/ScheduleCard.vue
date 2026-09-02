<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import { loadSchedule, store, toast } from "../store";

const busy = ref(false);
const collapsed = ref(false);
type Mode = "balanced" | "reset_first" | "fable_first";

const MODE_LABEL: Record<Mode, string> = {
  balanced: "均衡分配",
  reset_first: "优先重置窗口",
  fable_first: "优先重置窗口 + Fable 已用最高",
};

const mode = ref<Mode>("balanced");
const ceiling = ref(0.98);

const modeLabel = computed(() => MODE_LABEL[mode.value] ?? MODE_LABEL.balanced);
const dirty = computed(() =>
  !!store.schedule &&
  (store.schedule.mode !== mode.value ||
    Math.abs(store.schedule.max_utilization - ceiling.value) > 1e-9));

function adopt(): void {
  if (!store.schedule) return;
  mode.value = store.schedule.mode;
  ceiling.value = store.schedule.max_utilization;
}

onMounted(async () => {
  try {
    await loadSchedule();
    adopt();
  } catch (error: any) {
    toast(`读取调度设置失败：${error.message}`, "error");
  }
});

async function save(): Promise<void> {
  busy.value = true;
  try {
    store.schedule = await api("/api/schedule", {
      method: "POST",
      body: JSON.stringify({ mode: mode.value, max_utilization: ceiling.value }),
    });
    adopt();
    toast("调度设置已保存", "ok");
  } catch (error: any) {
    toast(`保存调度设置失败：${error.message}`, "error");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="card" :class="{ collapsed }">
    <h2>
      账号调度
      <span v-if="store.schedule" class="badge">
        {{ modeLabel }}
      </span>
      <span v-if="store.schedule" class="badge">
        用量上限 {{ (store.schedule.max_utilization * 100).toFixed(0) }}%
      </span>
      <span class="spacer"></span>
      <button class="ghost small" type="button"
              :aria-expanded="!collapsed" aria-controls="schedule-card-body"
              @click="collapsed = !collapsed">
        {{ collapsed ? "展开" : "折叠" }}
      </button>
    </h2>

    <div id="schedule-card-body" v-show="!collapsed">
      <p class="muted" style="margin-top: 0">
        决定新会话分配给哪个账号。已经开始的对话仍固定在原账号上，不会中途切换。
      </p>

      <div class="modes">
        <label class="mode" :class="{ picked: mode === 'balanced' }">
          <input v-model="mode" type="radio" value="balanced" />
          <span>
            <b>均衡分配</b>
            <small>把新会话交给活跃会话最少的账号，各账号用量平均。</small>
          </span>
        </label>
        <label class="mode" :class="{ picked: mode === 'reset_first' }">
          <input v-model="mode" type="radio" value="reset_first" />
          <span>
            <b>优先重置窗口</b>
            <small>
              同样按活跃会话数均分，只是给 48 小时内要重置的账号一点提前量
              （最多相当于 2 个会话），把快清零的额度先花掉；
              提前量用完就回到正常轮换，不会把并发堆到一个账号上。
            </small>
          </span>
        </label>
        <label class="mode" :class="{ picked: mode === 'fable_first' }">
          <input v-model="mode" type="radio" value="fable_first" />
          <span>
            <b>优先重置窗口 + Fable 已用最高</b>
            <small>
              在「优先重置窗口」基础上，对<b>非 fable</b> 模型的请求再看一层：
              48 小时内要重置的账号中，7 天 Fable 窗口已用越高的越先用。
              这些账号的 fable 额度本就用尽（发 fable 请求也会被拒），
              但通用额度即将清零，正好先花掉；fable 额度还有余量的账号则留给
              fable 请求。fable 请求本身仍按「优先重置窗口」分配。
            </small>
          </span>
        </label>
      </div>

      <div class="row" style="margin-top: 10px">
        <div class="grow">
          <label class="ceiling">用量上限：{{ (ceiling * 100).toFixed(0) }}%</label>
          <input v-model.number="ceiling" type="range" min="0.5" max="1.2" step="0.01" />
          <p class="muted">
            所有模式都生效：账号用量超过此值后排到所有有余量的账号之后；
            窗口用满（约 100%）的账号会被自动分配直接跳过，避免烧超额度，
            直到所有账号都用满才继续兜底服务。claude-fable-5 另有独立的
            7 天窗口（7d_fable），取两者中更满的一个来判断。
          </p>
        </div>
      </div>

      <div class="row" style="margin-top: 10px">
        <button :disabled="busy || !dirty" @click="save">保存</button>
        <button class="ghost" :disabled="busy || !dirty" @click="adopt">撤销</button>
      </div>

      <p class="muted">
        额度数据每 5 分钟在后台刷新一次（零模型开销），不会拖慢请求。
        判断偏差最多让某次请求多试一个账号：上游返回 429 时会自动换号重试。
      </p>
    </div>
  </section>
</template>

<style scoped>
.card.collapsed > h2 { margin-bottom: 0; }
.modes { display: grid; gap: 8px; }
.mode {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 10px; border: 1px solid var(--line); border-radius: 8px; cursor: pointer;
  /* The global rule makes every label a block with vertical margins; inside a
     card these are option rows, not field captions. */
  margin: 0; font-size: 13px; color: inherit;
}
.mode.picked { border-color: var(--accent); }
.mode small { display: block; color: var(--muted); margin-top: 2px; }
/* Inputs are 100% wide globally, which would stretch a radio across the row. */
.mode input[type="radio"] { width: auto; flex: none; margin: 3px 0 0; }
.ceiling { margin: 0 0 4px; }
/* The global input box styling (border, padding, page background) would frame
   the slider like a text field. */
input[type="range"] { padding: 0; border: none; background: none; }
</style>
