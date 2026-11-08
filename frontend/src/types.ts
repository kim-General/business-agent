export type JsonRecord = Record<string, unknown>
export type ViewName = 'workspace' | 'history' | 'quality'
export type RunStatus = 'queued' | 'running' | 'awaiting_clarification' | 'completed' | 'failed' | 'cancelled'

export interface Dataset {
  id: string
  name: string
  description?: string
  date_start?: string
  date_end?: string
  hash?: string
  metrics_version?: string
  table_counts?: Record<string, number>
  order_count?: number
  raw: JsonRecord
}

export interface Session {
  id: string
  dataset_id: string
  title: string
  created_at?: string
  updated_at?: string
  context: JsonRecord
  context_version: number
}

export interface QueryColumn {
  name: string
  label: string
  type?: string
  unit?: string
}

export interface Evidence {
  sql?: string
  parameters?: unknown
  dataset_id?: string
  dataset_hash?: string
  metrics_version?: string
  metric_definition?: string
  scope?: unknown
  checks?: unknown
  warnings?: string[]
  [key: string]: unknown
}

export interface QueryResult {
  id: string
  run_id: string
  title?: string
  answer: string
  columns: QueryColumn[]
  rows: JsonRecord[]
  total: number
  page: number
  page_size: number
  chart?: JsonRecord
  evidence: Evidence
  warnings: string[]
  summary?: JsonRecord
  raw: JsonRecord
}

export interface Clarification {
  id: string
  field: string
  question: string
  options: Array<{ label: string; value: string }>
}

export interface RunEvent {
  id: string
  type: string
  timestamp?: string
  message?: string
  stage?: string
  data: JsonRecord
}

export interface Run {
  id: string
  session_id: string
  dataset_id?: string
  question: string
  status: RunStatus
  stage?: string
  result_id?: string
  created_at?: string
  elapsed_ms?: number
  error?: string
  clarification?: Clarification
  events: RunEvent[]
  result?: QueryResult
  context?: JsonRecord
}

export interface QualityReport {
  dataset_id: string
  checks: Array<{ name: string; status: string; count?: number; message: string; details?: unknown }>
  summary: JsonRecord
  raw: JsonRecord
}
