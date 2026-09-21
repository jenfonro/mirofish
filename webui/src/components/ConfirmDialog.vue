<script setup lang="ts">
import { onMounted, ref } from "vue";

defineProps<{ title: string; message: string; busy: boolean }>();
const emit = defineEmits<{ confirm: []; close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
onMounted(() => dialog.value?.showModal());
</script>

<template>
  <dialog ref="dialog" aria-labelledby="confirm-title"
          @cancel.prevent="!busy && emit('close')">
    <h3 id="confirm-title">{{ title }}</h3>
    <p class="muted mt-3">{{ message }}</p>
    <div class="row mt-3" style="justify-content:flex-end">
      <button class="btn ghost" :disabled="busy" @click="emit('close')">取消</button>
      <button class="btn danger" :disabled="busy" @click="emit('confirm')">
        {{ busy ? "删除中…" : "确认删除" }}
      </button>
    </div>
  </dialog>
</template>

<style scoped>
dialog {
  width: min(420px, calc(100vw - 32px)); padding: 20px;
  border: 1px solid var(--border); border-radius: var(--r-lg);
  background: var(--surface); color: var(--ink); box-shadow: var(--shadow-lg);
}
dialog::backdrop { background: var(--overlay); }
</style>
