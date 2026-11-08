import type { Dataset, Evidence, JsonRecord, QualityReport, QueryResult, Run, Session } from './types'

const base = '/api/v1'
const record = (value: unknown): JsonRecord => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
export { record }

export async function request<T = JsonRecord>(path: string, options?: RequestInit): Promise<T> {
  let response: Response
  try { response = await fetch(base + path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } }) }
  catch { throw new Error('无法连接分析服务，请确认后端已启动。') }
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as JsonRecord
    throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}
export const post = <T = JsonRecord>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

export function normalizeDataset(raw: JsonRecord): Dataset {
  const range = Array.isArray(raw.purchase_range) ? raw.purchase_range : []
  const counts = record(raw.table_counts) as Record<string, number>
  return { id: String(raw.id), name: String(raw.name || raw.id), description: String(raw.description || ''), date_start: String(range[0] || '').slice(0, 10), date_end: String(range[1] || '').slice(0, 10), hash: String(raw.hash || ''), metrics_version: String(raw.metrics_version || '1'), table_counts: counts, order_count: counts.orders, raw }
}
export function normalizeRun(raw: JsonRecord): Run {
  return { id: String(raw.id), session_id: String(raw.session_id), question: String(raw.question || ''), status: raw.status as Run['status'], stage: String(raw.phase || ''), result_id: raw.result_id ? String(raw.result_id) : undefined, created_at: String(raw.created_at || ''), error: raw.error ? (typeof raw.error === 'string' ? raw.error : JSON.stringify(raw.error)) : undefined, clarification: raw.clarification as Run['clarification'], events: [] }
}
export function normalizeResult(raw: JsonRecord): QueryResult {
  const columns = Array.isArray(raw.columns) ? raw.columns.map((item) => { const c = record(item); return { name: String(c.key || c.name), label: String(c.label || c.key || c.name), type: String(c.type || ''), unit: c.unit ? String(c.unit) : undefined } }) : []
  const charts = Array.isArray(raw.charts) ? raw.charts : []
  const summary = typeof raw.summary === 'string' ? raw.summary : raw.summary ? JSON.stringify(raw.summary, null, 2) : ''
  return { id: String(raw.id), run_id: String(raw.run_id || ''), title: String(raw.title || '查询结果'), answer: summary, columns, rows: Array.isArray(raw.rows) ? raw.rows as JsonRecord[] : [], total: Number(raw.total || 0), page: Math.floor(Number(raw.offset || 0) / Number(raw.limit || 100)) + 1, page_size: Number(raw.limit || 100), chart: charts[0] ? record(charts[0]) : undefined, evidence: { scope: raw.scope, metric: raw.metric }, warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [], raw }
}
export async function fetchResult(id: string, page = 1): Promise<QueryResult> {
  const [raw, evidence] = await Promise.all([request(`/results/${encodeURIComponent(id)}?offset=${(page - 1) * 100}&limit=100`), request<Evidence>(`/results/${encodeURIComponent(id)}/evidence`)])
  const result = normalizeResult(raw)
  const queries = Array.isArray(evidence.queries) ? evidence.queries.map(record) : []
  result.evidence = { ...result.evidence, ...evidence, sql: queries.map((query, index) => `-- 查询 ${index + 1}${query.grain ? ` · 粒度：${query.grain}` : ''}\n${query.sql}`).join('\n\n'), parameters: queries.map((query) => query.params), dataset_hash: String(evidence.data_hash || evidence.dataset_hash || '') }
  return result
}
export async function fetchQuality(datasetId: string): Promise<QualityReport> {
  const raw = await request(`/datasets/${encodeURIComponent(datasetId)}/quality`)
  return { dataset_id: datasetId, checks: Array.isArray(raw.checks) ? raw.checks.map((item) => { const c = record(item); return { name: String(c.name || '检查项目'), status: String(c.status || 'info'), count: c.count === undefined ? undefined : Number(c.count), message: typeof c.detail === 'string' ? c.detail : JSON.stringify(c.detail ?? c.message ?? ''), details: c.details } }) : [], summary: record(raw.summary || raw.quality_summary), raw }
}
export async function fetchSession(id: string): Promise<Session & { messages: JsonRecord[] }> {
  return request<Session & { messages: JsonRecord[] }>(`/sessions/${encodeURIComponent(id)}`)
}
