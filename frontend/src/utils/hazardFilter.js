import { compact } from '@/api/http'

/** 是否存在生效中的筛选条件（空字符串、false 等默认值视为未设置） */
export function hasActiveHazardFilters(filters) {
  return Object.values(compact(filters)).some((value) => value !== false)
}

/**
 * 前端侧的条件冲突预判，与后端 HazardFilter.conflicts 口径保持一致。
 * 页面先于请求给出提示；后端 422 仍作为最终兜底。
 */
export function describeHazardConflicts(filters = {}) {
  const conflicts = []
  if (filters.status === 'closed' && filters.open_only) {
    conflicts.push('「整改状态 = 已销号」与「仅看未销号」互相冲突，不可能同时满足')
  }
  if (filters.status === 'closed' && filters.overdue_only) {
    conflicts.push('「整改状态 = 已销号」与「仅看逾期」互相冲突，已销号隐患不存在逾期')
  }
  if (
    filters.discovered_from &&
    filters.discovered_to &&
    filters.discovered_from > filters.discovered_to
  ) {
    conflicts.push('发现日期范围无效：开始日期晚于结束日期')
  }
  return conflicts
}
