import { compact, http } from './http'

export function fetchHazards(params) {
  return http.get('/hazards', { params: compact(params) })
}

/** 导出当前筛选条件下的隐患 CSV；走 blob，浏览器按附件下载 */
export function exportHazards(params) {
  return http.get('/hazards/export', {
    params: compact(params),
    responseType: 'blob',
  })
}

export function fetchHazard(id) {
  return http.get(`/hazards/${id}`)
}

export function createHazard(payload) {
  return http.post('/hazards', payload)
}

export function updateHazard(id, payload) {
  return http.put(`/hazards/${id}`, payload)
}

export function deleteHazard(id) {
  return http.delete(`/hazards/${id}`)
}

export function addRectification(id, payload) {
  return http.post(`/hazards/${id}/rectifications`, payload)
}

export function transitionHazard(id, payload) {
  return http.post(`/hazards/${id}/transition`, payload)
}
