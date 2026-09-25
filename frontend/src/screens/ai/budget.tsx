import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { today } from '@/api/core'
import { fmt } from '@/lib/format'
import { type LLMKey, type LLMModel, quotaDay } from '@/screens/ai/api'
import { Badge, Empty, ErrorNotice, LINK, Metric, Panel, Progress, Skeleton } from '@/ui/kit'
import { type Column, DataTable } from '@/ui/table'
import { useCountdown, useKeys, useModels, useProviderLabel } from './shared'

interface KeyModelRow {
  key: LLMKey
  model: LLMModel
  requests: number
  tokens: number
}

export function Budget() {
  const keys = useKeys(60_000)
  const models = useModels()
  // Same key and query as the header clock, so this costs nothing extra.
  const bar = useQuery({
    queryKey: ['bar'],
    queryFn: () => today.bar(),
    refetchInterval: 30_000,
  })
  const providerLabel = useProviderLabel()
  const pacific = useCountdown(keys.data?.resetInSeconds, keys.dataUpdatedAt)
  const eastern = useCountdown(bar.data?.resetsInSeconds, bar.dataUpdatedAt)

  if (keys.isError) return <ErrorNotice title="Could not load the budget" error={keys.error} />
  if (!keys.data) return <Skeleton className="h-96" />

  const status = keys.data
  const enabledFor = (provider: string) =>
    status.keys.filter((k) => k.enabled && k.provider === provider).length
  const bulkBudget = status.budget.filter((b) => b.bulk)
  const bulkUncapped = bulkBudget.some((b) => b.unlimited)
  const bulkLeft = bulkBudget.reduce((s, b) => s + b.remainingToday, 0)
  const day = quotaDay(status.quotaTimezone)

  const perKey: KeyModelRow[] = status.keys
    .filter((k) => k.enabled)
    .flatMap((k) =>
      (models.data?.models ?? [])
        .filter((m) => m.provider === k.provider)
        .map((m) => {
          const usage = k.usage.filter((u) => u.day === day && u.model === m.id)
          return {
            key: k,
            model: m,
            requests: usage.reduce((s, u) => s + u.requests, 0),
            tokens: usage.reduce((s, u) => s + u.tokens, 0),
          }
        }),
    )

  const keyColumns: Column<KeyModelRow>[] = [
    {
      key: 'key',
      header: 'Key',
      width: 'minmax(140px,1fr)',
      cell: (r) => (
        <span className="truncate">{r.key.label || <span className="num">{r.key.hint}</span>}</span>
      ),
    },
    {
      key: 'model',
      header: 'Model',
      width: 'minmax(160px,1.2fr)',
      cell: (r) => r.model.label,
    },
    {
      key: 'rpd',
      header: 'Requests left / RPD',
      width: 'minmax(200px,1.2fr)',
      cell: (r) => {
        // A daily ceiling of zero means the provider imposes none (a paid account), so there
        // is nothing to count down: say so rather than showing an empty bar at 0 / 0.
        if (r.model.rpd <= 0) {
          return (
            <span className="text-body-compact text-ink-muted">
              No daily cap, {fmt.int(r.requests)} used
            </span>
          )
        }
        const left = Math.max(0, r.model.rpd - r.requests)
        return (
          <div className="flex w-full items-center gap-2">
            <Progress
              className="flex-1"
              value={left / r.model.rpd}
              label={`${r.model.label} requests left`}
            />
            <span className="num shrink-0 text-body-compact text-ink-muted">
              {fmt.int(left)} / {fmt.int(r.model.rpd)}
            </span>
          </div>
        )
      },
    },
    {
      key: 'rpm',
      header: 'RPM',
      width: '70px',
      align: 'right',
      cell: (r) => fmt.int(r.model.rpm),
    },
    {
      key: 'tpm',
      header: 'TPM',
      width: '80px',
      align: 'right',
      cell: (r) => fmt.compact(r.model.tpm),
    },
    {
      key: 'tokens',
      header: 'Tokens today',
      width: '110px',
      align: 'right',
      cell: (r) => fmt.int(r.tokens),
    },
  ]

  const modelColumns: Column<LLMModel>[] = [
    {
      key: 'provider',
      header: 'Provider',
      width: 'minmax(120px,0.8fr)',
      cell: (m) => providerLabel(m.provider),
    },
    {
      key: 'model',
      header: 'Model',
      width: 'minmax(180px,1.2fr)',
      cell: (m) => <span title={m.id}>{m.label}</span>,
    },
    {
      key: 'id',
      header: 'Id',
      width: 'minmax(160px,1fr)',
      cell: (m) => <span className="num text-body-compact text-ink-subtle">{m.id}</span>,
    },
    {
      key: 'kind',
      header: 'Kind',
      width: '90px',
      cell: (m) => <span className="text-ink-subtle">{m.kind}</span>,
    },
    {
      key: 'rpm',
      header: 'RPM',
      width: '70px',
      align: 'right',
      cell: (m) => fmt.int(m.rpm),
    },
    {
      key: 'tpm',
      header: 'TPM',
      width: '80px',
      align: 'right',
      cell: (m) => fmt.int(m.tpm),
    },
    {
      key: 'rpd',
      header: 'RPD',
      width: '80px',
      align: 'right',
      cell: (m) => (m.rpd > 0 ? fmt.int(m.rpd) : '—'),
    },
    {
      key: 'notes',
      header: 'Notes',
      width: 'minmax(200px,1fr)',
      cell: (m) => (
        <div className="flex min-w-0 gap-1 overflow-hidden">
          {m.recommended && <Badge>Recommended</Badge>}
          {m.bulk && <Badge tone="outline">Bulk</Badge>}
          {m.discovered && <Badge tone="warn">Limits guessed</Badge>}
        </div>
      ),
    },
  ]

  return (
    <div className="flex flex-col gap-3">
      <Panel>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric
            label="Assistant requests left today"
            value={bulkUncapped ? 'No cap' : fmt.int(bulkLeft)}
            hint="Bulk models, across every enabled Key."
          />
          <Metric
            label="Google AI Studio quota resets in"
            value={fmt.countdown(pacific)}
            hint={`00:00 US Pacific (${status.quotaTimezone}). Assistant Keys only.`}
          />
          <Metric
            label="BRAIN simulations reset in"
            value={bar.isError ? '—' : fmt.countdown(eastern)}
            hint="00:00 US Eastern. A separate clock: the 5,000 daily simulations."
          />
          <Metric
            label="Enabled Keys"
            value={`${fmt.int(status.enabled)} / ${fmt.int(status.keys.length)}`}
            hint="Each Key adds its own daily allowance."
          />
        </div>
      </Panel>

      <Panel
        title="Remaining today, per model"
        description="Requests left across every enabled Key, against what those Keys allow in a day."
      >
        {status.budget.length === 0 ? (
          <Empty title="No budget without an enabled Key">
            Add a free Key and its daily allowance appears here.{' '}
            <Link to="/ai/$tab" params={{ tab: 'providers' }} className={LINK}>
              Choose a provider
            </Link>
          </Empty>
        ) : (
          <div className="grid grid-cols-1 gap-x-6 gap-y-3 md:grid-cols-2">
            {status.budget.map((b) => {
              const total = b.perKeyPerDay * enabledFor(b.provider)
              return (
                <div key={`${b.provider}/${b.model}`} className="flex flex-col gap-1.5">
                  <div className="flex items-center justify-between gap-2 text-body-compact">
                    <span className="flex min-w-0 items-center gap-2">
                      <span className="truncate text-ink">{b.label}</span>
                      <span className="shrink-0 text-ink-subtle">{providerLabel(b.provider)}</span>
                      {b.bulk && <Badge tone="outline">Bulk</Badge>}
                    </span>
                    <span className="num text-ink-muted">
                      {b.unlimited
                        ? 'No daily cap'
                        : `${fmt.int(b.remainingToday)} / ${fmt.int(total)}`}
                    </span>
                  </div>
                  {!b.unlimited && (
                    <Progress
                      value={total ? b.remainingToday / total : null}
                      label={`${b.label} requests left`}
                    />
                  )}
                </div>
              )
            })}
          </div>
        )}
      </Panel>

      <Panel
        title="Per Key"
        description="Each enabled Key's own allowance for every model its provider offers. RPM and TPM are per-minute ceilings."
        bodyClassName="p-0"
      >
        {models.isError ? (
          <ErrorNotice className="m-4" title="Could not load the models" error={models.error} />
        ) : (
          <DataTable
            label="Budget per Key and model"
            rows={perKey}
            columns={keyColumns}
            rowKey={(r) => `${r.key.id}/${r.model.id}`}
            loading={models.isLoading}
            empty="No enabled Keys."
            maxHeight="50vh"
          />
        )}
      </Panel>

      <Panel
        title="Model limits"
        description={
          models.data && (
            <>
              {models.data.note} Chat default{' '}
              <span className="num">{models.data.defaults.chat}</span>, careful work{' '}
              <span className="num">{models.data.defaults.deep}</span>. Limits are per Key.
            </>
          )
        }
        bodyClassName="p-0"
      >
        {models.isError ? (
          <ErrorNotice className="m-4" title="Could not load the models" error={models.error} />
        ) : (
          <DataTable
            label="Model limits"
            rows={models.data?.models ?? []}
            columns={modelColumns}
            rowKey={(m) => `${m.provider}/${m.id}`}
            loading={models.isLoading}
            maxHeight="60vh"
          />
        )}
      </Panel>
    </div>
  )
}
