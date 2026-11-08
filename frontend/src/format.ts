import type { QueryColumn, QueryResult } from './types'

const numericTypes = new Set(['currency', 'integer', 'number', 'percentage'])
const decimalPattern = /^([+-]?)(\d+)(?:\.(\d*))?$/

export function numericValue(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  if (typeof value === 'string' && !decimalPattern.test(value.trim())) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function isNumericColumn(column: QueryColumn, rows: QueryResult['rows']): boolean {
  if (column.type) return numericTypes.has(column.type)
  return rows.some((row) => numericValue(row[column.name]) !== null)
}

// Keep decimal strings exact for presentation; convert to Number only for chart coordinates.
function decimalText(value: unknown, digits: number, shift = 0, trim = false): string {
  const match = decimalPattern.exec(String(value).trim())
  if (!match) return String(value)
  const fraction = match[3] || ''
  const decimals = fraction.length - shift
  let integer = BigInt(match[2]! + fraction)
  const precision = decimals - digits
  if (precision > 0) {
    const divisor = 10n ** BigInt(precision)
    integer = (integer + divisor / 2n) / divisor
  } else if (precision < 0) integer *= 10n ** BigInt(-precision)
  const padded = integer.toString().padStart(digits + 1, '0')
  const whole = (digits ? padded.slice(0, -digits) : padded).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const tail = digits ? padded.slice(-digits) : ''
  const fractionText = trim ? tail.replace(/0+$/, '') : tail
  return `${match[1] === '-' && integer !== 0n ? '-' : ''}${whole}${fractionText ? `.${fractionText}` : ''}`
}

export function formatCell(value: unknown, column?: QueryColumn): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'object') return JSON.stringify(value)
  if (numericValue(value) !== null) {
    if (column?.type === 'percentage') return `${decimalText(value, 2, 2)}%`
    if (column?.type === 'currency') return decimalText(value, 2)
    if (column?.type === 'integer') return decimalText(value, 0)
    if (column?.type === 'number' || typeof value === 'number') return decimalText(value, 4, 0, true)
  }
  return String(value)
}

export function columnLabel(column: QueryColumn): string {
  const unit = column.type === 'percentage' ? '%' : column.unit
  return unit && !column.label.includes(unit) ? `${column.label}（${unit}）` : column.label
}

export function chartColumns(result: QueryResult): QueryColumn[] {
  const spec = result.chart
  if (!spec || !Array.isArray(spec.y)) return []
  const names = spec.y.map(String)
  const requested = names.map((name) => result.columns.find((column) => column.name === name))
    .filter((column): column is QueryColumn => Boolean(column))
    .filter((column) => isNumericColumn(column, result.rows))
  const first = requested[0]
  if (!first) return []
  // A single axis must never combine monetary values, counts and rates.
  return requested.filter((column) => column.type === first.type && column.unit === first.unit)
}

export function hasChart(result: QueryResult): boolean {
  return Boolean(result.chart?.x && result.rows.length && chartColumns(result).length)
}
