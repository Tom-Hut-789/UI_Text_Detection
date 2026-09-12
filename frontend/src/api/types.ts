/** 与后端 Pydantic 模型一一对应的前端类型定义。 */

export type TaskStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED' | 'INTERRUPTED'

/**
 * 任务状态，外加一个**前端本地态** `IDLE`：页面刚打开、还没附加任何任务时使用。
 *
 * 它必须与后端的 `PENDING` 区分开——`PENDING` 表示「任务已入队等待执行」，
 * 是一个真实存在、正在占用的任务。若拿它当「还没开始」的占位值，会让
 * 「是否有任务正在处理」这类判断在空页面上就返回 true。
 */
export type TaskUiStatus = TaskStatus | 'IDLE'

export interface ApiResponse<T> {
  code: number
  message: string
  data: T
}

export interface TaskInfo {
  id: string
  filename: string
  status: TaskStatus
  total_rows: number
  processed_rows: number
  total_images: number
  processed_images: number
  defective_images: number
  error_message?: string | null
  created_at?: string
  updated_at?: string
  has_result: boolean
}

export interface ColumnSpec {
  col_index: number
  letter: string
  header: string
  language: string
  is_english: boolean
}

/** 单张截图的模型结论。 */
export interface DefectVerdict {
  is_ui_screenshot: boolean
  extracted_text: string
  has_truncation: boolean
  has_overlap: boolean
  has_missing_glyph: boolean
  severity: 'none' | 'low' | 'medium' | 'high'
  reason: string
}

export interface Finding {
  row_index: number
  col_index: number
  language: string
  is_english: boolean
  header: string
  verdict: DefectVerdict | null
  error: string | null
}

export interface LanguageStat {
  language: string
  col_index: number
  is_english: boolean
  checked: number
  truncation: number
  overlap: number
  missing_glyph: number
  defective: number
  failed: number
}

export interface ResultSummary {
  task: TaskInfo
  languages: LanguageStat[]
  summary: {
    languages_checked: number
    images_checked: number
    defective_images: number
    failed_images: number
  }
}

export interface HealthInfo {
  app: string
  model: string
  mock_llm: boolean
  llm_configured: boolean
  concurrency: number
}

/** ---- SSE 事件载荷 ---- */
export interface ProgressPayload {
  processed_rows: number
  total_rows: number
  processed_images: number
  total_images: number
  defective_images: number
  status: TaskUiStatus
  current_step: string
}

export interface StartedPayload {
  task_id: string
  filename: string
  sheet_name: string
  english_col: number
  total_rows: number
  total_images: number
  columns: ColumnSpec[]
  rows: number[]
  resumed: boolean
  skipped: number
  prior_findings: Finding[]
}

export interface CompletePayload {
  task_id: string
  status: TaskStatus
  total_rows: number
  total_images: number
  processed_images: number
  defective_images: number
  download_url: string
}

export interface ImageStartedPayload {
  row_index: number
  col_index: number
  language: string
}
