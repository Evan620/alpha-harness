/**
 * The standing objective and its budget. The person sets it here; Vision has no action for
 * it, and the gate refuses any step that would spend past the ceiling.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { TargetIcon, XIcon } from 'lucide-react'
import { useState } from 'react'
import { agent } from '@/api/agent'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { Button, Input } from '@/ui/kit'

export function useGoal() {
  return useQuery({ queryKey: ['vision', 'goal'], queryFn: agent.goal })
}

/** Reads "820 of 1,200 simulations left", or the objective alone when uncapped. */
export function GoalChip({ onClick }: { onClick: () => void }) {
  const { data } = useGoal()
  const goal = data?.goal
  if (!goal) return null
  const left = goal.remaining['brain_simulations']
  const cap = goal.budgets['brain_simulations'] ?? 0
  return (
    <button
      type="button"
      onClick={onClick}
      title={goal.objective}
      className={cn(
        'flex max-w-[190px] items-center gap-1 rounded-pill border px-2 py-0.5 text-[11px]',
        goal.status === 'active'
          ? 'border-primary text-primary hover:bg-surface-2'
          : 'border-hairline text-ink-subtle hover:bg-surface-2',
      )}
    >
      <TargetIcon className="size-3 shrink-0" />
      <span className="truncate">{goal.objective}</span>
      {left !== null && <span className="shrink-0 num">{fmt.int(left)}</span>}
      {left === null && cap === 0 && <span className="shrink-0">∞</span>}
    </button>
  )
}

export function GoalCard({ onDone }: { onDone: () => void }) {
  const queryClient = useQueryClient()
  const { data } = useGoal()
  const goal = data?.goal
  const [objective, setObjective] = useState('')
  const [sims, setSims] = useState('500')
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['vision', 'goal'] })
  const save = useMutation({
    mutationFn: agent.setGoal,
    onSuccess: () => {
      refresh()
      onDone()
    },
  })
  const clear = useMutation({ mutationFn: agent.clearGoal, onSuccess: refresh })

  return (
    <div className="rounded-sm border border-hairline-strong bg-surface-2 p-3">
      <div className="mb-1 flex items-center gap-2 text-body font-medium text-ink">
        <TargetIcon className="size-4 text-ink-muted" /> Goal
      </div>
      {goal ? (
        <div className="space-y-2">
          <p className="text-body text-ink">{goal.objective}</p>
          <div className="text-body-compact text-ink-muted">
            {goal.status === 'active' ? 'Active' : `Stopped: ${goal.stoppedReason}`} ·{' '}
            {goal.remaining['brain_simulations'] === null
              ? 'no simulation ceiling'
              : `${fmt.int(goal.remaining['brain_simulations'])} of ${fmt.int(goal.budgets['brain_simulations'])} simulations left`}
          </div>
          <Button
            variant="secondary"
            size="sm"
            loading={clear.isPending}
            onClick={() => clear.mutate()}
          >
            <XIcon /> Clear goal
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-body-compact text-ink-muted">
            What should Vision work toward, and the most it may spend getting there. The budget is
            enforced when an action runs, not merely suggested.
          </p>
          <Input
            placeholder="e.g. Find 2 submittable EUR/D1 alphas"
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
          />
          <div className="flex items-center gap-2">
            <label htmlFor="goal-sims" className="text-body-compact text-ink-muted">
              Simulations
            </label>
            <Input
              id="goal-sims"
              type="number"
              min={0}
              max={5000}
              className="w-28"
              value={sims}
              onChange={(e) => setSims(e.target.value)}
            />
            <span className="text-[11px] text-ink-subtle">0 = no ceiling</span>
          </div>
          <Button
            variant="primary"
            size="sm"
            disabled={objective.trim().length < 3}
            loading={save.isPending}
            onClick={() =>
              save.mutate({
                objective: objective.trim(),
                brain_simulations: Math.max(0, Number(sims) || 0),
              })
            }
          >
            Set goal
          </Button>
        </div>
      )}
    </div>
  )
}
