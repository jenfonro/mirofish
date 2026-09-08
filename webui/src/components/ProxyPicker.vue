<script setup lang="ts">
/**
 * Searchable proxy picker.
 *
 * A pool can hold dozens of nodes, most of which differ only by host, so a
 * plain <select> is unusable: the label you recognise a node by is its name,
 * and a node without one is only identifiable by its full connection string.
 */
import { computed, ref } from "vue";
import type { ProxyNode } from "../types";

const props = defineProps<{ nodes: ProxyNode[]; modelValue: string }>();
const emit = defineEmits<{ (event: "update:modelValue", value: string): void }>();

const open = ref(false);
const query = ref("");

function label(node: ProxyNode): string {
  // Named nodes read by name; an unnamed one is only recognisable by the
  // endpoint it dials, so show the whole line rather than a bare host.
  return node.name || `${node.scheme}://${node.host}:${node.port}`;
}

const current = computed(() => {
  const node = props.nodes.find((item) => item.id === props.modelValue);
  return node ? label(node) : "无代理";
});

const matches = computed(() => {
  const q = query.value.trim().toLowerCase();
  if (!q) return props.nodes;
  return props.nodes.filter((node) =>
    label(node).toLowerCase().includes(q) || node.host.toLowerCase().includes(q));
});

function pick(id: string): void {
  emit("update:modelValue", id);
  open.value = false;
  query.value = "";
}
</script>

<template>
  <div class="picker">
    <button class="ghost picker-value" type="button" @click="open = !open">
      <span>{{ current }}</span>
      <span class="caret">▾</span>
    </button>

    <div v-if="open" class="panel">
      <input v-model="query" class="search" placeholder="搜索…" autocomplete="off" />
      <ul>
        <li :class="{ picked: !modelValue }" @click="pick('')">无代理</li>
        <li v-for="node in matches" :key="node.id"
            :class="{ picked: node.id === modelValue, off: !node.active }"
            :title="`${node.scheme}://${node.host}:${node.port}`"
            @click="pick(node.id)">
          <span class="dot" :class="node.active ? 'ok' : 'bad'"></span>
          {{ label(node) }}
          <span v-if="node.assigned" class="muted">（{{ node.assigned }} 个账号）</span>
        </li>
        <li v-if="!matches.length" class="muted empty">无匹配节点</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.picker { position: relative; }
.picker-value {
  display: flex; width: 100%; align-items: center; justify-content: space-between;
  gap: 8px; text-align: left;
}
.caret { color: var(--muted); flex: none; }
.panel {
  position: absolute; z-index: 60; left: 0; right: 0; top: calc(100% + 4px);
  background: var(--surface); color: var(--ink); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
}
.search { margin: 0 0 6px; }
.panel ul { list-style: none; margin: 0; padding: 0; max-height: 260px; overflow-y: auto; }
.panel li {
  display: flex; align-items: center; gap: 6px; padding: 7px 8px;
  border-radius: 6px; cursor: pointer; font-size: 13px;
}
.panel li:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.panel li.picked { color: var(--accent); }
.panel li.off { opacity: 0.6; }
.panel li.empty { cursor: default; }
.panel li.empty:hover { background: none; }
</style>
