<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../api";
import { loadAccounts, loadProxies, store, toast } from "../store";
import ProxyPicker from "./ProxyPicker.vue";

const alias = ref("");
const email = ref("");
const code = ref("");
// Chosen before the login round trip, so the account is created behind the
// exit it will keep using: the login itself is what binds the upstream
// identity to a region.
const proxyId = ref("");
const stage = ref<"start" | "verify">("start");
const busy = ref(false);

onMounted(async () => {
  if (store.proxies) return;
  try {
    await loadProxies();
  } catch {
    // The pool is optional; a login without one simply goes out directly.
  }
});

async function sendCode() {
  busy.value = true;
  try {
    await api("/api/login/start", {
      method: "POST",
      body: JSON.stringify({
        alias: alias.value.trim(),
        email: email.value.trim(),
        proxy_id: proxyId.value,
      }),
    });
    stage.value = "verify";
    toast("验证码已发送，请查收邮箱", "ok");
  } catch (error: any) {
    toast(`发送验证码失败：${error.message}`, "error");
  } finally {
    busy.value = false;
  }
}

async function verify() {
  busy.value = true;
  try {
    const result = await api<{
      alias: string;
      plan?: string;
      profile_pending?: boolean;
    }>("/api/login/finish", {
      method: "POST",
      body: JSON.stringify({ alias: alias.value.trim(), code: code.value.trim() }),
    });
    if (result.profile_pending) {
      toast(`登录成功：${result.alias}；账号资料暂未加载，可稍后刷新`, "info");
    } else {
      toast(`登录成功：${result.alias}（${result.plan || "未知套餐"}）`, "ok");
    }
    alias.value = email.value = code.value = "";
    proxyId.value = "";
    stage.value = "start";
  } catch (error: any) {
    toast(`登录失败：${error.message}`, "error");
  } finally {
    // Also on failure: the credentials are saved before the profile is read,
    // so a refused profile still leaves a new (parked) account in the list.
    await loadAccounts();
    busy.value = false;
  }
}
</script>

<template>
  <section class="card">
    <h2>添加账号</h2>
    <p class="muted">邮箱验证码登录；凭证只写入本机加密存储。</p>
    <label>账号名称（用于 X-Mirofish-Account）</label>
    <input v-model="alias" placeholder="work" :disabled="stage === 'verify'" />
    <label>邮箱</label>
    <input v-model="email" type="email" placeholder="you@example.com"
           :disabled="stage === 'verify'" />
    <label>代理</label>
    <ProxyPicker v-if="stage === 'start'" v-model="proxyId"
                 :nodes="store.proxies?.nodes ?? []" />
    <p v-else class="muted">登录进行中，代理已固定。</p>
    <template v-if="stage === 'verify'">
      <label>6 位验证码</label>
      <input v-model="code" maxlength="6" inputmode="numeric" placeholder="123456"
             @keyup.enter="verify" />
    </template>
    <div class="row" style="margin-top: 14px">
      <button v-if="stage === 'start'" :disabled="busy || !alias.trim() || !email.trim()"
              @click="sendCode">
        {{ busy ? "发送中…" : "发送验证码" }}
      </button>
      <template v-else>
        <button :disabled="busy || code.trim().length !== 6" @click="verify">
          {{ busy ? "验证中…" : "完成登录" }}
        </button>
        <button class="ghost" :disabled="busy" @click="stage = 'start'">重新开始</button>
      </template>
    </div>
  </section>
</template>
