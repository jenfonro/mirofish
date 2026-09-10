<script setup lang="ts">
import { computed, ref } from "vue";
import { api, streamChat } from "../api";
import { loadAccounts, loadUsage, store, toast } from "../store";

const account = ref("");
const model = ref("");
const models = ref<string[]>([]);
const prompt = ref("你好，请用一句话介绍你自己。");
const maxTokens = ref(256);
const output = ref("");
const usageLine = ref("");
const running = ref(false);
const elapsed = ref(0);
let controller: AbortController | null = null;
let timer: ReturnType<typeof setInterval> | null = null;

const accountOptions = computed(() => store.accounts.map((a) => a.alias));

async function loadModels() {
  try {
    const extra: Record<string, string> = {};
    if (account.value) extra["X-Mirofish-Account"] = account.value;
    const data = await api<{ models?: string[]; default_model?: string }>(
      "/v1/models", { headers: extra },
    );
    models.value = data.models || [];
    if (!model.value || !models.value.includes(model.value)) {
      const preferred = data.default_model;
      model.value = preferred && models.value.includes(preferred)
        ? preferred : (models.value[0] || "");
    }
    if (!models.value.length) toast("上游没有返回模型列表", "info");
  } catch (e: any) {
    toast(`加载模型失败：${e.message}`, "error");
  }
}

async function send() {
  if (running.value) {
    controller?.abort();
    return;
  }
  output.value = "";
  usageLine.value = "";
  elapsed.value = 0;
  running.value = true;
  controller = new AbortController();
  timer = setInterval(() => { elapsed.value += 0.1; }, 100);
  try {
    await streamChat(
      {
        model: model.value || undefined,
        max_tokens: Number(maxTokens.value) || 256,
        messages: [{ role: "user", content: prompt.value }],
      },
      account.value,
      {
        onDelta(text) { output.value += text; },
        onUsage(u) {
          usageLine.value = `输入 ${u.prompt_tokens} / 输出 ${u.completion_tokens} tokens`;
        },
      },
      controller.signal,
    );
    loadAccounts().catch(() => undefined);
    loadUsage().catch(() => undefined);
  } catch (e: any) {
    if (e?.name === "AbortError") {
      output.value += "\n（已中止）";
    } else {
      output.value += (output.value ? "\n" : "") + `错误：${e.message}`;
    }
  } finally {
    running.value = false;
    controller = null;
    if (timer) { clearInterval(timer); timer = null; }
  }
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <h3>测试台</h3>
      <span class="chip warn">会产生真实模型调用</span>
      <span class="spacer"></span>
      <span v-if="running" class="muted tnum">{{ elapsed.toFixed(1) }}s</span>
    </div>
    <div class="panel-body">
      <div class="field-row">
        <div class="grow">
          <label>账号（留空按默认/轮询选择）</label>
          <select v-model="account">
            <option value="">自动选择</option>
            <option v-for="a in accountOptions" :key="a" :value="a">{{ a }}</option>
          </select>
        </div>
        <div class="grow">
          <label>模型</label>
          <div class="row" style="gap:6px">
            <div style="flex:1;min-width:140px">
              <select v-if="models.length" v-model="model">
                <option v-for="id in models" :key="id" :value="id">{{ id }}</option>
              </select>
              <input v-else v-model="model" placeholder="留空使用默认模型" />
            </div>
            <button class="btn ghost" title="从上游读取模型目录（零调用成本）"
                    @click="loadModels">读取列表</button>
          </div>
        </div>
        <div style="width:110px">
          <label>max_tokens</label>
          <input v-model.number="maxTokens" type="number" min="1" />
        </div>
      </div>

      <div class="field mt-3">
        <label>提示词</label>
        <textarea v-model="prompt" rows="3"></textarea>
      </div>

      <div class="row mt-3">
        <button class="btn" @click="send">
          {{ running ? "中止" : "发送" }}
        </button>
        <span v-if="usageLine" class="muted">{{ usageLine }}</span>
      </div>

      <pre v-if="output || running" class="output" style="margin-top:14px">{{ output || "等待响应…" }}</pre>
    </div>
  </div>
</template>
