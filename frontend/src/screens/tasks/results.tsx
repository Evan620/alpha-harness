/**
 * One task's whole result set, with the statistics the task card has no room for.
 *
 * Built for a Settings Sampler sweep, which runs the same expression across many markets: the
 * question there is not "which Alpha is best" but "where does this idea work", and that is a
 * comparison across regions rather than a leaderboard.
 */

import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from '@tanstack/react-router'
import { ArrowLeftIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { portfolio } from '@/screens/portfolio/api'
import { labTasks, type RankedAlpha } from '@/screens/tasks/api'
import {
  Button,
  Empty,
  ErrorNotice,
  Metric,
  Page,
  PageHeader,
  Panel,
  Skeleton,
  signTone,
  TEXT_TONE,
} from '@/ui/kit'
import { type Column, DataTable, type Sort } from '@/ui/table'

/** The sweep's whole result set, not a page of it: the comparison needs every row. */
const LIMIT = 2000

const setting = (r: RankedAlpha, key: string) => String(r.settings?.[key] ?? '')
const market = (r: RankedAlpha) => setting(r, 'region') || DASH

/** Sorted client-side: the rows are already here, and a sweep is thousands at most. */
function compare(a: RankedAlpha, b: RankedAlpha, sort: Sort): number {
  const pick = (r: RankedAlpha): string | number | null => {
    switch (sort.key) {
      case 'alphaId':
        return r.alphaId ?? ''
      case 'region':
        return market(r)
      case 'universe':
        return setting(r, 'universe')
      case 'neutralization':
        return setting(r, 'neutralization')
      case 'failed':
        return r.failedChecks.length
      default:
        return (r as unknown as Record<string, number | null>)[sort.key] ?? null
    }
  }
  const x = pick(a)
  const y = pick(b)
  if (x == null) return y == null ? 0 : 1
  if (y == null) return -1
  const order = typeof x === 'string' ? String(x).localeCompare(String(y)) : Number(x) - Number(y)
  return sort.desc ? -order : order
}

const METRICS: { key: keyof RankedAlpha; label: string; show: (v: number | null) => string }[] = [
  { key: 'sharpe', label: 'Sharpe', show: (v) => fmt.ratio(v) },
  { key: 'turnover', label: 'Turnover', show: (v) => fmt.pct(v, 2) },
  { key: 'fitness', label: 'Fitness', show: (v) => fmt.ratio(v) },
  { key: 'returns', label: 'Returns', show: (v) => fmt.pct(v, 2) },
  { key: 'drawdown', label: 'Drawdown', show: (v) => fmt.pct(v, 2) },
  { key: 'margin', label: 'Margin', show: (v) => fmt.bps(v, 2) },
]

const SIGNED = new Set(['sharpe', 'fitness', 'returns', 'margin'])

function columns(): Column<RankedAlpha>[] {
  return [
    {
      key: 'alphaId',
      header: 'Alpha',
      width: 'minmax(96px,1fr)',
      sortable: true,
      cell: (r) => <span className="num truncate text-ink">{r.alphaId ?? DASH}</span>,
    },
    {
      key: 'region',
      header: 'Region',
      width: 'minmax(70px,0.7fr)',
      sortable: true,
      cell: (r) => <span className="num">{market(r)}</span>,
    },
    {
      key: 'universe',
      header: 'Universe',
      width: 'minmax(84px,0.9fr)',
      sortable: true,
      cell: (r) => <span className="num truncate">{setting(r, 'universe') || DASH}</span>,
    },
    {
      key: 'neutralization',
      header: 'Neutralization',
      width: 'minmax(96px,1fr)',
      sortable: true,
      cell: (r) => <span className="num truncate">{setting(r, 'neutralization') || DASH}</span>,
    },
    {
      key: 'failed',
      header: 'Checks Failed',
      width: 'minmax(120px,1.4fr)',
      sortable: true,
      cell: (r) =>
        r.failedChecks.length === 0 ? (
          <span className="text-pnl-positive">none</span>
        ) : (
          <span className="truncate text-pnl-negative" title={r.failedChecks.join(', ')}>
            {r.failedChecks.join(', ')}
          </span>
        ),
    },
    ...METRICS.map(
      (m): Column<RankedAlpha> => ({
        key: String(m.key),
        header: m.label,
        width: 'minmax(84px,0.8fr)',
        align: 'right',
        sortable: true,
        cell: (r) => {
          const value = r[m.key] as number | null
          return (
            <span className={cn('num', SIGNED.has(String(m.key)) && TEXT_TONE[signTone(value)])}>
              {m.show(value)}
            </span>
          )
        },
      }),
    ),
  ]
}

/** One row of the across-regions table. */
interface RegionRow {
  region: string
  alphas: number
  submittable: number
  failed: number
  bestSharpe: number | null
}

export function TaskResultsScreen() {
  const { taskId } = useParams({ from: '/tasks/$taskId' })
  const id = Number(taskId)
  const [sort, setSort] = useState<Sort>({ key: 'sharpe', desc: true })

  const tasks = useQuery({ queryKey: ['tasks'], queryFn: labTasks.list })
  const task = tasks.data?.tasks.find((t) => t.id === id)

  const top = useQuery({
    queryKey: ['tasks', id, 'results'],
    queryFn: () => labTasks.top(id, LIMIT),
    enabled: Number.isFinite(id),
  })
  const rows = useMemo(() => top.data ?? [], [top.data])

  const byRegion = useMemo(() => {
    const seen = new Map<string, RegionRow>()
    for (const r of rows) {
      const key = market(r)
      const row = seen.get(key) ?? {
        region: key,
        alphas: 0,
        submittable: 0,
        failed: 0,
        bestSharpe: null,
      }
      row.alphas += 1
      if (r.submittable || r.pending) row.submittable += 1
      else row.failed += 1
      if (r.sharpe != null && (row.bestSharpe == null || r.sharpe > row.bestSharpe)) {
        row.bestSharpe = r.sharpe
      }
      seen.set(key, row)
    }
    return [...seen.values()].sort((a, b) => b.submittable - a.submittable || b.alphas - a.alphas)
  }, [rows])

  // Correlation needs the daily series, so it is asked for only once there are Alphas to ask
  // about, and it says plainly when they have not been downloaded yet.
  const ids = useMemo(
    () => rows.map((r) => r.alphaId).filter((a): a is string => Boolean(a)),
    [rows],
  )
  const correlation = useQuery({
    queryKey: ['tasks', id, 'correlation', ids.length],
    queryFn: () => portfolio.compute(ids, 0),
    enabled: ids.length >= 2,
    retry: false,
  })

  const sorted = useMemo(() => [...rows].sort((a, b) => compare(a, b, sort)), [rows, sort])
  const green = rows.filter((r) => r.submittable || r.pending).length
  const pending = rows.filter((r) => r.pending).length

  return (
    <Page>
      <PageHeader
        title={task?.templateName || task?.labName || `Task ${taskId}`}
        description={
          task
            ? `${task.labName} · ${task.status}${task.alphaId ? ` · ${task.alphaId}` : ''}`
            : 'Every Alpha this task produced'
        }
        actions={
          <Button variant="ghost" render={<Link to="/tasks" />}>
            <ArrowLeftIcon />
            Back to Tasks
          </Button>
        }
      />

      {top.isError && <ErrorNotice error={top.error} title="Could not read this task's results" />}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Metric boxed label="Alphas" value={fmt.int(rows.length)} />
        <Metric
          boxed
          tone="profit"
          label="Submittable"
          value={`${pending > 0 ? '~' : ''}${fmt.int(green)}`}
        />
        <Metric
          boxed
          tone={rows.length - green > 0 ? 'loss' : 'neutral'}
          label="Refused"
          value={fmt.int(rows.length - green)}
        />
        <Metric boxed label="Regions" value={fmt.int(byRegion.length)} />
      </div>

      <Panel
        title="Across Regions"
        description="Where the same expression is worth submitting, and where it is refused."
      >
        {top.isPending ? (
          <Skeleton className="h-40" />
        ) : byRegion.length === 0 ? (
          <Empty title="No Alphas back yet." />
        ) : (
          <DataTable
            label="Results by region"
            rows={byRegion}
            rowKey={(r) => r.region}
            columns={[
              {
                key: 'region',
                header: 'Region',
                width: 'minmax(80px,1fr)',
                cell: (r) => <span className="num text-ink">{r.region}</span>,
              },
              {
                key: 'alphas',
                header: 'Alphas',
                width: 'minmax(80px,0.8fr)',
                align: 'right',
                cell: (r) => fmt.int(r.alphas),
              },
              {
                key: 'submittable',
                header: 'Submittable',
                width: 'minmax(96px,1fr)',
                align: 'right',
                cell: (r) => (
                  <span className={cn('num', r.submittable > 0 && 'text-pnl-positive')}>
                    {fmt.int(r.submittable)}
                  </span>
                ),
              },
              {
                key: 'failed',
                header: 'Refused',
                width: 'minmax(84px,0.8fr)',
                align: 'right',
                cell: (r) => (
                  <span className={cn('num', r.failed > 0 && 'text-pnl-negative')}>
                    {fmt.int(r.failed)}
                  </span>
                ),
              },
              {
                key: 'bestSharpe',
                header: 'Best Sharpe',
                width: 'minmax(96px,1fr)',
                align: 'right',
                cell: (r) => (
                  <span className={cn('num', TEXT_TONE[signTone(r.bestSharpe)])}>
                    {fmt.ratio(r.bestSharpe)}
                  </span>
                ),
              },
            ]}
            maxHeight="40vh"
            empty="No Alphas back yet."
          />
        )}
      </Panel>

      <Panel
        title="Correlation"
        description="Pairwise daily PnL correlation between this task's Alphas, from their stored series."
      >
        {ids.length < 2 ? (
          <Empty title="Two Alphas with an id are needed before anything can be correlated." />
        ) : correlation.isPending ? (
          <Skeleton className="h-24" />
        ) : correlation.isError ? (
          <ErrorNotice error={correlation.error} title="Could not correlate these Alphas" />
        ) : (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Metric
                boxed
                label="Measured Pairs"
                value={fmt.int(correlation.data?.measuredPairs)}
              />
              <Metric
                boxed
                label="Highest"
                tone={(correlation.data?.highest?.correlation ?? 0) >= 0.7 ? 'loss' : 'neutral'}
                value={fmt.ratio(correlation.data?.highest?.correlation)}
              />
              <Metric boxed label="Alphas Correlated" value={fmt.int(correlation.data?.alphas)} />
              <Metric
                boxed
                label="Without PnL"
                value={fmt.int(correlation.data?.missing.length)}
                hint={
                  (correlation.data?.missing.length ?? 0) > 0
                    ? 'Download their PnL in the Pool'
                    : ''
                }
              />
            </div>
            {correlation.data?.highest && (
              <p className="text-body-compact text-ink-subtle">
                The closest pair is{' '}
                <span className="num text-ink">{correlation.data.highest.a}</span> and{' '}
                <span className="num text-ink">{correlation.data.highest.b}</span> at{' '}
                <span className="num text-ink">
                  {fmt.ratio(correlation.data.highest.correlation)}
                </span>
                . Two results of one sweep that move together are one idea, not two submissions.
              </p>
            )}
          </div>
        )}
      </Panel>

      <Panel
        title="Every Alpha"
        actions={<span className="num text-ink-subtle">{fmt.int(rows.length)}</span>}
      >
        <DataTable
          label="Task results"
          rows={sorted}
          columns={columns()}
          rowKey={(r) => String(r.trialId)}
          sort={sort}
          onSort={setSort}
          loading={top.isPending}
          error={top.error}
          maxHeight="70vh"
          empty="No Alphas back yet."
        />
      </Panel>
    </Page>
  )
}
