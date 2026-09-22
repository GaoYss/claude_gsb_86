import axios from 'axios'

import { compact, http } from './http'

export function fetchHazards(params) {
  return http.get('/hazards', { params: compact(params) })
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

async function readBlobError(blob) {
  // 后端报错是 JSON，但 responseType=blob 时响应体也是 Blob，需要读出来解析中文 detail
  try {
    const data = JSON.parse(await blob.text())
    if (typeof data.detail === 'string') return data.detail
  } catch {
    // 非 JSON 响应时走兜底提示
  }
  return ''
}

/**
 * 导出隐患台账：参数与列表查询完全一致（不含分页），
 * 由后端同一筛选口径生成，返回 Blob 与建议文件名。
 */
export async function exportHazards(params) {
  const baseURL = import.meta.env.VITE_API_BASE_URL || '/api/v1'
  let response
  try {
    response = await axios.get(`${baseURL}/hazards/export`, {
      params: compact(params),
      responseType: 'blob',
      timeout: 60000,
    })
  } catch (error) {
    const appError = new Error('导出失败')
    if (error.response) {
      appError.status = error.response.status
      appError.message =
        (error.response.data instanceof Blob && (await readBlobError(error.response.data))) ||
        `导出失败（HTTP ${error.response.status}）`
    } else if (error.request) {
      appError.status = 0
      appError.message = '无法连接后端服务，请确认 API 已启动'
    } else {
      appError.status = -1
      appError.message = error.message
    }
    throw appError
  }

  let filename = ''
  const disposition = response.headers['content-disposition']
  if (disposition) {
    const match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
    if (match) filename = decodeURIComponent(match[1])
  }
  const today = new Date()
  const pad = (value) => String(value).padStart(2, '0')
  const fallback = `隐患台账_${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}.csv`
  return { blob: response.data, filename: filename || fallback }
}
