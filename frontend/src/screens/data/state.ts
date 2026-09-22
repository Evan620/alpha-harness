/** Non-component pieces the Data Explorer tabs share: the Fields filter store and helpers. */

import { create } from 'zustand'
import type { FieldFilter, FieldSortKey } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { cn } from '@/lib/cn'
import { DASH, fmt, isNum } from '@/lib/format'
import { useDatasetPick } from '@/screens/data/dataset-pick'
import { STATUS } from '@/ui/kit'
import type { Sort } from '@/ui/table'

/** The kit's STATUS box, one size down for the dense Data Explorer rows. */
export const STAT = cn(STATUS, 'inline-flex h-6 px-2')

export type FieldFilterState = Omit<FieldFilter, 'sort_by' | 'sort_desc' | 'limit' | 'offset'>

interface FieldFilterStore {
  filter: FieldFilterState
  sort: Sort
  /**
   * Whether the reader picked {@link sort} themselves. Recorded rather than inferred: the
   * default *is* Alphas descending, so clicking that column is indistinguishable from never
   * having clicked at all.
   */
  chosen: boolean
  offset: number
  set: (change: Partial<FieldFilterState>) => void
  replace: (filter: FieldFilterState) => void
  setSort: (sort: Sort) => void
  /** Order by how well each row answers the search — the way back from a column. */
  rank: () => void
  page: (offset: number) => void
}

/** What a search is sorted by until the reader picks a column, and what it goes back to. */
export const RELEVANCE = 'relevance'
const DEFAULT_SORT: Sort = { key: 'alpha_count', desc: true }

/**
 * The sort a change to the search box leaves behind.
 *
 * Relevance exists only while a smart search is running, so it has to give way when the box
 * empties or the mode turns exact. A column the reader picked themselves outlives all of
 * that: switching modes to compare two readings of the same search must not silently
 * re-sort the table under them.
 */
function sortAfter(previous: FieldFilterStore, filter: FieldFilterState): Sort {
  const rankable = !!filter.search && (filter.search_mode ?? 'smart') === 'smart'
  const { sort, chosen } = previous
  if (sort.key === RELEVANCE) return rankable ? sort : DEFAULT_SORT
  return rankable && !chosen ? { key: RELEVANCE, desc: true } : sort
}

/** Lives outside the Fields tab so the filter survives leaving the Data Explorer and coming back. */
export const useFieldFilter = create<FieldFilterStore>()((set) => ({
  filter: {},
  sort: DEFAULT_SORT,
  chosen: false,
  offset: 0,
  set: (change) =>
    set((s) => {
      const filter = { ...s.filter, ...change }
      // Emptying the box ends the search the column was picked for, so the next one is free
      // to rank itself again. Without this, one column click silences ranking for the session.
      const cleared = 'search' in change && !change.search && !!s.filter.search
      const chosen = cleared ? false : s.chosen
      return { filter, chosen, sort: sortAfter({ ...s, chosen }, filter), offset: 0 }
    }),
  // Clearing the filters clears how they were ordered too, so the next search can rank again.
  replace: (filter) => set({ filter, sort: DEFAULT_SORT, chosen: false, offset: 0 }),
  setSort: (sort) =>
    set({
      sort: { key: sort.key as FieldSortKey, desc: sort.desc },
      chosen: true,
      offset: 0,
    }),
  // Not `chosen`: asking for the best match is handing the ordering back to the search, so a
  // later search ranks itself again instead of being held to this one.
  rank: () => set({ sort: { key: RELEVANCE, desc: true }, chosen: false, offset: 0 }),
  page: (offset) => set({ offset }),
}))

const NONE: string[] = []

/**
 * The datasets the Fields tab filters on, and how to change them: while a lab picks datasets,
 * that pick (kept across a reload); otherwise the Fields filter's own.
 */
export function useDatasetChoice(): [string[], (ids: string[]) => void] {
  const picking = useDatasetPick((s) => s.active)
  const picked = useDatasetPick((s) => s.ids)
  const filtered = useFieldFilter((s) => s.filter.dataset_ids ?? NONE)
  if (picking) {
    return [
      picked,
      (ids) => {
        useDatasetPick.getState().setIds(ids)
        useFieldFilter.getState().page(0)
      },
    ]
  }
  return [filtered, (ids) => useFieldFilter.getState().set({ dataset_ids: ids })]
}

export const isActive = (v: unknown) =>
  v != null && v !== '' && v !== false && !(Array.isArray(v) && v.length === 0)

export const sameScope = (a: Scope, b: Scope) =>
  a.region === b.region &&
  a.delay === b.delay &&
  a.universe === b.universe &&
  a.instrumentType === b.instrumentType

/** `×1.4` */
export const multiplier = (v: number | null | undefined) =>
  isNum(v) ? `×${fmt.ratio(v, 1)}` : DASH

/** Client-side sort for tables the backend returns whole. Absent values last. */
export function sortRows<T>(rows: T[], sort: Sort): T[] {
  const key = sort.key as keyof T
  return [...rows].sort((a, b) => {
    const x = a[key]
    const y = b[key]
    if (x == null) return y == null ? 0 : 1
    if (y == null) return -1
    const c = x < y ? -1 : x > y ? 1 : 0
    return sort.desc ? -c : c
  })
}

/** `themes` is JSON array text of strings or `{id,name}` objects. */
export function parseThemes(raw: string | null): string[] {
  if (!raw) return []
  try {
    const list: unknown = JSON.parse(raw)
    if (!Array.isArray(list)) return []
    return list.map((t) =>
      typeof t === 'string'
        ? t
        : ((t as { name?: string; id?: string })?.name ??
          (t as { id?: string })?.id ??
          JSON.stringify(t)),
    )
  } catch {
    return [raw]
  }
}
