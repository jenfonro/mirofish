<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api";
import { store, toast } from "../store";

// The appeal is submitted to the upstream exactly as the official desktop
// client does (POST /feedback, kind=appeal, account bearer, no UA, no device
// signature). This page gathers the same form fields the client sends: a
// structured `appeal` sub-object plus one free-text note. Field names/values
// mirror the captured contract (mirasim-appeal-* / server.cjs Duo).

const DEFAULT_ERROR =
  "unexpected status 403 Forbidden: this account is suspended; contact support";

// Enum values are the official ones; labels are ours.
const ISSUE_OPTIONS = [
  { value: "refused", label: "被拒绝 / 封停 (403)" },
  { value: "limited", label: "被限流 / 额度受限" },
  { value: "signin", label: "无法登录" },
  { value: "other", label: "其他" },
];
const USAGE_OPTIONS = [
  { value: "personal", label: "个人使用" },
  { value: "team", label: "团队使用" },
  { value: "teaching", label: "教学 / 学习" },
  { value: "other", label: "其他" },
];

const account = ref("");
const issue = ref("refused");
const errorText = ref(DEFAULT_ERROR);
const since = ref("");
const usage = ref("personal");
const usageNote = ref("");
const shared = ref("no");    // 账号是否还有别人在用：默认否
const scripted = ref("yes"); // 有脚本或批量任务调用：默认是
const resold = ref("no");    // 流量是否有转给第三方：默认否
const contact = ref("");
const note = ref("我不太清楚具体是什么触发的，希望能帮忙看一下、恢复使用，谢谢。");
const proxyEnabled = ref(false);
const proxyUrl = ref("");
const running = ref(false);
const result = ref("");

const accountOptions = computed(() => store.accounts.map((a) => a.alias));

// Only limited/refused require the error text, matching the official form.
const errorRequired = computed(() => issue.value === "limited" || issue.value === "refused");

// Auto-fill the contact from the chosen account's email; the operator may edit.
watch(account, (alias) => {
  const acct = store.accounts.find((a) => a.alias === alias);
  if (acct && acct.email) contact.value = acct.email;
});

// The exact envelope the request will carry (proxy is transport-only).
const appealBody = computed(() => ({
  issue: issue.value,
  errorText: errorRequired.value ? errorText.value.trim() : "",
  since: since.value.trim(),
  usage: usage.value,
  usageNote: usageNote.value.trim(),
  shared: shared.value,
  scripted: scripted.value,
  resold: resold.value,
  contact: contact.value.trim(),
}));

const preview = computed(() =>
  JSON.stringify({ text: note.value.trim(), kind: "appeal", appeal: appealBody.value }, null, 2));

async function submit() {
  if (!account.value) { toast("请先选择账号", "error"); return; }
  if (!note.value.trim()) { toast("请填写说明", "error"); return; }
  if (!usageNote.value.trim()) { toast("请填写账号用途", "error"); return; }
  running.value = true;
  result.value = "";
  try {
    const body: Record<string, unknown> = {
      alias: account.value,
      note: note.value.trim(),
      appeal: appealBody.value,
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
        按官方客户端的申诉表单向上游提交，字段与官方一致。选中账号后会自动带上该账号的
        凭证与设备标识，联系方式默认填入账号邮箱（可改）。用途和说明请简短、如实填写。
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

      <div class="field-row mt-2">
        <div class="grow">
          <label>遇到的问题</label>
          <select v-model="issue">
            <option v-for="o in ISSUE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </div>
        <div class="grow">
          <label>从什么时候开始</label>
          <input v-model="since" type="text" placeholder="例如：今天下午 / 昨天开始（可留空）" />
        </div>
      </div>

      <div class="mt-2" v-if="errorRequired">
        <label>报错原文</label>
        <input v-model="errorText" type="text" placeholder="把上游返回的报错原文粘进来" />
      </div>

      <div class="field-row mt-2">
        <div class="grow">
          <label>账号用途</label>
          <select v-model="usage">
            <option v-for="o in USAGE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </div>
        <div class="grow">
          <label>用途说明（一句话）</label>
          <input v-model="usageNote" type="text" placeholder="你在用它做什么" />
        </div>
      </div>

      <div class="mt-3">
        <label>如实回答</label>
        <div class="qna">
          <span>账号是否还有别人在用</span>
          <div class="yn">
            <button type="button" :class="{ on: shared === 'no' }" @click="shared = 'no'">否</button>
            <button type="button" :class="{ on: shared === 'yes' }" @click="shared = 'yes'">是</button>
          </div>
        </div>
        <div class="qna">
          <span>有脚本或批量任务调用</span>
          <div class="yn">
            <button type="button" :class="{ on: scripted === 'no' }" @click="scripted = 'no'">否</button>
            <button type="button" :class="{ on: scripted === 'yes' }" @click="scripted = 'yes'">是</button>
          </div>
        </div>
        <div class="qna">
          <span>流量是否有转给第三方</span>
          <div class="yn">
            <button type="button" :class="{ on: resold === 'no' }" @click="resold = 'no'">否</button>
            <button type="button" :class="{ on: resold === 'yes' }" @click="resold = 'yes'">是</button>
          </div>
        </div>
      </div>

      <div class="mt-3">
        <label>说明</label>
        <textarea v-model="note" rows="3" placeholder="简短如实说明即可"></textarea>
      </div>

      <div class="mt-3">
        <label class="chk"><input type="checkbox" v-model="proxyEnabled" /> 通过代理提交</label>
        <input v-if="proxyEnabled" v-model="proxyUrl" type="text" class="mt-1"
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
   scope small overrides for the checkbox and the yes/no rows. */
.chk {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-weight: 400;
  cursor: pointer;
}
.chk input[type="checkbox"] { width: auto; margin: 0; cursor: pointer; }
.qna {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 0;
}
.qna + .qna { border-top: 1px solid var(--border-2); }
.qna > span { font-size: 13px; color: var(--ink-2); }
.yn { display: inline-flex; gap: 6px; flex-shrink: 0; }
.yn button {
  width: auto;
  min-width: 42px;
  padding: 4px 12px;
  font-size: 12.5px;
  border: 1px solid var(--border-2);
  border-radius: var(--r);
  background: var(--surface-2);
  color: var(--ink-3);
  cursor: pointer;
  transition: border-color 0.12s ease, color 0.12s ease, background 0.12s ease;
}
.yn button.on {
  border-color: var(--border-focus);
  background: var(--accent-soft);
  color: var(--ink);
  font-weight: 500;
}
</style>
