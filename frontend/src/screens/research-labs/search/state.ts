/** The Search Lab's choices, kept between visits. */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { DEFAULT_SCOPE } from '@/lib/scope'

export interface SearchLabDraft {
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

export const useSearchLab = create<
  SearchLabDraft & { set: (change: Partial<SearchLabDraft>) => void }
>()(
  persist(
    (set) => ({
      region: DEFAULT_SCOPE.region,
      delay: DEFAULT_SCOPE.delay,
      universe: DEFAULT_SCOPE.universe,
      datasetIds: [],
      cores: 4,
      simulations: null,
      decay: 0,
      vectorOperators: null,
      neutralizations: [],
      set: (change) => set(change),
    }),
    { name: 'alpha-harness-search-lab' },
  ),
)
