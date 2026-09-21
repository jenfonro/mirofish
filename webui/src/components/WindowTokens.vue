<script setup lang="ts">
import { compact } from "../limits";
import type { WindowModelUsage } from "../types";

defineProps<{ models: WindowModelUsage[] }>();
</script>

<template>
  <div v-if="models.length" class="window-tokens">
    <div v-for="m in models" :key="m.model"
         :title="`${m.model}：${m.requests} 次 · 输入 ${m.input_tokens.toLocaleString()} · `
           + `输出 ${m.output_tokens.toLocaleString()} · 缓存读 ${m.cache_read_tokens.toLocaleString()} · `
           + `缓存写 ${m.cache_write_tokens.toLocaleString()}`">
      <div class="row token-head">
        <span class="mono">{{ m.model.replace("claude-", "") }}</span>
        <span class="tnum">{{ m.requests }} 次 · {{ compact(m.total_tokens) }}</span>
      </div>
      <div class="muted token-detail">
        入 {{ compact(m.input_tokens) }} / 出 {{ compact(m.output_tokens) }}
        · 缓存读 {{ compact(m.cache_read_tokens) }} / 写 {{ compact(m.cache_write_tokens) }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.window-tokens { display: grid; gap: 6px; margin-top: 8px; }
.token-head { justify-content: space-between; font-size: 11px; gap: 4px; }
.token-detail { font-size: 10.5px; }
</style>
