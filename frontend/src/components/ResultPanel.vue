<script setup lang="ts">
import { ref } from 'vue'
import { DataAnalysis, Document, Download, Grid } from '@element-plus/icons-vue'
import type { QueryResult } from '../types'
import { columnLabel, formatCell, hasChart, isNumericColumn } from '../format'
import ResultChart from './ResultChart.vue'

const props = defineProps<{ result: QueryResult; loading?: boolean }>()
const emit = defineEmits<{ evidence: [result: QueryResult]; page: [page: number] }>()
const tab = ref('table')
function download() {
  const escape = (value: unknown) => `"${String(value ?? '').replaceAll('"', '""')}"`
  const content = '\uFEFF' + [props.result.columns.map((c) => escape(columnLabel(c))).join(','), ...props.result.rows.map((row) => props.result.columns.map((c) => escape(row[c.name])).join(','))].join('\r\n')
  const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([content], { type: 'text/csv;charset=utf-8' })); link.download = `经营分析-${props.result.id || 'result'}-当前页.csv`; link.click(); URL.revokeObjectURL(link.href)
}
</script>

<template>
  <section class="result-panel">
    <div class="result-toolbar">
      <div class="result-tabs"><button :class="{ selected: tab === 'table' }" @click="tab = 'table'"><el-icon><Grid /></el-icon>数据表</button><button :class="{ selected: tab === 'chart' }" @click="tab = 'chart'"><el-icon><DataAnalysis /></el-icon>图表</button></div>
      <div class="result-actions"><button title="下载当前页 CSV" @click="download"><el-icon><Download /></el-icon><span>导出</span></button><button @click="emit('evidence', result)"><el-icon><Document /></el-icon>查询依据</button></div>
    </div>
    <div v-if="tab === 'table'" v-loading="loading" class="table-wrap">
      <el-table :data="result.rows" size="small" stripe max-height="330" empty-text="当前范围没有匹配数据" :header-cell-style="{ background: '#f7f9fc', color: '#67768d', fontWeight: 500 }">
        <el-table-column v-for="column in result.columns" :key="column.name" :prop="column.name" :label="columnLabel(column)" min-width="125" show-overflow-tooltip :align="isNumericColumn(column, result.rows) ? 'right' : 'left'"><template #default="scope">{{ formatCell(scope.row[column.name], column) }}</template></el-table-column>
      </el-table>
      <div class="table-footer"><span>共 {{ result.total.toLocaleString() }} 行<span v-if="result.total > result.rows.length"> · 当前 {{ result.rows.length }} 行</span></span><el-pagination v-if="result.total > result.page_size" small layout="prev, pager, next" :total="result.total" :page-size="result.page_size" :current-page="result.page" @current-change="emit('page', $event)" /></div>
    </div>
    <template v-else><ResultChart :result="result" /><div v-if="!hasChart(result)" class="chart-empty">当前结果适合使用数据表查看。</div></template>
    <div v-if="result.warnings.length" class="result-notes"><p v-for="warning in result.warnings" :key="warning">{{ warning }}</p></div>
  </section>
</template>
