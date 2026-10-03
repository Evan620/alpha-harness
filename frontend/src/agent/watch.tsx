/**
 * The watch indicator, like Claude Code's background-task count: a chip that is there while
 * Vision is waiting on work it chose to watch, and gone once the watch fires and Vision is
 * back. Nothing to read or manage here; /watches lists them if you want detail.
 */

import { useQuery } from '@tanstack/react-query'
import { RadarIcon } from 'lucide-react'
import { agent } from '@/api/agent'

export function WatchChip() {
  const { data } = useQuery({
    queryKey: ['vision', 'watches'],
    queryFn: agent.watches,
    refetchInterval: 4_000,
  })
  const n = data?.length ?? 0
  if (!n) return null
  return (
    <span
      title={`${n} watch${n === 1 ? '' : 'es'} active: Vision comes back on its own when the work finishes`}
      className="flex items-center gap-1 rounded-pill border border-primary px-2 py-0.5 text-[11px] text-ink"
    >
      <RadarIcon className="size-3 animate-pulse" />
      {n} watching
    </span>
  )
}
