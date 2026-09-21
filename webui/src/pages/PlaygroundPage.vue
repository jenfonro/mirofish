<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { api, streamChat } from "../api";
import { accountName } from "../accounts";
import { loadAccounts, loadUsage, store, toast } from "../store";

const account = ref("");
const model = ref("");
const models = ref<string[]>([]);
const prompt = ref("你好，请用一句话介绍你自己。");
const maxTokens = ref(256);
const output = ref("");
const usageLine = ref("");
const running = ref(false);
const modelsLoading = ref(false);
const elapsed = ref(0);
let controller: AbortController | null = null;
let timer: ReturnType<typeof setInterval> | null = null;

const accountOptions = computed(() => store.accounts.map((a) => ({
  alias: a.alias, name: accountName(a),
})));
const validRequest = computed(() =>
  !!prompt.value.trim() && Number.isInteger(maxTokens.value) && maxTokens.value >= 2);

watch(account, () => { models.value = []; });

async function loadModels() {
  modelsLoading.value = true;
  try {
    const extra: Record<string, string> = {};
    if (account.value) extra["X-Mirofish-Account"] = account.value;
    const data = await api<{
      mirofish_model_ids?: string[];
      data?: { id?: string }[];
      default_model?: string;
    }>(
      "/v1/models", { headers: extra },
    );
    models.value = data.mirofish_model_ids
      ?? (data.data ?? []).map((entry) => entry.id ?? "").filter(Boolean);
    if (!model.value || !models.value.includes(model.value)) {
      const preferred = data.default_model;
      model.value = preferred && models.value.includes(preferred)
        ? preferred : (models.value[0] || "");
    }
    if (!models.value.length) toast("上游没有返回模型列表", "info");
  } catch (e: any) {
    toast(`加载模型失败：${e.message}`, "error");
  } finally {
    await loadAccounts().catch(() => toast("加载账号失败", "error"));
    modelsLoading.value = false;
  }
}

async function send() {
  if (running.value) {
    controller?.abort();
    return;
  }
  if (!validRequest.value) {
    toast("请输入提示词，max_tokens 至少为 2；不发送 one-token 探测", "info");
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
        max_tokens: maxTokens.value,
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
    loadUsage().catch(() => undefined);
  } catch (e: any) {
    if (e?.name === "AbortError") {
      output.value += "\n（已中止）";
    } else {
      output.value += (output.value ? "\n" : "") + `错误：${e.message}`;
    }
  } finally {
    if (timer) { clearInterval(timer); timer = null; }
    await loadAccounts().catch(() => toast("加载账号失败", "error"));
    running.value = false;
    controller = null;
  }
}

onUnmounted(() => {
  controller?.abort();
  if (timer) clearInterval(timer);
});
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
      <p class="muted mb-2">
        只有指定账号的真实模型对话成功才能恢复健康状态；401 需重新登录。
        读取资料、额度和模型列表不会恢复状态，也不以 one-token 合成响应判定成功。
      </p>
      <div class="field-row">
        <div class="grow">
          <label for="chat-account">账号（留空按固定策略选择）</label>
          <select id="chat-account" v-model="account" :disabled="running || modelsLoading">
            <option value="">自动选择</option>
            <option v-for="a in accountOptions" :key="a.alias" :value="a.alias">{{ a.name }}（{{ a.alias }}）</option>
          </select>
        </div>
        <div class="grow">
          <label for="chat-model">模型</label>
          <div class="row" style="gap:6px">
            <div style="flex:1;min-width:140px">
              <select v-if="models.length" id="chat-model" v-model="model" :disabled="running || modelsLoading">
                <option v-for="id in models" :key="id" :value="id">{{ id }}</option>
              </select>
              <input v-else id="chat-model" v-model="model" placeholder="留空使用默认模型" :disabled="running || modelsLoading" />
            </div>
            <button class="btn ghost" title="从上游读取模型目录（零调用成本）"
                    :disabled="running || modelsLoading" @click="loadModels">读取列表</button>
          </div>
        </div>
        <div style="width:110px">
          <label for="chat-tokens">max_tokens</label>
          <input id="chat-tokens" v-model.number="maxTokens" type="number" min="2" step="1" :disabled="running" />
        </div>
      </div>

      <div class="field mt-3">
        <label for="chat-prompt">提示词</label>
        <textarea id="chat-prompt" v-model="prompt" rows="3" :disabled="running"></textarea>
      </div>

      <div class="row mt-3">
        <button class="btn" :disabled="modelsLoading || (!running && !validRequest)" @click="send">
          {{ running ? "中止" : "发送" }}
        </button>
        <span v-if="usageLine" class="muted">{{ usageLine }}</span>
      </div>

      <pre v-if="output || running" class="output" style="margin-top:14px">{{ output || "等待响应…" }}</pre>
    </div>
  </div>
</template>
