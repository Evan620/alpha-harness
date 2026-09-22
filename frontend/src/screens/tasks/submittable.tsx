/** Every submittable Alpha from every task in one sortable list. */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { DASH, fmt } from '@/lib/format'
import { useRefetchOn } from '@/lib/ws'
import { MetricBadge, signTone, TEXT_TONE } from '@/ui/kit'
import { type Column, DataTable, type Sort } from '@/ui/table'
import { labTasks, type TaskAlpha } from './api'

const setting = (r: TaskAlpha, key: string) => r.settings?.[key] as string | number | undefined

/** What each column sorts on: numbers as numbers, the rest as text, missing values last. */
const SORT: Record<string, (r: TaskAlpha) => number | string | null | undefined> = {
  alphaId: (r) => r.alphaId,
  sharpe: (r) => r.sharpe,
  turnover: (r) => r.turnover,
  fitness: (r) => r.fitness,
  returns: (r) => r.returns,
  drawdown: (r) => r.drawdown,
  margin: (r) => r.margin,
  longCount: (r) => r.longCount,
  shortCount: (r) => r.shortCount,
  region: (r) => setting(r, 'region'),
  universe: (r) => setting(r, 'universe'),
  delay: (r) => setting(r, 'delay'),
  neutralization: (r) => setting(r, 'neutralization'),
  decay: (r) => setting(r, 'decay'),
  maxTrade: (r) => setting(r, 'maxTrade'),
  maxPosition: (r) => setting(r, 'maxPosition'),
  checks: (r) => (r.pending ? 'PENDING' : 'PASS'),
  task: (r) => r.taskName,
}

const num = (value: string) => <span className="num">{value}</span>

const COLUMNS: Column<TaskAlpha>[] = [
  {
    key: 'alphaId',
    header: 'Alpha',
    width: '104px',
    sortable: true,
    cell: (r) => num(r.alphaId ?? DASH),
  },
  {
    key: 'sharpe',
    header: 'Sharpe',
    width: '88px',
    align: 'right',
    sortable: true,
    cell: (r) => (
      <MetricBadge tone={signTone(r.sharpe) === 'loss' ? 'loss' : 'profit'}>
        {fmt.ratio(r.sharpe)}
      </MetricBadge>
    ),
  },
  {
    key: 'turnover',
    header: 'Turnover',
    width: '92px',
    align: 'right',
    sortable: true,
    cell: (r) => num(fmt.pct(r.turnover, 2)),
  },
  {
    key: 'fitness',
    header: 'Fitness',
    width: '80px',
    align: 'right',
    sortable: true,
    cell: (r) => (
      <span className={`num ${TEXT_TONE[signTone(r.fitness)]}`}>{fmt.ratio(r.fitness)}</span>
    ),
  },
  {
    key: 'returns',
    header: 'Returns',
    width: '88px',
    align: 'right',
    sortable: true,
    cell: (r) => num(fmt.pct(r.returns, 2)),
  },
  {
    key: 'drawdown',
    header: 'Drawdown',
    width: '96px',
    align: 'right',
    sortable: true,
    cell: (r) => num(fmt.pct(r.drawdown, 2)),
  },
  {
    key: 'margin',
    header: 'Margin',
    width: '96px',
    align: 'right',
    sortable: true,
    cell: (r) => num(fmt.bps(r.margin, 2)),
  },
  {
    key: 'longCount',
    header: 'Long',
    width: '72px',
    align: 'right',
    sortable: true,
    cell: (r) => num(r.longCount == null ? DASH : String(r.longCount)),
  },
  {
    key: 'shortCount',
    header: 'Short',
    width: '72px',
    align: 'right',
    sortable: true,
    cell: (r) => num(r.shortCount == null ? DASH : String(r.shortCount)),
  },
  {
    key: 'region',
    header: 'Region',
    width: '76px',
    sortable: true,
    cell: (r) => setting(r, 'region') ?? DASH,
  },
  {
    key: 'universe',
    header: 'Universe',
    width: '104px',
    sortable: true,
    cell: (r) => setting(r, 'universe') ?? DASH,
  },
  {
    key: 'delay',
    header: 'Delay',
    width: '68px',
    sortable: true,
    cell: (r) => (setting(r, 'delay') == null ? DASH : `D${setting(r, 'delay')}`),
  },
  {
    key: 'neutralization',
    header: 'Neutralization',
    width: '150px',
    sortable: true,
    cell: (r) => setting(r, 'neutralization') ?? DASH,
  },
  {
    key: 'decay',
    header: 'Decay',
    width: '68px',
    align: 'right',
    sortable: true,
    cell: (r) => num(String(setting(r, 'decay') ?? DASH)),
  },
  {
    key: 'maxTrade',
    header: 'Max Trade',
    width: '96px',
    sortable: true,
    cell: (r) => setting(r, 'maxTrade') ?? DASH,
  },
  {
    key: 'maxPosition',
    header: 'Max Position',
    width: '112px',
    sortable: true,
    cell: (r) => setting(r, 'maxPosition') ?? DASH,
  },
  {
    key: 'checks',
    header: 'Checks',
    width: '92px',
    sortable: true,
    cell: (r) =>
      // Green either way: nothing has refused these, which is what the pane lists.
      r.pending ? (
        <span className="text-pnl-positive">Pending</span>
      ) : (
        <span className="text-pnl-positive">Pass</span>
      ),
  },
  {
    key: 'task',
    header: 'Task',
    width: 'minmax(200px,1fr)',
    sortable: true,
    cell: (r) => <span className="truncate text-ink-muted">{r.taskName}</span>,
  },
]

function compare(a: TaskAlpha, b: TaskAlpha, sort: Sort) {
  const get = SORT[sort.key] ?? SORT['sharpe']
  const x = get?.(a)
  const y = get?.(b)
  // Missing values sink to the bottom whichever way the column is sorted.
  if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1
  const order =
    typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))
  return sort.desc ? -order : order
}

export function SubmittableAlphas({ onOpenAlpha }: { onOpenAlpha: (alphaId: string) => void }) {
  const query = useQuery({ queryKey: ['submittable-alphas'], queryFn: labTasks.submittable })
  // Its own key, refreshed at most every 30s: reading every task's Alphas takes about a second,
  // too long to redo on each of the Tasks screen's two-second updates.
  useRefetchOn('studies', ['submittable-alphas'], 30_000)
  const [sort, setSort] = useState<Sort>({ key: 'sharpe', desc: true })
  const rows = [...(query.data ?? [])].sort((a, b) => compare(a, b, sort))
  return (
    <div className="flex flex-col gap-3">
      <p className="text-body-compact text-ink-subtle">
        <span className="num text-ink">{fmt.int(rows.length)}</span> submittable Alphas from every
        task: no check fails, only PASS, WARNING or PENDING, apart from checks that never block a
        submission (Prod Correlation, Regular Submission and the theme and pyramid labels).
      </p>
      <DataTable
        label="Submittable Alphas"
        rows={rows}
        columns={COLUMNS}
        rowKey={(r) => r.alphaId ?? String(r.trialId)}
        onRowClick={(r) => r.alphaId && onOpenAlpha(r.alphaId)}
        sort={sort}
        onSort={setSort}
        loading={query.isPending}
        error={query.error}
        empty="No submittable Alphas yet."
      />
    </div>
  )
}
