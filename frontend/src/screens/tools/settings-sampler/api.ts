/** Settings Sampler: where one proven expression could also run, and queueing it there. */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type SettingsPlan = Schemas['SettingsPlan']
export type RegionPlan = Schemas['RegionPlan']
export type MarketRow = Schemas['MarketRow']
export type Pair = Schemas['Pair']

export interface MarketPick {
  region: string
  delay: number
  universe: string
}

/** How every simulation in the sweep is held, whatever market it lands in. Each is optional:
 * left out, an Alpha's own value stands, or the platform default when there is no Alpha. */
export interface Holding {
  decay?: number
  truncation?: number
  nanHandling?: 'ON' | 'OFF'
  /** `P0Y0M0D` to `P6Y0M0D`, BRAIN's own bounds. */
  testPeriod?: string
}

/** Where the sweep's expression comes from: an Alpha, or an expression typed in. */
export type Source = ({ alphaId: string } | { expression: string }) & Holding

export type SampleRequest = Source & {
  /** Empty means every market the plan offers; likewise for each filter below. */
  markets: MarketPick[]
  neutralizations: string[]
  pairs: Pair[]
  /** Concurrent slots the task holds; ten simulations ride in each. */
  cores: number
}

const B = '/api/tools/settings-sampler'

export const settingsSampler = {
  /** Free: reads the Alpha (if any) and the catalog, simulates nothing. */
  preview: (source: Source) => http.post<SettingsPlan>(`${B}/preview`, source),
  addTask: (body: SampleRequest) => http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}

export const marketKey = (m: MarketPick) => `${m.region}|${m.delay}|${m.universe}`
export const pairLabel = (p: Pair) =>
  p.maxTrade === 'OFF' && p.maxPosition === 'OFF'
    ? 'Neither'
    : p.maxTrade === 'ON'
      ? 'Max Trade'
      : 'Max Position'
