/**
 * The goal: what Vision keeps working toward, turn after turn, until a separate judge says it
 * is met, it runs out of turns or budget, or you stop it. You set it here; Vision has no
 * action for it, and the budget is enforced where actions run.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { PauseIcon, PlayIcon, TargetIcon, XIcon } from 'lucide-react'
import { useState } from 'react'
import { agent, type Goal } from '@/api/agent'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { Button, Input } from '@/ui/kit'

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

function budgetLine(goal: Goal) {
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

export function GoalCard({
  onStart,
  onPause,
  onResume,
  onDone,
}: {
  onStart: (goal: Goal) => void
  onPause: () => void
  onResume: () => void
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const { data } = useGoal()
  const goal = data?.goal
  const [objective, setObjective] = useState('')
  const [doneWhen, setDoneWhen] = useState('')
  const [sims, setSims] = useState('500')
  const [turns, setTurns] = useState('20')
  const refresh = () => queryClient.invalidateQueries({ queryKey: GOAL_KEY })

  const save = useMutation({
    mutationFn: agent.setGoal,
    onSuccess: (res) => {
      queryClient.setQueryData(GOAL_KEY, res)
      onDone()
      onStart(res.goal)
    },
  })
  const clear = useMutation({ mutationFn: agent.clearGoal, onSuccess: refresh })

  if (goal) {
    const s = statusOf(goal.status)
    return (
      <div className="rounded-sm border border-hairline-strong bg-surface-2 p-3">
        <div className="mb-1 flex items-center gap-2 text-body font-medium text-ink">
          <TargetIcon className="size-4 text-ink-muted" /> Goal
          <span className={cn('ml-auto text-body-compact', s.tone)}>{s.label}</span>
        </div>
        <p className="text-body text-ink">{goal.objective}</p>
        {goal.doneWhen && (
          <p className="mt-1 text-body-compact text-ink-muted">Met when: {goal.doneWhen}</p>
        )}
        <div className="mt-2 text-body-compact text-ink-muted">
          Turn {goal.turns} of {goal.maxTurns}, {budgetLine(goal)}
        </div>
        {goal.lastReason && (
          <div className="mt-2 rounded-xs bg-canvas px-2 py-1.5 text-body-compact text-ink">
            <span className="text-ink-subtle">Judge: </span>
            {goal.lastReason}
          </div>
        )}
        {goal.history.length > 1 && (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-ink-subtle">
              Every verdict ({goal.history.length})
            </summary>
            <ol className="mt-1 space-y-0.5 text-[11.5px] text-ink-muted">
              {goal.history.map((h) => (
                <li key={`${h.at}-${h.verdict}`}>
                  <span className="num text-ink-subtle">T{h.turn}</span>{' '}
                  <span className={statusOf(h.verdict).tone}>{h.verdict}</span> {h.reason}
                </li>
              ))}
            </ol>
          </details>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          {goal.running ? (
            <Button variant="secondary" size="sm" onClick={onPause}>
              <PauseIcon /> Pause
            </Button>
          ) : (
            goal.status !== 'met' && (
              <Button variant="primary" size="sm" onClick={onResume}>
                <PlayIcon /> Resume
              </Button>
            )
          )}
          <Button
            variant="ghost"
            size="sm"
            loading={clear.isPending}
            onClick={() => clear.mutate()}
          >
            <XIcon /> Clear goal
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-sm border border-hairline-strong bg-surface-2 p-3">
      <div className="mb-1 flex items-center gap-2 text-body font-medium text-ink">
        <TargetIcon className="size-4 text-ink-muted" /> Goal
      </div>
      <p className="text-body-compact text-ink-muted">
        Vision keeps working turn after turn until a separate judge sees evidence the goal is met,
        or it runs out of turns or budget. The budget is enforced when an action runs.
      </p>
      <div className="mt-2 space-y-2">
        <Input
          placeholder="Goal, e.g. Get EUR/D1 to 10 submitted-quality alphas"
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
        />
        <Input
          placeholder="Met when (optional), e.g. 1 alpha passes every check, prod corr < 0.6"
          value={doneWhen}
          onChange={(e) => setDoneWhen(e.target.value)}
        />
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-body-compact text-ink-muted">
            Simulations
            <Input
              type="number"
              min={0}
              max={5000}
              className="w-24"
              value={sims}
              onChange={(e) => setSims(e.target.value)}
            />
          </label>
          <label className="flex items-center gap-2 text-body-compact text-ink-muted">
            Max turns
            <Input
              type="number"
              min={1}
              max={100}
              className="w-20"
              value={turns}
              onChange={(e) => setTurns(e.target.value)}
            />
          </label>
        </div>
        <p className="text-[11px] text-ink-subtle">
          0 simulations means no ceiling. In Ask first mode the loop stops at each approval card;
          switch to Auto with /permissions to let it run unattended.
        </p>
        <Button
          variant="primary"
          size="sm"
          disabled={objective.trim().length < 3}
          loading={save.isPending}
          onClick={() =>
            save.mutate({
              objective: objective.trim(),
              done_when: doneWhen.trim(),
              brain_simulations: Math.max(0, Number(sims) || 0),
              max_turns: Math.min(100, Math.max(1, Number(turns) || 20)),
            })
          }
        >
          <PlayIcon /> Start
        </Button>
      </div>
    </div>
  )
}
