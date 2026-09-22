/** LLM Power Pool Lab: an LLM writes Power Pool Alphas for your datasets while the task runs in Tasks. */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { PlusIcon } from 'lucide-react'
import { toast } from 'sonner'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { errorMessage } from '@/api/http'
import { fmt } from '@/lib/format'
import { DEFAULT_SCOPE, useScopeOptions } from '@/lib/scope'
import { useDebounced } from '@/lib/use-debounced'
import { CORES, MAX_SIMULATIONS, useLabMarket } from '@/screens/research-labs/lab-task'
import { NeutralizationPicker } from '@/screens/research-labs/neutralization'
import { type PowerPoolRequest, powerPoolLab } from '@/screens/research-labs/power-pool/api'
import { DatasetsPanel, Setting } from '@/screens/research-labs/task-settings'
import {
  Button,
  Disclosure,
  ErrorNotice,
  Input,
  Metric,
  Notice,
  Page,
  PageHeader,
  Panel,
  Segmented,
} from '@/ui/kit'
import { Select } from '@/ui/overlay'

interface PowerPoolDraft {
  region: string
  delay: number
  universe: string
  datasetIds: string[]
  cores: number
  simulations: number | null
  model: string | null
  /** Empty keeps every neutralization BRAIN offers for the market. */
  neutralizations: string[]
}

const useDraft = create<PowerPoolDraft & { set: (change: Partial<PowerPoolDraft>) => void }>()(
  persist(
    (set) => ({
      region: DEFAULT_SCOPE.region,
      delay: DEFAULT_SCOPE.delay,
      universe: DEFAULT_SCOPE.universe,
      datasetIds: [],
      cores: 4,
      simulations: null,
      model: null,
      neutralizations: [],
      set: (change) => set(change),
    }),
    { name: 'alpha-harness-power-pool-lab' },
  ),
)

const PRE =
  'num max-h-80 overflow-auto rounded-md border border-hairline bg-canvas p-3 text-body-compact whitespace-pre-wrap text-ink-muted'

export function PowerPoolLabScreen() {
  const draft = useDraft()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { names, choose } = useLabMarket(draft, draft.set, '/labs/power-pool')
  const options = useQuery({
    queryKey: ['power-pool-lab', 'options'],
    queryFn: powerPoolLab.options,
  })
  const models = options.data?.models ?? []
  const model =
    draft.model && models.some((m) => m.id === draft.model)
      ? draft.model
      : (options.data?.defaultModel ?? null)

  // BRAIN's legal list for this market; the LLM draws from whatever is chosen, or all of it.
  const scopeOptions = useScopeOptions({
    instrumentType: 'EQUITY',
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
  })

  const body: PowerPoolRequest = {
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
    dataset_ids: draft.datasetIds,
    model,
    neutralizations: draft.neutralizations,
    cores: draft.cores,
    simulations: draft.simulations ?? 0,
  }
  const key = JSON.stringify(body)
  const settled = useDebounced(key, 300)
  const preview = useQuery({
    queryKey: ['power-pool-lab', 'preview', settled],
    queryFn: () => powerPoolLab.preview(JSON.parse(settled) as PowerPoolRequest),
    enabled: settled === key,
    placeholderData: keepPreviousData,
  })
  const plan = preview.data
  const maxSimulations = options.data?.maxSimulations ?? MAX_SIMULATIONS
  const sims = draft.simulations
  const add = useMutation({
    mutationFn: () => powerPoolLab.addTask(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['lab-tasks'] })
      toast.success('Task Added', {
        action: {
          label: 'Open Tasks',
          onClick: () => void navigate({ to: '/tasks' }),
        },
      })
    },
    onError: (error) => toast.error(errorMessage(error)),
  })
  const ready =
    plan !== undefined &&
    settled === key &&
    !preview.isFetching &&
    plan.problems.length === 0 &&
    sims !== null &&
    sims >= 1 &&
    sims <= maxSimulations

  return (
    <Page>
      <PageHeader
        title="LLM Power Pool Lab"
        actions={
          <Button
            variant="primary"
            disabled={!ready}
            loading={add.isPending}
            onClick={() => add.mutate()}
          >
            <PlusIcon />
            Add Task
          </Button>
        }
      />
      {options.isError && <ErrorNotice error={options.error} title="Could not load the models" />}
      {options.isSuccess && models.length === 0 && (
        <Notice tone="warn" title="Add a Key in LLM Integration to use this lab." />
      )}
      <DatasetsPanel
        ids={draft.datasetIds}
        names={names}
        onChoose={choose}
        onRemove={(id) => draft.set({ datasetIds: draft.datasetIds.filter((x) => x !== id) })}
      />
      <Panel title="Settings">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
            <Setting label="Model">
              <Select
                label="Model"
                items={models.map((m) => ({
                  value: m.id,
                  label: `${m.label} · ${fmt.int(m.remainingToday)} left today`,
                }))}
                value={model}
                onChange={(v) => draft.set({ model: v })}
              />
            </Setting>
            <Setting label="Cores">
              <Segmented
                label="Cores"
                items={CORES.map((v) => ({ value: v, label: v }))}
                value={draft.cores}
                onChange={(cores) => draft.set({ cores })}
              />
            </Setting>
            <Setting label="Simulations">
              <Input
                type="number"
                min={1}
                max={maxSimulations}
                placeholder="500"
                aria-label="Simulations"
                className="w-32"
                value={sims ?? ''}
                onChange={(e) => {
                  const n = Number(e.target.value)
                  draft.set({
                    simulations:
                      e.target.value === '' || !Number.isFinite(n)
                        ? null
                        : Math.max(0, Math.floor(n)),
                  })
                }}
              />
            </Setting>
          </div>
          {scopeOptions.neutralizations.length > 0 && (
            <NeutralizationPicker
              available={scopeOptions.neutralizations}
              value={draft.neutralizations}
              onChange={(next) => draft.set({ neutralizations: next })}
              hint="None chosen draws from every one BRAIN offers here."
            />
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric boxed label="Datasets" value={fmt.int(draft.datasetIds.length)} />
            <Metric boxed label="Fields" value={fmt.int(plan?.fields)} />
            <Metric boxed label="LLM Calls" value={fmt.int(plan?.llmCalls)} hint="20 Alphas each" />
            <Metric
              boxed
              label="Universes"
              value={fmt.int(plan?.universes.length)}
              hint={`${fmt.int(plan?.neutralizations.length)} Neutralizations`}
            />
          </div>
          <p className="text-body-compact text-pretty text-ink-subtle">
            Each Alpha gets a random Universe, Neutralization and Decay; Truncation 0.08.
          </p>
          {preview.isError && <ErrorNotice error={preview.error} title="Could not plan the task" />}
          {plan?.problems.map((m) => (
            <Notice key={m} tone="error" title={m} />
          ))}
          {plan?.warnings.map((m) => (
            <Notice key={m} tone="warn" title={m} />
          ))}
          {plan?.prompt && (
            <Disclosure summary={`Prompt · ~${fmt.int(plan.prompt.tokens)} tokens`}>
              <div className="flex flex-col gap-2">
                <pre className={PRE} role="region" aria-label="System prompt">
                  {plan.prompt.system}
                </pre>
                <pre className={PRE} role="region" aria-label="User prompt">
                  {plan.prompt.user}
                </pre>
              </div>
            </Disclosure>
          )}
        </div>
      </Panel>
    </Page>
  )
}
