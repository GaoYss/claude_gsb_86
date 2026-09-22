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
import { deadlineHint, formatDate, todayString } from '@/utils/format'
import {
  describeHazardConflicts,
  hasActiveHazardFilters,
} from '@/utils/hazardFilter'
import { downloadBlob } from '@/utils/download'

const route = useRoute()
const dictionary = useDictionaryStore()
const toast = useToastStore()
const confirm = useConfirmStore()

const { filters, page, pageSize, syncQuery, reset } = useListQuery(
  {
    keyword: '',
    reservoir_id: '',
    category: '',
    severity: '',
    status: '',
    source: '',
    overdue_only: false,
    open_only: false,
    discovered_from: '',
    discovered_to: '',
  },
  { numericKeys: ['reservoir_id'] },
)

const rows = ref([])
const total = ref(0)
const pages = ref(0)
const summary = ref({ total: 0, open: 0, overdue: 0 })
const loading = ref(false)
const exporting = ref(false)
const serverConflict = ref('')

// 前端先于请求做冲突预判，口径与后端 HazardFilter.conflicts 保持一致
const conflicts = computed(() => describeHazardConflicts(filters.value))
const hasConflict = computed(() => conflicts.value.length > 0 || Boolean(serverConflict.value))
const conflictMessage = computed(() =>
  serverConflict.value || conflicts.value.join('；'),
)
const hasFilters = computed(() => hasActiveHazardFilters(filters.value))

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

// 下钻详情时带上当前列表地址，返回时筛选条件、分页原样恢复
function backQuery() {
  return { return: route.fullPath }
}

// 从筛选列表登记隐患时，带上当前水库作为预填，并保留返回地址
const createQuery = computed(() => {
  const query = {}
  if (filters.value.reservoir_id) query.reservoir_id = filters.value.reservoir_id
  query.return = route.fullPath
  return query
})

async function load() {
  // 条件互相冲突：不发请求、不展示看似正常的空表，直接在页面说明
  if (conflicts.value.length) {
    serverConflict.value = ''
    rows.value = []
    total.value = 0
    pages.value = 0
    summary.value = { total: 0, open: 0, overdue: 0 }
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
    summary.value = data.summary
    serverConflict.value = ''
  } catch (error) {
    // 后端兜底：冲突类 422 在页面内直示，其余错误走全局提示
    if (error.status === 422 && /冲突|晚于/.test(error.message)) {
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

// 清空条件回到默认视图（watch 会自动以默认条件重新查询）
function resetFilters() {
  reset()
}

async function exportList() {
  if (hasConflict.value) {
    toast.error('当前筛选条件存在冲突，请先调整后再导出')
    return
  }
  exporting.value = true
  try {
    // 导出参数与列表请求完全相同（不带分页），保证导出即所见
    const blob = await exportHazards({ ...filters.value })
    downloadBlob(blob, `隐患台账_${todayString()}.csv`)
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
</script>

<template>
  <div>
    <PageHeader title="隐患与整改" description="登记巡查发现的隐患，跟踪整改与验收销号全过程">
      <template #actions>
        <RouterLink class="btn btn-primary" :to="{ name: 'hazard-create', query: createQuery }">
          登记隐患
        </RouterLink>
      </template>
    </PageHeader>

    <HazardFilterBar v-model="filters" @reset="resetFilters" />

    <!-- 条件冲突：直接说明冲突点与解决方式，不返回看似正常的空表 -->
    <div v-if="hasConflict" class="conflict-banner" role="alert">
      <div class="conflict-text">
        <strong>筛选条件存在冲突：</strong>{{ conflictMessage }}
      </div>
      <div class="conflict-actions">
        <button class="btn btn-sm btn-danger" type="button" @click="resetFilters">清空筛选条件</button>
      </div>
    </div>

    <section class="card">
      <div class="result-bar">
        <template v-if="!hasConflict">
          <span class="result-count">
            共 <strong>{{ summary.total }}</strong> 条符合条件的隐患
            <template v-if="hasFilters">（已按当前条件筛选）</template>
          </span>
          <span class="result-meta">
            未销号 <strong>{{ summary.open }}</strong> · 逾期 <strong class="text-danger">{{ summary.overdue }}</strong>
          </span>
        </template>
        <span v-else class="result-count muted">计数与导出在冲突解除后可用</span>
        <div class="result-actions">
          <button
            class="btn btn-sm"
            type="button"
            :disabled="exporting || hasConflict"
            @click="exportList"
          >
            {{ exporting ? '导出中…' : '导出 CSV（当前筛选）' }}
          </button>
        </div>
      </div>

      <template v-if="!hasConflict">
        <DataTable
          v-if="loading || rows.length"
          :columns="columns"
          :rows="rows"
          :loading="loading"
          empty-text="没有符合条件的隐患"
        >
          <template #code="{ row }">
            <RouterLink class="mono" :to="{ name: 'hazard-detail', params: { id: row.id }, query: backQuery() }">
              {{ row.code }}
            </RouterLink>
          </template>
          <template #title="{ row }">
            <RouterLink :to="{ name: 'hazard-detail', params: { id: row.id }, query: backQuery() }">
              {{ row.title }}
            </RouterLink>
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
              <RouterLink
                class="btn-link"
                :to="{ name: 'hazard-detail', params: { id: row.id }, query: backQuery() }"
              >
                跟踪
              </RouterLink>
              <RouterLink
                class="btn-link"
                :to="{ name: 'hazard-edit', params: { id: row.id }, query: backQuery() }"
              >
                编辑
              </RouterLink>
              <button class="btn-link danger" type="button" @click="remove(row)">删除</button>
            </div>
          </template>
        </DataTable>

        <!-- 无结果：区分“库中还没有隐患”与“筛选过严”，给出可操作入口 -->
        <EmptyState
          v-else-if="!loading && total === 0"
          :title="hasFilters ? '没有符合当前筛选条件的隐患' : '还没有隐患记录'"
          :description="
            hasFilters
              ? '可以放宽或清空部分筛选条件后重试；若隐患尚未登记，可直接登记一条。'
              : '巡查发现异常后，可在此登记第一条隐患并跟踪整改闭环。'
          "
        >
          <template #action>
            <div class="empty-actions">
              <button v-if="hasFilters" class="btn" type="button" @click="resetFilters">
                清空筛选条件
              </button>
              <RouterLink class="btn btn-primary" :to="{ name: 'hazard-create', query: createQuery }">
                登记隐患
              </RouterLink>
            </div>
          </template>
        </EmptyState>

        <PaginationBar
          v-if="total > 0"
          v-model:page="page"
          v-model:page-size="pageSize"
          :total="total"
          :pages="pages"
        />
      </template>
    </section>
  </div>
</template>
