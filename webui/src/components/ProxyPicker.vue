<script setup lang="ts">
import { computed, ref } from "vue";
import { proxyLabel, proxyStatus } from "../proxies";
import type { ProxyNode } from "../types";

const props = defineProps<{ nodes: ProxyNode[]; modelValue: string; disabled?: boolean }>();
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
const open = ref(false);
const query = ref("");
const picker = ref<HTMLElement | null>(null);
const current = computed(() => {
  const node = props.nodes.find((item) => item.id === props.modelValue);
  return node ? proxyLabel(node) : props.modelValue || "无代理";
});
const matches = computed(() => {
  const q = query.value.trim().toLowerCase();
  return props.nodes.filter((node) =>
    `${proxyLabel(node)} ${node.host} ${node.port}`.toLowerCase().includes(q));
});

function pick(id: string) {
  emit("update:modelValue", id);
  open.value = false;
  query.value = "";
}

function closeOutside(event: FocusEvent) {
  if (!picker.value?.contains(event.relatedTarget as Node | null)) open.value = false;
}
</script>

<template>
  <div ref="picker" class="proxy-picker" @keydown.esc.stop="open = false"
       @focusout="closeOutside">
    <button class="btn ghost picker-value" type="button" :disabled="disabled"
            :aria-expanded="open" aria-label="选择固定代理" @click="open = !open">
      <span>{{ current }}</span><span aria-hidden="true">▾</span>
    </button>
    <div v-if="open && !disabled" class="picker-panel">
      <input v-model="query" aria-label="搜索代理" placeholder="搜索名称 / 主机 / 端口…"
             autocomplete="off" />
      <ul aria-label="代理节点">
        <li>
          <button type="button" :class="{ picked: !modelValue }" @click="pick('')">无代理</button>
        </li>
        <li v-for="node in matches" :key="node.id">
          <button type="button" :class="{ picked: node.id === modelValue }"
                  @click="pick(node.id)">
            <span class="dot" :class="proxyStatus(node).tone === 'ok' ? 'ok'
              : proxyStatus(node).tone === 'danger' ? 'bad' : 'off'"></span>
            <span>{{ proxyLabel(node) }}</span>
          </button>
        </li>
        <li v-if="!matches.length" class="muted">无匹配节点</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.proxy-picker { min-width: 0; }
.picker-value { width: 100%; justify-content: space-between; text-align: left; }
.picker-value span:first-child { overflow: hidden; text-overflow: ellipsis; }
.picker-panel {
  margin-top: 4px; padding: 8px; border: 1px solid var(--border);
  border-radius: var(--r); background: var(--surface); color: var(--ink);
}
.picker-panel ul {
  list-style: none; margin: 6px 0 0; padding: 0; max-height: 200px; overflow-y: auto;
}
.picker-panel button {
  display: flex; align-items: center; gap: 6px; width: 100%; padding: 7px 8px;
  background: transparent; border: none; border-radius: var(--r-sm);
  text-align: left; cursor: pointer; overflow-wrap: anywhere;
}
.picker-panel button:hover, .picker-panel button:focus-visible { background: var(--surface-2); }
.picker-panel .picked { color: var(--accent); }
</style>
