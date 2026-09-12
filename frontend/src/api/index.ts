/** 后端接口封装。开发态走 Vite 代理（同源 /api），生产态可用 VITE_API_BASE 指定绝对地址。 */
import type {
  ApiResponse,
  Finding,
  HealthInfo,
  ResultSummary,
  TaskInfo,
} from './types'

export const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')

/** 供 EventSource / <a download> 使用的绝对地址（EventSource 不认相对路径的跨域场景）。 */
export function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(apiUrl(path), init)
  let body: ApiResponse<T> | null = null
  try {
    body = (await res.json()) as ApiResponse<T>
  } catch {
    // 非 JSON 响应（如网关错误页），走下面的统一报错
  }
  if (!res.ok) {
    throw new Error(body?.message || `请求失败（HTTP ${res.status}）`)
  }
  if (body && typeof body.code === 'number' && body.code !== 200) {
    throw new Error(body.message || '接口返回异常')
  }
  return (body?.data ?? (null as T)) as T
}

export const api = {
  health: () => request<HealthInfo>('/api/v1/health'),

  upload(file: File, onProgress?: (percent: number) => void): Promise<{ task_id: string }> {
    // 用 XHR 而不是 fetch：上传进度是 fetch 至今无法可靠提供的
    return new Promise((resolve, reject) => {
      const form = new FormData()
      form.append('file', file)
      const xhr = new XMLHttpRequest()
      xhr.open('POST', apiUrl('/api/v1/tasks/upload'))
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      }
      xhr.onload = () => {
        try {
          const body = JSON.parse(xhr.responseText) as ApiResponse<{ task_id: string }>
          if (xhr.status >= 200 && xhr.status < 300) resolve(body.data)
          else reject(new Error(body.message || `上传失败（HTTP ${xhr.status}）`))
        } catch {
          reject(new Error(`上传失败（HTTP ${xhr.status}）`))
        }
      }
      xhr.onerror = () => reject(new Error('网络异常，上传失败'))
      xhr.send(form)
    })
  },

  listTasks: () => request<TaskInfo[]>('/api/v1/tasks'),
  getTask: (id: string) => request<TaskInfo>(`/api/v1/tasks/${id}`),
  getFindings: (id: string) => request<{ task: TaskInfo; findings: Finding[] }>(`/api/v1/tasks/${id}/findings`),
  getResult: (id: string) => request<ResultSummary>(`/api/v1/tasks/${id}/result`),
  resume: (id: string) =>
    request<{ task_id: string }>(`/api/v1/tasks/${id}/resume`, { method: 'POST' }),
  downloadUrl: (id: string) => apiUrl(`/api/v1/tasks/${id}/download`),
  streamUrl: (id: string) => apiUrl(`/api/v1/tasks/${id}/stream`),
}
