<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { QueryResult } from '../types'
import { chartColumns, columnLabel, formatCell, hasChart, numericValue } from '../format'

echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])
const props = defineProps<{ result: QueryResult }>()
const host = ref<HTMLDivElement>()
let chart: echarts.ECharts | undefined
let observer: ResizeObserver | undefined
const mode = ref<'bar' | 'line'>(props.result.chart?.type === 'line' ? 'line' : 'bar')
const categoryKey = computed(() => String(props.result.chart?.x || ''))
const numericKeys = computed(() => chartColumns(props.result))
const available = computed(() => hasChart(props.result))

function render() {
  if (!host.value || !available.value) { chart?.dispose(); chart = undefined; return }
  chart ??= echarts.init(host.value)
  const rows = props.result.rows.slice(0, 30)
  const firstColumn = numericKeys.value[0]!
  chart.setOption({
    animationDuration: 250,
    color: ['#3974ed', '#24a79c', '#a17ae1'],
    grid: { top: 48, right: 30, bottom: 64, left: 80, containLabel: true },
    tooltip: {
      trigger: 'axis', renderMode: 'richText', backgroundColor: '#fff', borderColor: '#e5ebf5', textStyle: { color: '#263852' },
      formatter: (params: unknown) => {
        const items = params as Array<{ dataIndex: number; seriesIndex: number; axisValueLabel: string }>
        return [items[0]?.axisValueLabel || '', ...items.map((item) => {
          const column = numericKeys.value[item.seriesIndex]!
          return `${columnLabel(column)}：${formatCell(rows[item.dataIndex]?.[column.name], column)}`
        })].join('\n')
      },
    },
    legend: { top: 1, left: 16, icon: 'roundRect', itemWidth: 10, itemHeight: 5, textStyle: { fontSize: 11, color: '#697990' } },
    xAxis: { type: 'category', data: rows.map((row) => String(row[categoryKey.value] ?? '未知')), axisLine: { lineStyle: { color: '#e6ebf3' } }, axisTick: { show: false }, axisLabel: { color: '#7d8ba0', fontSize: 10, interval: 0, overflow: 'truncate', width: 95, rotate: rows.length > 10 ? 25 : 0 } },
    yAxis: { type: 'value', name: firstColumn.type === 'percentage' ? '%' : firstColumn.unit || '', nameTextStyle: { color: '#7d8ba0' }, splitNumber: 4, axisLabel: { color: '#7d8ba0', fontSize: 10, formatter: (value: number) => Math.abs(value) >= 10000 && firstColumn.type !== 'percentage' ? `${(value / 10000).toFixed(1)}万` : value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }, splitLine: { lineStyle: { color: '#edf1f7', type: 'dashed' } } },
    series: numericKeys.value.map((column) => ({ name: column.label, type: mode.value, data: rows.map((row) => { const value = numericValue(row[column.name]); return value === null ? null : column.type === 'percentage' ? value * 100 : value }), barMaxWidth: 30, smooth: false, symbolSize: 6, itemStyle: { borderRadius: mode.value === 'bar' ? [4, 4, 0, 0] : 0 } })),
  }, true)
}
function observe() { if (host.value) { observer?.disconnect(); observer = new ResizeObserver(() => chart?.resize()); observer.observe(host.value) } }
onMounted(async () => { await nextTick(); render(); observe() })
watch(() => props.result.id, () => { mode.value = props.result.chart?.type === 'line' ? 'line' : 'bar' })
watch(() => [props.result, mode.value], async () => { await nextTick(); render(); observe() }, { deep: true })
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>

<template>
  <div v-if="available" class="result-chart">
    <div class="chart-caption"><span>{{ result.chart?.title || '数据对比' }} <small v-if="result.rows.length > 30">图表显示当前页前 30 行</small></span><div class="chart-switch"><button :class="{ active: mode === 'bar' }" @click="mode = 'bar'">柱状</button><button :class="{ active: mode === 'line' }" @click="mode = 'line'">趋势</button></div></div>
    <div ref="host" class="chart-canvas" role="img" :aria-label="`${result.title || '查询结果'}图表`"></div>
  </div>
</template>
