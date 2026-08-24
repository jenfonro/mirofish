<script setup lang="ts">
import { computed, ref } from "vue";
import { api } from "../api";
import { loadAccounts, loadProxies, store, toast } from "../store";

const subscription = ref("");
const busy = ref(false);
const collapsed = ref(false);

const isMihomo = computed(() => store.proxies?.backend === "mihomo");

function formatTime(value: string | null): string {
  if (!value) return "从未";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

async function saveSubscription() {
  busy.value = true;
  try {
    store.proxies = await api("/api/proxies/subscription", {
      method: "POST",
      body: JSON.stringify({ url: subscription.value.trim() }),
    });
    subscription.value = "";
    toast("订阅已保存并刷新", "ok");
    await loadAccounts();
  } catch (error: any) {
    toast(`保存订阅失败：${error.message}`, "error");
  } finally {
    busy.value = false;
  }
}

async function refresh() {
  busy.value = true;
  try {
    store.proxies = await api("/api/proxies/refresh", { method: "POST" });
    toast("代理池已刷新", "ok");
    await loadAccounts();
  } catch (error: any) {
    toast(`刷新代理池失败：${error.message}`, "error");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="card" :class="{ collapsed }">
    <h2>
      代理池
      <span v-if="store.proxies" class="badge">
        {{ isMihomo ? "Mihomo 后端" : "直连后端" }}
      </span>
      <span v-if="store.proxies?.configured" class="badge">
        <span class="dot" :class="store.proxies.active ? 'ok' : 'bad'"></span>
        可用 {{ store.proxies.active }} / {{ store.proxies.total }}
      </span>
      <span v-if="store.proxies?.configured" class="badge">
        已绑定 {{ store.proxies.assigned }} 账号
      </span>
      <span class="spacer"></span>
      <button class="ghost small" :disabled="busy || !store.proxies?.configured"
              @click="refresh">刷新池</button>
      <button class="ghost small" type="button"
              :aria-expanded="!collapsed" aria-controls="proxy-card-body"
              @click="collapsed = !collapsed">
        {{ collapsed ? "展开" : "折叠" }}
      </button>
    </h2>

    <div id="proxy-card-body" v-show="!collapsed">
      <p v-if="store.proxies?.last_error" class="error-line">
        上次刷新出错：{{ store.proxies.last_error }}
      </p>
      <p class="muted" style="margin-top: 0">
        每个账号固定绑定一个节点；节点网络失败或上游拒绝该账号的出口区域时自动轮换。
        上次刷新：{{ formatTime(store.proxies?.last_refresh ?? null) }}
        <template v-if="store.proxies?.skipped_nodes">
          · 跳过 {{ store.proxies.skipped_nodes }} 个不支持的节点
        </template>
      </p>

      <template v-if="isMihomo">
        <p class="muted">
          Mihomo 模式的订阅来自 .env（SS/VMess/VLESS/Trojan 等协议由容器内置的 Mihomo 引擎处理）。
          修改订阅或节点排除规则后重新 <code>docker compose up -d --force-recreate</code> 即可。
        </p>
      </template>
      <template v-else>
        <div class="row">
          <div class="grow">
            <label>订阅链接（只写入本机加密存储，不会回显）</label>
            <input v-model="subscription" type="url" placeholder="https://…/sub?token=…" />
          </div>
          <button :disabled="busy || !subscription.trim()" @click="saveSubscription">
            保存并刷新
          </button>
        </div>
        <p class="muted">直连模式支持 HTTP(S) / SOCKS5 节点；其他协议请使用 Docker 的 Mihomo 模式。</p>
      </template>

      <div v-if="store.proxies?.nodes?.length" class="scroll-x" style="margin-top: 10px">
        <table>
          <thead>
            <tr>
              <th>节点</th><th>入口</th><th class="num">绑定账号</th>
              <th class="num">连续失败</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="node in store.proxies.nodes" :key="node.id">
              <td>{{ node.name }}</td>
              <td class="mono muted">{{ node.scheme }}://{{ node.host }}:{{ node.port }}</td>
              <td class="num">{{ (node as any).assigned ?? 0 }}</td>
              <td class="num">{{ node.failure_count ?? 0 }}</td>
              <td>
                <span class="badge">
                  <span class="dot" :class="node.active ? 'ok' : 'bad'"></span>
                  {{ node.active ? "可用" : "不可用" }}
                </span>
                <div v-if="node.last_error" class="muted">{{ node.last_error }}</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!store.proxies?.configured" class="muted">
        未配置代理；所有账号直连上游。
      </p>
    </div>
  </section>
</template>

<style scoped>
.card.collapsed > h2 { margin-bottom: 0; }
.scroll-x { overflow-x: auto; }
.error-line { color: var(--critical); font-size: 13px; }
</style>
