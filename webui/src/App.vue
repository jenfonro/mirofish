<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { getKey, setKey } from "./api";
import { iconSvg } from "./icons";
import {
  savePage, saveSkin, saveTheme, storedPage, storedSkin, storedTheme,
  type PageId, type ThemeMode,
} from "./main";
import { connect, loadAccounts, store } from "./store";
import OverviewPage from "./pages/OverviewPage.vue";
import AccountsPage from "./pages/AccountsPage.vue";
import UsagePage from "./pages/UsagePage.vue";
import NetworkPage from "./pages/NetworkPage.vue";
import SchedulePage from "./pages/SchedulePage.vue";
import PlaygroundPage from "./pages/PlaygroundPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";

const keyInput = ref("");
const keyError = ref("");
const theme = ref<ThemeMode>(storedTheme());
const skin = ref(storedSkin());
const page = ref<PageId>(storedPage());
const booted = ref(false);
const navOpen = ref(false);

const THEME_LABEL: Record<ThemeMode, string> = {
  system: "系统", light: "浅色", dark: "深色",
};

const PAGES: { id: PageId; label: string; icon: keyof typeof import("./icons").icons }[] = [
  { id: "overview", label: "总览", icon: "overview" },
  { id: "accounts", label: "账号", icon: "accounts" },
  { id: "usage", label: "用量", icon: "usage" },
  { id: "network", label: "网络", icon: "network" },
  { id: "schedule", label: "调度", icon: "schedule" },
  { id: "playground", label: "测试", icon: "playground" },
  { id: "settings", label: "设置", icon: "settings" },
];

const pageTitle = computed(() =>
  PAGES.find((p) => p.id === page.value)?.label ?? "总览");

function go(id: PageId) {
  page.value = id;
  savePage(id);
  navOpen.value = false;
}

function cycleTheme() {
  const order: ThemeMode[] = ["system", "light", "dark"];
  theme.value = order[(order.indexOf(theme.value) + 1) % order.length];
  saveTheme(theme.value);
}

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

function onKeydown(e: KeyboardEvent) {
  if (e.key === "Escape") navOpen.value = false;
}

onMounted(async () => {
  window.addEventListener("keydown", onKeydown);
  if (getKey()) await connect();
  booted.value = true;
});
onUnmounted(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div class="toasts">
    <div v-for="item in store.toasts" :key="item.id" class="toast" :class="item.kind">
      {{ item.text }}
    </div>
  </div>

  <!-- Gate -->
  <div v-if="booted && !store.connected" class="gate-shell">
    <div class="gate-card">
      <div class="gate-brand">
        <span class="nav-logo" aria-hidden="true"></span>
        <div>
          <h1>Mirofish Relay</h1>
          <p>连接本地中转控制台</p>
        </div>
      </div>
      <div class="field">
        <label>X-Mirofish-Proxy-Key</label>
        <input
          v-model="keyInput"
          type="password"
          autocomplete="off"
          placeholder="proxy.key 中的 Proxy Key"
          @keyup.enter="submitKey"
        />
        <p class="muted mt-2">
          密钥在数据目录 <span class="mono">proxy.key</span>。
          Docker：`docker compose exec mirofish cat /data/proxy.key`
        </p>
      </div>
      <p v-if="keyError" class="gate-error">{{ keyError }}</p>
      <div class="gate-actions">
        <button class="btn" :disabled="store.checking || !keyInput.trim()" @click="submitKey">
          {{ store.checking ? "验证中…" : "保存并连接" }}
        </button>
      </div>
    </div>
  </div>

  <!-- App shell -->
  <div v-else-if="booted" class="shell">
    <div v-if="navOpen" class="nav-scrim" @click="navOpen = false"></div>
    <aside class="nav" :class="{ open: navOpen }">
      <div class="nav-brand">
        <span class="nav-logo" aria-hidden="true"></span>
        <div>
          <div class="nav-title">Mirofish</div>
          <div class="nav-sub">Relay Console</div>
        </div>
      </div>
      <ul class="nav-list">
        <li v-for="item in PAGES" :key="item.id">
          <button
            class="nav-item"
            :class="{ active: page === item.id }"
            @click="go(item.id)"
          >
            <span class="icon" v-html="iconSvg(item.icon)"></span>
            <span>{{ item.label }}</span>
            <span v-if="item.id === 'accounts' && store.accounts.length" class="count">
              {{ store.accounts.length }}
            </span>
          </button>
        </li>
      </ul>
      <div class="nav-foot">
        <div class="nav-foot-row">
          <button class="btn ghost sm" style="flex:1" @click="cycleTheme">
            {{ THEME_LABEL[theme] }}
          </button>
          <button class="btn ghost sm" style="flex:1" @click="toggleSkin">
            {{ skin === "miku" ? "Miku" : "标准" }}
          </button>
        </div>
        <div v-if="store.health" class="nav-ver">
          v{{ store.health.version }} · {{ store.health.proxy_backend }}
        </div>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <button class="menu-btn" aria-label="菜单" @click="navOpen = true">
          <span class="icon" style="width:16px;height:16px;display:block" v-html="iconSvg('menu')"></span>
        </button>
        <h2>{{ pageTitle }}</h2>
        <span class="spacer"></span>
        <template v-if="store.health">
          <span class="chip ok">
            <span class="dot ok"></span>
            {{ store.health.accounts }} 账号
          </span>
          <span class="chip">{{ store.health.proxy_backend }}</span>
        </template>
        <button class="btn ghost sm" @click="editKey">更换密钥</button>
      </header>

      <div class="content">
        <div class="content-inner">
          <OverviewPage v-if="page === 'overview'" @navigate="go" />
          <AccountsPage v-else-if="page === 'accounts'" />
          <UsagePage v-else-if="page === 'usage'" />
          <NetworkPage v-else-if="page === 'network'" />
          <SchedulePage v-else-if="page === 'schedule'" />
          <PlaygroundPage v-else-if="page === 'playground'" />
          <SettingsPage
            v-else-if="page === 'settings'"
            :theme="theme"
            :skin="skin"
            @update:theme="theme = $event; saveTheme($event)"
            @update:skin="skin = $event; saveSkin($event)"
            @edit-key="editKey"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.icon { display: inline-flex; width: 16px; height: 16px; }
.icon :deep(svg) { width: 100%; height: 100%; }
.menu-btn .icon :deep(svg) { width: 16px; height: 16px; }
</style>
