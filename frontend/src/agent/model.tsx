/**
 * The header chip showing which model Vision thinks with. Switching is a typed command,
 * /model <id or slug> [provider] or /model auto, handled in the panel. The endpoint is not
 * in Vision's action catalog, so the agent cannot switch its own model. A pasted slug is
 * checked against the provider's own model list before Vision will use it.
 */

import { useQuery } from '@tanstack/react-query'
import { CpuIcon } from 'lucide-react'
import { agent } from '@/api/agent'
import { cn } from '@/lib/cn'

const KEY = ['vision', 'model']

export function useVisionModel() {
  return useQuery({ queryKey: KEY, queryFn: agent.model })
}

export function ModelChip({ onClick }: { onClick: () => void }) {
  const { data } = useVisionModel()
  const label = data?.effective?.label ?? 'No model'
  return (
    <button
      type="button"
      onClick={onClick}
      title={`Model (/model): ${data?.effective?.id ?? 'add a key first'}`}
      className={cn(
        'flex max-w-32 items-center gap-1 rounded-pill border px-2 py-0.5 text-[11px] transition-colors',
        data?.effective
          ? 'border-hairline text-ink-subtle hover:bg-surface-2 hover:text-ink'
          : 'border-status-warning text-status-warning hover:bg-surface-2',
      )}
    >
      <CpuIcon className="size-3 shrink-0" />
      <span className="truncate">{label}</span>
    </button>
  )
}
