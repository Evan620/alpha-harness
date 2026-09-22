/** Stored Alphas as key-value Markdown for an LLM to review (see `lib/llm-copy`). */

import { fmt } from '@/lib/format'
import { coreMetrics, kvMarkdown } from '@/lib/llm-copy'
import { type AlphaPageRequest, type AlphaRow, pool } from './api'

const PAGE = 500

/** Every Alpha the table's filters match, not only the page on screen. */
async function everyAlpha(body: AlphaPageRequest): Promise<AlphaRow[]> {
  const rows: AlphaRow[] = []
  for (let offset = 0; ; offset += PAGE) {
    const page = await pool.query({ ...body, limit: PAGE, offset })
    rows.push(...page.results)
    if (!page.results.length || rows.length >= page.total) return rows
  }
}

const text = (v: string | number | null | undefined) => (v == null ? '—' : String(v))

export async function alphasMarkdown(body: AlphaPageRequest, noun: string): Promise<string> {
  const rows = await everyAlpha(body)
  return kvMarkdown({
    title: `${fmt.int(rows.length)} ${noun}`,
    alphas: rows.map((r) => ({
      expression: r.expression,
      sharpe: r.sharpe,
      settings: [
        ['Region', text(r.region)],
        ['Universe', text(r.universe)],
        ['Delay', text(r.delay)],
        ['Decay', text(r.decay)],
        ['Neutralization', text(r.neutralization)],
        ['Truncation', text(r.truncation)],
        ['Max Trade', text(r.maxTrade)],
        ['Max Position', text(r.maxPosition)],
      ],
      traits: [
        ['Type', text(r.type)],
        ['Status', text(r.status)],
        ['Classifications', r.classifications?.join(', ') || '—'],
        ['Pyramids', r.pyramids?.join(', ') || '—'],
        ['Operator Count', text(r.operatorCount)],
      ],
      metrics: [
        ...coreMetrics(r),
        ['Train Sharpe', fmt.ratio(r.trainSharpe)],
        ['Test Sharpe', fmt.ratio(r.testSharpe)],
        ['Submitted', fmt.date(r.dateSubmitted)],
      ],
    })),
  })
}
