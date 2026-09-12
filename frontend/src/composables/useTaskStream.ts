/**
 * SSE 任务进度订阅。
 *
 * 浏览器原生 EventSource 会在连接断开后自动重连，并带上 `Last-Event-ID` 头；
 * 后端据此回放漏掉的事件，因此这里无需自己做补偿逻辑。
 * 唯一要处理的是：任务进入终态后必须**主动关闭**连接，
 * 否则 EventSource 会对一个已经不会再产出事件的流无限重连。
 */
import { computed, onScopeDispose, ref } from 'vue'
import { api } from '@/api'
import type {
  CompletePayload,
  Finding,
  ImageStartedPayload,
  ProgressPayload,
  StartedPayload,
  TaskInfo,
  TaskStatus,
  TaskUiStatus,
} from '@/api/types'

export type StreamStatus = 'idle' | 'connecting' | 'open' | 'closed' | 'error'

/**
 * 状态是否表示「有任务正在跑（含排队）」。
 *
 * 抽成纯函数是因为 App.vue 与 TaskProgress.vue 都要用；此前两处各写了一份
 * `status === 'PROCESSING' || status === 'PENDING'`，任何一处漏改都会让
 * 上传区在错误的时候被禁用。
 */
export function isBusyStatus(status: TaskUiStatus): boolean {
  return status === 'PROCESSING' || status === 'PENDING'
}

export function useTaskStream() {
  const status = ref<StreamStatus>('idle')
  const task = ref<TaskInfo | null>(null)
  const started = ref<StartedPayload | null>(null)
  const progress = ref<ProgressPayload>({
    processed_rows: 0,
    total_rows: 0,
    processed_images: 0,
    total_images: 0,
    defective_images: 0,
    // 必须是 IDLE 而不是 PENDING：后者会被 isBusyStatus 判定为「正在跑」，
    // 导致页面一打开上传区就是禁用状态，文件拖不进去。
    status: 'IDLE',
    current_step: '',
  })
  /** 实时结论，按「行-列」去重，断线重放不会产生重复行 */
  const findings = ref<Map<string, Finding>>(new Map())
  const activeCells = ref<Set<string>>(new Set())
  const errorMessage = ref('')
  const finished = ref(false)
  /** 是否有任务正在处理中（含排队）。未附加任务时为 false，上传区因此保持可用。 */
  const busy = computed(() => isBusyStatus(progress.value.status))

  let source: EventSource | null = null

  function close() {
    source?.close()
    source = null
    status.value = 'closed'
  }

  function upsertFinding(f: Finding) {
    const key = `${f.row_index}-${f.col_index}`
    // Map 是浅层响应式，替换键值才能触发依赖更新
    findings.value.set(key, f)
    findings.value = new Map(findings.value)
    activeCells.value.delete(key)
    activeCells.value = new Set(activeCells.value)
  }

  function open(taskId: string) {
    close()
    findings.value = new Map()
    activeCells.value = new Set()
    errorMessage.value = ''
    finished.value = false
    status.value = 'connecting'

    const es = new EventSource(api.streamUrl(taskId))
    source = es

    es.onopen = () => {
      status.value = 'open'
    }

    es.addEventListener('state', (e) => {
      const info = JSON.parse((e as MessageEvent).data) as TaskInfo
      task.value = info
      progress.value = {
        ...progress.value,
        processed_images: info.processed_images,
        total_images: info.total_images,
        processed_rows: info.processed_rows,
        total_rows: info.total_rows,
        defective_images: info.defective_images,
        status: info.status,
      }
    })

    es.addEventListener('started', (e) => {
      const payload = JSON.parse((e as MessageEvent).data) as StartedPayload
      started.value = payload
      progress.value = {
        ...progress.value,
        total_rows: payload.total_rows,
        total_images: payload.total_images,
        status: 'PROCESSING',
      }
      // 断点续跑时，先恢复上一轮已经拿到的结论
      payload.prior_findings?.forEach(upsertFinding)
    })

    es.addEventListener('progress', (e) => {
      progress.value = JSON.parse((e as MessageEvent).data) as ProgressPayload
    })

    es.addEventListener('image_started', (e) => {
      const p = JSON.parse((e as MessageEvent).data) as ImageStartedPayload
      activeCells.value.add(`${p.row_index}-${p.col_index}`)
      activeCells.value = new Set(activeCells.value)
    })

    es.addEventListener('finding', (e) => {
      upsertFinding(JSON.parse((e as MessageEvent).data) as Finding)
    })

    es.addEventListener('complete', (e) => {
      const payload = JSON.parse((e as MessageEvent).data) as CompletePayload
      progress.value = {
        ...progress.value,
        processed_images: payload.processed_images,
        total_images: payload.total_images,
        defective_images: payload.defective_images,
        status: 'COMPLETED',
        current_step: '全部处理完成',
      }
      finished.value = true
      close()
    })

    es.addEventListener('error', (e) => {
      // 有 data 的是后端主动推送的业务错误；没有的是连接层错误
      const raw = (e as MessageEvent).data
      if (raw) {
        const payload = JSON.parse(raw) as { message: string; status: TaskStatus }
        errorMessage.value = payload.message
        progress.value = { ...progress.value, status: payload.status }
        close()
      } else if (es.readyState === EventSource.CLOSED) {
        status.value = 'error'
        close()
      }
      // readyState === CONNECTING 时是浏览器正在自动重连，保持等待
    })

    es.addEventListener('closed', () => {
      finished.value = true
      close()
    })
  }

  onScopeDispose(close)

  return {
    status,
    task,
    started,
    progress,
    findings,
    activeCells,
    errorMessage,
    finished,
    busy,
    open,
    close,
  }
}
