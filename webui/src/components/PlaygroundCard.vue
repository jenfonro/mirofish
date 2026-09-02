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
let controller: AbortController | null = null;

const accountOptions = computed(() => store.accounts.map((item) => item.alias));

async function loadModels() {
  try {
    const extra: Record<string, string> = {};
    if (account.value) extra["X-Mirofish-Account"] = account.value;
    const data = await api<{
      mirofish_model_ids?: string[];
      data?: { id?: string }[];
      default_model?: string;
    }>("/v1/models", { headers: extra });
    models.value = data.mirofish_model_ids
      ?? (data.data ?? []).map((entry) => entry.id ?? "").filter(Boolean);
    if (!model.value || !models.value.includes(model.value)) {
      const preferred = data.default_model;
      model.value = preferred && models.value.includes(preferred)
        ? preferred
        : (models.value[0] || "");
    }
    if (!models.value.length) toast("上游没有返回模型列表", "info");
  } catch (error: any) {
    toast(`加载模型失败：${error.message}`, "error");
  }
}

async function send() {
  if (running.value) {
    controller?.abort();
    return;
  }
  output.value = "";
  usageLine.value = "";
  running.value = true;
  controller = new AbortController();
  try {
    await streamChat(
      {
        model: model.value || undefined,
        max_tokens: Number(maxTokens.value) || 256,
        messages: [{ role: "user", content: prompt.value }],
      },
      account.value,
      {
        onDelta(text) {
          output.value += text;
        },
        onUsage(usage) {
          usageLine.value =
            `输入 ${usage.prompt_tokens} / 输出 ${usage.completion_tokens} tokens`;
        },
      },
      controller.signal,
    );
    loadUsage().catch(() => undefined);
  } catch (error: any) {
    if (error?.name === "AbortError") {
      output.value += "\n（已中止）";
    } else {
      output.value += (output.value ? "\n" : "") + `错误：${error.message}`;
    }
  } finally {
    // Either outcome updates the account's status: a success clears a recorded
    // 401/503, a failure records one. Refresh on both, not just on success.
    loadAccounts().catch(() => undefined);
    running.value = false;
    controller = null;
  }
}
</script>

<template>
  <section class="card">
    <h2>测试台<span class="muted" style="font-weight: 400">（流式输出；会产生真实模型调用。指定被标记异常的账号发送一次即可让它恢复调度）</span></h2>
    <div class="row">
      <div class="grow">
        <label>账号（留空按默认/轮询选择；异常账号只能在此显式指定）</label>
        <select v-model="account">
          <option value="">自动选择</option>
          <option v-for="alias in accountOptions" :key="alias" :value="alias">{{ alias }}</option>
        </select>
      </div>
      <div class="grow">
        <label>模型</label>
        <div class="row" style="gap: 6px">
          <div class="grow">
            <select v-if="models.length" v-model="model">
              <option v-for="id in models" :key="id" :value="id">{{ id }}</option>
            </select>
            <input v-else v-model="model" placeholder="留空使用默认模型" />
          </div>
          <button class="ghost" @click="loadModels" title="从上游读取模型目录（零调用成本）">
            读取列表
          </button>
        </div>
      </div>
      <div style="width: 110px">
        <label>max_tokens</label>
        <input v-model.number="maxTokens" type="number" min="1" />
      </div>
    </div>
    <label>提示词</label>
    <textarea v-model="prompt" rows="3"></textarea>
    <div class="row" style="margin-top: 12px; align-items: center">
      <button @click="send">{{ running ? "中止" : "发送" }}</button>
      <span v-if="usageLine" class="muted">{{ usageLine }}</span>
    </div>
    <pre v-if="output || running" class="output">{{ output || "等待响应…" }}</pre>
  </section>
</template>
