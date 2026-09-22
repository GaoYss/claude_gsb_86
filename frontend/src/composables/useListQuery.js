import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

/**
 * 列表页筛选条件：初始化时读取 URL 参数，变更后写回 URL。
 * 这样从详情页跳转（如 /inspections?reservoir_id=3）能带出过滤条件，刷新也不丢状态。
 *
 * @param {Record<string, unknown>} defaults 筛选默认值，同时决定字段是否参与 URL 同步
 * @param {object} options
 * @param {number} options.pageSize 默认每页条数
 * @param {string[]} options.numericKeys 以数字类型还原的字段（如下拉值为 number 的水库 id）
 */
export function useListQuery(defaults, { pageSize = 20, numericKeys = [] } = {}) {
  const route = useRoute()
  const router = useRouter()

  const filters = ref({ ...defaults })
  const page = ref(1)
  const size = ref(pageSize)

  for (const [key, fallback] of Object.entries(defaults)) {
    const raw = route.query[key]
    if (raw === undefined) continue
    const value = Array.isArray(raw) ? raw[0] : raw
    if (typeof fallback === 'boolean') {
      filters.value[key] = value === 'true'
    } else if (numericKeys.includes(key)) {
      const parsed = Number(value)
      filters.value[key] = value === '' || Number.isNaN(parsed) ? fallback : parsed
    } else {
      filters.value[key] = value
    }
  }
  if (route.query.page) page.value = Number(route.query.page) || 1
  if (route.query.page_size) size.value = Number(route.query.page_size) || pageSize

  function syncQuery() {
    const query = { ...filters.value, page: page.value, page_size: size.value }
    for (const key of Object.keys(query)) {
      const value = query[key]
      if (value === '' || value === null || value === undefined || value === false) delete query[key]
    }
    if (query.page === 1) delete query.page
    if (query.page_size === pageSize) delete query.page_size
    router.replace({ query })
  }

  function reset() {
    filters.value = { ...defaults }
    page.value = 1
    size.value = pageSize
  }

  /** 筛选条件变化时回到第一页 */
  watch(filters, () => {
    page.value = 1
  }, { deep: true })

  return { filters, page, pageSize: size, syncQuery, reset }
}
