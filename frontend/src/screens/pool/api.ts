/**
 * Alphas (spec §4.5): the local store of Alphas, their submission checks and correlations.
 * Request bodies are snake_case ONLY — camelCase keys are silently dropped — and nothing here
 * submits an alpha.
 */

import type { components } from '@/api/generated'
import { http, qs } from '@/api/http'
import type { AlphaCheck, Scope } from '@/api/types'

export type AlphaMetricKey =
  | 'sharpe'
  | 'fitness'
  | 'turnover'
  | 'returns'
  | 'drawdown'
  | 'margin'
  | 'operator_count'
  | 'k_ratio'
  | 'calmar'
export type AlphaSortKey = AlphaMetricKey | 'date_created' | 'date_submitted'

export interface AlphaPageRequest {
  submitted?: boolean
  sort_by?: AlphaSortKey
  sort_desc?: boolean
  regions?: string[] | null
  delays?: number[] | null
  universes?: string[] | null
  minimum?: Partial<Record<AlphaMetricKey, number>>
  maximum?: Partial<Record<AlphaMetricKey, number>>
  search?: string | null
  /** Only Alphas the Evolution Lab can breed from, for when this table picks seeds. */
  evolvable?: boolean
  /** 1..500 */
  limit?: number
  offset?: number
}

type Schemas = components['schemas']

export type VaultOverview = Schemas['VaultOverview']
export type AlphaRow = Schemas['AlphaRow']
export type AlphaPage = Schemas['AlphaPage']
export type AlphaSettings = Schemas['AlphaSettings']
export type AlphaDetail = Omit<Schemas['AlphaDetail'], 'checks'> & { checks: AlphaCheck[] }
export type SubmittableAlpha = Omit<Schemas['SubmittableAlpha'], 'checks'> & {
  checks: AlphaCheck[]
}
export type SubmittableResponse = Omit<Schemas['SubmittableResponse'], 'alphas' | 'shortlist'> & {
  alphas: SubmittableAlpha[]
  shortlist: SubmittableAlpha[]
}

export interface BrainCorrelation {
  schema?: {
    name?: string
    title?: string
    properties: { name: string; title?: string; type?: string }[]
  }
  records?: unknown[][]
  min?: number
  max?: number
  [k: string]: unknown
}

export const BRAIN_ALPHA_URL = (alphaId: string) =>
  `https://platform.worldquantbrain.com/alpha/${alphaId}`

export const pool = {
  overview: () => http.get<VaultOverview>('/api/vault'),
  /** Raw BRAIN counts by stage and status. One BRAIN read. */
  summary: () => http.get<Record<string, number>>('/api/alphas/summary'),
  sync: () => http.post<Schemas['SyncStarted']>('/api/vault/sync'),
  query: (body: AlphaPageRequest) => http.post<AlphaPage>('/api/vault/alphas/query', body),
  detail: (alphaId: string) =>
    http.get<AlphaDetail>(`/api/vault/alphas/${encodeURIComponent(alphaId)}/detail`),
  /** ≤ 100 ids; downloads daily PnL where missing, in the background. */
  kRatio: (alphaIds: string[]) =>
    http.post<Schemas['KRatioStarted']>('/api/vault/alphas/k-ratio', {
      alpha_ids: alphaIds,
    }),
  submittable: (scope: Scope, limit = 200) =>
    http.get<SubmittableResponse>(
      `/api/vault/submittable${qs({ region: scope.region, delay: scope.delay, universe: scope.universe, instrument_type: scope.instrumentType, limit })}`,
    ),
  /** Re-runs the submission checks on BRAIN without submitting. */
  check: (alphaId: string) =>
    http.get<{ is?: { checks?: AlphaCheck[] } }>(
      `/api/alphas/${encodeURIComponent(alphaId)}/check`,
    ),
  /** A slow, rate-limited BRAIN job. */
  correlations: (alphaId: string, kind: 'self' | 'prod') =>
    http.get<BrainCorrelation & { fetchedAt: string }>(
      `/api/alphas/${encodeURIComponent(alphaId)}/correlations/${kind}`,
    ),
}
