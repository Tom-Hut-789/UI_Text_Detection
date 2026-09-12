<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage, type UploadFile } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { api } from '@/api'

const props = defineProps<{ busy: boolean }>()
const emit = defineEmits<{ (e: 'uploaded', taskId: string): void }>()

const uploading = ref(false)
const percent = ref(0)
const currentFile = ref('')

const disabled = computed(() => props.busy || uploading.value)
const MAX_MB = 200

function validate(file: File): boolean {
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) {
    ElMessage.error('仅支持 .xlsx / .xlsm 格式的 Excel 文件')
    return false
  }
  if (file.size > MAX_MB * 1024 * 1024) {
    ElMessage.error(`文件超过 ${MAX_MB}MB 上限`)
    return false
  }
  return true
}

async function handleChange(uploadFile: UploadFile) {
  const raw = uploadFile.raw
  if (!raw || !validate(raw)) return

  uploading.value = true
  percent.value = 0
  currentFile.value = raw.name
  try {
    const { task_id } = await api.upload(raw, (p) => {
      // 上传完成后还有服务端落盘时间，压到 95% 留出余量
      percent.value = Math.min(95, p)
    })
    percent.value = 100
    ElMessage.success('上传成功，已开始解析并检测')
    emit('uploaded', task_id)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '上传失败')
  } finally {
    uploading.value = false
    currentFile.value = ''
  }
}
</script>

<template>
  <el-card shadow="never" class="!rounded-xl">
    <template #header>
      <div class="flex items-center justify-between">
        <span class="font-semibold">上传检测文件</span>
        <el-tag size="small" type="info" effect="plain">
          需含「英语(英语/English)」语种列的多语种截图表
        </el-tag>
      </div>
    </template>

    <el-upload
      drag
      :auto-upload="false"
      :show-file-list="false"
      :disabled="disabled"
      accept=".xlsx,.xlsm"
      :on-change="handleChange"
    >
      <el-icon class="el-icon--upload text-brand-500"><UploadFilled /></el-icon>
      <div class="el-upload__text">
        将 Excel 拖到此处，或<em>点击选择文件</em>
      </div>
      <template #tip>
        <div class="el-upload__tip">
          支持 .xlsx / .xlsm，单文件不超过 {{ MAX_MB }}MB。后端会逐行抽取内嵌截图并对每种语言调用多模态大模型。
        </div>
      </template>
    </el-upload>

    <el-progress
      v-if="uploading"
      class="mt-4"
      :percentage="percent"
      :stroke-width="14"
      striped
      striped-flow
    />
    <p v-if="uploading" class="m-0 mt-2 text-sm text-gray-500">正在上传 {{ currentFile }}…</p>
  </el-card>
</template>
