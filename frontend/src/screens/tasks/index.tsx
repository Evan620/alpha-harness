/** Tasks: everything the labs added. Only here does a task run, wait for cores, pause or stop. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from '@tanstack/react-router'
import {
  CopyIcon,
  EllipsisIcon,
  ExternalLinkIcon,
  PauseIcon,
  PencilIcon,
  PlayIcon,
  SquareIcon,
  StarIcon,
  Trash2Icon,
} from 'lucide-react'
import { type ComponentProps, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { errorMessage } from '@/api/http'
import { DASH, fmt } from '@/lib/format'
import { useRefetchOn } from '@/lib/ws'
import { DetailSheet } from '@/screens/pool/detail'
import { MAX_SIMULATIONS } from '@/screens/research-labs/lab-task'
import { type LabTask, labTasks, type RankedAlpha, type TaskStatus } from '@/screens/tasks/api'
import { resultsMarkdown } from '@/screens/tasks/copy'
import { SubmittableAlphas } from '@/screens/tasks/submittable'
import {
  Badge,
  Button,
  Disclosure,
  Empty,
  ErrorNotice,
  Field,
  Fieldset,
  Input,
  LINK,
  Metric,
  MetricBadge,
  Notice,
  Page,
  PageHeader,
  Panel,
  Progress,
  Segmented,
  signTone,
  TEXT_TONE,
} from '@/ui/kit'
import { Confirm, Dialog, Menu } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'

/** Matches `labs.params.SETTINGS_SAMPLER`. */
const SETTINGS_SAMPLER = 'settings-sampler'

const STATUS: Record<TaskStatus, { label: string; tone: ComponentProps<typeof Badge>['tone'] }> = {
  IDLE: { label: 'Not Started', tone: 'outline' },
  QUEUED: { label: 'Waiting', tone: 'warn' },
  RUNNING: { label: 'Running', tone: 'profit' },
  PAUSED: { label: 'Paused', tone: 'muted' },
  COMPLETE: { label: 'Complete', tone: 'neutral' },
  FAILED: { label: 'Failed', tone: 'loss' },
}

const TOP_COLUMNS: Column<RankedAlpha>[] = [
  {
    key: 'expression',
    header: 'Expression',
    width: 'minmax(280px,3fr)',
    cell: (r) => (
      <span className="num block truncate text-ink" title={r.expression ?? undefined}>
        {r.expression ?? DASH}
      </span>
    ),
  },
  {
    key: 'sharpe',
    header: 'Sharpe',
    width: '90px',
    align: 'right',
    cell: (r) =>
      r.sharpe == null ? (
        DASH
      ) : (
        <MetricBadge tone={r.sharpe > 0 ? 'profit' : r.sharpe < 0 ? 'loss' : 'neutral'}>
          {fmt.ratio(r.sharpe)}
        </MetricBadge>
      ),
  },
  {
    key: 'fitness',
    header: 'Fitness',
    width: '80px',
    align: 'right',
    cell: (r) => fmt.ratio(r.fitness),
  },
  {
    key: 'turnover',
    header: 'Turnover',
    width: '88px',
    align: 'right',
    cell: (r) => fmt.pct(r.turnover),
  },
  {
    key: 'universe',
    header: 'Universe',
    width: '96px',
    cell: (r) => r.settings?.universe ?? DASH,
  },
  {
    key: 'neutralization',
    header: 'Neutralization',
    width: '120px',
    cell: (r) => r.settings?.neutralization ?? DASH,
  },
]

const setting = (key: string, header: string, width: string): Column<RankedAlpha> => ({
  key,
  header,
  width,
  cell: (r) => <span className="num">{(r.settings?.[key] as string | undefined) ?? DASH}</span>,
})

/**
 * A Settings Sampler row is only ever the same expression, so the settings lead instead and
 * Sharpe closes. The Alpha the sweep started from is starred as the reference point.
 */
const SAMPLER_COLUMNS: Column<RankedAlpha>[] = [
  {
    key: 'number',
    header: 'Trial',
    width: '84px',
    cell: (r) => (
      <span className="num flex items-center gap-1.5 text-ink-subtle">
        {r.source && <StarIcon className="size-3 shrink-0 fill-primary text-primary" />}
        {r.number}
      </span>
    ),
  },
  setting('region', 'Region', '96px'),
  setting('universe', 'Universe', '116px'),
  {
    key: 'delay',
    header: 'Delay',
    width: '72px',
    cell: (r) => <span className="num">{`D${r.settings?.['delay'] ?? DASH}`}</span>,
  },
  // Takes the slack, so the table fills its pane and Sharpe closes at the right edge.
  setting('neutralization', 'Neutralization', 'minmax(180px,1fr)'),
  setting('maxTrade', 'Max Trade', '104px'),
  setting('maxPosition', 'Max Position', '128px'),
  {
    key: 'sharpe',
    header: 'Sharpe',
    width: '100px',
    align: 'right',
    cell: (r) =>
      r.sharpe == null ? (
        DASH
      ) : (
        <MetricBadge tone={r.sharpe > 0 ? 'profit' : r.sharpe < 0 ? 'loss' : 'neutral'}>
          {fmt.ratio(r.sharpe)}
        </MetricBadge>
      ),
  },
]

/** What a task searches for leads the table when it is not Sharpe, which the table shows anyway. */
const topColumns = (task: LabTask): Column<RankedAlpha>[] =>
  task.lab === SETTINGS_SAMPLER
    ? SAMPLER_COLUMNS
    : task.objectiveLabel === 'Sharpe'
      ? TOP_COLUMNS
      : [
          ...TOP_COLUMNS.slice(0, 1),
          {
            key: 'value',
            header: task.objectiveLabel,
            width: '112px',
            align: 'right',
            cell: (r) => <span className={TEXT_TONE[signTone(r.value)]}>{fmt.ratio(r.value)}</span>,
          },
          ...TOP_COLUMNS.slice(1),
        ]

type Act = { action: 'runAll' } | { action: 'run' | 'pause' | 'stop' | 'remove'; task: LabTask }

export function TasksScreen() {
  const queryClient = useQueryClient()
  const list = useQuery({ queryKey: ['lab-tasks'], queryFn: labTasks.list })
  useRefetchOn('studies', ['lab-tasks'], 2_000)
  useRefetchOn('simulations', ['lab-tasks'], 5_000)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [editing, setEditing] = useState<LabTask | null>(null)
  const [confirming, setConfirming] = useState<Act | null>(null)
  const [alphaId, setAlphaId] = useState<string | null>(null)
  const [view, setView] = useState<'tasks' | 'submittable'>('tasks')

  // Nothing picked yet: open on what is running, then stay there. Re-deriving this every render
  // would move the pane out from under the reader the moment that task finished.
  const running = list.data?.tasks.find((t) => t.status === 'RUNNING')?.id ?? null
  useEffect(() => {
    if (running != null) setSelectedId((id) => id ?? running)
  }, [running])

  const act = useMutation({
    mutationFn: async (a: Act) => {
      if (a.action === 'runAll') await labTasks.runAll()
      else await labTasks[a.action](a.task.id)
    },
    onSuccess: () => {
      setConfirming(null)
      for (const key of [['lab-tasks'], ['bar'], ['today'], ['simulations']])
        void queryClient.invalidateQueries({ queryKey: key })
    },
  })

  // A failed action leaves its notice behind; the next dialog must not open wearing it.
  const ask = (a: Act) => {
    act.reset()
    setConfirming(a)
  }

  const all = list.data?.tasks ?? []
  const slots = list.data?.slots ?? 8
  const open = all.filter((t) => t.status !== 'COMPLETE' && t.status !== 'FAILED')
  const total = (tasks: LabTask[], pick: (t: LabTask) => number) =>
    tasks.reduce((n, t) => n + pick(t), 0)
  const runningCores = total(
    open.filter((t) => t.status === 'RUNNING'),
    (t) => t.cores,
  )
  const assignedCores = total(open, (t) => t.cores)
  const waiting = all.filter((t) => t.status === 'QUEUED').length
  const fresh = all.filter((t) => t.status === 'IDLE').length
  const selected = all.find((t) => t.id === selectedId) ?? null
  const copy = confirming ? confirmCopy(confirming, fresh) : null

  const columns: Column<LabTask>[] = [
    {
      key: 'task',
      header: 'Task',
      width: 'minmax(220px,2fr)',
      cell: (t) => (
        <span className="block min-w-0 truncate" title={t.datasetIds.join(', ')}>
          <span className="text-ink">
            {t.labName}
            {t.templateName ? ` · ${t.templateName}` : ''}
          </span>
          <span className="text-ink-subtle">
            {/* A sweep spans many markets, so naming the source Alpha's one would mislead. */}
            {t.lab === SETTINGS_SAMPLER ? (
              <>
                {' · '}
                <span className="num">{t.alphaId ?? DASH}</span>
                {' · '}
                <span className="num">{fmt.int(t.markets)}</span>
                {t.markets === 1 ? ' Market' : ' Markets'}
              </>
            ) : (
              <>
                {' · '}
                <span className="num">{`${t.region} D${t.delay}`}</span>
                {/* Neither seeds nor datasets: say nothing rather than report "0 datasets"
                    about something the task never had. */}
                {(t.seeds > 0 || t.datasetIds.length > 0) && (
                  <>
                    {' · '}
                    <span className="num">
                      {fmt.int(t.seeds > 0 ? t.seeds : t.datasetIds.length)}
                    </span>
                    {t.seeds > 0 ? ' seeds' : t.datasetIds.length === 1 ? ' dataset' : ' datasets'}
                  </>
                )}
              </>
            )}
          </span>
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      // Wide enough for the longest badge ("Not Started" measures 96px) plus the cell's px-3.
      width: '124px',
      cell: (t) => <TaskBadge task={t} />,
    },
    {
      key: 'cores',
      header: 'Cores',
      width: '64px',
      align: 'right',
      cell: (t) => fmt.int(t.cores),
    },
    {
      key: 'simulations',
      header: 'Simulations',
      width: 'minmax(220px,1.5fr)',
      cell: (t) => (
        <span className="flex w-full min-w-0 items-center gap-2">
          <Progress
            className="flex-1"
            value={t.target > 0 ? t.simulated / t.target : 0}
            label="Simulated"
          />
          <span className="num shrink-0 text-body-compact">
            {fmt.int(t.simulated)} / {fmt.int(t.target)}
          </span>
        </span>
      ),
    },
    {
      key: 'best',
      header: 'Best',
      width: '96px',
      align: 'right',
      cell: (t) => (
        <span title={t.objectiveLabel} className={TEXT_TONE[signTone(t.best)]}>
          {fmt.ratio(t.best)}
        </span>
      ),
    },
    {
      key: 'actions',
      header: '',
      width: '186px',
      align: 'right',
      cell: (t) => (
        <Actions
          task={t}
          onAct={(action) =>
            action === 'pause' ? act.mutate({ action, task: t }) : ask({ action, task: t })
          }
          onEdit={() => setEditing(t)}
        />
      ),
    },
  ]

  return (
    <Page>
      <PageHeader
        title="Tasks"
        actions={
          <Button
            variant="primary"
            disabled={fresh === 0}
            onClick={() => ask({ action: 'runAll' })}
          >
            <PlayIcon />
            Run All
          </Button>
        }
      />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric
          boxed
          label="Running Cores"
          value={list.isPending ? DASH : `${fmt.int(runningCores)} / ${fmt.int(slots)}`}
        />
        <Metric
          boxed
          label="Cores Assigned"
          value={list.isPending ? DASH : fmt.int(assignedCores)}
          tone={assignedCores > slots ? 'warn' : 'neutral'}
          hint={waiting > 0 ? `${fmt.int(waiting)} waiting` : undefined}
        />
        <Metric
          boxed
          label="Simulations Assigned"
          value={list.isPending ? DASH : fmt.int(total(open, (t) => t.target))}
        />
        <Metric
          boxed
          label="Simulated"
          value={list.isPending ? DASH : fmt.int(total(open, (t) => t.simulated))}
        />
      </div>
      {list.isError && all.length > 0 && (
        <ErrorNotice error={list.error} title="Could not load tasks" />
      )}
      {act.isError && confirming === null && <ErrorNotice error={act.error} />}

      <Panel
        actions={
          <Segmented
            label="View"
            items={[
              { value: 'tasks', label: 'Tasks' },
              { value: 'submittable', label: 'Submittable Alphas' },
            ]}
            value={view}
            onChange={setView}
          />
        }
      >
        {view === 'submittable' ? (
          <SubmittableAlphas onOpenAlpha={setAlphaId} />
        ) : list.data && all.length === 0 ? (
          <Empty title="No tasks yet">
            <Link to="/labs" className={LINK}>
              Open Research Labs
            </Link>
          </Empty>
        ) : (
          <DataTable
            label="Tasks"
            rows={all}
            columns={columns}
            rowKey={(t) => String(t.id)}
            onRowClick={(t) => setSelectedId(t.id)}
            loading={list.isPending}
            error={list.error}
          />
        )}
      </Panel>
      {view === 'tasks' && selected && <TaskDetail task={selected} onOpenAlpha={setAlphaId} />}

      {editing && (
        <EditTask key={editing.id} task={editing} slots={slots} onClose={() => setEditing(null)} />
      )}
      <DetailSheet alphaId={alphaId} onClose={() => setAlphaId(null)} />
      <Confirm
        open={confirming !== null}
        onOpenChange={(isOpen) => {
          if (!isOpen) {
            setConfirming(null)
            act.reset()
          }
        }}
        title={copy?.title ?? ''}
        confirmLabel={copy?.label ?? 'Confirm'}
        danger={confirming?.action === 'stop' || confirming?.action === 'remove'}
        pending={act.isPending}
        onConfirm={() => confirming && act.mutate(confirming)}
      >
        {copy?.body}
        {act.isError && <ErrorNotice error={act.error} className="mt-3" />}
      </Confirm>
    </Page>
  )
}

function confirmCopy(a: Act, fresh: number): { title: string; label: string; body?: string } {
  switch (a.action) {
    case 'runAll':
      return {
        title: `Run ${fmt.int(fresh)} ${fresh === 1 ? 'task' : 'tasks'}?`,
        label: 'Run All',
      }
    case 'run':
      if (a.task.status === 'PAUSED') return { title: 'Resume this task?', label: 'Resume' }
      if (a.task.status === 'FAILED')
        return {
          title: 'Retry this task?',
          label: 'Retry',
          body: 'Simulations it sent before it failed are scored on the way.',
        }
      return { title: 'Run this task?', label: 'Run Task' }
    case 'stop':
      // The second press, on a task that has been stopping and has not stopped. It says
      // what it will cost, because forcing gives up on simulations the quota already paid
      // for — which is the right trade only once the ordinary stop has failed.
      if (a.task.stopping)
        return {
          title: 'Force this task to stop?',
          label: 'Force Stop',
          body: 'It is waiting on simulations that have not come back. Forcing cancels what it can on BRAIN, ends the task and frees its cores. Any simulation that finishes anyway is still kept in Alphas.',
        }
      return {
        title: 'Stop this task?',
        label: 'Stop Task',
        body: 'Simulations already sent finish and are kept; the rest come off the queue.',
      }
    default:
      return {
        title: 'Remove this task?',
        label: 'Remove Task',
        body: 'The Alphas it found stay in Alphas.',
      }
  }
}

function TaskBadge({ task }: { task: LabTask }) {
  const { label, tone } =
    task.stopping && task.status === 'RUNNING'
      ? { label: 'Stopping', tone: 'warn' as const }
      : (STATUS[task.status] ?? STATUS.IDLE)
  return <Badge tone={tone}>{label}</Badge>
}

/** A failed task can run again: simulations it sent before it failed are scored then. */
const RUN_LABEL = { IDLE: 'Run', PAUSED: 'Resume', FAILED: 'Retry' } as const

function Actions({
  task,
  onAct,
  onEdit,
}: {
  task: LabTask
  onAct: (action: 'run' | 'pause' | 'stop' | 'remove') => void
  onEdit: () => void
}) {
  const { status, stopping } = task
  const finished = status === 'COMPLETE' || status === 'FAILED'
  return (
    // Inside a clickable row: a click on these must not also select the row.
    <span
      role="group"
      aria-label="Task actions"
      className="flex items-center justify-end gap-0.5"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      {(status === 'IDLE' || status === 'PAUSED' || status === 'FAILED') && (
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={RUN_LABEL[status]}
          title={RUN_LABEL[status]}
          onClick={() => onAct('run')}
        >
          <PlayIcon />
        </Button>
      )}
      {(status === 'RUNNING' || status === 'QUEUED') && !stopping && (
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Pause"
          title="Pause"
          onClick={() => onAct('pause')}
        >
          <PauseIcon />
        </Button>
      )}
      {/* Stays through `stopping`, unlike Pause and Edit. A task waiting on a simulation
          that never comes back is exactly when someone needs this button, and hiding it
          left them with a task holding cores and nothing on screen to press. */}
      {(status === 'RUNNING' || status === 'PAUSED' || status === 'QUEUED') && (
        <Button
          size="icon-sm"
          variant={stopping ? 'danger' : 'ghost'}
          aria-label={stopping ? 'Force stop' : 'Stop'}
          title={stopping ? 'Force stop' : 'Stop'}
          onClick={() => onAct('stop')}
        >
          <SquareIcon />
        </Button>
      )}
      {!finished && !stopping && (
        <Button size="icon-sm" variant="ghost" aria-label="Edit" title="Edit" onClick={onEdit}>
          <PencilIcon />
        </Button>
      )}
      {status !== 'RUNNING' && (
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Remove"
          title="Remove"
          onClick={() => onAct('remove')}
        >
          <Trash2Icon />
        </Button>
      )}
      <TaskActionsMenu task={task} />
    </span>
  )
}

/** Task actions that are not one-click enough to earn a button of their own. */
function TaskActionsMenu({ task }: { task: LabTask }) {
  const navigate = useNavigate()
  return (
    <Menu
      trigger={
        <Button size="icon-sm" variant="ghost" aria-label={`More actions for task ${task.id}`}>
          <EllipsisIcon />
        </Button>
      }
      items={[
        {
          label: 'Submission Planner',
          disabled: !task.simulated,
          onClick: () =>
            void navigate({ to: '/tools/submission-planner', search: { task: task.id } }),
        },
      ]}
    />
  )
}

function TaskDetail({
  task,
  onOpenAlpha,
}: {
  task: LabTask
  onOpenAlpha: (alphaId: string) => void
}) {
  const top = useQuery({
    queryKey: ['lab-tasks', 'top', task.id],
    // The whole sweep is worth scrolling; the table virtualises, so the rows are cheap.
    queryFn: () => labTasks.top(task.id, Math.min(Math.max(task.target, 50), 5000)),
  })
  // The Alpha the sweep came from leads and is never ranked: it is the reference, not a
  // result. Everything else arrives sorted on the objective already.
  const found = top.data ?? []
  const source = found.find((r) => r.source)
  const rows = source ? [source, ...found.filter((r) => r !== source)] : found
  // Red only where a check refuses the Alpha. A row still waiting on BRAIN is green like a
  // passing one: nothing has said no, which is the question this pane answers. Whether it is
  // submittable *yet* is the Submittable count's job, and that one does hold pending back.
  const verdict = (r: RankedAlpha) =>
    r.submittable || r.pending ? 'bg-pnl-positive-tint' : 'bg-pnl-negative-tint'
  const rowClass = (r: RankedAlpha) =>
    // The source keeps its verdict, and a heavier rule under it so the ranking below reads
    // as its own block.
    r.source ? `${verdict(r)} border-b-2 border-b-hairline-strong` : verdict(r)

  const sampler = task.lab === SETTINGS_SAMPLER
  const done = task.status === 'COMPLETE' || task.status === 'FAILED'
  // The green rows: nothing has refused them. Pending ones are in here, which is what makes
  // the figure an estimate — a check BRAIN has not run yet can still come back FAIL.
  const pending = found.filter((r) => r.pending).length
  const green = found.filter((r) => r.submittable || r.pending).length
  const red = found.length - green
  // From when it first ran, not when it was added — a task can sit idle for days. Older rows
  // predate that being recorded, so they fall back to when they were created.
  const began = Date.parse(task.startedAt ?? task.createdAt ?? '')
  const ended = task.finishedAt ? Date.parse(task.finishedAt) : Date.now()
  const elapsed = Number.isNaN(began) ? null : Math.max(0, (ended - began) / 1000)

  const title = [
    task.labName,
    task.templateName,
    task.lab === SETTINGS_SAMPLER ? task.alphaId : `${task.region} D${task.delay}`,
  ]
    .filter(Boolean)
    .join(' · ')
  const description =
    task.lab === SETTINGS_SAMPLER
      ? // Held at the source Alpha's values for every simulation in the sweep.
        `${fmt.int(task.markets)} Markets · Decay ${task.decay ?? DASH} · Truncation ${task.truncation ?? DASH} · NaN Handling ${task.nanHandling ?? DASH}`
      : task.seeds > 0
        ? `${task.universe ?? DASH} · ${fmt.int(task.seeds)} seeds · Population ${fmt.int(task.population)} · Mutation ${fmt.pct(task.mutationRate, 0)}`
        : `Decay ${task.decay ?? DASH} · ${fmt.int(task.fields)} fields · ${task.datasetIds.join(', ')}`
  const copyResults = () =>
    navigator.clipboard.writeText(resultsMarkdown(task, rows)).then(
      () => toast.success(`Copied ${fmt.int(rows.length)} results`),
      (e: unknown) => toast.error(errorMessage(e)),
    )

  return (
    <Panel
      title={title}
      description={description}
      actions={
        <>
          <Button size="sm" variant="ghost" disabled={!rows.length} onClick={copyResults}>
            <CopyIcon />
            Copy Results
          </Button>
          <TaskBadge task={task} />
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric
            boxed
            label="Simulated"
            value={`${fmt.int(task.simulated)} / ${fmt.int(task.target)}`}
            hint={task.cached > 0 ? `${fmt.int(task.cached)} from cache, no quota spent` : ''}
          />
          {/* Nothing is in flight once a task is over, so the box would only ever read 0. */}
          {!done && <Metric boxed label="In Flight" value={fmt.int(task.queued + task.running)} />}
          {sampler ? (
            <>
              {/* `~` because the pending rows counted here have checks BRAIN has not run
                  yet, any one of which can still come back FAIL. */}
              <Metric
                boxed
                tone="profit"
                label="Submittable"
                value={
                  <>
                    {pending > 0 && '~'}
                    {fmt.int(green)}
                  </>
                }
              />
              <Metric
                boxed
                tone={red > 0 ? 'loss' : 'neutral'}
                label="Failed"
                value={fmt.int(red)}
                hint={task.failed > 0 ? `${fmt.int(task.failed)} could not simulate` : ''}
              />
            </>
          ) : (
            <Metric boxed label="Failed" value={fmt.int(task.failed)} />
          )}
          <Metric
            boxed
            label="Time Elapsed"
            value={elapsed == null ? DASH : fmt.duration(elapsed)}
            hint={done ? '' : 'still running'}
          />
        </div>
        {task.message && (
          <Notice tone={task.status === 'FAILED' ? 'error' : 'info'} title={task.message} />
        )}
        {task.template && (
          <Disclosure summary="Template">
            <code className="num text-body-compact break-all text-ink">{task.template}</code>
          </Disclosure>
        )}
        {top.isError && top.data && (
          <ErrorNotice error={top.error} title="Could not load the best Alphas" />
        )}
        {/* A sweep's results are a comparison across markets, which needs more room than a
            card: the whole set, grouped by region and correlated, gets its own page. */}
        <div className="flex justify-end">
          <Button
            size="sm"
            variant="secondary"
            render={<Link to="/tasks/$taskId" params={{ taskId: String(task.id) }} />}
          >
            <ExternalLinkIcon />
            Open full results
          </Button>
        </div>
        <DataTable
          label={task.lab === SETTINGS_SAMPLER ? 'Results' : 'Top Alphas'}
          rows={rows}
          columns={topColumns(task)}
          rowKey={(r) => String(r.trialId)}
          onRowClick={(r) => r.alphaId && onOpenAlpha(r.alphaId)}
          rowClass={rowClass}
          loading={top.isPending}
          error={top.error}
          empty="No Alphas back yet."
        />
        {task.lab === SETTINGS_SAMPLER && (
          // A two-column grid rather than padded text: the equals signs line up whatever the
          // labels are and whatever the font does.
          <p className="num grid w-fit grid-cols-[auto_auto] gap-x-2 gap-y-0.5 text-body-compact text-ink-subtle">
            <span className="text-pnl-positive">GREEN</span>
            <span>= PASS or WARNING or PENDING</span>
            <span className="text-pnl-negative">RED</span>
            <span>= FAIL or ERROR</span>
          </p>
        )}
      </div>
    </Panel>
  )
}

function EditTask({ task, slots, onClose }: { task: LabTask; slots: number; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [cores, setCores] = useState(task.cores)
  const [simulations, setSimulations] = useState(String(task.target))
  const count = Number(simulations)
  // Below what it has already simulated the task is finished the moment it is saved, and the
  // count reads past its own target. Stop is the way to end a task early.
  const least = Math.max(1, task.simulated)
  const valid = Number.isInteger(count) && count >= least && count <= MAX_SIMULATIONS
  const change = useMutation({
    mutationFn: () => labTasks.change(task.id, { cores, simulations: count }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['lab-tasks'] })
      onClose()
    },
  })

  return (
    <Dialog
      open
      onOpenChange={(isOpen) => !isOpen && onClose()}
      title="Edit Task"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!valid}
            loading={change.isPending}
            onClick={() => change.mutate()}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Fieldset legend="Cores">
          <Segmented
            label="Cores"
            items={Array.from({ length: slots }, (_, i) => ({ value: i + 1, label: i + 1 }))}
            value={cores}
            onChange={setCores}
          />
        </Fieldset>
        <Field
          label="Simulations"
          hint={
            task.simulated > 0 && (
              <>
                <span className="num">{fmt.int(task.simulated)}</span> simulated so far: the least
                it can be set to
              </>
            )
          }
        >
          <Input
            type="number"
            min={least}
            max={MAX_SIMULATIONS}
            step={1}
            className="w-40"
            value={simulations}
            onChange={(e) => setSimulations(e.target.value)}
          />
        </Field>
        {change.isError && <ErrorNotice error={change.error} title="Could not change the task" />}
      </div>
    </Dialog>
  )
}
