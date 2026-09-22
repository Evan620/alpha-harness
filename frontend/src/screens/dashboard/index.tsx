/**
 * Dashboard (spec §4.1): the day in one sentence, the Submittable Alphas counter, today's
 * allowance and queue, and the work in flight. The live matrix has its own screen.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { AwardIcon, CpuIcon, LayersIcon, SendIcon } from 'lucide-react'
import { type ReactNode, useEffect, useState } from 'react'
import { today } from '@/api/core'
import { http } from '@/api/http'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useRefetchOn } from '@/lib/ws'
import {
  ErrorNotice,
  LINK,
  Page,
  Panel,
  QuotaGauge,
  Skeleton,
  TEXT_TONE,
  type Tone,
} from '@/ui/kit'
import { CompetitionPanel } from './competition'
import { GettingStarted } from './getting-started'
import { WorkInFlight } from './work'

export function DashboardScreen() {
  const day = useQuery({ queryKey: ['today'], queryFn: () => today.get() })
  useRefetchOn('simulations', ['today'], 10_000)
  const sims = day.data?.simulations

  const submittable = useQuery({
    queryKey: ['pool', 'submittable-count'],
    queryFn: () => http.get<{ total: number }>('/api/vault/submittable?limit=1'),
  })
  useRefetchOn('simulations', ['pool', 'submittable-count'], 5000)
  const total = submittable.data?.total

  return (
    <Page>
      {sims ? (
        <Hero name={day.data?.you.fullName} text={sims.headline} />
      ) : (
        !day.isError && (
          <Skeleton className="mx-1 mt-2 h-18 w-2/3" label="Loading today's figures" />
        )
      )}
      {/* `RunToday` (./run-today) is deliberately unmounted, not dead: dispatching from the
          Dashboard is coming back. */}
      <GettingStarted today={day.data} />
      <CompetitionPanel />
      {day.isError && <ErrorNotice error={day.error} title="Today's figures could not load" />}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={<AwardIcon />}
          label="Submittable Alphas"
          action={
            <Link
              to="/pool/$tab"
              params={{ tab: 'submittable' }}
              className={cn(LINK, 'text-body-compact')}
            >
              Open
            </Link>
          }
          loading={submittable.isPending}
          value={submittable.isError ? '—' : fmt.int(total)}
          tone={total ? 'profit' : 'neutral'}
          hint={submittable.isError ? 'The count could not load.' : 'Submittable on BRAIN'}
        />
        <StatTile
          icon={<CpuIcon />}
          label="Simulations Left Today"
          loading={!sims}
          value={
            sims && (
              <>
                {sims.exact ? '' : '~'}
                {fmt.int(sims.remaining)}
                <span className="text-body text-ink-subtle"> / {fmt.int(sims.limit)}</span>
              </>
            )
          }
          extra={
            sims && (
              <QuotaGauge used={sims.used} limit={sims.limit} label="Simulation quota depletion" />
            )
          }
          hint={
            sims && (
              <>
                Resets in{' '}
                <ResetCountdown seconds={sims.resetsInSeconds} since={day.dataUpdatedAt} />
                {sims.exact ? '' : ' · estimate until the first result today'}
              </>
            )
          }
        />
        <StatTile
          icon={<SendIcon />}
          label="Sent Today"
          loading={!sims}
          value={sims && fmt.int(sims.used)}
          hint={
            sims && (
              <>
                <span className="num">
                  {fmt.pct(sims.limit ? sims.used / sims.limit : null, 0)}
                </span>{' '}
                of the allowance
              </>
            )
          }
        />
        <StatTile
          icon={<LayersIcon />}
          label="Queued"
          loading={!sims}
          value={sims && fmt.int(sims.queued)}
          hint={
            sims && (
              <>
                <span className="num">
                  {fmt.int(sims.engine.slotsUsed)}/{fmt.int(sims.engine.slots)}
                </span>{' '}
                cores busy
              </>
            )
          }
        />
      </div>

      <WorkInFlight />
    </Page>
  )
}

/**
 * The day in one sentence, as the page's heading, greeted by the account's name from
 * `/api/today` (BRAIN profile, falling back to the email). Figures keep tabular digits.
 */
function Hero({ name, text }: { name?: string | null | undefined; text: string }) {
  const parts = text.split(/(~?\d[\d,.]*%?)/)
  return (
    <header className="flex flex-col px-1 pt-2 pb-1">
      {name && <p className="text-display text-ink">Welcome, {name}</p>}
      {/* text-wrap beats the base h1 balance rule, which splits this into an extra short line. */}
      <h1 className="text-display text-wrap text-ink-muted">
        {parts.map((part, i) =>
          i % 2 === 1 ? (
            <span key={i} className="num text-display text-primary">
              {part}
            </span>
          ) : (
            part
          ),
        )}
      </h1>
    </header>
  )
}

/** One layout for every tile: icon + label (and action) on top, the figure, then a hint. */
function StatTile({
  icon,
  label,
  action,
  value,
  hint,
  tone = 'neutral',
  loading,
  extra,
}: {
  icon?: ReactNode
  label: string
  action?: ReactNode
  value: ReactNode
  hint?: ReactNode
  tone?: Tone
  loading: boolean
  extra?: ReactNode
}) {
  return (
    <Panel bodyClassName="flex h-full flex-col justify-between gap-3">
      <div className="flex min-h-5 items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {icon && <span className="text-ink-subtle [&_svg]:size-3.5">{icon}</span>}
          <span className="text-caption font-medium uppercase tracking-wide text-ink-subtle">
            {label}
          </span>
        </div>
        {action}
      </div>
      {loading ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-6 w-28" />
          <Skeleton className="h-4 w-40" />
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <span className={cn('num text-headline font-semibold leading-none', TEXT_TONE[tone])}>
            {value}
          </span>
          {extra}
          {hint && <span className="text-body-compact text-ink-subtle">{hint}</span>}
        </div>
      )}
    </Panel>
  )
}

/** Ticks locally between refetches, in its own component so the page does not re-render. */
function ResetCountdown({ seconds, since }: { seconds: number; since: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  return <span className="num">{fmt.countdown(seconds - Math.max(0, (now - since) / 1000))}</span>
}
