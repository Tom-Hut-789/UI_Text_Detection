<script setup lang="ts">
import { computed } from 'vue'
import type { Finding, ProgressPayload, StartedPayload, TaskInfo } from '@/api/types'
import { isBusyStatus, type StreamStatus } from '@/composables/useTaskStream'

const props = defineProps<{
  task: TaskInfo | null
  started: StartedPayload | null
  progress: ProgressPayload
  findings: Map<string, Finding>
  activeCells: Set<string>
  streamStatus: StreamStatus
  errorMessage: string
}>()

const imagePercent = computed(() => {
  const { processed_images: done, total_images: total } = props.progress
  if (!total) return 0
  return Math.min(100, Math.round((done / total) * 100))
})

const isRunning = computed(() => isBusyStatus(props.progress.status))

/** 实时结论列表：缺陷优先、再按行列排序，最多展示 200 条以免 DOM 过大 */
const feed = computed(() => {
  const list = [...props.findings.values()]
  list.sort((a, b) => {
    const da = a.verdict?.has_truncation || a.verdict?.has_overlap || a.verdict?.has_missing_glyph ? 0 : 1
    const db = b.verdict?.has_truncation || b.verdict?.has_overlap || b.verdict?.has_missing_glyph ? 0 : 1
    if (da !== db) return da - db
    return a.row_index - b.row_index || a.col_index - b.col_index
  })
  return list.slice(0, 200)
})

const completionRate = computed(() => {
  const { processed_images: done, total_images: total } = props.progress
  if (!total) return '0 / 0'
  return `${done} / ${total}`
})

function defectLabels(f: Finding): string[] {
  if (!f.verdict) return []
  const out: string[] = []
  if (f.verdict.has_truncation) out.push('截断')
  if (f.verdict.has_overlap) out.push('重叠')
  if (f.verdict.has_missing_glyph) out.push('缺字')
  return out
}

function isActive(f: Finding): boolean {
  return props.activeCells.has(`${f.row_index}-${f.col_index}`)
}

function severityOf(f: Finding): string {
  if (f.error) return 'severity-high'
  return `severity-${f.verdict?.severity ?? 'none'}`
}
</script>

<template>
  <el-card shadow="never" class="!rounded-xl">
    <template #header>
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-3">
          <span class="font-semibold">检测进度</span>
          <el-tag v-if="task" size="small" effect="plain">{{ task.filename }}</el-tag>
          <el-tag v-if="started?.resumed" size="small" type="warning" effect="plain">
            断点续跑 · 已跳过 {{ started.skipped }} 张
          </el-tag>
        </div>
        <div class="flex items-center gap-2">
          <el-tag v-if="streamStatus === 'open' && isRunning" size="small" type="primary">实时连接中</el-tag>
          <el-tag v-else-if="streamStatus === 'error'" size="small" type="danger">连接中断</el-tag>
          <el-tag v-else-if="progress.status === 'COMPLETED'" size="small" type="success">已完成</el-tag>
        </div>
      </div>
    </template>

    <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <div class="rounded-lg bg-slate-50 p-3">
        <div class="text-xs text-gray-500">已处理截图</div>
        <div class="mt-1 text-xl font-semibold text-brand-600">{{ completionRate }}</div>
      </div>
      <div class="rounded-lg bg-slate-50 p-3">
        <div class="text-xs text-gray-500">数据行</div>
        <div class="mt-1 text-xl font-semibold">
          {{ progress.processed_rows }} / {{ progress.total_rows }}
        </div>
      </div>
      <div class="rounded-lg bg-slate-50 p-3">
        <div class="text-xs text-gray-500">检出缺陷截图</div>
        <div class="mt-1 text-xl font-semibold text-red-600">{{ progress.defective_images }}</div>
      </div>
      <div class="rounded-lg bg-slate-50 p-3">
        <div class="text-xs text-gray-500">待检测语种列</div>
        <div class="mt-1 text-xl font-semibold">{{ Math.max(0, (started?.columns.length ?? 1) - 1) }}</div>
      </div>
    </div>

    <el-progress
      class="mt-4"
      :percentage="imagePercent"
      :stroke-width="16"
      :status="progress.status === 'COMPLETED' ? 'success' : undefined"
      striped
      :striped-flow="isRunning"
    />

    <div class="mt-2 flex items-center gap-2 text-sm text-gray-500">
      <el-icon v-if="isRunning" class="is-loading"><Loading /></el-icon>
      <span>{{ progress.current_step || '等待开始…' }}</span>
    </div>

    <el-alert
      v-if="errorMessage"
      class="mt-4"
      type="error"
      :closable="false"
      show-icon
      :title="errorMessage"
    />

    <div v-if="feed.length" class="mt-5">
      <div class="mb-2 flex items-center justify-between">
        <span class="text-sm font-medium text-gray-600">
          实时结论（{{ findings.size }} 条，缺陷优先展示前 200 条）
        </span>
      </div>
      <el-table :data="feed" size="small" max-height="320" class="thin-scroll" stripe>
        <el-table-column prop="row_index" label="行" width="64" align="center" />
        <el-table-column prop="language" label="语种" width="150" show-overflow-tooltip />
        <el-table-column label="结论" width="150">
          <template #default="{ row }">
            <el-tag v-if="row.error" size="small" type="danger">检测失败</el-tag>
            <el-tag v-else-if="!defectLabels(row).length" size="small" type="success">无缺陷</el-tag>
            <el-tag
              v-for="label in defectLabels(row)"
              :key="label"
              class="mr-1"
              size="small"
              type="danger"
            >
              {{ label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="依据" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="severityOf(row)">
              {{ row.error || row.verdict?.reason || '—' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="" width="72" align="center">
          <template #default="{ row }">
            <el-icon v-if="isActive(row)" class="is-loading text-brand-500"><Loading /></el-icon>
            <span v-else class="text-green-600">✓</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </el-card>
</template>
