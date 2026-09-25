/**
 * The goal: what Vision keeps working toward, turn after turn, until a separate judge says it
 * is met, it runs out of turns or budget, or you stop it. Set with `/goal <condition>` in the
 * panel, as in Claude Code and Hermes; Vision has no action for it, and the budget is
 * enforced where actions run.
 */

import { useQuery } from '@tanstack/react-query'
import { TargetIcon } from 'lucide-react'
import { agent, type Goal } from '@/api/agent'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'

export const GOAL_KEY = ['vision', 'goal'] as const

export function useGoal() {
  return useQuery({ queryKey: GOAL_KEY, queryFn: agent.goal })
}

const STATUS: Record<string, { label: string; tone: string }> = {
  active: { label: 'Working', tone: 'text-primary' },
  waiting: { label: 'Waiting', tone: 'text-status-warning' },
  paused: { label: 'Paused', tone: 'text-ink-subtle' },
  met: { label: 'Met', tone: 'text-pnl-positive' },
  blocked: { label: 'Needs you', tone: 'text-status-warning' },
  impossible: { label: 'Not possible', tone: 'text-pnl-negative' },
  spent: { label: 'Budget used', tone: 'text-status-warning' },
  turns_out: { label: 'Out of turns', tone: 'text-status-warning' },
  stalled: { label: 'Stalled', tone: 'text-status-warning' },
  stopped: { label: 'Stopped', tone: 'text-ink-subtle' },
}

export const statusOf = (status: string) =>
  STATUS[status] ?? { label: status, tone: 'text-ink-subtle' }

export function budgetLine(goal: Goal) {
  const left = goal.remaining['brain_simulations']
  const cap = goal.budgets['brain_simulations'] ?? 0
  return left === null || left === undefined
    ? 'no simulation ceiling'
    : `${fmt.int(left)} of ${fmt.int(cap)} simulations left`
}

/** Header chip: status, turn, and what is left. */
export function GoalChip({ onClick }: { onClick: () => void }) {
  const { data } = useGoal()
  const goal = data?.goal
  if (!goal) return null
  const s = statusOf(goal.status)
  return (
    <button
      type="button"
      onClick={onClick}
      title={`${goal.objective}\n${goal.lastReason}`}
      className={cn(
        'flex max-w-[200px] items-center gap-1 rounded-pill border px-2 py-0.5 text-[11px] hover:bg-surface-2',
        goal.running ? 'border-primary' : 'border-hairline',
        s.tone,
      )}
    >
      <TargetIcon className={cn('size-3 shrink-0', goal.status === 'active' && 'animate-pulse')} />
      <span className="truncate">{s.label}</span>
      <span className="num shrink-0 text-ink-subtle">
        {goal.turns}/{goal.maxTurns}
      </span>
    </button>
  )
}
