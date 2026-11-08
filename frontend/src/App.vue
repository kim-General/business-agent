<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ArrowDown, ArrowRight, ChatDotRound, CircleCheck, Clock, Close, Connection, DataAnalysis, DataBoard, DataLine, Document, FolderOpened, Histogram, House, InfoFilled, Loading, Plus, Position, RefreshRight, Search, Setting, CircleCheck as Shield, Stopwatch as StopWatch, Tickets, TrendCharts } from '@element-plus/icons-vue'
import { useWorkspaceStore } from './stores/workspace'
import type { JsonRecord, Run } from './types'
import { record } from './api'
import EvidenceDrawer from './components/EvidenceDrawer.vue'
import ResultPanel from './components/ResultPanel.vue'
import QualityPage from './components/QualityPage.vue'

const store = useWorkspaceStore()
const { datasets, datasetId, sessions, session, runs, view, booting, loadingSession, submitting, error, streamWarning, health, catalog, selectedDataset, activeRun, busy, evidenceResult, evidenceOpen } = storeToRefs(store)
const draft = ref('')
const clarificationText = ref('')
const messagesHost = ref<HTMLElement>()
const searchText = ref('')
const contextExpanded = ref(false)
const infoOpen = ref(false)
const metrics: Record<string, string> = { goods_sales: '商品销售额', avg_monthly_goods_sales: '月均商品销售额', avg_order_goods_sales: '客单价', order_count: '订单数', payment_amount: '支付金额', freight_amount: '运费', late_rate: '延迟率', avg_review_score: '平均评分', low_review_rate: '低分评价率', repeat_customers: '复购客户数', quality: '数据质量', relationship_explanation: '数据关联说明' }
const dimensions: Record<string, string> = { category: '品类', seller: '商家', customer_state: '地区', payment_type: '支付方式', order_status: '订单状态', month: '月份' }
const phases: Record<string, string> = { queued: '等待处理', understand: '理解问题', understanding: '理解问题', plan: '生成计划', planning: '生成计划', semantic_check: '核对口径', sql_guard: '检查查询', compile: '编译查询', dry_run: '预检查询', execute: '查询数据', executing: '查询数据', verify: '核验结果', persist: '保存记录', answer: '整理回答', completed: '分析完成' }
const examples = [
  { icon: TrendCharts, label: '销售增长分析', question: '2025年4月已送达订单的商品销售额比3月变化多少？哪个品类贡献最大？能确定是促销带来的吗？', detail: '期间比较 · 品类贡献' },
  { icon: Histogram, label: '品类销售排名', question: '2025年4月各品类商品销售额排名', detail: '销售额 · Top 品类' },
  { icon: StopWatch, label: '商家履约表现', question: '2025年5月至少10笔已送达订单的商家中，哪个商家延迟率最高？', detail: '延迟率 · 样本约束' },
  { icon: ChatDotRound, label: '物流与用户评价', question: '已送达订单中，延迟和准时订单的平均评分分别是多少？', detail: '关联分析 · 评价覆盖' },
]
const csvExamples = [
  { icon: TrendCharts, label: '销售期间比较', question: '2025年4月商品销售额比3月变化多少？', detail: '期间比较 · 商品销售额' },
  { icon: Histogram, label: '品类销售排名', question: '2025年4月各品类商品销售额排名', detail: '销售额 · 品类排名' },
  { icon: StopWatch, label: '订单延迟分析', question: '2025年5月各商家的延迟率排名', detail: '履约表现 · 商家' },
  { icon: ChatDotRound, label: '物流与用户评价', question: '已送达订单中，延迟和准时订单的平均评分分别是多少？', detail: '关联分析 · 评价覆盖' },
]
const visibleExamples = computed(() => datasetId.value === 'csv-demo' ? csvExamples : examples)
const filteredSessions = computed(() => sessions.value.filter((item) => item.title.toLowerCase().includes(searchText.value.toLowerCase())))
const dateRange = computed(() => selectedDataset.value?.date_start ? `${selectedDataset.value.date_start} — ${selectedDataset.value.date_end}` : '等待数据接入')
const modeLabel = computed(() => health.value.mode === 'llm' ? '模型分析' : health.value.mode === 'rules' ? '规则模式' : '未连接')
const phaseProgress = (run: Run) => ['execute', 'executing', 'verify', 'persist', 'answer', 'completed'].includes(run.stage || '') ? ['verify', 'persist', 'answer', 'completed'].includes(run.stage || '') ? 3 : 2 : ['plan', 'planning', 'semantic_check', 'sql_guard', 'compile', 'dry_run'].includes(run.stage || '') ? 1 : 0
const contextChips = computed(() => {
  const context = session.value?.context || {}
  const chips: string[] = []
  if (context.metric) chips.push(metrics[String(context.metric)] || String(context.metric))
  if (Array.isArray(context.periods)) context.periods.forEach((period) => { const p = record(period); chips.push(String(p.label || `${p.start} ~ ${p.end}`)) })
  const filters = record(context.filters)
  const categories = Array.isArray(catalog.value.categories) ? catalog.value.categories.map(record) : []
  const sellers = Array.isArray(catalog.value.sellers) ? catalog.value.sellers.map(record) : []
  if (Array.isArray(filters.category_ids)) filters.category_ids.forEach((id) => chips.push(String(categories.find((category) => category.id === id)?.name || id)))
  if (Array.isArray(filters.seller_ids)) filters.seller_ids.forEach((id) => chips.push(String(sellers.find((seller) => seller.id === id)?.name || id)))
  if (context.dimension) chips.push(`按${dimensions[String(context.dimension)] || context.dimension}`)
  if (context.date_field) chips.push(context.date_field === 'delivered_at' ? '送达时间' : '下单时间')
  return chips
})
const phaseLabel = (run: Run) => phases[run.stage || ''] || [...run.events].reverse().find((event) => event.type === 'phase_changed')?.message || '正在分析'
const statusLabel = (run: Run) => ({ queued: '排队中', running: '分析中', awaiting_clarification: '等待补充', completed: '已完成', failed: '未完成', cancelled: '已取消' })[run.status]
const displayDate = (value?: string) => { if (!value) return '—'; const date = new Date(value); return Number.isNaN(date.getTime()) ? value.slice(0, 16) : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }
function send() { const message = draft.value.trim(); if (!message || busy.value) return; draft.value = ''; void store.submit(message) }
function useExample(question: string) { if (busy.value) return; draft.value = question; send() }
function handleKeys(event: KeyboardEvent) { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send() } }
async function scrollToBottom() { await nextTick(); messagesHost.value?.scrollTo({ top: messagesHost.value.scrollHeight, behavior: 'smooth' }) }
function contextJson(context: JsonRecord) { return JSON.stringify(context, null, 2) }
watch(() => [runs.value.length, runs.value.map((run) => `${run.status}:${run.events.length}:${run.result?.id || ''}`).join('|')], scrollToBottom)
onMounted(store.bootstrap)
onBeforeUnmount(store.closeStream)
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#" @click.prevent="view = 'workspace'"><div class="brand-mark"><i></i><i></i><i></i></div><div><strong>观数</strong><span>经营分析工作台</span></div></a>
      <button class="new-session" :disabled="submitting || booting" @click="store.createSession"><el-icon><Plus /></el-icon>新建分析<span>＋</span></button>
      <nav class="main-nav" aria-label="主要导航"><button :class="{ active: view === 'workspace' }" @click="view = 'workspace'"><el-icon><ChatDotRound /></el-icon><span>对话分析</span><small>AI</small></button><button :class="{ active: view === 'history' }" @click="view = 'history'"><el-icon><Clock /></el-icon><span>历史记录</span></button><button :class="{ active: view === 'quality' }" @click="view = 'quality'"><el-icon><Document /></el-icon><span>数据质量</span></button></nav>
      <div class="session-heading"><span>最近会话</span><span>{{ sessions.length }}</span></div>
      <div class="sidebar-sessions"><button v-for="item in sessions.slice(0, 12)" :key="item.id" :class="{ active: session?.id === item.id && view === 'workspace' }" :title="item.title" @click="store.selectSession(item.id)"><el-icon><Tickets /></el-icon><span>{{ item.title || '新分析' }}</span><el-icon class="session-arrow"><ArrowRight /></el-icon></button><div v-if="!sessions.length" class="no-sessions">开始一次对话，分析记录将保存在这里。</div></div>
      <div class="sidebar-footer"><div class="environment-tag"><i></i><span>本地分析环境</span><el-icon><Shield /></el-icon></div><button class="sidebar-settings" @click="infoOpen = true"><div class="profile-avatar">观</div><div><strong>电商经营数据</strong><span>只读查询 · 可复现</span></div><el-icon><Setting /></el-icon></button></div>
    </aside>

    <main class="main-shell">
      <header class="topbar"><div class="breadcrumb"><el-icon><House /></el-icon><span>工作空间</span><span class="slash">/</span><strong>{{ view === 'workspace' ? '对话分析' : view === 'quality' ? '数据质量' : '历史记录' }}</strong></div><div class="topbar-right"><div class="dataset-select"><el-icon><FolderOpened /></el-icon><el-select :model-value="datasetId" size="small" :disabled="booting" aria-label="切换数据集" @update:model-value="store.changeDataset(String($event))"><el-option v-for="dataset in datasets" :key="dataset.id" :label="dataset.name" :value="dataset.id" /></el-select></div><div class="mode-badge" :class="{ llm: health.mode === 'llm' }"><span></span>{{ modeLabel }}</div><button class="topbar-info" title="关于当前环境" @click="infoOpen = true"><el-icon><InfoFilled /></el-icon></button></div></header>

      <div v-if="booting" class="boot-state"><el-icon class="is-loading"><Loading /></el-icon><h3>正在连接数据工作空间</h3><p>读取数据集、业务口径与分析记录</p></div>
      <template v-else>
        <div v-if="error" class="global-error"><el-icon><InfoFilled /></el-icon><span>{{ error }}</span><el-button v-if="!datasets.length" text size="small" @click="store.bootstrap">重试连接</el-button><button title="关闭提示" @click="error = ''"><el-icon><Close /></el-icon></button></div>
        <QualityPage v-if="view === 'quality'" :dataset="selectedDataset" />

        <section v-else-if="view === 'history'" class="page-view history-page"><div class="page-heading"><div><div class="eyebrow">ANALYSIS HISTORY</div><h1>历史记录</h1><p>查看已保存的对话、查询依据与原计划回放。</p></div><el-input v-model="searchText" :prefix-icon="Search" placeholder="搜索会话" clearable class="history-search" /></div><div class="surface history-surface"><div class="section-heading"><h3>分析会话 <span>{{ sessions.length }}</span></h3><span>{{ selectedDataset?.name }}</span></div><el-table :data="filteredSessions" :header-cell-style="{ background: '#f7f9fc', color: '#718096', fontWeight: 500 }" empty-text="还没有保存的分析会话" @row-click="(row: { id: string }) => store.selectSession(row.id)"><el-table-column prop="title" label="会话名称" min-width="340"><template #default="scope"><div class="history-title"><el-icon><ChatDotRound /></el-icon><span>{{ scope.row.title || '新分析' }}</span></div></template></el-table-column><el-table-column label="最近更新" width="180"><template #default="scope">{{ displayDate(scope.row.updated_at) }}</template></el-table-column><el-table-column label="上下文版本" width="130"><template #default="scope">v{{ scope.row.context_version }}</template></el-table-column><el-table-column width="110"><template #default="scope"><el-button link type="primary" @click.stop="store.selectSession(scope.row.id)">查看分析 <el-icon><ArrowRight /></el-icon></el-button></template></el-table-column></el-table></div><p class="history-note">进入会话后，可查看每次查询的证据，并按保存的计划与数据版本重新执行。</p></section>

        <section v-else class="workspace-view">
          <div class="workspace-title"><div><h1>{{ session?.title && runs.length ? session.title : '对话分析' }}</h1><span>{{ runs.length ? '查询与上下文已保存在当前会话' : '从一个经营问题开始，获得可核对的答案' }}</span></div><button v-if="runs.length" class="plain-action" @click="store.createSession"><el-icon><Plus /></el-icon>新分析</button><div v-else class="workspace-count"><span class="live-dot"></span>{{ Object.keys(selectedDataset?.table_counts || {}).length }} 张业务表已接入</div></div>

          <div v-if="runs.length || contextChips.length" class="context-bar"><div class="context-label"><el-icon><Connection /></el-icon>分析上下文</div><div class="context-chips"><span v-for="chip in contextChips" :key="chip">{{ chip }}</span><em v-if="!contextChips.length">条件将在首次成功查询后确定</em></div><button :class="{ expanded: contextExpanded }" title="查看完整上下文" @click="contextExpanded = !contextExpanded"><el-icon><ArrowDown /></el-icon></button><pre v-if="contextExpanded" class="context-json">{{ contextJson(session?.context || {}) }}</pre></div>

          <div ref="messagesHost" v-loading="loadingSession" class="conversation-scroll">
            <div v-if="!runs.length" class="welcome-content"><div class="welcome-heading"><div class="welcome-symbol"><el-icon><DataAnalysis /></el-icon></div><div><div class="eyebrow">BUSINESS INTELLIGENCE</div><h2>让经营数据，回答你的问题</h2><p>查数、比较、追问，沿着业务问题找到数据依据。</p></div></div>
              <div class="dataset-overview"><div class="overview-main"><div class="dataset-file-icon"><el-icon><DataBoard /></el-icon></div><div><h3>{{ selectedDataset?.name || '暂无数据集' }}<span>当前数据集</span></h3><p>下单日期 {{ dateRange }}</p></div></div><div class="overview-stat"><span>订单记录</span><strong>{{ selectedDataset?.order_count?.toLocaleString() ?? '—' }}<small> 笔</small></strong></div><div class="overview-stat"><span>商品 / 商家</span><strong>{{ selectedDataset?.table_counts?.products ?? '—' }}<small> / </small>{{ selectedDataset?.table_counts?.sellers ?? '—' }}</strong></div><div class="overview-version">{{ datasetId === 'excel-main' ? '主数据版本' : '独立演示版本' }}<span><el-icon><CircleCheck /></el-icon>数据已隔离</span></div></div>
              <div class="examples-heading"><h3>推荐从这些问题开始</h3><span>点击即可查询</span></div><div class="example-grid"><button v-for="example in visibleExamples" :key="example.label" class="example-card" :disabled="busy || !datasets.length" @click="useExample(example.question)"><div class="example-top"><el-icon><component :is="example.icon" /></el-icon><el-icon class="example-arrow"><ArrowRight /></el-icon></div><h4>{{ example.label }}</h4><p>{{ example.question }}</p><span>{{ example.detail }}</span></button></div>
              <div class="method-strip"><div><el-icon><CircleCheck /></el-icon><span>统一指标口径</span></div><div><el-icon><Shield /></el-icon><span>只读分析查询</span></div><div><el-icon><Document /></el-icon><span>保留查询依据</span></div></div>
            </div>

            <div v-else class="message-list"><article v-for="(run, index) in runs" :key="run.id" class="conversation-turn"><div class="user-message"><div class="user-bubble">{{ run.question }}</div><div class="user-avatar">我</div></div><div class="assistant-message"><div class="assistant-avatar"><el-icon><DataAnalysis /></el-icon></div><div class="assistant-content"><div class="assistant-heading"><strong>观数</strong><span>经营分析助手</span><span class="run-status" :class="run.status"><i></i>{{ statusLabel(run) }}</span></div>
              <div v-if="['queued', 'running'].includes(run.status)" class="run-progress"><div class="progress-text"><el-icon class="is-loading"><Loading /></el-icon>{{ phaseLabel(run) }}<button @click="store.cancel(run)">停止分析</button></div><div class="progress-steps"><span v-for="(label, step) in ['理解问题', '核对口径', '执行查询', '核验结果']" :key="label" :class="{ reached: (phaseProgress(run) >= step) }"><i></i>{{ label }}</span></div><p v-if="streamWarning" class="connection-warning">{{ streamWarning }}</p></div>
              <div v-if="run.status === 'awaiting_clarification' && run.clarification" class="clarification-card"><div class="clarification-title"><el-icon><InfoFilled /></el-icon>需要补充一个条件</div><p>{{ run.clarification.question }}</p><div class="clarification-options"><el-button v-for="option in run.clarification.options" :key="option.value" :loading="submitting" @click="store.clarify(run, option.value)">{{ option.label }}</el-button></div><div class="clarification-input"><el-input v-model="clarificationText" placeholder="也可以输入具体条件" maxlength="1000" @keydown.enter="clarificationText.trim() && store.clarify(run, clarificationText.trim()).then(() => clarificationText = '')" /><el-button type="primary" :disabled="!clarificationText.trim() || submitting" @click="store.clarify(run, clarificationText.trim()).then(() => clarificationText = '')">确认</el-button><el-button text @click="store.cancel(run)">取消</el-button></div></div>
              <template v-if="run.result"><h3 class="answer-title">{{ run.result.title }}</h3><div class="answer-text">{{ run.result.answer }}</div><ResultPanel v-if="run.result.columns.length" :result="run.result" @evidence="store.showEvidence" @page="store.resultPage(run, $event)" /><div v-else class="text-result-evidence"><button @click="store.showEvidence(run.result!)"><el-icon><Document /></el-icon>查看查询依据</button></div><div v-if="!run.result.columns.length && run.result.warnings.length" class="result-notes"><p v-for="warning in run.result.warnings" :key="warning">{{ warning }}</p></div></template>
              <div v-if="run.status === 'failed'" class="run-error"><el-icon><InfoFilled /></el-icon><div><strong>本次分析未完成</strong><p>{{ run.error || '查询过程中发生错误，请调整问题后重试。' }}</p><span>已确认的会话条件仍保留。</span></div></div><div v-if="run.status === 'cancelled'" class="cancelled-message">分析已取消，本轮未修改已确认条件。</div>
              <div v-if="['completed', 'failed', 'cancelled'].includes(run.status)" class="answer-footer"><span>分析 {{ String(index + 1).padStart(2, '0') }}<span v-if="run.created_at"> · {{ displayDate(run.created_at) }}</span></span><div><button v-if="run.result_id" :disabled="busy" @click="store.replay(run)"><el-icon><RefreshRight /></el-icon>按原计划重放</button><el-popover v-if="run.events.length" placement="top" :width="330" trigger="click"><template #reference><button><el-icon><DataLine /></el-icon>运行轨迹</button></template><div class="timeline-popover"><div v-for="event in run.events" :key="event.id"><i></i><span>{{ event.message || ({ result_ready: '结果已保存', answer_ready: '回答已生成', run_completed: '运行完成', run_failed: '运行失败', run_cancelled: '已取消', clarification_required: '等待补充条件' } as Record<string, string>)[event.type] || event.type }}</span></div></div></el-popover></div></div>
            </div></div></article></div>
          </div>

          <footer class="composer-area"><div v-if="runs.length && !busy" class="followup-suggestions"><span>继续追问</span><button v-for="question in ['按品类拆开', '和上月比', '只看数码家电']" :key="question" @click="useExample(question)">{{ question }}<el-icon><ArrowRight /></el-icon></button></div><div class="composer" :class="{ focused: draft.length > 0, disabled: activeRun?.status === 'awaiting_clarification' }"><textarea v-model="draft" :disabled="busy || !datasets.length" rows="2" maxlength="4000" :placeholder="activeRun?.status === 'awaiting_clarification' ? '请先回答上方的补充条件，或取消本次分析' : '描述你想了解的经营问题，例如：2025 年 4 月哪个品类带动了销售增长？'" aria-label="输入经营分析问题" @keydown="handleKeys"></textarea><div class="composer-bottom"><div><el-icon><Connection /></el-icon><span>{{ selectedDataset?.name || '等待数据' }}</span><span class="composer-divider"></span><span class="input-tip">Enter 发送 · Shift + Enter 换行</span></div><button v-if="activeRun && activeRun.status !== 'awaiting_clarification'" class="send-button stop-button" title="停止分析" @click="store.cancel(activeRun)"><i></i></button><button v-else class="send-button" :disabled="!draft.trim() || busy || !datasets.length" title="发送问题" @click="send"><el-icon v-if="submitting" class="is-loading"><Loading /></el-icon><el-icon v-else><Position /></el-icon></button></div></div><div class="composer-note"><el-icon><InfoFilled /></el-icon><span>销售额默认统计已送达订单的商品金额，不含运费。结果以当前数据与指标口径为依据。</span></div></footer>
        </section>
      </template>
    </main>
    <EvidenceDrawer v-model="evidenceOpen" :result="evidenceResult" />
    <el-dialog v-model="infoOpen" title="当前分析环境" width="480px"><div class="environment-dialog"><div><span>运行模式</span><strong>{{ health.mode === 'llm' ? '模型规划模式' : health.mode === 'rules' ? '规则规划模式' : '服务未连接' }}</strong></div><div v-if="health.model"><span>模型</span><strong>{{ health.model }}</strong></div><div><span>当前数据集</span><strong>{{ selectedDataset?.name || '未连接' }}</strong></div><div><span>下单日期范围</span><strong>{{ dateRange }}</strong></div><div><span>查询边界</span><strong>固定业务指标 · 只读查询</strong></div><p>规则模式支持预置业务指标及组合查询。启用模型模式需由服务端配置模型接口；浏览器不保存密钥。</p><code v-if="selectedDataset?.hash">SHA-256 {{ selectedDataset.hash }}</code></div></el-dialog>
  </div>
</template>

