<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CircleCheck, Refresh, Warning, DocumentChecked } from '@element-plus/icons-vue'
import { fetchQuality } from '../api'
import type { Dataset, QualityReport } from '../types'

const props = defineProps<{ dataset?: Dataset }>()
const report = ref<QualityReport>()
const loading = ref(false)
const error = ref('')
let version = 0
async function load() {
  if (!props.dataset) return
  const current = ++version; loading.value = true; error.value = ''; report.value = undefined
  try { const body = await fetchQuality(props.dataset.id); if (current === version) report.value = body } catch (cause) { if (current === version) error.value = cause instanceof Error ? cause.message : String(cause) } finally { if (current === version) loading.value = false }
}
watch(() => props.dataset?.id, load, { immediate: true })
const passed = computed(() => report.value?.checks.filter((c) => ['pass', 'passed', 'ok', 'success'].includes(c.status.toLowerCase())).length || 0)
const warnings = computed(() => report.value?.checks.filter((c) => ['warning', 'warn', 'failed', 'fail', 'error'].includes(c.status.toLowerCase())).length || 0)
const statusType = (status: string) => ['pass', 'passed', 'ok', 'success'].includes(status.toLowerCase()) ? 'success' : ['warning', 'warn'].includes(status.toLowerCase()) ? 'warning' : ['fail', 'failed', 'error'].includes(status.toLowerCase()) ? 'danger' : 'info'
const statusLabel = (status: string) => ({ pass: '通过', passed: '通过', ok: '通过', success: '通过', warning: '需关注', warn: '需关注', failed: '未通过', fail: '未通过', error: '异常', info: '已说明' })[status.toLowerCase()] || status
</script>

<template>
  <section class="page-view quality-page">
    <div class="page-heading"><div><div class="eyebrow">DATA QUALITY</div><h1>数据质量</h1><p>检查关联完整性、状态性缺失与订单金额对账。</p></div><el-button :icon="Refresh" :loading="loading" @click="load">重新检查</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="page-alert" />
    <div class="quality-summary"><div class="quality-stat"><div class="stat-icon blue"><el-icon><DocumentChecked /></el-icon></div><div><span>检查项目</span><strong>{{ report?.checks.length ?? '—' }}</strong></div></div><div class="quality-stat"><div class="stat-icon green"><el-icon><CircleCheck /></el-icon></div><div><span>检查通过</span><strong>{{ report ? passed : '—' }}</strong></div></div><div class="quality-stat"><div class="stat-icon amber"><el-icon><Warning /></el-icon></div><div><span>需要关注</span><strong>{{ report ? warnings : '—' }}</strong></div></div></div>
    <div v-loading="loading" class="quality-checks surface"><div class="section-heading"><h3>检查明细</h3><span>{{ dataset?.name }}</span></div><el-table :data="report?.checks || []" size="default" empty-text="尚未返回检查结果" :header-cell-style="{ background: '#f7f9fc', color: '#718096', fontWeight: 500 }"><el-table-column prop="name" label="检查项" min-width="180" /><el-table-column label="状态" width="100"><template #default="scope"><el-tag :type="statusType(scope.row.status)" size="small" effect="light">{{ statusLabel(scope.row.status) }}</el-tag></template></el-table-column><el-table-column prop="count" label="记录数" width="100" align="right"><template #default="scope">{{ scope.row.count == null ? '—' : Number(scope.row.count).toLocaleString() }}</template></el-table-column><el-table-column prop="message" label="说明" min-width="330" /></el-table></div>
    <div class="quality-footnote"><el-icon><CircleCheck /></el-icon><p>取消与未送达订单的日期缺失需要按状态解释；无评价表示未覆盖，不代表零分或好评。</p></div>
  </section>
</template>
