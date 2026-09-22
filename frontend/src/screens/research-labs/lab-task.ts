/** What Search Lab and Template Lab share around a task: its draft, market and datasets. */

import { useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { useEffect, useMemo } from 'react'
import { catalog } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { useScope } from '@/lib/scope'
import { type PickFrom, useDatasetPick } from '@/screens/data/dataset-pick'

export interface LabDraft {
  region: string
  delay: number
  universe: string
  datasetIds: string[]
  cores: number
  /** `null` until the user assigns them: a task always has simulations chosen on purpose. */
  simulations: number | null
  decay: number
  /** `null` until the user chooses: then the lab allows `vec_avg`. */
  vectorOperators: string[] | null
  /** Empty leaves the lab on its own four group neutralizations. */
  neutralizations: string[]
}

export const MAX_SIMULATIONS = 100_000

/** Matches `labs.search.MAX_CORES`: a task may hold every slot the engine has. */
export const CORES = [1, 2, 3, 4, 5, 6, 7, 8]

/** A draft's market and datasets: dataset names, and the round trip to the Data Explorer to choose them. */
type LabMarket = Pick<LabDraft, 'region' | 'delay' | 'universe' | 'datasetIds'>

export function useLabMarket(
  draft: LabMarket,
  set: (change: Partial<LabMarket>) => void,
  from: PickFrom,
) {
  const navigate = useNavigate()
  const [, setDataScope] = useScope('data')
  const scope: Scope = {
    instrumentType: 'EQUITY',
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
  }
  const chosen = draft.datasetIds.length > 0

  // Back from the Data Explorer with a finished pick for this lab.
  useEffect(() => {
    const pick = useDatasetPick.getState().take(from)
    if (pick)
      set({
        region: pick.scope.region,
        delay: pick.scope.delay,
        universe: pick.scope.universe,
        datasetIds: pick.ids,
      })
  }, [from, set])

  const datasets = useQuery({
    queryKey: ['catalog', 'datasets', scope, ''],
    queryFn: () => catalog.datasets(scope),
    enabled: chosen,
  })
  const names = useMemo(
    () => new Map((datasets.data ?? []).map((d) => [d.dataset_id, d.name ?? d.dataset_id])),
    [datasets.data],
  )

  const choose = () => {
    useDatasetPick.getState().start(scope, draft.datasetIds, from)
    setDataScope(scope)
    void navigate({ to: '/data/$tab', params: { tab: 'fields' } })
  }
  return { chosen, names, choose }
}

/** `vec_avg` until the user chooses vector operators. */
export function vectorOperatorsOf(draft: LabDraft, available: string[] | undefined): string[] {
  return draft.vectorOperators ?? (available?.includes('vec_avg') ? ['vec_avg'] : [])
}

/** The market and settings both labs send to preview a task. */
export function labBody(draft: LabDraft, vectorOperators: string[]) {
  return {
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
    dataset_ids: draft.datasetIds,
    vector_operators: vectorOperators,
    neutralizations: draft.neutralizations,
    decay: draft.decay,
    cores: draft.cores,
  }
}

export function simulationsValid(draft: LabDraft, maxSimulations: number): boolean {
  return draft.simulations !== null && draft.simulations >= 1 && draft.simulations <= maxSimulations
}
