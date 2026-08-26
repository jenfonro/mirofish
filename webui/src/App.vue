<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { getKey, setKey } from "./api";
import { saveSkin, saveTheme, storedSkin, storedTheme } from "./main";
import { connect, store } from "./store";
import AccountsCard from "./components/AccountsCard.vue";
import AddAccountCard from "./components/AddAccountCard.vue";
import LimitsCard from "./components/LimitsCard.vue";
import PlaygroundCard from "./components/PlaygroundCard.vue";
import ProxyCard from "./components/ProxyCard.vue";
import ScheduleCard from "./components/ScheduleCard.vue";
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

const skin = ref(storedSkin());
// Character art is optional: each <img> hides itself on load error so the
// skin degrades to colors-only until PNGs are dropped into webui/public/miku/.
// Bound via :src so Vite never tries to resolve the (possibly absent) files.
const MIKU_ART = {
  logo: "/miku/logo.png",
  gate: "/miku/gate.png",
  mascot: "/miku/mascot.png",
  bg: "/miku/bg.jpg",
};
const art = reactive({ logo: true, gate: true, mascot: true, bg: true });

function toggleSkin() {
  skin.value = skin.value === "miku" ? "plain" : "miku";
  saveSkin(skin.value);
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
    <img v-if="skin === 'miku' && art.logo" class="miku-logo" :src="MIKU_ART.logo"
         alt="" @error="art.logo = false" />
    <h1>Mirofish Relay</h1>
    <span v-if="store.health" class="badge">
      <span class="dot ok"></span>{{ store.health.accounts }} 个账号
    </span>
    <span v-if="store.health" class="badge">代理后端 {{ store.health.proxy_backend }}</span>
    <span v-if="store.health" class="badge">v{{ store.health.version }}</span>
    <span class="spacer"></span>
    <button class="ghost small" @click="toggleSkin">皮肤：{{ skin === "miku" ? "Miku ♪" : "标准" }}</button>
    <button class="ghost small" @click="cycleTheme">主题：{{ THEME_LABEL[theme] }}</button>
    <button v-if="store.connected" class="ghost small" @click="editKey">更换密钥</button>
  </header>

  <img v-if="skin === 'miku' && store.connected && art.bg" class="miku-bg" :src="MIKU_ART.bg"
       alt="" @error="art.bg = false" />

  <main v-if="booted && store.connected" class="grid">
    <AccountsCard class="span2" />
    <LimitsCard class="span2" />
    <AddAccountCard />
    <UsageCard />
    <ScheduleCard class="span2" />
    <ProxyCard class="span2" />
    <PlaygroundCard class="span2" />
  </main>

  <main v-else-if="booted" class="gate">
    <img v-if="skin === 'miku' && art.gate" class="miku-gate-art" :src="MIKU_ART.gate"
         alt="" @error="art.gate = false" />
    <div class="card gate-card">
      <h2>连接本地中转</h2>
      <p class="muted">
        输入数据目录 <span class="mono">proxy.key</span> 中的本地代理密钥。
        Docker 部署可运行 <span class="mono">docker compose exec mirofish cat /data/proxy.key</span>
        获取；密钥仅保存在浏览器 localStorage，用于调用管理 API。
      </p>
      <label>X-Mirofish-Proxy-Key</label>
      <input v-model="keyInput" type="password" autocomplete="off"
             placeholder="proxy.key 中的 Proxy Key" @keyup.enter="submitKey" />
      <p v-if="keyError" class="gate-error">{{ keyError }}</p>
      <div class="row" style="margin-top: 14px">
        <button :disabled="store.checking || !keyInput.trim()" @click="submitKey">
          {{ store.checking ? "验证中…" : "保存并连接" }}
        </button>
      </div>
    </div>
  </main>

  <img v-if="skin === 'miku' && art.mascot" class="miku-mascot" :src="MIKU_ART.mascot"
       alt="" @error="art.mascot = false" />
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

.gate { display: flex; justify-content: center; align-items: flex-end; gap: 18px; padding: 12vh 18px 0; }
.gate-card { width: 420px; max-width: 100%; }
.gate-error { color: var(--critical); font-size: 13px; margin: 8px 0 0; }

.miku-logo { width: 30px; height: 30px; border-radius: 50%; object-fit: cover; flex: none; }
.miku-bg {
  position: fixed;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  z-index: -1;
  opacity: 0.13;
  pointer-events: none;
  user-select: none;
}
.miku-gate-art {
  height: 300px;
  user-select: none;
  filter: drop-shadow(0 8px 20px rgba(57, 197, 187, 0.3));
}
@media (max-width: 760px) {
  .miku-gate-art { display: none; }
}
.miku-mascot {
  position: fixed;
  right: 14px;
  bottom: -6px;
  width: 148px;
  z-index: 5;
  pointer-events: none;
  user-select: none;
  filter: drop-shadow(0 6px 16px rgba(57, 197, 187, 0.35));
  animation: miku-bob 4.2s ease-in-out infinite;
}
@keyframes miku-bob {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-7px); }
}
@media (max-width: 1100px) {
  .miku-mascot { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .miku-mascot { animation: none; }
}
</style>
