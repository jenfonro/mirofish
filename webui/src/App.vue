<script setup lang="ts">
import { onMounted, ref } from "vue";
import { getKey, setKey } from "./api";
import { saveTheme, storedTheme } from "./main";
import { connect, store } from "./store";
import AccountsCard from "./components/AccountsCard.vue";
import AddAccountCard from "./components/AddAccountCard.vue";
import PlaygroundCard from "./components/PlaygroundCard.vue";
import ProxyCard from "./components/ProxyCard.vue";
import UsageCard from "./components/UsageCard.vue";

const keyInput = ref("");
const keyError = ref("");
const theme = ref(storedTheme());
const booted = ref(false);

const THEME_LABEL: Record<string, string> = { system: "跟随系统", light: "浅色", dark: "深色" };

function cycleTheme() {
  const order = ["system", "light", "dark"];
  theme.value = order[(order.indexOf(theme.value) + 1) % order.length];
  saveTheme(theme.value);
}

async function submitKey() {
  keyError.value = "";
  setKey(keyInput.value);
  if (!(await connect())) {
    keyError.value = "密钥无效或服务不可达";
  }
}

function editKey() {
  keyInput.value = "";
  store.connected = false;
}

onMounted(async () => {
  if (getKey()) await connect();
  booted.value = true;
});
</script>

<template>
  <div class="toasts">
    <div v-for="item in store.toasts" :key="item.id" class="toast" :class="item.kind">
      {{ item.text }}
    </div>
  </div>

  <header class="topbar">
    <h1>Mirofish Relay</h1>
    <span v-if="store.health" class="badge">
      <span class="dot ok"></span>{{ store.health.accounts }} 个账号
    </span>
    <span v-if="store.health" class="badge">代理后端 {{ store.health.proxy_backend }}</span>
    <span v-if="store.health" class="badge">v{{ store.health.version }}</span>
    <span class="spacer"></span>
    <button class="ghost small" @click="cycleTheme">主题：{{ THEME_LABEL[theme] }}</button>
    <button v-if="store.connected" class="ghost small" @click="editKey">更换密钥</button>
  </header>

  <main v-if="booted && store.connected" class="grid">
    <AccountsCard class="span2" />
    <AddAccountCard />
    <UsageCard />
    <ProxyCard class="span2" />
    <PlaygroundCard class="span2" />
  </main>

  <main v-else-if="booted" class="gate">
    <div class="card gate-card">
      <h2>连接本地中转</h2>
      <p class="muted">
        输入启动时打印的本地代理密钥（Proxy Key）。密钥仅保存在浏览器
        localStorage，用于调用本机管理 API。
      </p>
      <label>X-Mirofish-Proxy-Key</label>
      <input v-model="keyInput" type="password" autocomplete="off"
             placeholder="启动日志中的 Proxy Key" @keyup.enter="submitKey" />
      <p v-if="keyError" class="gate-error">{{ keyError }}</p>
      <div class="row" style="margin-top: 14px">
        <button :disabled="store.checking || !keyInput.trim()" @click="submitKey">
          {{ store.checking ? "验证中…" : "保存并连接" }}
        </button>
      </div>
    </div>
  </main>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.topbar h1 { font-size: 16px; margin-right: 6px; }
.topbar .spacer { flex: 1; }

.grid {
  max-width: 1180px;
  margin: 22px auto 60px;
  padding: 0 18px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  align-items: start;
}
.span2 { grid-column: 1 / -1; }
@media (max-width: 860px) {
  .grid { grid-template-columns: 1fr; }
}

.gate { display: flex; justify-content: center; padding: 12vh 18px 0; }
.gate-card { width: 420px; max-width: 100%; }
.gate-error { color: var(--critical); font-size: 13px; margin: 8px 0 0; }
</style>
