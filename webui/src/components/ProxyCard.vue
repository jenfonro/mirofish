<script setup lang="ts">
import { computed, ref } from "vue";
import { api } from "../api";
import { loadProxies, store, toast } from "../store";
import type { ProxyNode } from "../types";

const busy = ref("");
const collapsed = ref(false);
const showPasswords = ref(false);

// One dialog for both add and edit: the fields are identical, and "editing"
// is just starting from an existing node.
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

const nodes = computed<ProxyNode[]>(() => store.proxies?.nodes ?? []);

function openAdd(): void {
  form.value = {
    id: "", mode: "single", name: "", scheme: "socks5", host: "",
    port: "", username: "", password: "", bulk: "",
  };
}

function openEdit(node: ProxyNode): void {
  form.value = {
    id: node.id, mode: "single", name: node.name, scheme: node.scheme,
    host: node.host, port: String(node.port), username: node.username ?? "",
    password: node.password ?? "", bulk: "",
  };
}

async function save(): Promise<void> {
  const f = form.value;
  if (!f) return;
  busy.value = "form";
  try {
    if (f.mode === "bulk") {
      const result = await api<{ added: number; failed: string[] }>(
        "/api/proxies/import",
        { method: "POST", body: JSON.stringify({ text: f.bulk }) });
      await loadProxies();
      form.value = null;
      toast(result.failed.length
        ? `已导入 ${result.added} 个，${result.failed.length} 行无法解析`
        : `已导入 ${result.added} 个节点`,
        result.failed.length ? "info" : "ok");
      return;
    }
    const body = JSON.stringify({
      name: f.name.trim(), scheme: f.scheme, host: f.host.trim(),
      port: Number(f.port), username: f.username, password: f.password,
    });
    if (f.id) {
      await api(`/api/proxies/${f.id}`, { method: "PATCH", body });
    } else {
      await api("/api/proxies", { method: "POST", body });
    }
    await loadProxies();
    form.value = null;
    toast(f.id ? "节点已保存" : "节点已添加", "ok");
  } catch (error: any) {
    toast(`保存失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

async function test(node: ProxyNode): Promise<void> {
  busy.value = node.id;
  try {
    const result = await api<{ ok: boolean; latency_ms?: number; error?: string }>(
      `/api/proxies/${node.id}/test`, { method: "POST" });
    await loadProxies();
    toast(result.ok
      ? `${node.name} 可用（${result.latency_ms} ms）`
      : `${node.name} 不可用：${result.error}`,
      result.ok ? "ok" : "error");
  } catch (error: any) {
    toast(`测试失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

async function remove(node: ProxyNode): Promise<void> {
  if (!confirm(`删除节点 ${node.name}？绑定它的账号会重新分配。`)) return;
  busy.value = node.id;
  try {
    await api(`/api/proxies/${node.id}`, { method: "DELETE" });
    await loadProxies();
    toast(`已删除 ${node.name}`, "ok");
  } catch (error: any) {
    toast(`删除失败：${error.message}`, "error");
  } finally {
    busy.value = "";
  }
}

function mask(value: string): string {
  if (!value) return "—";
  return showPasswords.value ? value : "•".repeat(Math.min(8, value.length));
}
</script>

<template>
  <section class="card" :class="{ collapsed }">
    <h2>
      代理池
      <span v-if="store.proxies?.configured" class="badge">
        <span class="dot" :class="store.proxies.active ? 'ok' : 'bad'"></span>
        可用 {{ store.proxies.active }} / {{ store.proxies.total }}
      </span>
      <span v-if="store.proxies?.configured" class="badge">
        已绑定 {{ store.proxies.assigned }} 账号
      </span>
      <span class="spacer"></span>
      <button class="ghost small" :disabled="!!busy" @click="openAdd">添加代理</button>
      <button class="ghost small" :disabled="!!busy" @click="loadProxies">刷新池</button>
      <button class="ghost small" type="button"
              :aria-expanded="!collapsed" aria-controls="proxy-card-body"
              @click="collapsed = !collapsed">
        {{ collapsed ? "展开" : "折叠" }}
      </button>
    </h2>

    <div id="proxy-card-body" v-show="!collapsed">
      <p class="muted" style="margin-top: 0">
        每个账号固定绑定一个节点；节点网络失败或上游拒绝该账号的出口区域时自动轮换。
        节点由你手动维护，relay 不会去拉订阅，也不会自己探测。
      </p>

      <p v-if="!nodes.length" class="muted">
        还没有节点。点「添加代理」单条录入，或批量粘贴 <code>socks5://user:pass@host:port</code> 这类链接。
      </p>

      <div v-else class="scroll-x">
        <table>
          <thead>
            <tr>
              <th>状态</th><th>名称 / 主机</th><th class="num">端口</th>
              <th>用户</th>
              <th>
                密码
                <button class="ghost small" type="button"
                        @click="showPasswords = !showPasswords">
                  {{ showPasswords ? "隐藏" : "显示" }}
                </button>
              </th>
              <th class="num">绑定</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="node in nodes" :key="node.id">
              <td>
                <span class="badge" :title="node.last_error || '可用'">
                  <span class="dot" :class="node.active ? 'ok' : 'bad'"></span>
                  {{ node.active ? "可用" : "不可用" }}
                </span>
              </td>
              <td class="identity">
                <div>{{ node.name }}</div>
                <div class="muted mono">{{ node.scheme }}://{{ node.host }}</div>
              </td>
              <td class="num mono">{{ node.port }}</td>
              <td class="mono">{{ node.username || "—" }}</td>
              <td class="mono">{{ mask(node.password ?? "") }}</td>
              <td class="num">{{ node.assigned }}</td>
              <td class="actions">
                <button class="ghost small" :disabled="busy === node.id"
                        @click="test(node)">测试</button>
                <button class="ghost small" :disabled="!!busy"
                        @click="openEdit(node)">修改</button>
                <button class="danger small" :disabled="busy === node.id"
                        @click="remove(node)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="form" class="modal-back" @click.self="form = null">
      <div class="modal">
        <h3>{{ form.id ? "修改代理" : "添加代理" }}</h3>

        <div v-if="!form.id" class="row tabs">
          <button class="ghost small" :class="{ picked: form.mode === 'single' }"
                  @click="form.mode = 'single'">单条录入</button>
          <button class="ghost small" :class="{ picked: form.mode === 'bulk' }"
                  @click="form.mode = 'bulk'">批量导入</button>
        </div>

        <template v-if="form.mode === 'single'">
          <label>名称</label>
          <input v-model="form.name" placeholder="便于识别的名称，留空则用主机名" />

          <label>协议</label>
          <select v-model="form.scheme">
            <option value="socks5">SOCKS5</option>
            <option value="http">HTTP</option>
            <option value="https">HTTPS</option>
          </select>

          <label>服务器</label>
          <div class="row">
            <input v-model="form.host" class="grow" placeholder="主机或 IP" />
            <input v-model="form.port" class="port" placeholder="端口" inputmode="numeric" />
          </div>

          <label>认证</label>
          <div class="row">
            <input v-model="form.username" class="grow" placeholder="用户名" autocomplete="off" />
            <input v-model="form.password" class="grow" placeholder="密码"
                   type="password" autocomplete="off" />
          </div>
        </template>

        <template v-else>
          <label>代理列表（每行一条，名称留空）</label>
          <textarea v-model="form.bulk" rows="8" spellcheck="false"
                    placeholder="socks5://user:pass@198.51.100.24:1080&#10;203.0.113.77:8080"></textarea>
          <p class="muted">
            支持 <code>socks5://</code>、<code>http://</code>、<code>https://</code>，
            以及不带协议的 <code>host:port</code>（按 SOCKS5 处理）。
            以 <code>#</code> 开头的行会被忽略；解析不了的行会单独报出来，不影响其余。
          </p>
        </template>

        <div class="row modal-actions">
          <span class="spacer"></span>
          <button class="ghost" :disabled="busy === 'form'" @click="form = null">取消</button>
          <button :disabled="busy === 'form'
                    || (form.mode === 'single' ? !form.host.trim() || !form.port : !form.bulk.trim())"
                  @click="save">确定</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.scroll-x { overflow-x: auto; }
.identity { line-height: 1.35; }
.identity .muted { font-size: 12px; }
.actions { white-space: nowrap; }
.modal-back {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 50;
}
.modal {
  background: var(--bg-1, #fff); color: inherit; padding: 18px 20px;
  border-radius: 10px; width: min(560px, calc(100vw - 32px));
  max-height: calc(100vh - 64px); overflow-y: auto;
  border: 1px solid var(--line);
}
.modal h3 { margin: 0 0 12px; font-size: 15px; }
.modal label { margin-top: 10px; }
.modal .port { width: 96px; flex: none; }
.tabs .picked { border-color: var(--accent); color: var(--accent); }
.modal-actions { margin-top: 16px; }
</style>
