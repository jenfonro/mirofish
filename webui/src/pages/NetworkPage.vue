<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import { proxyLabel, proxyStatus } from "../proxies";
import { loadAccounts, loadProxies, store, toast } from "../store";
import type { ProxyNode } from "../types";
import ConfirmDialog from "../components/ConfirmDialog.vue";

const busy = ref("");
const showPasswords = ref(false);
const deleting = ref<ProxyNode | null>(null);
const form = ref<null | {
  id: string;
  mode: "single" | "bulk";
  name: string;
  scheme: string;
  host: string;
  port: string;
  username: string;
  password: string;
  bulk: string;
}>(null);
const nodes = computed(() => store.proxies?.nodes ?? []);
const valid = computed(() => {
  const f = form.value;
  if (!f) return false;
  if (f.mode === "bulk") return !!f.bulk.trim();
  const port = Number(f.port);
  return !!f.host.trim() && Number.isInteger(port) && port >= 1 && port <= 65535;
});

function openAdd(mode: "single" | "bulk") {
  form.value = {
    id: "", mode, name: "", scheme: "socks5", host: "", port: "",
    username: "", password: "", bulk: "",
  };
}

function openEdit(node: ProxyNode) {
  form.value = {
    id: node.id, mode: "single", name: node.name, scheme: node.scheme,
    host: node.host, port: String(node.port), username: node.username ?? "",
    password: node.password ?? "", bulk: "",
  };
}

async function reload() {
  await Promise.all([
    loadProxies().catch(() => toast("读取代理列表失败", "error")),
    loadAccounts().catch(() => toast("加载账号失败", "error")),
  ]);
}

async function save() {
  const f = form.value;
  if (!f || !valid.value || busy.value) return;
  busy.value = "form";
  try {
    if (f.mode === "bulk") {
      const result = await api<{ added: number; failed: string[] }>("/api/proxies/import", {
        method: "POST", body: JSON.stringify({ text: f.bulk }),
      });
      if (result.failed.length) {
        f.bulk = result.failed.join("\n");
        toast(`已导入 ${result.added} 个；${result.failed.length} 行无法解析，已保留供修改`, "info");
      } else {
        form.value = null;
        toast(`已导入 ${result.added} 个节点`, "ok");
      }
    } else {
      await api(f.id ? `/api/proxies/${encodeURIComponent(f.id)}` : "/api/proxies", {
        method: f.id ? "PATCH" : "POST",
        body: JSON.stringify({
          name: f.name.trim(), scheme: f.scheme, host: f.host.trim(),
          port: Number(f.port), username: f.username, password: f.password,
        }),
      });
      form.value = null;
      toast(f.id ? "节点已保存" : "节点已添加", "ok");
    }
  } catch (error: any) {
    toast(`保存失败：${error.message}`, "error");
  } finally {
    await reload();
    busy.value = "";
  }
}

async function test(node: ProxyNode) {
  busy.value = node.id;
  try {
    const result = await api<{ ok: boolean; latency_ms?: number; error?: string }>(
      `/api/proxies/${encodeURIComponent(node.id)}/test`, { method: "POST" });
    toast(result.ok
      ? `${proxyLabel(node)} 可用（${result.latency_ms} ms）`
      : `${proxyLabel(node)} 不可用：${result.error}`, result.ok ? "ok" : "error");
  } catch (error: any) {
    toast(`测试失败：${error.message}`, "error");
  } finally {
    await reload();
    busy.value = "";
  }
}

async function remove() {
  const node = deleting.value;
  if (!node) return;
  busy.value = node.id;
  try {
    await api(`/api/proxies/${encodeURIComponent(node.id)}`, { method: "DELETE" });
    deleting.value = null;
    toast(`已删除 ${proxyLabel(node)}`, "ok");
  } catch (error: any) {
    toast(`删除失败：${error.message}`, "error");
  } finally {
    await reload();
    busy.value = "";
  }
}

onMounted(() => loadProxies().catch(() => toast("读取代理列表失败", "error")));
</script>

<template>
  <div class="kpi-row">
    <div class="kpi">
      <div class="kpi-label">代理管理</div>
      <div class="kpi-value" style="font-size:18px">固定出口</div>
      <div class="kpi-hint">HTTP / HTTPS / SOCKS5</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">连通正常</div>
      <div class="kpi-value">{{ nodes.filter((node) => proxyStatus(node).tone === 'ok').length }}<span class="unit">/ {{ nodes.length }}</span></div>
      <div class="kpi-hint">只在手动测试或真实请求后更新</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">已绑定账号</div>
      <div class="kpi-value">{{ store.proxies?.assigned ?? 0 }}</div>
      <div class="kpi-hint">每个账号固定一个节点</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">连通性测试</div>
      <div class="kpi-value" style="font-size:18px">Google 204</div>
      <div class="kpi-hint">手动触发，不调用模型</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <h3>代理节点</h3>
      <span class="spacer"></span>
      <button class="btn ghost sm" :disabled="!!busy" @click="reload">刷新列表</button>
      <button class="btn ghost sm" :disabled="!!busy" @click="openAdd('bulk')">批量导入</button>
      <button class="btn sm" :disabled="!!busy" @click="openAdd('single')">添加代理</button>
    </div>
    <div class="panel-body">
      <p class="muted">手动维护固定出口，不自动轮换或探测；在账号修改面板选择节点，默认无代理。</p>
    </div>
    <div v-if="!nodes.length" class="empty">
      <div class="empty-title">未配置代理</div>
      <div class="empty-hint">添加节点或批量粘贴 HTTP / HTTPS / SOCKS5 链接。</div>
    </div>
    <div v-else class="table-wrap">
      <table class="data">
        <thead>
          <tr>
            <th>状态</th><th>名称 / 主机</th><th class="num">端口</th><th>用户</th>
            <th>
              密码
              <button class="btn ghost sm" @click="showPasswords = !showPasswords">
                {{ showPasswords ? "隐藏" : "显示" }}
              </button>
            </th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="node in nodes" :key="node.id">
            <td>
              <span class="chip" :class="proxyStatus(node).tone" :title="node.last_error || undefined">
                {{ proxyStatus(node).label }}
              </span>
            </td>
            <td>
              <div>{{ proxyLabel(node) }}</div>
              <div class="muted mono">{{ node.scheme }}://{{ node.host }}</div>
            </td>
            <td class="num mono">{{ node.port }}</td>
            <td class="mono">{{ node.username || "—" }}</td>
            <td class="mono">{{ node.password ? (showPasswords ? node.password : "••••••••") : "—" }}</td>
            <td>
              <div class="row" style="flex-wrap:nowrap">
                <button class="btn ghost sm" :disabled="!!busy" @click="test(node)">测试 Google 204</button>
                <button class="btn ghost sm" :disabled="!!busy" @click="openEdit(node)">修改</button>
                <button class="btn danger sm" :disabled="!!busy" @click="deleting = node">删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <template v-if="form">
    <div class="drawer-scrim" @click="!busy && (form = null)"></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="代理编辑"
           @keydown.esc="!busy && (form = null)">
      <div class="drawer-head">
        <h3>{{ form.id ? "修改代理" : form.mode === 'bulk' ? "批量导入" : "添加代理" }}</h3>
        <button class="btn ghost sm" :disabled="!!busy" @click="form = null">关闭</button>
      </div>
      <div class="drawer-body">
        <template v-if="form.mode === 'single'">
          <div class="field">
            <label for="proxy-name">名称</label>
            <input id="proxy-name" v-model="form.name" placeholder="可留空" :disabled="!!busy" />
          </div>
          <div class="field">
            <label for="proxy-scheme">协议</label>
            <select id="proxy-scheme" v-model="form.scheme" :disabled="!!busy">
              <option value="http">HTTP</option>
              <option value="https">HTTPS</option>
              <option value="socks5">SOCKS5</option>
            </select>
          </div>
          <div class="field">
            <label for="proxy-host">主机</label>
            <input id="proxy-host" v-model="form.host" placeholder="主机或 IP" :disabled="!!busy" />
          </div>
          <div class="field">
            <label for="proxy-port">端口</label>
            <input id="proxy-port" v-model="form.port" type="number" min="1" max="65535" :disabled="!!busy" />
          </div>
          <div class="field">
            <label for="proxy-user">用户</label>
            <input id="proxy-user" v-model="form.username" autocomplete="off" :disabled="!!busy" />
          </div>
          <div class="field">
            <label for="proxy-password">密码</label>
            <input id="proxy-password" v-model="form.password" type="password" autocomplete="off" :disabled="!!busy" />
          </div>
        </template>
        <div v-else class="field">
          <label for="proxy-bulk">代理列表（每行一条）</label>
          <textarea id="proxy-bulk" v-model="form.bulk" rows="10" spellcheck="false" :disabled="!!busy"
                    placeholder="socks5://user:pass@198.51.100.24:1080&#10;https://203.0.113.77:8080"></textarea>
          <p class="muted mt-2">支持 HTTP / HTTPS / SOCKS5；host:port 按 SOCKS5 处理；# 开头的行忽略。</p>
        </div>
        <div class="row">
          <button class="btn ghost" :disabled="!!busy" @click="form = null">取消</button>
          <button class="btn" :disabled="!!busy || !valid" @click="save">
            {{ busy === "form" ? "保存中…" : "保存" }}
          </button>
        </div>
      </div>
    </aside>
  </template>

  <ConfirmDialog v-if="deleting" title="删除代理"
                 :message="`删除 ${proxyLabel(deleting)}？如有账号绑定此节点，请先在账号修改面板解除绑定。`"
                 :busy="!!busy" @close="deleting = null" @confirm="remove" />
</template>
