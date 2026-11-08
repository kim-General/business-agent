<script setup lang="ts">
import { computed, ref } from 'vue'
import { CopyDocument, CircleCheck } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { QueryResult } from '../types'

const props = defineProps<{ modelValue: boolean; result?: QueryResult }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const active = ref('scope')
const evidence = computed(() => props.result?.evidence || {})
const readable = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const fields = computed(() => Object.entries(evidence.value).filter(([key, value]) => !['sql', 'parameters'].includes(key) && value !== null && value !== undefined))
const fieldNames: Record<string, string> = { dataset_id: '数据集', dataset_hash: '数据 SHA-256', metrics_version: '指标版本', metric_definition: '指标口径', scope: '查询范围', checks: '校验结果', warnings: '限制与说明', time_range: '时间范围', filters: '筛选条件', metric: '指标', tables: '涉及数据表', plan: '分析计划', data_version: '数据版本', query_id: '查询编号', validation: '执行校验' }
async function copy() { try { await navigator.clipboard.writeText(String(evidence.value.sql || '')); ElMessage.success('SQL 已复制') } catch { ElMessage.warning('无法复制，请手动选择文本') } }
</script>

<template>
  <el-drawer :model-value="modelValue" title="查询依据" size="560px" class="evidence-drawer" @update:model-value="emit('update:modelValue', $event)">
    <div class="evidence-intro"><div class="verified-label"><el-icon><CircleCheck /></el-icon>可核对的分析记录</div><p>查询编号 <code>{{ result?.run_id || result?.id || '—' }}</code></p></div>
    <el-tabs v-model="active"><el-tab-pane label="范围与口径" name="scope"><div v-for="[key, value] in fields" :key="key" class="evidence-field"><h4>{{ fieldNames[key] || key }}</h4><pre>{{ readable(value) }}</pre></div><el-empty v-if="!fields.length" description="暂无额外依据" :image-size="70" /></el-tab-pane><el-tab-pane label="执行 SQL" name="sql"><div class="code-heading"><span>只读查询</span><el-button text size="small" :icon="CopyDocument" @click="copy">复制</el-button></div><pre class="sql-code">{{ evidence.sql || '本次运行未返回 SQL。' }}</pre><template v-if="evidence.parameters"><h4 class="evidence-subtitle">绑定参数</h4><pre class="sql-code">{{ readable(evidence.parameters) }}</pre></template></el-tab-pane></el-tabs>
  </el-drawer>
</template>
