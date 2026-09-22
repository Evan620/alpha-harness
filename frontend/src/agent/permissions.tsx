/**
 * Vision's permission mode, like Claude Code's: Ask first, or Auto. Only the person sets it,
 * here; the endpoint is not in Vision's action catalog, so the agent cannot change its own mode.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckIcon, LockIcon, ShieldCheckIcon, ZapIcon } from 'lucide-react'
import { agent, type PermissionMode } from '@/api/agent'
import { cn } from '@/lib/cn'

const MODES: { mode: PermissionMode; title: string; body: string; icon: typeof ZapIcon }[] = [
  {
    mode: 'ask',
    title: 'Ask first',
    body: 'Reads run at once. Anything that writes, runs simulations or spends LLM budget waits for your Approve.',
    icon: ShieldCheckIcon,
  },
  {
    mode: 'auto',
    title: 'Auto',
    body: 'Vision acts without asking. Every action is still run once through the gate and logged. It still asks in chat before deleting or stopping things you did not ask for.',
    icon: ZapIcon,
  },
]

export function usePermissions() {
  return useQuery({ queryKey: ['vision', 'permissions'], queryFn: agent.permissions })
}

export function ModeChip({ onClick }: { onClick: () => void }) {
  const { data } = usePermissions()
  const auto = data?.mode === 'auto'
  return (
    <button
      type="button"
      onClick={onClick}
      title="Permissions (/permissions)"
      className={cn(
        'flex items-center gap-1 rounded-pill border px-2 py-0.5 text-[11px] transition-colors',
        auto
          ? 'border-status-warning text-status-warning hover:bg-surface-2'
          : 'border-hairline text-ink-subtle hover:bg-surface-2 hover:text-ink',
      )}
    >
      {auto ? <ZapIcon className="size-3" /> : <ShieldCheckIcon className="size-3" />}
      {auto ? 'Auto' : 'Ask first'}
    </button>
  )
}

export function PermissionsCard({ onDone }: { onDone: () => void }) {
  const queryClient = useQueryClient()
  const { data } = usePermissions()
  const set = useMutation({
    mutationFn: agent.setPermissions,
    onSuccess: (next) => queryClient.setQueryData(['vision', 'permissions'], next),
  })
  return (
    <div className="rounded-sm border border-hairline-strong bg-surface-2 p-3">
      <div className="mb-2 text-body font-medium text-ink">Permissions</div>
      <div className="space-y-2">
        {MODES.map((m) => {
          const active = data?.mode === m.mode
          return (
            <button
              key={m.mode}
              type="button"
              disabled={set.isPending}
              onClick={() => set.mutate(m.mode, { onSuccess: onDone })}
              className={cn(
                'flex w-full gap-3 rounded-sm border px-3 py-2 text-left transition-colors',
                active ? 'border-primary bg-surface-3' : 'border-hairline hover:bg-surface-3',
              )}
            >
              <m.icon className="mt-0.5 size-4 shrink-0 text-ink-muted" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-body text-ink">
                  {m.title}
                  {active && <CheckIcon className="size-3.5 text-pnl-positive" />}
                </div>
                <div className="text-body-compact text-ink-muted">{m.body}</div>
              </div>
            </button>
          )
        })}
      </div>
      <div className="mt-3 text-[11px] tracking-wide text-ink-subtle uppercase">
        Always yours, in every mode
      </div>
      <ul className="mt-1 space-y-0.5 text-body-compact text-ink-muted">
        {(data?.alwaysYours ?? []).map((r) => (
          <li key={r.action} className="flex items-center gap-1.5">
            <LockIcon className="size-3 shrink-0 text-ink-subtle" />
            <span className="truncate">
              {r.summary} <span className="font-mono text-[11px] text-ink-subtle">{r.action}</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
