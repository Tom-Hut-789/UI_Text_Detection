<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { HealthInfo, TaskInfo } from '@/api/types'
import { useTaskStream } from '@/composables/useTaskStream'
import UploadPanel from '@/components/UploadPanel.vue'
import TaskProgress from '@/components/TaskProgress.vue'
import ResultPanel from '@/components/ResultPanel.vue'
import HistoryPanel from '@/components/HistoryPanel.vue'

const taskId = ref('')
const health = ref<HealthInfo | null>(null)
const history = ref<TaskInfo[]>([])
const historyLoading = ref(false)

const {
  status: streamStatus,
  task,
  started,
  progress,
  findings,
  activeCells,
  errorMessage,
  busy,
  open: openStream,
} = useTaskStream()

const currentStatus = computed(() => task.value?.status ?? progress.value.status)
const showProgress = computed(() => Boolean(taskId.value))
/** 任务已结束（无论成功失败）才拉取结果面板，避免无谓请求 */
const showResult = computed(() =>
  ['COMPLETED', 'INTERRUPTED', 'FAILED'].includes(currentStatus.value ?? ''),
)

async function loadHealth() {
  try {
    health.value = await api.health()
  } catch {
    health.value = null
  }
}

async function loadHistory() {
  historyLoading.value = true
  try {
    history.value = await api.listTasks()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '加载历史任务失败')
  } finally {
    historyLoading.value = false
  }
}

function attach(id: string) {
  taskId.value = id
  openStream(id)
}

function onUploaded(id: string) {
  attach(id)
  loadHistory()
}

function onSelectHistory(id: string) {
  if (id === taskId.value) return
  attach(id)
}

function onResumed() {
  openStream(taskId.value) // 重新订阅，后端会从当前进度继续推送
  loadHistory()
}

onMounted(() => {
  loadHealth()
  loadHistory()
})
</script>

<template>
  <div class="min-h-full bg-[var(--page-bg)]">
    <header class="border-b border-gray-200 bg-white">
      <div class="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-3 px-5 py-4">
        <div class="flex items-center gap-3">
          <div class="grid h-10 w-10 place-items-center rounded-lg bg-brand-600 text-lg font-bold text-white">
            UI
          </div>
          <div>
            <h1 class="m-0 text-lg font-semibold leading-tight">多语种UI文本格式检测系统</h1>
            <p class="m-0 text-xs text-gray-500">
              基于多模态大模型的截断 / 重叠 / 缺字自动检测
            </p>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <el-tag v-if="health" size="small" effect="plain">模型：{{ health.model }}</el-tag>
          <el-tag v-if="health" size="small" effect="plain">并发：{{ health.concurrency }}</el-tag>
          <el-tag v-if="health?.mock_llm" size="small" type="warning">MOCK 模式</el-tag>
          <el-tag v-else-if="health && !health.llm_configured" size="small" type="danger">
            未配置 API Key
          </el-tag>
        </div>
      </div>
    </header>

    <main class="mx-auto grid max-w-[1400px] grid-cols-1 gap-5 px-5 py-6 lg:grid-cols-3">
      <section class="flex flex-col gap-5 lg:col-span-2">
        <UploadPanel :busy="busy" @uploaded="onUploaded" />

        <TaskProgress
          v-if="showProgress"
          :task="task"
          :started="started"
          :progress="progress"
          :findings="findings"
          :active-cells="activeCells"
          :stream-status="streamStatus"
          :error-message="errorMessage"
        />

        <ResultPanel
          v-if="showResult && taskId"
          :task-id="taskId"
          :status="currentStatus"
          @resumed="onResumed"
        />

        <el-card v-if="!showProgress" shadow="never" class="!rounded-xl">
          <el-empty description="上传 Excel 后，这里会实时显示每一张截图的检测进度">
            <template #description>
              <div class="text-sm text-gray-500">
                <p class="mb-2">上传 Excel 后，这里会实时显示逐张截图的检测进度与缺陷结论。</p>
                <p class="m-0 text-xs text-gray-400">
                  检测维度：截断（文本被容器切断 / 尾部省略号）、重叠（文本互相压字）、缺字（豆腐块）
                </p>
              </div>
            </template>
          </el-empty>
        </el-card>
      </section>

      <aside class="flex flex-col gap-5">
        <HistoryPanel
          :tasks="history"
          :active-id="taskId"
          :loading="historyLoading"
          @select="onSelectHistory"
          @refresh="loadHistory"
        />

        <el-card shadow="never" class="!rounded-xl">
          <template #header><span class="font-semibold">检测口径</span></template>
          <ul class="m-0 list-none space-y-3 p-0 text-sm text-gray-600">
            <li>
              <el-tag size="small" type="danger">截断</el-tag>
              <span class="ml-2">文本未显示完整：尾部省略号、字符被容器边界切断、语义不完整。</span>
            </li>
            <li>
              <el-tag size="small" type="danger">重叠</el-tag>
              <span class="ml-2">文本与相邻文字、图标或控件像素互相覆盖。</span>
            </li>
            <li>
              <el-tag size="small" type="danger">缺字</el-tag>
              <span class="ml-2">出现豆腐块 □、空心方框或乱码问号，通常是小语种字体缺失。</span>
            </li>
          </ul>
          <p class="mb-0 mt-4 text-xs text-gray-400">
            英文列作为基准先做 OCR，其文本会作为小语种截断判定的对照依据。
          </p>
        </el-card>
      </aside>
    </main>
  </div>
</template>
