/** The neutralization chooser every lab shares: two families, pickable whole or one at a time. */

import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { splitNeutralizations } from '@/lib/neutralization'
import type { Choice } from '@/lib/scope'
import { Button, Chips, Fieldset } from '@/ui/kit'

/**
 * Every neutralization the market offers, grouped, with a button per family that ticks or
 * unticks the whole of it.
 *
 * The families are a shortcut, never a restriction: both can be on at once, and any hand-picked
 * mix is legal. A sweep across the risk models and a sweep across the group neutralizations are
 * different experiments, which is what makes the two buttons worth having — but nothing here
 * stops a run that mixes them.
 */
export function NeutralizationPicker({
  available,
  value,
  onChange,
  legend = 'Neutralization',
  hint,
  disabled,
}: {
  /** The legal values for the scope, from BRAIN's settings schema. */
  available: Choice[]
  value: string[]
  onChange: (value: string[]) => void
  legend?: string
  /** What an empty selection means, which is the caller's convention to state. */
  hint?: string | undefined
  disabled?: boolean | undefined
}) {
  const blocks = splitNeutralizations(available)
  const chosen = new Set(value)
  // The order BRAIN gave, so a selection reads the same way wherever it is shown.
  const order = available.map((choice) => choice.value)
  const sorted = (values: Iterable<string>) =>
    [...new Set(values)].sort((a, b) => order.indexOf(a) - order.indexOf(b))

  return (
    <Fieldset legend={legend} hint={hint}>
      <div className="flex flex-col gap-3">
        {blocks.map(({ group, items }) => {
          const ids = items.map((item) => item.value)
          const on = ids.filter((id) => chosen.has(id)).length
          const all = on === ids.length
          return (
            <div key={group.id} className="flex flex-col gap-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant={all ? 'primary' : 'secondary'}
                  disabled={disabled}
                  aria-pressed={all}
                  title={group.hint}
                  // All of them on already means the button's job is to take them off again.
                  onClick={() =>
                    onChange(
                      all ? value.filter((v) => !ids.includes(v)) : sorted([...value, ...ids]),
                    )
                  }
                >
                  {group.label}
                </Button>
                <span
                  className={cn(
                    'num text-caption',
                    on > 0 && !all ? 'text-ink-muted' : 'text-ink-subtle',
                  )}
                >
                  {fmt.int(on)} of {fmt.int(ids.length)}
                </span>
              </div>
              <Chips
                label={group.label}
                disabled={disabled}
                value={ids.filter((id) => chosen.has(id))}
                onChange={(next) =>
                  onChange(sorted([...value.filter((v) => !ids.includes(v)), ...next]))
                }
                items={items.map((item) => ({ value: item.value, label: item.label }))}
              />
            </div>
          )
        })}
      </div>
    </Fieldset>
  )
}
