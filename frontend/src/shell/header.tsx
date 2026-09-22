/**
 * The bar above every screen: the clocks a consultant runs against (session, simulations
 * left, queued with the matrix on hover, quota reset), connection trouble, and ⌘K search.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ClockIcon, SearchIcon, ZapIcon } from 'lucide-react'
import { type ReactNode, useEffect, useState } from 'react'
import { simulations, today } from '@/api/core'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useCores, useLive } from '@/lib/live'
import type { CellState } from '@/lib/matrix'
import { useRefetchOn } from '@/lib/ws'
import { Button, STATUS } from '@/ui/kit'
import { Tooltip } from '@/ui/overlay'
import { useCommandMenu } from './command-menu'

export function Header() {
  const openMenu = useCommandMenu((s) => s.setOpen)

  return (
    <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-hairline bg-canvas px-4">
      <HeaderCores />
      <div className="flex items-center gap-4">
        <Clocks />
        <ConnectionNotice />
        <Button
          size="icon-sm"
          aria-label="Go to a screen or lab (⌘K)"
          title="Go to a screen or lab (⌘K)"
          onClick={() => openMenu(true)}
        >
          <SearchIcon />
        </Button>
      </div>
    </header>
  )
}

/** One tile per concurrent simulation core, always in the top bar (DESIGN.md core-slot). */
function HeaderCores() {
  const live = useLive((s) => s.simulations)
  const active = useQuery({
    queryKey: ['simulations', 'active'],
    queryFn: () => simulations.active(),
    enabled: live === null,
    // Always mounted, so it speaks for every screen: a restarted backend may have no
    // snapshot to replay, and one read taken while it was down would stand forever.
    refetchInterval: 5000,
  })
  const engine = useQuery({
    queryKey: ['simulations', 'engine'],
    queryFn: () => simulations.engine(),
  })
  useRefetchOn('simulations', ['simulations', 'engine'], 2000)

  const slots = engine.data?.slots ?? 8
  const maxBatch = engine.data?.maxBatch ?? 10
  const dailyLimitHit = Boolean(engine.data?.dailyLimitHit)
  const sessionLost = Boolean(engine.data?.sessionLost)
  const { cores } = useCores(live ?? active.data, slots, maxBatch)

  return (
    <nav
      aria-label={`${slots} simulation cores execution matrix`}
      className="flex items-center gap-1.5 rounded-md border border-hairline bg-surface-1 p-1"
    >
      <Link
        to="/matrix"
        className="flex items-center gap-1 rounded-xs transition-colors hover:bg-surface-2 focus-visible:-outline-offset-2"
        title="Open Simulation Matrix"
        aria-label="Open Simulation Matrix"
      >
        {cores.map((core, i) => {
          const holder = core.holder
          let state: 'running' | 'queued' | 'warning' | 'idle' = 'idle'
          if (dailyLimitHit || sessionLost) {
            state = 'warning'
          } else if (holder) {
            state = holder.status === 'RUNNING' ? 'running' : 'queued'
          }

          const stateClasses = {
            running: 'bg-status-running text-white',
            queued: 'bg-status-queued text-ink',
            warning: 'bg-status-warning text-ink',
            idle: 'bg-status-idle text-ink-subtle border border-hairline hover:border-hairline-strong hover:text-ink',
          }[state]

          const tooltipContent = (
            <div className="flex flex-col gap-1 text-caption">
              <span className="font-medium text-ink">
                Core {i + 1}:{' '}
                {state === 'idle'
                  ? 'Idle'
                  : state === 'running'
                    ? 'Running'
                    : state === 'warning'
                      ? 'Warning'
                      : 'Queued'}
              </span>
              {holder && (
                <>
                  <span className="text-ink-muted">
                    {holder.region} · D{holder.delay} · {holder.universe}
                  </span>
                  <span className="num text-ink-subtle">
                    {holder.task} ({fmt.pct(holder.progress, 0)})
                  </span>
                </>
              )}
            </div>
          )

          return (
            <Tooltip key={i} content={tooltipContent}>
              <span
                role="status"
                aria-label={`Core ${i + 1}: ${state}`}
                className={cn(
                  'num flex size-7 shrink-0 items-center justify-center rounded-xs text-caption font-medium select-none transition-colors max-sm:size-5',
                  stateClasses,
                )}
              >
                <span className="max-sm:hidden">C{i + 1}</span>
                <span className="sm:hidden">{i + 1}</span>
              </span>
            </Tooltip>
          )
        })}
      </Link>
    </nav>
  )
}

/**
 * Silent while live updates flow. Only after the socket has been down for 2s does it say so,
 * so the normal connect on page load never flashes it.
 */
function ConnectionNotice() {
  const connected = useLive((s) => s.connected)
  const [lost, setLost] = useState(false)
  useEffect(() => {
    if (connected) return
    const timer = setTimeout(() => setLost(true), 2000)
    return () => {
      clearTimeout(timer)
      setLost(false)
    }
  }, [connected])

  if (connected || !lost) return null
  return (
    <Tooltip content="Live updates are paused: the connection to the backend dropped. The matrix and counts resume when it reconnects.">
      <span
        role="status"
        className="flex h-7 items-center gap-1.5 rounded-md border border-status-warning-edge bg-status-warning-tint px-3 text-body-compact whitespace-nowrap text-status-warning"
      >
        <span className="size-1.5 animate-pulse rounded-pill bg-status-warning" aria-hidden />
        Reconnecting…
      </span>
    </Tooltip>
  )
}

/** Seconds since the last fetch, so countdowns tick between polls. */
function useElapsed(since: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  return Math.max(0, Math.floor((now - since) / 1000))
}

function Clocks() {
  const bar = useQuery({
    queryKey: ['bar'],
    queryFn: () => today.bar(),
    refetchInterval: 30_000,
  })
  useRefetchOn('simulations', ['bar'], 3000)
  useRefetchOn('session', ['bar'])
  const elapsed = useElapsed(bar.dataUpdatedAt)
  if (!bar.data) return null

  const session =
    bar.data.expiresInSeconds == null ? null : Math.max(0, bar.data.expiresInSeconds - elapsed)
  // `exact` flips true once today's first simulation POST returns BRAIN's own quota headers.
  const { remaining, exact, queued } = bar.data.simulations

  return (
    // Below 1024px the two clocks hide; below 640px the quota figures drop their visible labels.
    <div className="flex items-center gap-2 text-body-compact text-ink-subtle">
      <Clock
        icon={<ClockIcon className="size-3.5 text-ink-subtle" aria-hidden />}
        label="BRAIN Session"
        hint="BRAIN Session Time To Live"
        wide
      >
        <span
          className={cn(
            'num',
            session !== null && session < 1800 ? 'text-status-warning' : 'text-ink',
          )}
        >
          {fmt.countdown(session)}
        </span>
      </Clock>
      <Clock
        icon={<ZapIcon className="size-3.5 text-primary" aria-hidden />}
        label="Simulations Left Today"
        hint="Simulations Left in Today's Quota"
        valueFirst
      >
        <span className="num font-medium text-ink">
          {exact ? '' : '~'}
          {fmt.int(remaining)}
        </span>
      </Clock>
      {/* Clickable, so a control rather than a status box. */}
      <Tooltip content={<MiniMatrix />}>
        <Button
          size="sm"
          render={<Link to="/matrix" />}
          aria-label={`${fmt.int(queued)} queued. Open the Simulation Matrix`}
        >
          <span className="num font-medium">{fmt.int(queued)}</span>
          <span className="max-sm:sr-only">Queued</span>
        </Button>
      </Tooltip>
      <Clock
        icon={<ClockIcon className="size-3.5 text-ink-subtle" aria-hidden />}
        label="Simulation Quota Reset in"
        wide
      >
        <span className="num text-ink">
          {fmt.countdown(Math.max(0, bar.data.resetsInSeconds - elapsed))}
        </span>
      </Clock>
    </div>
  )
}

function Clock({
  icon,
  label,
  hint,
  valueFirst,
  wide,
  children,
}: {
  icon?: ReactNode
  label: string
  hint?: string
  valueFirst?: boolean
  /** Hidden below 1024px. The others stay, showing only their figure below 640px. */
  wide?: boolean
  children: ReactNode
}) {
  const name = <span className="max-sm:sr-only">{label}</span>
  const body = (
    <span
      className={cn(
        STATUS,
        'bg-surface-1 transition-colors hover:border-hairline-strong',
        wide && 'max-lg:hidden',
      )}
    >
      {icon}
      {valueFirst ? children : name}
      {valueFirst ? name : children}
    </span>
  )
  return hint ? <Tooltip content={hint}>{body}</Tooltip> : body
}

const CELL: Record<CellState, string> = {
  RUNNING: 'bg-status-running',
  PENDING: 'bg-status-queued',
  EMPTY: 'bg-status-idle',
}

/**
 * The 8 cores × 10-Alpha batches at a glance, on hover of "queued". Mounts only while the
 * tooltip is open, and reads the same live snapshot as the Dashboard matrix.
 */
function MiniMatrix() {
  const live = useLive((s) => s.simulations)
  const active = useQuery({
    queryKey: ['simulations', 'active'],
    queryFn: () => simulations.active(),
    enabled: live === null,
  })
  const engine = useQuery({
    queryKey: ['simulations', 'engine'],
    queryFn: () => simulations.engine(),
  })
  const slots = engine.data?.slots ?? 8
  const maxBatch = engine.data?.maxBatch ?? 10
  const { cores } = useCores(live ?? active.data, slots, maxBatch)

  return (
    // Small cells, so the whole 8×10 reads at a glance without covering the page.
    <div className="flex items-stretch gap-2 p-1">
      {/* Y-axis label, read bottom-to-top like a chart axis. */}
      <span className="flex rotate-180 items-center justify-center text-caption font-medium tracking-wider uppercase whitespace-nowrap text-ink-subtle [writing-mode:vertical-rl]">
        {slots} Cores
      </span>
      <div
        role="img"
        aria-label={`Simulation matrix, ${slots} cores`}
        className="flex flex-col gap-1.5"
      >
        {cores.map((core, i) => (
          <div key={i} className="flex gap-1.5">
            {core.cells.map((cell, j) => (
              <span
                key={j}
                className={cn('size-4 rounded-xs border border-hairline-subtle', CELL[cell.state])}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
