<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import { loadSchedule, store, toast } from "../store";
import type { ScheduleSettings } from "../types";

const busy = ref(false);
const ceiling = ref(0.9);

const dirty = computed(() =>
  !!store.schedule &&
  Math.abs(store.schedule.max_utilization - ceiling.value) > 1e-9);

function adopt() {
  if (!store.schedule) return;
  ceiling.value = store.schedule.max_utilization;
}

async function save() {
  busy.value = true;
  try {
    store.schedule = await api<ScheduleSettings>("/api/schedule", {
      method: "POST",
      body: JSON.stringify({ max_utilization: ceiling.value }),
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
      <span class="chip accent">重置优先 + Fable 使用偏好</span>
      <span v-if="store.schedule" class="chip">
        硬阈值 {{ (store.schedule.max_utilization * 100).toFixed(0) }}%
      </span>
    </div>
    <div class="panel-body">
      <p class="muted mb-2" style="margin-bottom:14px">
        决定新会话分配给哪个账号。已经开始的对话仍固定在原账号上，不会中途切换。
      </p>

      <p class="muted">
        固定使用重置优先策略，并结合 Fable 使用偏好分配账号；不提供模式切换。
      </p>

      <div style="margin-top:18px">
        <label for="schedule-ceiling" style="margin-bottom:6px">
          调度硬阈值：<b class="tnum">{{ (ceiling * 100).toFixed(0) }}%</b>
        </label>
        <input id="schedule-ceiling" v-model.number="ceiling" type="range" min="0.1" max="1" step="0.01"
               :disabled="busy || !store.schedule"
               style="padding:0;border:none;background:none;accent-color:var(--accent)" />
        <p class="muted mt-2">
          默认 90%，可设为 10%–100%。调度使用本地缓存的额度窗口，
          包括 7d_claude 和 Fable 5 / 5-1 共享的 7d_fable。
          任一适用窗口达到阈值即停止分配，不以超额账号兜底。
        </p>
      </div>

      <div class="row" style="margin-top:18px">
        <button class="btn" :disabled="busy || !dirty" @click="save">保存</button>
        <button class="btn ghost" :disabled="busy || !dirty" @click="adopt">撤销</button>
      </div>

      <p class="muted mt-3">
        缓存 TTL：{{ store.schedule?.limits_ttl ?? 600 }} 秒。读取或保存设置不会刷新额度；
        如需更新，请到「用量」页手动刷新。
      </p>
    </div>
  </div>
</template>
