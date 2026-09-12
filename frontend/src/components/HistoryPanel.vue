<script setup lang="ts">
import { Download } from '@element-plus/icons-vue'
import { api } from '@/api'
import type { TaskInfo, TaskStatus } from '@/api/types'

defineProps<{ tasks: TaskInfo[]; activeId: string; loading: boolean }>()
const emit = defineEmits<{ (e: 'select', id: string): void; (e: 'refresh'): void }>()

const STATUS_META: Record<TaskStatus, { label: string; type: 'success' | 'info' | 'warning' | 'danger' | 'primary' }> = {
  PENDING: { label: '排队中', type: 'info' },
  PROCESSING: { label: '处理中', type: 'primary' },
  COMPLETED: { label: '已完成', type: 'success' },
  FAILED: { label: '失败', type: 'danger' },
  INTERRUPTED: { label: '已中断', type: 'warning' },
}

function meta(status: TaskStatus) {
  return STATUS_META[status] ?? STATUS_META.PENDING
}

function time(iso?: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

function downloadResult(id: string) {
  window.open(api.downloadUrl(id), '_blank')
}
</script>

<template>
  <el-card shadow="never" class="!rounded-xl">
    <template #header>
      <div class="flex items-center justify-between">
        <span class="font-semibold">历史任务</span>
        <el-button link type="primary" size="small" @click="emit('refresh')">刷新</el-button>
      </div>
    </template>

    <div v-loading="loading" class="thin-scroll max-h-[380px] overflow-y-auto pr-1">
      <el-empty v-if="!tasks.length && !loading" description="还没有任务记录" :image-size="60" />
      <div
        v-for="t in tasks"
        :key="t.id"
        class="mb-2 cursor-pointer rounded-lg border p-3 transition hover:border-brand-500 hover:bg-brand-50/50"
        :class="t.id === activeId ? 'border-brand-500 bg-brand-50' : 'border-gray-200'"
        @click="emit('select', t.id)"
      >
        <div class="flex items-start justify-between gap-2">
          <span class="truncate text-sm font-medium" :title="t.filename">{{ t.filename }}</span>
          <el-tag size="small" :type="meta(t.status).type" effect="plain">
            {{ meta(t.status).label }}
          </el-tag>
        </div>
        <div class="mt-2 flex items-center justify-between text-xs text-gray-500">
          <span>{{ time(t.created_at) }}</span>
          <span>
            {{ t.processed_images }}/{{ t.total_images }} 张
            <span v-if="t.defective_images" class="ml-1 text-red-600">
              · {{ t.defective_images }} 缺陷
            </span>
          </span>
        </div>
        <div v-if="t.status === 'COMPLETED' && t.has_result" class="mt-2 text-right">
          <el-button
            size="small"
            link
            type="primary"
            :icon="Download"
            @click.stop="downloadResult(t.id)"
          >
            下载结果
          </el-button>
        </div>
        <p v-else-if="t.error_message" class="mt-2 line-clamp-2 text-xs text-red-500">
          {{ t.error_message }}
        </p>
      </div>
    </div>
  </el-card>
</template>
