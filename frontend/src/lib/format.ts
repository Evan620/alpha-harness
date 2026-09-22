/**
 * Formatting for a quant interface: every figure is read against the one above it, so
 * precision is fixed per measure. A genuinely absent value is an em dash, never 0.
 */

export const DASH = '—'

export const isNum = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)

// Built once: a table of a few hundred rows formats thousands of figures per render, and
// `toLocaleString` rebuilds its formatter on every call.
const INT = new Intl.NumberFormat('en-US')
const COMPACT = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 })
const DAY = { month: 'short', day: 'numeric', year: 'numeric' } as const
const DATE = new Intl.DateTimeFormat('en-US', DAY)
// A bare YYYY-MM-DD is a calendar day, parsed as UTC midnight: read it in UTC, or west of
// UTC it shows the day before.
const DATE_UTC = new Intl.DateTimeFormat('en-US', { ...DAY, timeZone: 'UTC' })
const DATE_TIME = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

/** The moment an ISO string names, or null when it is not one. */
function moment(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const parsed = new Date(normaliseIso(iso))
  // Formatting an Invalid Date throws, which would blank the screen over one bad field.
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export const fmt = {
  /** 5,000 */
  int: (v: number | null | undefined) => (isNum(v) ? INT.format(Math.round(v)) : DASH),
  /** 1.58 — Sharpe and Fitness. */
  ratio: (v: number | null | undefined, digits = 2) => (isNum(v) ? v.toFixed(digits) : DASH),
  /** A fraction as a percent: 0.643 → 64.3% */
  pct: (v: number | null | undefined, digits = 1) =>
    isNum(v) ? `${(v * 100).toFixed(digits)}%` : DASH,
  /** A fraction as basis points: 0.0005 → 5.0 bps */
  bps: (v: number | null | undefined, digits = 1) =>
    isNum(v) ? `${(v * 10_000).toFixed(digits)} bps` : DASH,
  /** 12.4K */
  compact: (v: number | null | undefined) => (isNum(v) ? COMPACT.format(v) : DASH),
  /** 2h 05m, 4m 12s, 9s */
  duration: (seconds: number | null | undefined) => {
    if (!isNum(seconds) || seconds < 0) return DASH
    const s = Math.floor(seconds)
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
    if (m > 0) return `${m}m ${String(s % 60).padStart(2, '0')}s`
    return `${s}s`
  },
  /** 7h 21m — for countdowns to a reset. */
  countdown: (seconds: number | null | undefined) => {
    if (!isNum(seconds)) return DASH
    const s = Math.max(0, Math.floor(seconds))
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
  },
  /** Sep 10, 2026 */
  date: (iso: string | null | undefined) => {
    const at = moment(iso)
    if (!at) return DASH
    return ((iso?.length ?? 0) <= 10 ? DATE_UTC : DATE).format(at)
  },
  /** Sep 10, 14:05 */
  dateTime: (iso: string | null | undefined) => {
    const at = moment(iso)
    return at ? DATE_TIME.format(at) : DASH
  },
  /** 3 min ago */
  ago: (iso: string | null | undefined) => {
    const at = moment(iso)
    if (!at) return DASH
    const seconds = (Date.now() - at.getTime()) / 1000
    if (seconds < 60) return 'just now'
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
    if (seconds < 86_400) return `${Math.floor(seconds / 3600)} h ago`
    return `${Math.floor(seconds / 86_400)} d ago`
  },
}

/** Seconds elapsed since an ISO timestamp. */
export function secondsSince(iso: string | null | undefined, now = Date.now()): number | null {
  const at = moment(iso)
  return at ? Math.max(0, (now - at.getTime()) / 1000) : null
}

/** Some backend timestamps carry no offset; they are UTC. */
function normaliseIso(iso: string): string {
  return /[zZ]|[+-]\d\d:\d\d$/.test(iso) || iso.length <= 10 ? iso : `${iso}Z`
}
