<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, RefreshRight, Warning } from '@element-plus/icons-vue'
import { api } from '@/api'
import type { LanguageStat, ResultSummary, TaskUiStatus } from '@/api/types'

// 用 TaskUiStatus 而非 TaskStatus：App.vue 在 SSE 的 state 事件到达之前会回退到
// progress.status，此时可能是前端本地的 IDLE。本组件本就只对终态做处理，
// 收到 IDLE 时按「什么都不加载」处理即可。
const props = defineProps<{ taskId: string; status: TaskUiStatus | undefined }>()
const emit = defineEmits<{ (e: 'resumed'): void }>()

const loading = ref(false)
const resuming = ref(false)
const summary = ref<ResultSummary | null>(null)
const onlyDefective = ref(true)

async function load() {
  if (!props.taskId) return
  loading.value = true
  try {
    summary.value = await api.getResult(props.taskId)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '加载结果失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.taskId, props.status],
  ([id, status]) => {
    if (id && (status === 'COMPLETED' || status === 'INTERRUPTED' || status === 'FAILED')) load()
  },
  { immediate: true },
)

const languages = computed<LanguageStat[]>(() => {
  const list = summary.value?.languages ?? []
  return onlyDefective.value ? list.filter((l) => l.defective > 0 || l.failed > 0) : list
})

const canResume = computed(
  () => props.status === 'INTERRUPTED' || props.status === 'FAILED',
)

function download() {
  window.open(api.downloadUrl(props.taskId), '_blank')
}

async function resume() {
  resuming.value = true
  try {
    await api.resume(props.taskId)
    ElMessage.success('已提交续跑，将跳过已完成的截图')
    emit('resumed')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '续跑失败')
  } finally {
    resuming.value = false
  }
}

function defectRate(row: LanguageStat): number {
  if (!row.checked) return 0
  return Math.round((row.defective / row.checked) * 100)
}
</script>

<template>
  <el-card v-loading="loading" shadow="never" class="!rounded-xl">
    <template #header>
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-3">
          <span class="font-semibold">检测结果</span>
          <el-tag v-if="summary" size="small" effect="plain">
            {{ summary.summary.languages_checked }} 个语种 ·
            {{ summary.summary.images_checked }} 张截图
          </el-tag>
        </div>
        <div class="flex items-center gap-2">
          <el-button v-if="canResume" :icon="RefreshRight" :loading="resuming" @click="resume">
            断点续跑
          </el-button>
          <el-button
            type="primary"
            :icon="Download"
            :disabled="status !== 'COMPLETED'"
            @click="download"
          >
            下载检测结果
          </el-button>
        </div>
      </div>
    </template>

    <template v-if="summary">
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div class="rounded-lg border border-red-100 bg-red-50/60 p-3">
          <div class="text-xs text-gray-500">检出缺陷</div>
          <div class="mt-1 text-2xl font-semibold text-red-600">
            {{ summary.summary.defective_images }}
          </div>
        </div>
        <div class="rounded-lg bg-slate-50 p-3">
          <div class="text-xs text-gray-500">已检测截图</div>
          <div class="mt-1 text-2xl font-semibold">{{ summary.summary.images_checked }}</div>
        </div>
        <div class="rounded-lg bg-slate-50 p-3">
          <div class="text-xs text-gray-500">涉及语种</div>
          <div class="mt-1 text-2xl font-semibold">{{ summary.summary.languages_checked }}</div>
        </div>
        <div class="rounded-lg p-3" :class="summary.summary.failed_images ? 'bg-amber-50' : 'bg-slate-50'">
          <div class="text-xs text-gray-500">检测失败</div>
          <div
            class="mt-1 text-2xl font-semibold"
            :class="summary.summary.failed_images ? 'text-amber-600' : ''"
          >
            {{ summary.summary.failed_images }}
          </div>
        </div>
      </div>

      <el-alert
        v-if="summary.summary.defective_images === 0 && summary.summary.failed_images === 0"
        class="mt-4"
        type="success"
        :closable="false"
        show-icon
        title="未检出任何截断 / 重叠 / 缺字缺陷"
      />

      <div class="mt-5 flex items-center justify-between">
        <span class="text-sm font-medium text-gray-600">按语种统计</span>
        <el-switch v-model="onlyDefective" active-text="仅看有缺陷语种" />
      </div>

      <el-table
        :data="languages"
        size="small"
        class="mt-2 thin-scroll"
        max-height="460"
        stripe
        :default-sort="{ prop: 'defective', order: 'descending' }"
      >
        <el-table-column label="语种" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="row.is_english ? 'font-semibold text-brand-600' : ''">
              {{ row.language }}
            </span>
            <el-tag v-if="row.is_english" class="ml-2" size="small" type="info" effect="plain">
              基准
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="checked" label="已检测" width="80" align="center" sortable />
        <el-table-column prop="truncation" label="截断" width="72" align="center" sortable>
          <template #default="{ row }">
            <span :class="row.truncation ? 'font-semibold text-red-600' : 'text-gray-400'">
              {{ row.truncation }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="overlap" label="重叠" width="72" align="center" sortable>
          <template #default="{ row }">
            <span :class="row.overlap ? 'font-semibold text-red-600' : 'text-gray-400'">
              {{ row.overlap }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="missing_glyph" label="缺字" width="72" align="center" sortable>
          <template #default="{ row }">
            <span :class="row.missing_glyph ? 'font-semibold text-red-600' : 'text-gray-400'">
              {{ row.missing_glyph }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="defective" label="缺陷合计" width="96" align="center" sortable>
          <template #default="{ row }">
            <el-tag v-if="row.defective" size="small" type="danger">{{ row.defective }}</el-tag>
            <span v-else class="text-gray-400">0</span>
          </template>
        </el-table-column>
        <el-table-column label="缺陷率" width="130" sortable prop="defective">
          <template #default="{ row }">
            <el-progress
              :percentage="defectRate(row)"
              :stroke-width="8"
              :show-text="false"
              :color="defectRate(row) > 0 ? '#dc2626' : '#16a34a'"
            />
            <span class="text-xs text-gray-500">{{ defectRate(row) }}%</span>
          </template>
        </el-table-column>
        <el-table-column label="失败" width="72" align="center">
          <template #default="{ row }">
            <el-icon v-if="row.failed" class="text-amber-500"><Warning /></el-icon>
            <span v-else class="text-gray-300">—</span>
          </template>
        </el-table-column>
      </el-table>

      <p v-if="!languages.length" class="mt-3 text-sm text-gray-400">没有符合筛选条件的语种。</p>
    </template>

    <el-empty v-else-if="!loading" description="暂无检测结果" :image-size="80" />
  </el-card>
</template>
