<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import { TAG_TONES } from '@/utils/constants'

const props = defineProps({
  items: { type: Array, default: () => [] },
  toneKey: { type: String, default: '' },
  /** 返回某一项对应的下钻地址（vue-router location）；返回空值时该行不可点 */
  toFor: { type: Function, default: null },
})

const max = computed(() => Math.max(1, ...props.items.map((item) => item.count || 0)))

function width(item) {
  return `${Math.round(((item.count || 0) / max.value) * 100)}%`
}

function tone(item) {
  return TAG_TONES[props.toneKey]?.[item.value] || ''
}

function target(item) {
  return props.toFor?.(item) || null
}
</script>

<template>
  <div class="distribution">
    <component
      :is="target(item) ? RouterLink : 'div'"
      v-for="item in items"
      :key="item.value"
      class="distribution-row"
      :class="{ 'is-link': target(item) }"
      v-bind="target(item) ? { to: target(item) } : {}"
    >
      <span class="distribution-name">{{ item.label }}</span>
      <span class="distribution-bar">
        <span class="distribution-fill" :class="tone(item) ? `tone-${tone(item)}` : ''" :style="{ width: width(item) }" />
      </span>
      <span class="distribution-count">{{ item.count }}</span>
    </component>
  </div>
</template>
