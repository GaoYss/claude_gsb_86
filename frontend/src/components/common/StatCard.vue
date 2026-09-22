<script setup>
import { RouterLink } from 'vue-router'

defineProps({
  label: { type: String, required: true },
  value: { type: [Number, String], default: 0 },
  hint: { type: String, default: '' },
  tone: { type: String, default: '' },
  /** 传入则整张卡片可点击下钻（vue-router location） */
  to: { type: [String, Object], default: null },
})
</script>

<template>
  <component
    :is="to ? RouterLink : 'div'"
    class="stat-card"
    :class="{ 'stat-link': to }"
    v-bind="to ? { to } : {}"
  >
    <div class="stat-label">{{ label }}</div>
    <div class="stat-value" :class="tone ? `tone-${tone}` : ''">{{ value }}</div>
    <div class="stat-hint">{{ hint }}</div>
  </component>
</template>
