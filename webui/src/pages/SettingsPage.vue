<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import { loadSchedule, store, toast } from "../store";
import type { PageId, ThemeMode } from "../main";

const props = defineProps<{
  theme: ThemeMode;
  skin: string;
}>();
const emit = defineEmits<{
  "update:theme": [theme: ThemeMode];
  "update:skin": [skin: string];
  "edit-key": [];
}>();

const copied = ref("");

// 用量上限 (dispatch ceiling): the only scheduling knob. Above it an account is
// skipped on the request path; a fully-spent pool answers a local 429 rather
// than forwarding upstream.
const ceiling = ref(0.98);
const ceilingBusy = ref(false);
const ceilingDirty = computed(() =>
  !!store.schedule && Math.abs(store.schedule.max_utilization - ceiling.value) > 1e-9);

function adoptCeiling() {
  if (store.schedule) ceiling.value = store.schedule.max_utilization;
}

async function saveCeiling() {
  ceilingBusy.value = true;
  try {
    store.schedule = await api("/api/schedule", {
      method: "POST",
      body: JSON.stringify({ max_utilization: ceiling.value }),
    });
    adoptCeiling();
    toast("用量上限已保存", "ok");
  } catch (e: any) {
    toast(`保存失败：${e.message}`, "error");
  } finally {
    ceilingBusy.value = false;
  }
}

onMounted(async () => {
  try {
    await loadSchedule();
  } catch {
    /* the overview/connect already hydrates it; ignore a soft failure */
  }
  adoptCeiling();
});

async function copy(text: string, tag: string) {
  try {
    await navigator.clipboard.writeText(text);
    copied.value = tag;
    setTimeout(() => { if (copied.value === tag) copied.value = ""; }, 1500);
  } catch {
    /* clipboard may be blocked */
  }
}

const base = typeof location !== "undefined" ? location.origin : "http://127.0.0.1:8787";

const endpoints = [
  { name: "Anthropic Messages", path: "/v1/messages", method: "POST" },
  { name: "OpenAI Chat Completions", path: "/v1/chat/completions", method: "POST" },
  { name: "模型列表", path: "/v1/models", method: "GET" },
  { name: "Token 计数", path: "/v1/messages/count_tokens", method: "POST" },
];

function curlExample(path: string): string {
  return `curl -X POST "${base}${path}" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer sk-..." \\
  -H "X-Mirofish-Account: work" \\
  -d '{"model":"claude-sonnet-4-5","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'`;
}
</script>

<template>
  <!-- Appearance -->
  <div class="panel">
    <div class="panel-head"><h3>外观</h3></div>
    <div class="panel-body">
      <div class="field-row">
        <div class="grow">
          <label>主题</label>
          <div class="seg" style="width:fit-content">
            <button :class="{ active: props.theme === 'system' }"
                    @click="emit('update:theme', 'system')">跟随系统</button>
            <button :class="{ active: props.theme === 'light' }"
                    @click="emit('update:theme', 'light')">浅色</button>
            <button :class="{ active: props.theme === 'dark' }"
                    @click="emit('update:theme', 'dark')">深色</button>
          </div>
        </div>
        <div class="grow">
          <label>皮肤</label>
          <div class="seg" style="width:fit-content">
            <button :class="{ active: props.skin === 'plain' }"
                    @click="emit('update:skin', 'plain')">标准</button>
            <button :class="{ active: props.skin === 'miku' }"
                    @click="emit('update:skin', 'miku')">Miku ♪</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Dispatch ceiling -->
  <div class="panel">
    <div class="panel-head">
      <h3>用量上限</h3>
      <span class="spacer"></span>
      <span class="chip">{{ (ceiling * 100).toFixed(0) }}%</span>
    </div>
    <div class="panel-body">
      <p class="muted mb-2">
        账号在任一相关窗口（5 小时 / 7 天 / 7 天 Claude / fable 另看 7 天 Fable）
        用量达到此上限后，就不再被分配新请求，也不会转发到上游；所有账号都到顶时
        本机直接返回 429，等窗口重置。上游返回 429 时会自动换号重试。
      </p>
      <div class="field-row">
        <div class="grow">
          <input type="range" min="0.5" max="1" step="0.01" v-model.number="ceiling" />
        </div>
        <span class="chip accent" style="min-width:56px;text-align:center">
          {{ (ceiling * 100).toFixed(0) }}%
        </span>
      </div>
      <div class="row mt-2">
        <button class="btn" :disabled="ceilingBusy || !ceilingDirty" @click="saveCeiling">保存</button>
        <button class="btn ghost" :disabled="ceilingBusy || !ceilingDirty" @click="adoptCeiling">撤销</button>
      </div>
    </div>
  </div>

  <!-- Connection -->
  <div class="panel">
    <div class="panel-head">
      <h3>连接</h3>
      <span class="spacer"></span>
      <button class="btn ghost sm" @click="emit('edit-key')">更换密钥</button>
    </div>
    <div class="panel-body">
      <dl class="kv">
        <dt>服务状态</dt>
        <dd>
          <span class="chip" :class="store.connected ? 'ok' : 'danger'">
            {{ store.connected ? "已连接" : "未连接" }}
          </span>
        </dd>
        <dt v-if="store.health">版本</dt>
        <dd v-if="store.health" class="mono">v{{ store.health.version }}</dd>
        <dt v-if="store.health">代理后端</dt>
        <dd v-if="store.health">{{ store.health.proxy_backend }}</dd>
        <dt v-if="store.health?.default_account">默认账号</dt>
        <dd v-if="store.health?.default_account" class="mono">
          {{ store.health.default_account }}
        </dd>
        <dt>本机地址</dt>
        <dd class="mono" style="font-size:12px">{{ base }}</dd>
      </dl>
    </div>
  </div>

  <!-- Endpoints -->
  <div class="panel">
    <div class="panel-head">
      <h3>中转端点</h3>
      <span class="chip">Authorization: Bearer &lt;你的 key&gt;</span>
    </div>
    <div class="panel-body">
      <p class="muted mb-2">
        把客户端的 base URL 指到上面的本机地址，使用
        <span class="mono">X-Mirofish-Proxy-Key</span> 或
        <span class="mono">Authorization: Bearer</span> 鉴权。
        <span class="mono">X-Mirofish-Account</span> 可固定某个账号。
      </p>
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr><th>端点</th><th>方法</th><th>路径</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="ep in endpoints" :key="ep.path">
              <td>{{ ep.name }}</td>
              <td><span class="chip">{{ ep.method }}</span></td>
              <td class="mono">{{ ep.path }}</td>
              <td>
                <button class="btn ghost sm" @click="copy(base + ep.path, ep.path)">
                  {{ copied === ep.path ? "已复制" : "复制 URL" }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="mt-3">
        <label>示例（curl）</label>
        <pre class="output">{{ curlExample("/v1/messages") }}</pre>
        <button class="btn ghost sm mt-2" @click="copy(curlExample('/v1/messages'), 'curl')">
          {{ copied === "curl" ? "已复制" : "复制示例" }}
        </button>
      </div>
    </div>
  </div>
</template>
