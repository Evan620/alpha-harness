/**
 * Types shared by more than one screen; screen-specific contracts live next to their endpoints
 * in `api/<domain>.ts`. Turnover, returns, drawdown, margin, coverage and truncation are
 * FRACTIONS (0.64 = 64%), Sharpe and Fitness are plain ratios, and a timestamp without an
 * offset is UTC.
 */

import type { components } from './generated.ts'
import { qs } from './http.ts'

/** The four-part address almost every lab and catalog call takes. camelCase in the UI. */
export interface Scope {
  instrumentType: string
  region: string
  delay: number
  universe: string
}

/** The same scope for routes whose bodies accept snake_case only. */
export interface ScopeBody {
  instrument_type: string
  region: string
  delay: number
  universe: string
}

export const toScopeBody = (scope: Scope): ScopeBody => ({
  instrument_type: scope.instrumentType,
  region: scope.region,
  delay: scope.delay,
  universe: scope.universe,
})

/** A scope in the query string. Every route that reads one takes ``instrumentType``. */
export const scopeQs = (
  scope: Partial<Scope> | undefined,
  extra: Record<string, string | number | boolean | null | undefined> = {},
): string =>
  qs({
    region: scope?.region,
    delay: scope?.delay,
    universe: scope?.universe,
    instrumentType: scope?.instrumentType,
    ...extra,
  })

export const scopeLabel = (scope: Scope): string =>
  `${scope.region} · D${scope.delay} · ${scope.universe}`

/** BRAIN simulation settings, as BRAIN names them. */
export interface SimulationSettings {
  instrumentType?: string
  region: string
  universe: string
  delay: number
  decay?: number
  neutralization?: string
  truncation?: number
  pasteurization?: string
  unitHandling?: string
  nanHandling?: string
  language?: string
  visualization?: boolean
  testPeriod?: string | null
  maxTrade?: string | null
  maxPosition?: string | null
  [extra: string]: unknown
}

/** Generated from the backend's response models (`src/api/generated.ts`). */
type Schemas = components['schemas']

export type SimStatus = Schemas['SimStatus']

/**
 * One simulation record. A multi-simulation parent holds one of the 8 slots. `settings`
 * is narrowed here: the backend passes BRAIN's settings through untyped.
 */
export type SimulationRow = Omit<Schemas['SimulationRow'], 'settings'> & {
  settings: SimulationSettings | null
}

export type EngineStatus = Schemas['EngineStatus']

export type CheckResult = 'PASS' | 'FAIL' | 'PENDING' | 'WARNING' | 'ERROR'

export interface AlphaCheck {
  name: string
  result: CheckResult | null
  /** A threshold and a measurement on most checks, but not all: some carry a neutralization
   * name, some a list of pool names. Guard with `isNum` before doing arithmetic. */
  limit?: unknown
  value?: unknown
  message?: string | null
  [extra: string]: unknown
}

export type LLMUsage = Schemas['LLMUsage']

/** One market's state inside a whole-catalog sync, for the sync matrix. */
export type SyncMarket = Schemas['SyncMarket']

/** One catalog download. The live `sync` feed adds progress for a whole-catalog run. */
export type SyncRun = Schemas['SyncRunRow'] & {
  stage?: 'fields' | 'details'
  scopesDone?: number
  scopesTotal?: number
  markets?: SyncMarket[]
}

export type Session = Schemas['Session']

export type Today = Schemas['Today']

export type BarStatus = Schemas['Bar']

export type BackgroundTask = Schemas['BackgroundTask']

export type TasksSummary = Schemas['TasksSummary']
