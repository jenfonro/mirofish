<script setup lang="ts">
import { computed, ref } from "vue";
import { api } from "../api";
import { loadAccounts, loadProxies, store, toast } from "../store";

const subscription = ref("");
const busy = ref(false);

const isMihomo = computed(() => store.proxies?.backend === "mihomo");

function formatTime(v: string | null): string {
  if (!v) return "从未";
  try { return new Date(v).toLocaleString(); } catch { return v; }
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
  } catch (e: any) {
    toast(`保存订阅失败：${e.message}`, "error");
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
  } catch (e: any) {
    toast(`刷新失败：${e.message}`, "error");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <!-- Summary KPIs -->
  <div class="kpi-row">
    <div class="kpi">
      <div class="kpi-label">后端</div>
      <div class="kpi-value" style="font-size:18px">
        {{ isMihomo ? "Mihomo" : "直连" }}
      </div>
      <div class="kpi-hint">{{ isMihomo ? "容器内置引擎" : "HTTP(S) / SOCKS5" }}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">可用节点</div>
      <div class="kpi-value">
        {{ store.proxies?.active ?? 0 }}<span class="unit">/ {{ store.proxies?.total ?? 0 }}</span>
      </div>
      <div class="kpi-hint">
        <template v-if="store.proxies?.skipped_nodes">
          跳过 {{ store.proxies.skipped_nodes }} 个不支持的节点
        </template>
        <template v-else>全部纳入</template>
      </div>
    </div>
    <div class="kpi">
      <div class="kpi-label">已绑定账号</div>
      <div class="kpi-value">{{ store.proxies?.assigned ?? 0 }}</div>
      <div class="kpi-hint">每个账号固定一个节点</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">上次刷新</div>
      <div class="kpi-value" style="font-size:13px;font-weight:500;letter-spacing:0">
        {{ formatTime(store.proxies?.last_refresh ?? null) }}
      </div>
      <div class="kpi-hint">
        <span v-if="store.proxies?.last_error" style="color:var(--danger)">
          {{ store.proxies.last_error }}
        </span>
        <template v-else>正常</template>
      </div>
    </div>
  </div>

  <!-- Config -->
  <div class="panel">
    <div class="panel-head">
      <h3>订阅与配置</h3>
      <span class="spacer"></span>
      <button class="btn ghost sm" :disabled="busy || !store.proxies?.configured" @click="refresh">
        刷新池
      </button>
    </div>
    <div class="panel-body">
      <template v-if="isMihomo">
        <p class="muted">
          Mihomo 模式的订阅来自 .env（SS/VMess/VLESS/Trojan 等协议由容器内置的 Mihomo 引擎处理）。
          修改订阅或节点排除规则后重新
          <span class="mono">docker compose up -d --force-recreate</span> 即可。
        </p>
      </template>
      <template v-else>
        <div class="field-row">
          <div class="grow">
            <label>订阅链接（只写入本机加密存储，不会回显）</label>
            <input v-model="subscription" type="url" placeholder="https://…/sub?token=…" />
          </div>
          <button class="btn" :disabled="busy || !subscription.trim()" @click="saveSubscription">
            保存并刷新
          </button>
        </div>
        <p class="muted mt-2">直连模式支持 HTTP(S) / SOCKS5 节点；其他协议请使用 Docker 的 Mihomo 模式。</p>
      </template>
    </div>
  </div>

  <!-- Nodes -->
  <div class="panel">
    <div class="panel-head">
      <h3>节点列表</h3>
      <span class="spacer"></span>
      <span v-if="store.proxies?.nodes?.length" class="muted">
        {{ store.proxies.nodes.length }} 个节点
      </span>
    </div>
    <div v-if="!store.proxies?.nodes?.length" class="empty">
      <div class="empty-title">
        {{ store.proxies?.configured ? "节点列表为空" : "未配置代理" }}
      </div>
      <div class="empty-hint">
        {{ store.proxies?.configured ? "点「刷新池」重新拉取。" : "所有账号直连上游。" }}
      </div>
    </div>
    <div v-else class="table-wrap">
      <table class="data">
        <thead>
          <tr>
            <th>节点</th><th>入口</th>
            <th class="num">绑定</th><th class="num">失败</th><th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="node in store.proxies.nodes" :key="node.id">
            <td>{{ node.name }}</td>
            <td class="mono muted">{{ node.scheme }}://{{ node.host }}:{{ node.port }}</td>
            <td class="num tnum">{{ (node as any).assigned ?? 0 }}</td>
            <td class="num tnum">{{ node.failure_count ?? 0 }}</td>
            <td>
              <span class="chip" :class="node.active ? 'ok' : 'danger'">
                <span class="dot" :class="node.active ? 'ok' : 'bad'"></span>
                {{ node.active ? "可用" : "不可用" }}
              </span>
              <div v-if="node.last_error" class="muted" style="margin-top:2px">
                {{ node.last_error }}
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
