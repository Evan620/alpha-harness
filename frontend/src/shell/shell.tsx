/**
 * The gate and the workspace. `GET /api/today` decides: backend down, sign-in, or work.
 */

import { useQuery } from '@tanstack/react-query'
import { Outlet } from '@tanstack/react-router'
import { RefreshCwIcon, ServerCrashIcon, XIcon } from 'lucide-react'
import { Suspense, useState } from 'react'
import { Group, Panel, useDefaultLayout, usePanelRef } from 'react-resizable-panels'
import { AgentPanel } from '@/agent/panel'
import { useVision } from '@/agent/store'
import { today } from '@/api/core'
import { errorMessage } from '@/api/http'
import type { Today } from '@/api/types'
import { useLive } from '@/lib/live'
import { useRefetchOn } from '@/lib/ws'
import { Button, Empty, LINK, Notice, Skeleton, Spinner } from '@/ui/kit'
import { ResizeHandle, useMediaQuery, WIDE } from '@/ui/panels'
import { CommandMenu } from './command-menu'
import { Header } from './header'
import { Sidebar } from './sidebar'
import { SignIn } from './sign-in'

export function Shell() {
  const query = useQuery({ queryKey: ['today'], queryFn: () => today.get() })
  useRefetchOn('session', ['today'])

  if (query.isPending) {
    return (
      <div className="flex h-svh items-center justify-center">
        <Spinner className="size-5" />
      </div>
    )
  }

  // Only a load that never succeeded: a failed background refetch keeps its data, and swapping
  // the whole workspace for this screen would lose open dialogs and unsaved forms.
  if (query.data === undefined) {
    return (
      <div className="flex h-svh items-center justify-center p-6">
        <div className="w-full max-w-md rounded-lg border border-hairline bg-surface-1">
          <Empty icon={<ServerCrashIcon />} title="The backend is not answering">
            <p>{errorMessage(query.error)}</p>
            <code className="mt-3 block rounded-md bg-canvas px-2 py-1.5 text-caption break-all text-ink-muted">
              cd backend && uv run uvicorn alpha_harness.main:app --reload --port 8000
            </code>
          </Empty>
          <div className="flex justify-center border-t border-hairline p-3">
            <Button onClick={() => query.refetch()} loading={query.isFetching}>
              <RefreshCwIcon /> Try again
            </Button>
          </div>
        </div>
      </div>
    )
  }

  if (query.data.step === 'sign-in') return <SignIn storedEmail={query.data.you.email} />
  return <Workspace you={query.data.you} />
}

/** Sidebar | workspace, draggable from `lg` up. Dragged below its minimum it collapses to the
 * icon rail; below `lg` the rail is all there is. One keyed group at every width, so crossing
 * 1024px does not remount the screen and throw away whatever was half-typed in it. */
function Workspace({ you }: { you: Today['you'] }) {
  const wide = useMediaQuery(WIDE)
  const [dragCollapsed, setDragCollapsed] = useState(false)
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: 'ah-panels:shell',
    storage: localStorage,
    onlySaveAfterUserInteractions: true,
  })
  const sidebar = usePanelRef()
  const visionOpen = useVision((s) => s.open)
  const setVisionOpen = useVision((s) => s.setOpen)
  // Only one sidebar is wide at a time. While Vision is open the resizable nav leaves the Group
  // and a fixed icon rail stands in for it; expanding the rail closes Vision and the nav comes
  // back at its own size. Swapping panels rather than resizing one keeps this exact: the
  // library's collapse/expand compare percentages of a Group whose width Vision changes.
  const railForVision = wide && visionOpen
  const rail = !wide || dragCollapsed
  const toggleNav = () => {
    const panel = sidebar.current
    if (!panel) return
    if (panel.isCollapsed()) panel.expand()
    else panel.collapse()
  }
  return (
    <>
      <div className="flex h-svh min-w-0">
        {railForVision && (
          <div className="h-full w-[52px] shrink-0">
            <Sidebar you={you} collapsed onToggle={() => setVisionOpen(false)} />
          </div>
        )}
        <Group
          id="shell"
          orientation="horizontal"
          defaultLayout={defaultLayout}
          onLayoutChanged={onLayoutChanged}
          className="h-full min-w-0 flex-1"
        >
          {!railForVision && (
            <Panel
              key="sidebar"
              id="sidebar"
              defaultSize={wide ? 216 : 52}
              minSize={wide ? 180 : 52}
              maxSize={wide ? 320 : 52}
              collapsible={wide}
              collapsedSize={52}
              panelRef={sidebar}
              onResize={(size) => setDragCollapsed(size.inPixels <= 64)}
            >
              <Sidebar you={you} collapsed={rail} onToggle={wide ? toggleNav : undefined} />
            </Panel>
          )}
          {wide && !railForVision && <ResizeHandle key="handle" variant="edge" />}
          <Panel key="workspace" id="workspace" minSize={wide ? 480 : 0} className="min-w-0">
            <div className="flex h-full min-h-0 min-w-0 flex-col">
              <Header />
              <VerificationBanner />
              <main className="min-h-0 flex-1 overflow-auto">
                <Suspense fallback={<ScreenSkeleton />}>
                  <Outlet />
                </Suspense>
              </main>
            </div>
          </Panel>
        </Group>
        <AgentPanel />
      </div>
      <CommandMenu />
    </>
  )
}

function VerificationBanner() {
  const url = useLive((s) => s.verificationUrl)
  if (!url) return null
  return (
    <div className="border-b border-hairline p-2">
      <Notice
        tone="warn"
        title="BRAIN needs to verify your identity"
        action={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Dismiss"
            onClick={() => useLive.setState({ verificationUrl: null })}
          >
            <XIcon />
          </Button>
        }
      >
        Finish the check in your browser, then sign in again.{' '}
        <a href={url} target="_blank" rel="noopener noreferrer" className={LINK}>
          Open the verification page
        </a>
      </Notice>
    </div>
  )
}

function ScreenSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </div>
      <Skeleton className="h-80" label="Loading" />
    </div>
  )
}
