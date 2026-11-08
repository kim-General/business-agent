import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchResult, fetchSession, normalizeDataset, normalizeRun, post, record, request } from '../api'
import type { Dataset, JsonRecord, QueryResult, Run, RunEvent, Session, ViewName } from '../types'

export const useWorkspaceStore = defineStore('workspace', () => {
  const datasets = ref<Dataset[]>([])
  const datasetId = ref('excel-main')
  const sessions = ref<Session[]>([])
  const session = ref<Session>()
  const runs = ref<Run[]>([])
  const view = ref<ViewName>('workspace')
  const booting = ref(true)
  const loadingSession = ref(false)
  const submitting = ref(false)
  const error = ref('')
  const streamWarning = ref('')
  const health = ref<JsonRecord>({})
  const catalog = ref<JsonRecord>({})
  const evidenceResult = ref<QueryResult>()
  const evidenceOpen = ref(false)
  const selectedDataset = computed(() => datasets.value.find((d) => d.id === datasetId.value))
  const activeRun = computed(() => [...runs.value].reverse().find((run) => ['queued', 'running', 'awaiting_clarification'].includes(run.status)))
  const busy = computed(() => submitting.value || Boolean(activeRun.value))
  let source: EventSource | undefined
  const lastSeq = new Map<string, number>()
  let sessionGeneration = 0

  function closeStream() { source?.close(); source = undefined; streamWarning.value = '' }
  function reportError(cause: unknown) { error.value = cause instanceof Error ? cause.message : String(cause) }
  async function refreshSessions() { const id = datasetId.value; const body = await request<{ sessions: Session[] }>(`/sessions?dataset_id=${encodeURIComponent(id)}`); if (id === datasetId.value) sessions.value = body.sessions }
  async function refreshContext() { const id = session.value?.id; if (!id) return; const generation = sessionGeneration; const current = await fetchSession(id); if (generation !== sessionGeneration || id !== session.value?.id) return; session.value = current; await refreshSessions() }
  async function bootstrap() {
    booting.value = true; error.value = ''
    try {
      const [healthBody, datasetBody] = await Promise.all([request('/health'), request<{ datasets: JsonRecord[] }>('/datasets')])
      health.value = healthBody; datasets.value = datasetBody.datasets.map(normalizeDataset)
      if (!datasets.value.some((d) => d.id === datasetId.value)) datasetId.value = datasets.value[0]?.id || 'excel-main'
      catalog.value = await request(`/datasets/${encodeURIComponent(datasetId.value)}/catalog`)
      await refreshSessions()
    } catch (cause) { reportError(cause) } finally { booting.value = false }
  }
  async function changeDataset(id: string) {
    closeStream(); sessionGeneration++; datasetId.value = id; session.value = undefined; runs.value = []; error.value = ''; view.value = 'workspace'
    try { await Promise.all([refreshSessions(), request(`/datasets/${encodeURIComponent(id)}/catalog`).then((body) => { catalog.value = body })]) } catch (cause) { reportError(cause) }
  }
  async function createSession() {
    closeStream(); sessionGeneration++; error.value = ''; view.value = 'workspace'
    try { session.value = await post<Session>('/sessions', { dataset_id: datasetId.value }); runs.value = []; await refreshSessions(); return session.value } catch (cause) { reportError(cause); return undefined }
  }
  async function selectSession(id: string) {
    closeStream(); const generation = ++sessionGeneration; loadingSession.value = true; error.value = ''; view.value = 'workspace'
    try {
      const current = await fetchSession(id)
      const userMessages = current.messages.filter((message) => message.role === 'user' && message.run_id)
      const ids = [...new Set(userMessages.map((message) => String(message.run_id)))]
      const bodies = await Promise.all(ids.map((runId) => request(`/runs/${encodeURIComponent(runId)}`)))
      const loaded = await Promise.all(bodies.map(async (body) => { const run = normalizeRun(body); if (run.result_id) run.result = await fetchResult(run.result_id); return run }))
      if (generation !== sessionGeneration) return
      session.value = current; runs.value = loaded
      const active = activeRun.value
      if (active && active.status !== 'awaiting_clarification') subscribe(active)
    } catch (cause) { if (generation === sessionGeneration) reportError(cause) } finally { if (generation === sessionGeneration) loadingSession.value = false }
  }
  async function handleEvent(run: Run, type: string, raw: JsonRecord) {
    if (run.session_id !== session.value?.id) return
    const payload = record(raw.payload)
    const seq = Number(raw.seq || 0)
    if (seq && seq <= (lastSeq.get(run.id) || 0)) return
    if (seq) lastSeq.set(run.id, seq)
    const event: RunEvent = { id: String(raw.id || `${run.id}:${seq}`), type, data: payload, message: String(payload.label || payload.message || ''), stage: String(payload.phase || '') }
    run.events.push(event)
    if (type === 'phase_changed') { run.stage = String(payload.phase); run.status = 'running' }
    if (type === 'clarification_required') { run.clarification = payload.clarification as Run['clarification']; run.status = 'awaiting_clarification'; closeStream() }
    if (type === 'result_ready') {
      run.result_id = String(payload.result_id)
      try { run.result = await fetchResult(run.result_id); await refreshContext() } catch (cause) { reportError(cause) }
    }
    if (type === 'answer_ready' && run.result && payload.summary) run.result.answer = typeof payload.summary === 'string' ? payload.summary : JSON.stringify(payload.summary)
    if (type === 'run_completed') { run.status = 'completed'; closeStream(); if (!run.result && payload.result_id) { run.result_id = String(payload.result_id); try { run.result = await fetchResult(run.result_id) } catch (cause) { reportError(cause) } } await refreshContext() }
    if (type === 'run_failed') { run.status = 'failed'; run.error = String(payload.message || '分析未能完成'); closeStream(); await refreshSessions() }
    if (type === 'run_cancelled') { run.status = 'cancelled'; closeStream(); await refreshSessions() }
  }
  function subscribe(run: Run) {
    closeStream()
    const stream = new EventSource(`/api/v1/runs/${encodeURIComponent(run.id)}/events?after_seq=${lastSeq.get(run.id) || 0}`)
    source = stream
    let recovering = false
    const eventNames = ['phase_changed', 'clarification_required', 'result_ready', 'answer_ready', 'warning', 'run_completed', 'run_failed', 'run_cancelled']
    for (const type of eventNames) source.addEventListener(type, (event) => { streamWarning.value = ''; try { void handleEvent(run, type, JSON.parse((event as MessageEvent).data) as JsonRecord).catch(reportError) } catch (cause) { reportError(cause) } })
    source.onopen = () => { streamWarning.value = '' }
    source.onerror = async () => {
      if (source !== stream || recovering) return
      streamWarning.value = '连接正在恢复，分析任务会继续运行。'
      recovering = true
      try { await reconcileRun(run) } catch { /* EventSource continues replaying from the last event ID. */ } finally { recovering = false }
    }
  }
  async function reconcileRun(run: Run) {
    const generation = sessionGeneration
    const current = normalizeRun(await request(`/runs/${encodeURIComponent(run.id)}`))
    if (generation !== sessionGeneration || run.session_id !== session.value?.id) return
    run.status = current.status; run.stage = current.stage; run.error = current.error; run.clarification = current.clarification
    if (['completed', 'failed', 'cancelled', 'awaiting_clarification'].includes(current.status)) closeStream()
    if (current.result_id) { run.result_id = current.result_id; run.result = await fetchResult(current.result_id) }
    if (generation !== sessionGeneration || run.session_id !== session.value?.id) return
    if (current.status === 'completed') await refreshContext()
    else if (['failed', 'cancelled'].includes(current.status)) await refreshSessions()
  }
  async function submit(message: string) {
    if (!message.trim() || busy.value) return
    error.value = ''; submitting.value = true
    try {
      if (!session.value && !await createSession()) return
      const body = await post(`/sessions/${encodeURIComponent(session.value!.id)}/messages`, { message: message.trim(), client_message_id: crypto.randomUUID(), expected_context_version: session.value!.context_version })
      const run: Run = { id: String(body.run_id), session_id: session.value!.id, dataset_id: datasetId.value, question: message.trim(), status: 'queued', events: [] }
      runs.value.push(run); subscribe(runs.value[runs.value.length - 1]!); await refreshSessions()
    } catch (cause) { reportError(cause) } finally { submitting.value = false }
  }
  async function clarify(run: Run, answer: string) {
    if (!run.clarification || !session.value || submitting.value) return
    submitting.value = true; error.value = ''
    try { await post(`/runs/${encodeURIComponent(run.id)}/clarifications`, { clarification_id: run.clarification.id, answer, expected_context_version: session.value.context_version }); run.status = 'queued'; run.clarification = undefined; subscribe(run) } catch (cause) { reportError(cause) } finally { submitting.value = false }
  }
  async function cancel(run: Run) {
    try {
      const body = await post(`/runs/${encodeURIComponent(run.id)}/cancel`)
      if (run.session_id !== session.value?.id) return
      if (body.status === 'cancelled') { run.status = 'cancelled'; closeStream(); await refreshSessions() }
      else await reconcileRun(run)
    } catch (cause) { reportError(cause) }
  }
  async function replay(run: Run) {
    if (busy.value || !session.value) return
    submitting.value = true; error.value = ''
    try { const body = await post(`/runs/${encodeURIComponent(run.id)}/replay`); const next: Run = { id: String(body.run_id), session_id: session.value.id, question: `重放：${run.question}`, status: 'queued', events: [] }; runs.value.push(next); subscribe(runs.value[runs.value.length - 1]!) } catch (cause) { reportError(cause) } finally { submitting.value = false }
  }
  async function resultPage(run: Run, page: number) { if (!run.result_id) return; try { run.result = await fetchResult(run.result_id, page) } catch (cause) { reportError(cause) } }
  function showEvidence(result: QueryResult) { evidenceResult.value = result; evidenceOpen.value = true }

  return { datasets, datasetId, sessions, session, runs, view, booting, loadingSession, submitting, error, streamWarning, health, catalog, selectedDataset, activeRun, busy, evidenceResult, evidenceOpen, bootstrap, changeDataset, createSession, selectSession, submit, clarify, cancel, replay, resultPage, showEvidence, closeStream }
})
