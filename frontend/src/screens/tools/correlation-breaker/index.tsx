/**
 * Correlation Breaker (Tools): an Alpha BRAIN says is already in the production pool, re-shaped.
 *
 * The settings are the Alpha's own and are shown as read-only, because the whole idea is to
 * hold everything it was judged on and change only what it is exposed to. Every re-shape is
 * printed in full before anything is queued: a consultant should be able to read what will run.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { PlusIcon, UnlinkIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { errorMessage } from '@/api/http'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { CORES } from '@/screens/research-labs/lab-task'
import { Setting } from '@/screens/research-labs/task-settings'
import {
  Button,
  Empty,
  ErrorNotice,
  Field,
  Input,
  LINK,
  Metric,
  Notice,
  Page,
  PageHeader,
  Panel,
  Segmented,
  Skeleton,
} from '@/ui/kit'
import { correlationBreaker } from './api'

/** The settings strip: what every re-shape runs at, and cannot change. */
const HELD = [
  { label: 'Region', of: (s: Held) => s.region ?? DASH },
  { label: 'Delay', of: (s: Held) => (s.delay == null ? DASH : `D${s.delay}`) },
  { label: 'Universe', of: (s: Held) => s.universe ?? DASH },
  { label: 'Neutralization', of: (s: Held) => s.neutralization ?? DASH },
  { label: 'Decay', of: (s: Held) => (s.decay == null ? DASH : String(s.decay)) },
  { label: 'Truncation', of: (s: Held) => String(s.truncation ?? DASH) },
] as const

interface Held {
  region?: string | null
  delay?: number | null
  universe?: string | null
  neutralization?: string | null
  decay?: number | null
  truncation?: number | null
}

export function CorrelationBreakerScreen() {
  const search = useSearch({ from: '/tools/correlation-breaker' })
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  // The URL owns which Alpha is open, so a link from the Alpha screen is shareable.
  const alphaId = search.alpha ?? ''
  const [draft, setDraft] = useState(alphaId)
  const [chosen, setChosen] = useState<ReadonlySet<string>>(new Set())
  const [cores, setCores] = useState(1)

  useEffect(() => setDraft(search.alpha ?? ''), [search.alpha])

  const query = useQuery({
    queryKey: ['correlation-breaker', alphaId],
    queryFn: () => correlationBreaker.preview({ alphaId, recipes: [], cores: 1 }),
    enabled: alphaId.length > 0,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const plan = query.data
  const runnable = useMemo(() => (plan?.recipes ?? []).filter((r) => !r.blocked), [plan])

  // A new Alpha starts with every re-shape it can run ticked: the point is to try them.
  useEffect(() => {
    setChosen(new Set(runnable.map((r) => r.id)))
  }, [runnable])

  const analyse = (event: React.FormEvent) => {
    event.preventDefault()
    void navigate({
      to: '/tools/correlation-breaker',
      search: { alpha: draft.trim() || undefined },
      replace: true,
    })
  }

  const add = useMutation({
    mutationFn: () => correlationBreaker.addTask({ alphaId, recipes: [...chosen], cores }),
    onSuccess: (task) => {
      toast.success(`Queued ${task.name}`)
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void navigate({ to: '/tasks' })
    },
    onError: (e) => toast.error('Could not add task', { description: errorMessage(e) }),
  })

  const toggle = (id: string) =>
    setChosen((prev) => {
      const next = new Set(prev)
      if (!next.delete(id)) next.add(id)
      return next
    })

  return (
    <Page>
      <PageHeader
        title="Correlation Breaker"
        description="Re-shape an Alpha that is already in the Production Pool"
        actions={
          <Button
            variant="primary"
            disabled={chosen.size === 0}
            loading={add.isPending}
            onClick={() => add.mutate()}
          >
            <PlusIcon />
            Add Task
          </Button>
        }
      />

      <Panel title="Alpha">
        <form onSubmit={analyse} className="flex flex-wrap items-end gap-3">
          <Field label="Alpha ID" className="min-w-48 flex-1">
            <Input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              spellCheck={false}
              className="num"
            />
          </Field>
          <Button type="submit" variant="primary" disabled={!draft.trim()}>
            Analyse
          </Button>
        </form>
        {query.isError && <ErrorNotice error={query.error} title="Could not read this Alpha" />}
        {plan?.problems.map((problem) => (
          <Notice key={problem} tone="error" title={problem} className="mt-3" />
        ))}
      </Panel>

      {alphaId && query.isPending && <Skeleton className="h-64" label="Reading the Alpha" />}

      {plan?.expression && (
        <>
          <Panel
            title="Held Fixed"
            description="Every re-shape runs at the Alpha's own settings. Nothing here changes."
          >
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                {HELD.map((part) => (
                  <Metric
                    key={part.label}
                    boxed
                    size="sm"
                    label={part.label}
                    value={part.of(plan.settings)}
                  />
                ))}
              </div>
              {/* The biggest lever of all is a settings change, which this tool deliberately
                  refuses to make. Saying so beats letting it be discovered. */}
              <Notice tone="info" title="A Neutralization sweep is the other half of this">
                Re-running the same expression at RAM, Statistical or Crowding neutralization often
                breaks correlation on its own, and needs no new expression at all. That is a
                Simulation Settings change, so it belongs to the{' '}
                <a className={LINK} href={`/tools/settings-sampler?alpha=${alphaId}`}>
                  Settings Sampler
                </a>
                . Worth trying first, and it costs the same quota.
              </Notice>
              {plan.correlation && (
                <Notice
                  tone="warn"
                  title={`BRAIN last reported Production Correlation as ${String(
                    plan.correlation['result'] ?? 'PENDING',
                  )}`}
                >
                  {typeof plan.correlation['value'] === 'number' && (
                    <>
                      Its value was{' '}
                      <span className="num">{fmt.ratio(plan.correlation['value'])}</span> against a
                      limit of{' '}
                      <span className="num">
                        {fmt.ratio(Number(plan.correlation['limit'] ?? 0.7))}
                      </span>
                      .
                    </>
                  )}
                </Notice>
              )}
            </div>
          </Panel>

          <Panel
            title="Re-shapes"
            description="Each one wraps the Alpha below. Only the re-shape is shown; the binding is the same for all of them."
            actions={
              <Setting label="Cores">
                <Segmented
                  label="Cores"
                  items={CORES.map((v) => ({ value: v, label: v }))}
                  value={cores}
                  onChange={setCores}
                />
              </Setting>
            }
          >
            {/* Said once, so nine cards do not repeat the same two hundred characters. */}
            <pre className="num mb-4 overflow-x-auto rounded-sm border border-hairline bg-canvas p-3 text-body-compact whitespace-pre text-ink-muted">
              {plan.bound}
            </pre>
            {plan.recipes.length === 0 ? (
              <Empty title="Nothing to re-shape" />
            ) : (
              <ul className="flex flex-col gap-3">
                {plan.recipes.map((recipe) => {
                  const on = chosen.has(recipe.id)
                  return (
                    <li
                      key={recipe.id}
                      className={cn(
                        'rounded-md border p-3 transition-colors',
                        recipe.blocked
                          ? 'border-hairline bg-surface-1 opacity-70'
                          : on
                            ? 'border-primary bg-surface-2'
                            : 'border-hairline bg-surface-1',
                      )}
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div className="flex min-w-0 flex-col gap-1">
                          <label className="flex items-center gap-2 text-body font-medium text-ink">
                            <input
                              type="checkbox"
                              checked={on}
                              disabled={Boolean(recipe.blocked)}
                              onChange={() => toggle(recipe.id)}
                            />
                            {recipe.name}
                          </label>
                          <p className="max-w-3xl text-body-compact text-pretty text-ink-subtle">
                            {recipe.why}
                          </p>
                        </div>
                        {recipe.blocked && (
                          <span className="shrink-0 text-body-compact text-status-warning">
                            {recipe.blocked}
                          </span>
                        )}
                      </div>
                      <pre className="num mt-2 overflow-x-auto rounded-sm border border-hairline bg-canvas p-3 text-body-compact whitespace-pre text-ink-muted">
                        {recipe.transform}
                      </pre>
                      {recipe.caution && (
                        <p className="mt-2 text-body-compact text-pretty text-ink-subtle">
                          <UnlinkIcon className="mr-1 inline size-3.5 align-text-bottom" />
                          {recipe.caution}
                        </p>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </Panel>
        </>
      )}

      {!alphaId && !query.isPending && (
        <Panel>
          <Empty title="Give an Alpha ID">
            One that passes every check but Production Correlation is the one to bring here.
          </Empty>
        </Panel>
      )}
    </Page>
  )
}
