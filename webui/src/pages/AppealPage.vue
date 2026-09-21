<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api";
import { store, toast } from "../store";

// The appeal is submitted to the upstream exactly as the official client does
// (POST /feedback, kind=appeal, account bearer). This page only gathers the
// fields and assembles them into the free-text body the client would send.

const DEFAULT_ERROR =
  "unexpected status 403 Forbidden: this account is suspended; contact support";

const account = ref("");
const errorText = ref(DEFAULT_ERROR);
const since = ref("");
const purpose = ref("");
const sharedWithOthers = ref(false);   // 账号是否还有别人在用：默认否
const usesAutomation = ref(true);      // 有脚本或批量任务调用：默认是
const forwardsThirdParty = ref(false); // 流量是否转给第三方：默认否
const contact = ref("");
const detail = ref("");
const proxyEnabled = ref(false);
const proxyUrl = ref("");
const running = ref(false);
const result = ref("");

const accountOptions = computed(() => store.accounts.map((a) => a.alias));

function yesNo(v: boolean): string {
  return v ? "是" : "否";
}

// Auto-fill the contact from the chosen account's email; the operator may edit.
watch(account, (alias) => {
  const acct = store.accounts.find((a) => a.alias === alias);
  if (acct && acct.email) contact.value = acct.email;
});

const preview = computed(() => {
  const lines = [
    "我的账号被停用了，我在申诉。",
    `遇到的问题：${errorText.value.trim() || DEFAULT_ERROR}`,
  ];
  if (since.value.trim()) lines.push(`大概从这个时候开始：${since.value.trim()}`);
  if (purpose.value.trim()) lines.push(`我用它做什么：${purpose.value.trim()}`);
  lines.push(`这个账号还有别人在用吗：${yesNo(sharedWithOthers.value)}`);
  lines.push(`有用脚本或批量任务调用吗：${yesNo(usesAutomation.value)}`);
  lines.push(`流量有转给第三方吗：${yesNo(forwardsThirdParty.value)}`);
  if (contact.value.trim()) lines.push(`联系方式：${contact.value.trim()}`);
  if (detail.value.trim()) lines.push(`补充说明：${detail.value.trim()}`);
  lines.push("说实话我不太清楚具体发生了什么，希望能帮忙看一下、恢复使用，谢谢。");
  return lines.join("\n");
});

async function submit() {
  if (!account.value) { toast("请先选择账号", "error"); return; }
  running.value = true;
  result.value = "";
  try {
    const body: Record<string, unknown> = {
      alias: account.value,
      text: preview.value,
      contact: contact.value.trim() || undefined,
      proxy_enabled: proxyEnabled.value,
      proxy_url: proxyEnabled.value ? proxyUrl.value.trim() : undefined,
    };
    const res = await api<{ delivered: boolean; status: number; id: string; error?: unknown }>(
      "/api/appeal", { method: "POST", body: JSON.stringify(body) });
    if (res.delivered) {
      result.value = `已提交（状态 ${res.status}，编号 ${res.id}）。`;
      toast("申诉已提交", "ok");
    } else {
      const err = typeof res.error === "string" ? res.error : JSON.stringify(res.error ?? "");
      result.value = `未送达（状态 ${res.status}）：${err}`;
      toast("提交失败", "error");
    }
  } catch (e: any) {
    result.value = `提交失败：${e.message}`;
    toast(`提交失败：${e.message}`, "error");
  } finally {
    running.value = false;
  }
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <h3>账号申诉</h3>
      <span class="spacer"></span>
      <span class="chip">提交到官方 /feedback</span>
    </div>
    <div class="panel-body">
      <p class="muted mb-2">
        按官方客户端的申诉表单向上游提交。用途和说明请简短、如实填写。选中账号后会
        自动带上该账号的凭证与设备标识，联系方式默认填入账号邮箱（可改）。
      </p>

      <div class="field-row">
        <div class="grow">
          <label>账号</label>
          <select v-model="account">
            <option value="">请选择账号</option>
            <option v-for="a in accountOptions" :key="a" :value="a">{{ a }}</option>
          </select>
        </div>
        <div class="grow">
          <label>联系方式</label>
          <input v-model="contact" type="text" placeholder="选中账号后自动填入邮箱，可修改" />
        </div>
      </div>

      <div class="mt-2">
        <label>遇到了什么</label>
        <input v-model="errorText" type="text" placeholder="报错原文" />
      </div>

      <div class="field-row mt-2">
        <div class="grow">
          <label>从什么时候开始</label>
          <input v-model="since" type="text" placeholder="例如：今天下午 / 昨天开始" />
        </div>
        <div class="grow">
          <label>账号用途（一句话）</label>
          <input v-model="purpose" type="text" placeholder="你在用它做什么" />
        </div>
      </div>

      <div class="mt-3">
        <label>如实回答</label>
        <div class="field-row">
          <label class="chk"><input type="checkbox" v-model="sharedWithOthers" /> 账号是否还有别人在用（默认否）</label>
        </div>
        <div class="field-row">
          <label class="chk"><input type="checkbox" v-model="usesAutomation" /> 有脚本或批量任务调用（默认是）</label>
        </div>
        <div class="field-row">
          <label class="chk"><input type="checkbox" v-model="forwardsThirdParty" /> 流量是否有转给第三方（默认否）</label>
        </div>
      </div>

      <div class="mt-3">
        <label>说明</label>
        <textarea v-model="detail" rows="3" placeholder="补充说明，简短如实即可"></textarea>
      </div>

      <div class="mt-3">
        <div class="field-row">
          <label class="chk"><input type="checkbox" v-model="proxyEnabled" /> 通过代理提交</label>
        </div>
        <input v-if="proxyEnabled" v-model="proxyUrl" type="text"
               placeholder="http://user:pass@host:port 或 socks5://host:port" />
      </div>

      <div class="mt-3">
        <label>提交预览</label>
        <pre class="output">{{ preview }}</pre>
      </div>

      <div class="row mt-2">
        <button class="btn" :disabled="running || !account" @click="submit">
          {{ running ? "提交中…" : "提交申诉" }}
        </button>
      </div>
      <p v-if="result" class="muted mt-2">{{ result }}</p>
    </div>
  </div>
</template>

<style scoped>
/* The global `label`/`input` rules make labels block and inputs full-width;
   override just for the yes/no checkboxes so each renders as one inline row. */
.chk {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-weight: 400;
  cursor: pointer;
}
.chk input[type="checkbox"] {
  width: auto;
  margin: 0;
  cursor: pointer;
}
</style>
