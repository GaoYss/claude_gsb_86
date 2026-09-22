<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { deleteHazard, exportHazards, fetchHazards } from '@/api/hazards'
import DataTable from '@/components/common/DataTable.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PaginationBar from '@/components/common/PaginationBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import HazardFilterBar from '@/components/hazard/HazardFilterBar.vue'
import { useListQuery } from '@/composables/useListQuery'
import { useConfirmStore } from '@/stores/confirm'
import { useDictionaryStore } from '@/stores/dictionary'
import { useToastStore } from '@/stores/toast'
import { deadlineHint, formatDate } from '@/utils/format'

const route = useRoute()
const dictionary = useDictionaryStore()
const toast = useToastStore()
const confirm = useConfirmStore()

const { filters, page, pageSize, syncQuery, reset } = useListQuery({
  keyword: '',
  reservoir_id: '',
  category: '',
  severity: '',
  status: '',
  discovered_from: '',
  discovered_to: '',
  overdue_only: false,
  open_only: false,
})

const rows = ref([])
const total = ref(0)
const pages = ref(0)
const loading = ref(true)
// 后端判定的冲突（例如直接访问带冲突参数的 URL），与前端预判合并展示
const serverConflict = ref('')

const columns = [
  { key: 'code', label: '隐患编号', width: '150px' },
  { key: 'title', label: '隐患标题' },
  { key: 'reservoir', label: '水库', width: '140px' },
  { key: 'category', label: '部位', width: '100px' },
  { key: 'severity', label: '等级', width: '110px' },
  { key: 'status', label: '整改状态', width: '110px' },
  { key: 'discovered_on', label: '发现日期', width: '120px' },
  { key: 'deadline', label: '整改期限', width: '170px' },
  { key: 'actions', label: '操作', width: '150px' },
]

const FILTER_FIELDS = [
  'keyword',
  'reservoir_id',
  'category',
  'severity',
  'status',
  'discovered_from',
  'discovered_to',
  'overdue_only',
  'open_only',
]

const hasActiveFilters = computed(() =>
  FILTER_FIELDS.some((key) => {
    const value = filters.value[key]
    return value !== '' && value !== null && value !== undefined && value !== false
  }),
)

const activeFilterCount = computed(() =>
  FILTER_FIELDS.filter((key) => {
    const value = filters.value[key]
    return value !== '' && value !== null && value !== undefined && value !== false
  }).length,
)

// 与后端 validate_filter 同口径的前置校验：冲突时不发请求，直接在页面上说明
const conflicts = computed(() => {
  const list = []
  const value = filters.value
  if (
    value.discovered_from &&
    value.discovered_to &&
    value.discovered_from > value.discovered_to
  ) {
    list.push(
      `发现日期范围不合法：起始日期（${formatDate(value.discovered_from)}）` +
        `晚于截止日期（${formatDate(value.discovered_to)}）`,
    )
  }
  if (value.status === 'closed') {
    if (value.open_only) {
      list.push('整改状态选择了「已销号」，同时又勾选了「仅看未销号」，二者互相矛盾')
    }
    if (value.overdue_only) {
      list.push('已销号隐患不存在逾期，「状态 = 已销号」与「仅看逾期」无法同时生效')
    }
  }
  if (serverConflict.value && !list.includes(serverConflict.value)) {
    list.push(serverConflict.value)
  }
  return list
})

async function load() {
  // 每次查询前清掉上一次的服务端冲突提示，避免修改条件后旧提示残留
  serverConflict.value = ''
  // 条件冲突时不再请求，避免展示一张看起来正常、其实是被冲突条件清空的表
  if (conflicts.value.length) {
    rows.value = []
    total.value = 0
    pages.value = 0
    return
  }
  loading.value = true
  try {
    const data = await fetchHazards({
      ...filters.value,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = data.items
    total.value = data.total
    pages.value = data.pages
  } catch (error) {
    if (error.status === 422) {
      serverConflict.value = error.message
      rows.value = []
      total.value = 0
      pages.value = 0
    } else {
      toast.error(error.message)
    }
  } finally {
    loading.value = false
  }
}

watch(
  [filters, page, pageSize],
  () => {
    syncQuery()
    load()
  },
  { deep: true },
)

onMounted(load)

function resetFilters() {
  serverConflict.value = ''
  reset()
  // reset 会触发 filters watcher，由其统一 syncQuery + load，避免重复请求
}

const exporting = ref(false)

async function exportList() {
  if (conflicts.value.length) {
    toast.error('筛选条件存在冲突，请先调整或清空后再导出')
    return
  }
  exporting.value = true
  try {
    // 导出参数与列表请求完全一致（不含分页），保证导出内容与筛选结果、顶部计数同源
    const { blob, filename } = await exportHazards({ ...filters.value })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    toast.success('导出成功')
  } catch (error) {
    toast.error(error.message)
  } finally {
    exporting.value = false
  }
}

async function remove(row) {
  const ok = await confirm.ask(`确认删除隐患「${row.title}」？关联的整改跟踪记录会一并删除。`)
  if (!ok) return
  try {
    await deleteHazard(row.id)
    toast.success('隐患已删除')
    if (rows.value.length === 1 && page.value > 1) page.value -= 1
    else load()
  } catch (error) {
    toast.error(error.message)
  }
}

// 下钻到详情页时带上当前筛选条件，详情页据此返回原列表视图
function detailTarget(row) {
  return { name: 'hazard-detail', params: { id: row.id }, query: { ...route.query } }
}
</script>

<template>
  <div>
    <PageHeader title="隐患与整改" description="登记巡查发现的隐患，跟踪整改与验收销号全过程">
      <template #actions>
        <button class="btn" type="button" :disabled="exporting" @click="exportList">
          {{ exporting ? '导出中…' : '导出 CSV' }}
        </button>
        <RouterLink class="btn btn-primary" to="/hazards/new">登记隐患</RouterLink>
      </template>
    </PageHeader>

    <HazardFilterBar v-model="filters" @reset="resetFilters" />

    <section class="card">
      <div class="card-header">
        <div>
          <div class="card-title">隐患台账</div>
          <div class="card-subtitle">
            <template v-if="conflicts.length">筛选条件存在冲突，未执行查询</template>
            <template v-else-if="loading">查询中…</template>
            <template v-else>
              共 <strong>{{ total }}</strong> 条隐患
              <span v-if="hasActiveFilters">（已应用 {{ activeFilterCount }} 个筛选条件）</span>
            </template>
          </div>
        </div>
      </div>

      <div v-if="conflicts.length" class="alert alert-warning" role="alert">
        <div class="alert-title">筛选条件互相冲突，无法查询</div>
        <ul class="alert-list">
          <li v-for="item in conflicts" :key="item">{{ item }}</li>
        </ul>
        <div class="alert-actions">
          <button class="btn btn-primary btn-sm" type="button" @click="resetFilters">清空条件</button>
          <span class="muted">清空后将回到默认视图（全部隐患）</span>
        </div>
      </div>

      <EmptyState
        v-else-if="!loading && rows.length === 0"
        :title="hasActiveFilters ? '没有符合条件的隐患' : '暂无隐患'"
        :description="
          hasActiveFilters
            ? '当前筛选组合下没有隐患，可放宽部位、等级、状态条件或扩大发现日期范围后重试。'
            : '还没有登记任何隐患，登记后可在此跟踪整改与销号全过程。'
        "
      >
        <template #action>
          <div class="row-gap" style="justify-content: center">
            <button v-if="hasActiveFilters" class="btn" type="button" @click="resetFilters">
              清空筛选条件
            </button>
            <RouterLink class="btn btn-primary" to="/hazards/new">登记隐患</RouterLink>
          </div>
        </template>
      </EmptyState>

      <template v-else>
        <DataTable :columns="columns" :rows="rows" :loading="loading" empty-text="没有符合条件的隐患">
          <template #code="{ row }">
            <RouterLink class="mono" :to="detailTarget(row)">{{ row.code }}</RouterLink>
          </template>
          <template #title="{ row }">
            <RouterLink :to="detailTarget(row)">{{ row.title }}</RouterLink>
          </template>
          <template #reservoir="{ row }">
            <RouterLink v-if="row.reservoir" :to="`/reservoirs/${row.reservoir.id}`">
              {{ row.reservoir.name }}
            </RouterLink>
            <span v-else class="muted">—</span>
          </template>
          <template #category="{ row }">{{ dictionary.labelOf('structure_part', row.category) }}</template>
          <template #severity="{ row }">
            <StatusTag kind="hazard_severity" :value="row.severity" />
          </template>
          <template #status="{ row }">
            <StatusTag kind="hazard_status" :value="row.status" />
          </template>
          <template #discovered_on="{ row }">{{ formatDate(row.discovered_on) }}</template>
          <template #deadline="{ row }">
            <span class="nowrap">{{ formatDate(row.deadline) }}</span>
            <span v-if="row.is_overdue" class="tag tag-overdue" style="margin-left: 6px">逾期</span>
            <div class="timeline-meta">{{ deadlineHint(row.deadline, row.status === 'closed') }}</div>
          </template>
          <template #actions="{ row }">
            <div class="cell-actions">
              <RouterLink class="btn-link" :to="detailTarget(row)">跟踪</RouterLink>
              <RouterLink class="btn-link" :to="`/hazards/${row.id}/edit`">编辑</RouterLink>
              <button class="btn-link danger" type="button" @click="remove(row)">删除</button>
            </div>
          </template>
        </DataTable>
        <PaginationBar v-model:page="page" v-model:page-size="pageSize" :total="total" :pages="pages" />
      </template>
    </section>
  </div>
</template>
