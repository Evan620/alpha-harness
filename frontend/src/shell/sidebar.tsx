/**
 * Navigation: the six areas, a live count of submittable Alphas, and the account menu.
 * Collapses to icons below `lg`.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useRouterState } from '@tanstack/react-router'
import { ExternalLinkIcon, LogOutIcon } from 'lucide-react'
import { useEffect } from 'react'
import { toast } from 'sonner'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { auth } from '@/api/core'
import { errorMessage, http } from '@/api/http'
import type { Today } from '@/api/types'
import { cn } from '@/lib/cn'
import { useRefetchOn } from '@/lib/ws'
import { Menu } from '@/ui/overlay'
import { NAV } from './nav'
import { UpdateBadge, VersionBadge } from './update'

/** Areas a new consultant has to open once: Data (download fields) and AI (add a key). They flash until visited. */
const ONBOARDING: readonly string[] = ['data', 'ai']

const useVisited = create<{
  visited: string[]
  visit: (area: string) => void
}>()(
  persist(
    (set) => ({
      visited: [],
      visit: (area) =>
        set((s) => (s.visited.includes(area) ? s : { visited: [...s.visited, area] })),
    }),
    { name: 'alpha-harness-onboarding' },
  ),
)

export function Sidebar({ you, collapsed }: { you: Today['you']; collapsed: boolean }) {
  const queryClient = useQueryClient()
  const area = useRouterState({
    select: (s) => s.location.pathname.split('/')[1] ?? '',
  })
  const visited = useVisited((s) => s.visited)
  const visit = useVisited((s) => s.visit)
  // Reaching the area any way counts (sidebar, ⌘K or a link), so the flash never outlives the visit.
  useEffect(() => {
    if (ONBOARDING.includes(area)) visit(area)
  }, [area, visit])
  const submittable = useQuery({
    queryKey: ['pool', 'submittable-count'],
    queryFn: () => http.get<{ total: number }>('/api/vault/submittable?limit=1'),
  })
  useRefetchOn('simulations', ['pool', 'submittable-count'], 5000)
  const total = submittable.data?.total ?? 0

  const signOut = useMutation({
    mutationFn: () => auth.logout(),
    onSuccess: async () => {
      // Dropped, not invalidated: invalidating would refetch every mounted screen against a
      // dead session, and leave the last account's data in the cache behind the sign-in screen.
      // Today is kept and refetched: the shell observes it, and clearing it would leave the
      // shell watching a query nothing refetches, so the workspace stayed on screen.
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'today' })
      await queryClient.refetchQueries({ queryKey: ['today'] })
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  const name = you.fullName ?? you.userId ?? 'Signed in'
  const initials = name
    .split(/\s+/)
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()

  return (
    <aside className="flex h-full min-h-0 flex-col border-r border-hairline bg-canvas">
      <Link
        to="/dashboard"
        className={cn(
          'flex h-12 shrink-0 items-center border-b border-hairline transition-colors hover:bg-surface-1 focus-visible:-outline-offset-2',
          collapsed ? 'justify-center' : 'px-3',
        )}
        title="Alpha Harness"
      >
        <div className="flex min-w-0 items-center gap-2">
          <span
            className="flex size-7 shrink-0 items-center justify-center rounded-md border border-hairline-strong bg-surface-2 transition-colors"
            aria-hidden
          >
            <span className="text-body leading-none font-bold text-primary">α</span>
          </span>
          {!collapsed && (
            <span className="truncate text-body font-medium text-ink">Alpha Harness</span>
          )}
        </div>
      </Link>

      <nav aria-label="Main" className="flex flex-1 flex-col gap-0.5 overflow-y-auto p-2">
        {NAV.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            title={item.label}
            {...(collapsed && { 'aria-label': item.label })}
            className={cn(
              'group flex h-8 items-center gap-3 rounded-md text-body text-ink-subtle transition-colors hover:bg-surface-1 hover:text-ink data-[status=active]:bg-surface-2 data-[status=active]:text-ink',
              collapsed ? 'justify-center' : 'px-2',
              ONBOARDING.includes(item.area) &&
                !visited.includes(item.area) &&
                'animate-attention text-status-warning motion-reduce:bg-status-warning-tint',
            )}
          >
            <item.icon
              className="size-4 shrink-0 transition-colors group-hover:text-ink group-data-[status=active]:text-primary"
              aria-hidden
            />
            {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
            {!collapsed && item.area === 'pool' && total > 0 && (
              <span
                className="num rounded-pill border border-pnl-positive-edge bg-pnl-positive-tint px-1.5 py-0.5 text-caption font-medium text-pnl-positive"
                title="Submittable Alphas"
              >
                {total}
              </span>
            )}
          </Link>
        ))}
      </nav>

      <div className="flex flex-col gap-1 border-t border-hairline p-2">
        {/* Both above the account, where someone reading a screenshot looks for them. The
            update stays on the rail as an icon — it is the one thing here worth interrupting
            for — while the version hides: 52px has no room for a date. */}
        <UpdateBadge collapsed={collapsed} />
        {!collapsed && (
          <div className="px-1.5 pt-0.5 pb-1">
            <VersionBadge />
          </div>
        )}
        <Menu
          align="start"
          trigger={
            <button
              type="button"
              aria-label={`Account: ${name}`}
              className={cn(
                'flex h-10 w-full items-center gap-3 rounded-md text-left transition-colors hover:bg-surface-1 active:bg-surface-2 data-[popup-open]:bg-surface-2',
                collapsed ? 'justify-center' : 'px-1.5',
              )}
            >
              <span className="num flex size-7 shrink-0 items-center justify-center rounded-xs border border-hairline-strong bg-surface-3 text-caption font-medium text-ink-muted">
                {initials}
              </span>
              {!collapsed && (
                <span className="flex min-w-0 flex-col leading-tight">
                  <span className="truncate text-body font-medium text-ink">{name}</span>
                  <span className="truncate text-caption text-ink-subtle">
                    {you.email ?? you.userId}
                  </span>
                </span>
              )}
            </button>
          }
          items={[
            {
              label: 'Open the BRAIN Platform',
              icon: <ExternalLinkIcon />,
              onClick: () =>
                window.open(
                  'https://platform.worldquantbrain.com',
                  '_blank',
                  'noopener,noreferrer',
                ),
            },
            {
              label: 'Sign Out',
              icon: <LogOutIcon />,
              danger: true,
              disabled: signOut.isPending,
              onClick: () => signOut.mutate(),
            },
          ]}
        />
      </div>
    </aside>
  )
}
