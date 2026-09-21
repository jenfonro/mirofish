<script setup lang="ts">
import { computed } from "vue";
import { accountStatus } from "../accounts";
import type { Account } from "../types";

const props = defineProps<{ account: Account }>();
const status = computed(() => accountStatus(props.account));
</script>

<template>
  <span class="chip" :class="status.tone" :title="account.health?.message || undefined">
    {{ status.label }}
  </span>
  <span v-if="account.shared_quota_cooldown && status.label !== '窗口冷却'"
        class="chip warn">窗口冷却</span>
</template>
