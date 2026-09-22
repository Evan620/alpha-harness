/** Correlation Breaker: re-shape one Alpha's expression, holding its settings still. */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type BreakerPlan = Schemas['BreakerPlan']
export type BreakerRecipe = Schemas['BreakerRecipe']
export type BreakerSettings = Schemas['BreakerSettings']

export interface BreakerRequest {
  alphaId: string
  /** Empty runs every recipe the plan offers. */
  recipes: string[]
  cores: number
}

const B = '/api/tools/correlation-breaker'

export const correlationBreaker = {
  /** Free: reads the Alpha and the catalog, simulates nothing. */
  preview: (body: BreakerRequest) => http.post<BreakerPlan>(`${B}/preview`, body),
  addTask: (body: BreakerRequest) => http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
