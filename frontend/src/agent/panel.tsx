/**
 * Vision, the in-app agent. It sees the page you are on, can do anything the UI can, and asks
 * before anything that writes, simulates or spends. Tool calls and text stream in live.
 * Toggle with the floating button or ⌘J.
 */

import { useQueryClient } from '@tanstack/react-query'
import { useNavigate, useRouterState } from '@tanstack/react-router'
import {
  BrainIcon,
  CheckIcon,
  ChevronRightIcon,
  CircleAlertIcon,
  EyeIcon,
  LoaderCircleIcon,
  SendIcon,
  SquareIcon,
  TargetIcon,
  XIcon,
} from 'lucide-react'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import {
  type AgentEvent,
  type AgentProposal,
  agent,
  type Goal,
  type GoalStep,
  type PageContext,
} from '@/api/agent'
import { errorMessage } from '@/api/http'
import { cn } from '@/lib/cn'
import { NAV } from '@/shell/nav'
import { Badge, Button, Textarea } from '@/ui/kit'
import { GOAL_KEY, GoalCard, GoalChip, statusOf } from './goal'
import { Markdown } from './markdown'
import { ModeChip, PermissionsCard } from './permissions'
import { useVision } from './store'

type Part =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | {
      kind: 'tool'
      id: string
      tool: string
      label: string
      args: Record<string, unknown>
      status?: string
      httpStatus?: number | null
      preview?: string
    }
  | {
      kind: 'proposal'
      proposal: AgentProposal
      decided?: 'approved' | 'rejected'
    }
  | { kind: 'error'; text: string }

type Entry =
  | { role: 'user'; text: string }
  | { role: 'agent'; parts: Part[]; live: boolean }
  | { role: 'permissions' }
  | { role: 'goal' }
  | { role: 'loop'; kind: LoopKind; text: string; until?: number }

type LoopKind = 'start' | 'continue' | 'wait' | 'await_approval' | 'stop'

const COMMANDS = [
  { name: '/goal', hint: 'Set what Vision works toward, and the most it may spend' },
  { name: '/permissions', hint: 'Ask first or Auto: whether Vision asks before acting' },
  { name: '/new', hint: 'Start a new conversation' },
  { name: '/clear', hint: 'Clear this conversation' },
] as const

const STARTERS = [
  'What is this page for?',
  'Give me a two-minute tour of the platform.',
  'What should I do next to get a submittable alpha?',
]

const TOOL_NAMES: Record<string, string> = {
  explain_page: 'Reading page guide',
  find_actions: 'Finding actions',
  describe_action: 'Reading action schema',
  call_action: 'Calling',
  navigate: 'Opening page',
  approved: 'Running approved action',
}

function readContext(pathname: string): PageContext {
  const area = pathname.split('/')[1] ?? ''
  const nav = NAV.find((n) => n.area === area)
  const main = document.querySelector('main')
  return {
    pathname,
    title: nav?.label ?? document.title,
    area,
    visible_text: (main?.innerText ?? '').replace(/\n{3,}/g, '\n\n').slice(0, 12_000),
  }
}

/** Fold one streamed event into the live agent entry's parts. */
function apply(parts: Part[], e: AgentEvent): Part[] {
  const last = parts[parts.length - 1]
  switch (e.type) {
    case 'text':
      if (last?.kind === 'text')
        return [...parts.slice(0, -1), { ...last, text: last.text + e.delta }]
      return [...parts, { kind: 'text', text: e.delta }]
    case 'thinking':
      if (last?.kind === 'thinking')
        return [...parts.slice(0, -1), { ...last, text: last.text + e.delta }]
      return [...parts, { kind: 'thinking', text: e.delta }]
    case 'tool_start':
      return [...parts, { kind: 'tool', id: e.id, tool: e.tool, label: e.label, args: e.args }]
    case 'tool_end':
      return parts.map((p) =>
        p.kind === 'tool' && p.id === e.id && p.status === undefined
          ? {
              ...p,
              status: e.status,
              httpStatus: e.httpStatus,
              preview: e.preview,
            }
          : p,
      )
    case 'proposal':
      return [...parts, { kind: 'proposal', proposal: e.proposal }]
    case 'status':
      return [...parts, { kind: 'error', text: e.delta }]
    case 'error':
      return [...parts, { kind: 'error', text: e.message }]
    default:
      return parts
  }
}

export function AgentPanel() {
  const open = useVision((s) => s.open)
  const setOpen = useVision((s) => s.setOpen)
  const toggle = useVision((s) => s.toggle)
  const [entries, setEntries] = useState<Entry[]>([])
  const [text, setText] = useState('')
  const [pick, setPick] = useState(0)
  const [busy, setBusy] = useState(false)
  const [, setThreadIdState] = useState<number | null>(null)
  const threadRef = useRef<number | null>(null)
  const setThreadId = (id: number | null) => {
    threadRef.current = id
    setThreadIdState(id)
  }
  const waitTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cancelWait = () => {
    if (waitTimer.current) clearTimeout(waitTimer.current)
    waitTimer.current = null
  }
  // biome-ignore lint/correctness/useExhaustiveDependencies: clear a pending wait on unmount only
  useEffect(() => cancelWait, [])
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const scroller = useRef<HTMLDivElement>(null)
  const abort = useRef<AbortController | null>(null)
  const pinned = useRef(true)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') {
        e.preventDefault()
        toggle()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggle])

  const content = useRef<HTMLDivElement>(null)
  const [following, setFollowing] = useState(true)
  const follow = (on: boolean) => {
    pinned.current = on
    setFollowing(on)
  }
  const toBottom = () => {
    const el = scroller.current
    if (el) el.scrollTop = el.scrollHeight
  }

  // Follow every change in height (streamed text, Markdown re-render, tool cards opening),
  // not just new entries. Only an explicit scroll up by the reader stops it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the panel mounts its scroller only when open
  useEffect(() => {
    const node = content.current
    if (!node) return
    const observer = new ResizeObserver(() => {
      if (pinned.current) toBottom()
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [open])

  const run = async (
    starter: (onEvent: (e: AgentEvent) => void, signal: AbortSignal) => Promise<void>,
  ) => {
    setBusy(true)
    follow(true)
    requestAnimationFrame(toBottom)
    const controller = new AbortController()
    abort.current = controller
    setEntries((prev) => [...prev, { role: 'agent', parts: [], live: true }])
    const update = (fn: (parts: Part[]) => Part[]) =>
      setEntries((prev) => {
        const next = [...prev]
        const last = next[next.length - 1]
        if (last?.role === 'agent') next[next.length - 1] = { ...last, parts: fn(last.parts) }
        return next
      })
    let wrote = false
    let finished = false
    try {
      await starter((e) => {
        if (e.type === 'start' || e.type === 'done') setThreadId(e.threadId)
        if (e.type === 'navigate') void navigate({ to: e.to })
        if (e.type === 'tool_end' && e.status === 'executed') wrote = true
        update((parts) => apply(parts, e))
      }, controller.signal)
      finished = true
    } catch (error) {
      if (!controller.signal.aborted)
        update((parts) => [...parts, { kind: 'error', text: errorMessage(error) }])
    } finally {
      setEntries((prev) =>
        prev.map((en, i) =>
          i === prev.length - 1 && en.role === 'agent' ? { ...en, live: false } : en,
        ),
      )
      setBusy(false)
      abort.current = null
      if (wrote) void queryClient.invalidateQueries()
    }
    if (finished) await afterTurn()
  }

  const pushLoop = (kind: LoopKind, text: string, until?: number) =>
    setEntries((prev) => [...prev, { role: 'loop', kind, text, ...(until ? { until } : {}) }])

  /** After every finished turn: ask the server's judge what the goal loop does next. */
  const afterTurn = async () => {
    let step: GoalStep
    try {
      step = await agent.goalStep(threadRef.current)
    } catch {
      return
    }
    queryClient.setQueryData(GOAL_KEY, { goal: step.goal })
    if (step.action === 'none' || !step.goal) return
    const g = step.goal
    if (step.action === 'continue') {
      pushLoop('continue', `Turn ${g.turns + 1} of ${g.maxTurns}. ${g.lastReason}`)
      void runTurn(step.prompt)
    } else if (step.action === 'wait') {
      const until = Date.now() + step.seconds * 1000
      pushLoop('wait', g.lastReason, until)
      cancelWait()
      waitTimer.current = setTimeout(() => {
        waitTimer.current = null
        void runTurn(step.prompt)
      }, step.seconds * 1000)
    } else if (step.action === 'await_approval') {
      pushLoop(
        'await_approval',
        'Goal paused on the approval card above. Approve or reject to carry on.',
      )
    } else {
      pushLoop('stop', `${statusOf(g.status).label}: ${g.stoppedReason || g.lastReason}`)
    }
  }

  /** One turn. Loop turns carry the loop's own prompt and show as a loop line, not a bubble. */
  const runTurn = (message: string, shown?: string) => {
    if (shown) setEntries((prev) => [...prev, { role: 'user', text: shown }])
    return run((onEvent, signal) =>
      agent.turn(
        { text: message, thread_id: threadRef.current, context: readContext(pathname) },
        onEvent,
        signal,
      ),
    )
  }

  const startGoal = (g: Goal) => {
    cancelWait()
    pushLoop('start', `Goal started: ${g.objective}`)
    void runTurn(
      `[Goal set by the person]\nGoal: ${g.objective}\n${g.doneWhen ? `Met when: ${g.doneWhen}\n` : ''}Start working toward it: take the first concrete step.`,
    )
  }

  const pauseGoal = async () => {
    cancelWait()
    abort.current?.abort()
    const res = await agent.pauseGoal().catch(() => null)
    if (res) queryClient.setQueryData(GOAL_KEY, res)
    pushLoop('stop', 'Paused. Resume from the goal card or /goal.')
  }

  const resumeGoal = async () => {
    const res = await agent.resumeGoal().catch(() => null)
    if (!res?.goal) return
    queryClient.setQueryData(GOAL_KEY, res)
    pushLoop('start', 'Goal resumed.')
    void runTurn(
      `[Resuming your goal]\nGoal: ${res.goal.objective}\nPick up where you left off: take the next concrete step.`,
    )
  }

  const showGoal = () => {
    setEntries((prev) => [...prev.filter((e) => e.role !== 'goal'), { role: 'goal' }])
  }

  const showPermissions = () => {
    setEntries((prev) => [...prev.filter((e) => e.role !== 'permissions'), { role: 'permissions' }])
  }

  const send = (message: string) => {
    const trimmed = message.trim()
    if (!trimmed || busy) return
    setText('')
    // Slash commands are handled here and never reach the model.
    if (trimmed === '/permissions') return showPermissions()
    if (trimmed === '/goal') return showGoal()
    if (trimmed === '/new' || trimmed === '/clear') {
      setEntries([])
      setThreadId(null)
      return
    }
    cancelWait()
    void runTurn(trimmed, trimmed)
  }

  const decide = (proposal: AgentProposal, approve: boolean) => {
    setEntries((prev) =>
      prev.map((en) =>
        en.role === 'agent'
          ? {
              ...en,
              parts: en.parts.map((p) =>
                p.kind === 'proposal' && p.proposal.id === proposal.id
                  ? {
                      ...p,
                      decided: approve ? ('approved' as const) : ('rejected' as const),
                    }
                  : p,
              ),
            }
          : en,
      ),
    )
    void run((onEvent, signal) =>
      agent.decide(
        proposal.id,
        {
          approve,
          payload_hash: proposal.payloadHash,
          context: readContext(pathname),
        },
        onEvent,
        signal,
      ),
    )
  }

  // Slash commands: the menu opens on "/" and filters as you type.
  const slash = /^\/\S*$/.test(text) ? text.toLowerCase() : null
  const menu = slash ? COMMANDS.filter((c) => c.name.startsWith(slash)) : []
  const pickIndex = Math.min(pick, Math.max(menu.length - 1, 0))

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    send(text)
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open Vision"
        title="Vision (⌘J)"
        className="fixed right-4 bottom-4 z-40 flex h-10 items-center gap-2 rounded-full border border-hairline-strong bg-surface-2 px-4 text-body text-ink shadow-lg transition-colors hover:bg-surface-3"
      >
        <EyeIcon className="size-4" /> Vision
      </button>
    )
  }

  return (
    <aside
      aria-label="Vision"
      className="flex h-full w-[440px] max-w-[50vw] shrink-0 flex-col border-l border-hairline bg-surface-1"
    >
      <header className="flex items-center gap-2 border-b border-hairline px-3 py-2">
        <EyeIcon className="size-4 text-ink-muted" />
        <div className="min-w-0 flex-1">
          <div className="text-body font-medium text-ink">Vision</div>
          <div className="truncate text-[11px] text-ink-subtle">Sees {pathname}</div>
        </div>
        <GoalChip onClick={showGoal} />
        <ModeChip onClick={showPermissions} />
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={() => {
            setEntries([])
            setThreadId(null)
          }}
        >
          New
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label="Close" onClick={() => setOpen(false)}>
          <XIcon />
        </Button>
      </header>

      <div className="relative min-h-0 flex-1">
        <div
          ref={scroller}
          role="log"
          aria-live="polite"
          onWheel={(e) => {
            if (e.deltaY < 0) follow(false)
          }}
          onTouchMove={() => follow(false)}
          onKeyDown={(e) => {
            if (['ArrowUp', 'PageUp', 'Home'].includes(e.key)) follow(false)
          }}
          onScroll={(e) => {
            const el = e.currentTarget
            if (!pinned.current && el.scrollHeight - el.scrollTop - el.clientHeight < 24)
              follow(true)
          }}
          className="h-full overflow-auto px-3 py-3"
        >
          <div ref={content} className="space-y-4">
            {entries.length === 0 && (
              <div className="space-y-2">
                <p className="text-body text-ink-muted">
                  I can see this page and do anything you can do in the app. Type{' '}
                  <code className="rounded-xs bg-surface-3 px-1 font-mono text-[12px]">
                    /permissions
                  </code>{' '}
                  to choose whether I ask before acting.
                </p>
                {STARTERS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => send(s)}
                    className="block w-full rounded-sm border border-hairline bg-surface-2 px-3 py-2 text-left text-body text-ink hover:bg-surface-3"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
            {entries.map((entry, i) =>
              entry.role === 'goal' ? (
                <GoalCard
                  key={i}
                  onStart={startGoal}
                  onPause={() => void pauseGoal()}
                  onResume={() => void resumeGoal()}
                  onDone={() => setEntries((prev) => prev.filter((e) => e.role !== 'goal'))}
                />
              ) : entry.role === 'loop' ? (
                <LoopLine key={i} kind={entry.kind} text={entry.text} until={entry.until} />
              ) : entry.role === 'permissions' ? (
                <PermissionsCard
                  key={i}
                  onDone={() => setEntries((prev) => prev.filter((e) => e.role !== 'permissions'))}
                />
              ) : entry.role === 'user' ? (
                <div
                  key={i}
                  className="ml-10 rounded-sm bg-surface-3 px-3 py-2 text-body whitespace-pre-wrap text-ink"
                >
                  {entry.text}
                </div>
              ) : (
                <AgentEntry
                  key={i}
                  parts={entry.parts}
                  live={entry.live}
                  busy={busy}
                  onDecide={decide}
                />
              ),
            )}
          </div>
        </div>
        {!following && (
          <button
            type="button"
            onClick={() => {
              follow(true)
              toBottom()
            }}
            className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-hairline-strong bg-surface-2 px-3 py-1 text-body-compact text-ink shadow-md hover:bg-surface-3"
          >
            Jump to latest ↓
          </button>
        )}
      </div>

      <form
        onSubmit={onSubmit}
        className="relative flex items-end gap-2 border-t border-hairline p-3"
      >
        {menu.length > 0 && (
          <div
            role="listbox"
            aria-label="Commands"
            className="absolute right-3 bottom-full left-3 mb-1 overflow-hidden rounded-sm border border-hairline-strong bg-surface-2 shadow-lg"
          >
            {menu.map((c, i) => (
              <button
                key={c.name}
                type="button"
                role="option"
                aria-selected={i === pickIndex}
                onMouseEnter={() => setPick(i)}
                onMouseDown={(e) => {
                  e.preventDefault()
                  send(c.name)
                }}
                className={cn(
                  'flex w-full items-baseline gap-3 px-3 py-1.5 text-left',
                  i === pickIndex ? 'bg-surface-3' : 'hover:bg-surface-3',
                )}
              >
                <span className="font-mono text-body-compact text-ink">{c.name}</span>
                <span className="truncate text-body-compact text-ink-subtle">{c.hint}</span>
              </button>
            ))}
          </div>
        )}
        <Textarea
          value={text}
          onChange={(e) => {
            setText(e.target.value)
            setPick(0)
          }}
          onKeyDown={(e) => {
            if (menu.length > 0) {
              if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault()
                const step = e.key === 'ArrowDown' ? 1 : -1
                setPick((p) => (p + step + menu.length) % menu.length)
                return
              }
              if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
                e.preventDefault()
                const chosen = menu[pickIndex]
                if (chosen) send(chosen.name)
                return
              }
              if (e.key === 'Escape') {
                e.preventDefault()
                setText('')
                return
              }
            }
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send(text)
            }
          }}
          rows={2}
          placeholder="Ask Vision, or tell it what to do…"
          className="min-h-0 flex-1 resize-none py-2"
        />
        {busy ? (
          <Button
            type="button"
            variant="secondary"
            size="icon"
            aria-label="Stop"
            onClick={() => {
              const running = queryClient.getQueryData<{ goal: Goal | null }>(GOAL_KEY)?.goal
                ?.running
              if (running) void pauseGoal()
              else abort.current?.abort()
            }}
          >
            <SquareIcon />
          </Button>
        ) : (
          <Button
            type="submit"
            variant="primary"
            size="icon"
            aria-label="Send"
            disabled={!text.trim()}
          >
            <SendIcon />
          </Button>
        )}
      </form>
    </aside>
  )
}

function AgentEntry({
  parts,
  live,
  busy,
  onDecide,
}: {
  parts: Part[]
  live: boolean
  busy: boolean
  onDecide: (p: AgentProposal, approve: boolean) => void
}) {
  const lastKind = parts[parts.length - 1]?.kind
  return (
    <div className="space-y-2">
      {parts.map((part, i) => {
        if (part.kind === 'text')
          return part.text.trim() ? <Markdown key={i} text={part.text} /> : null
        if (part.kind === 'thinking')
          return <Thinking key={i} text={part.text} active={live && i === parts.length - 1} />
        if (part.kind === 'tool') return <ToolCall key={i} part={part} />
        if (part.kind === 'error')
          return (
            <div
              key={i}
              className="flex gap-2 rounded-sm border border-hairline px-3 py-2 text-body-compact text-pnl-negative"
            >
              <CircleAlertIcon className="mt-0.5 size-3.5 shrink-0" /> {part.text}
            </div>
          )
        return <Proposal key={i} part={part} busy={busy} onDecide={onDecide} />
      })}
      {live && lastKind !== 'thinking' && lastKind !== 'text' && (
        <div className="flex items-center gap-2 text-body-compact text-ink-subtle">
          <LoaderCircleIcon className="size-3.5 animate-spin" />{' '}
          {parts.length ? 'Working…' : 'Looking at the page…'}
        </div>
      )}
    </div>
  )
}

function Thinking({ text, active }: { text: string; active: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="text-[11.5px] text-ink-subtle">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 hover:text-ink-muted"
      >
        <ChevronRightIcon className={cn('size-3 transition-transform', open && 'rotate-90')} />
        <BrainIcon className={cn('size-3', active && 'animate-pulse')} />
        {active ? 'Thinking…' : 'Thought'}
      </button>
      {open && (
        <div className="mt-1 max-h-40 overflow-auto border-l border-hairline pl-3 whitespace-pre-wrap">
          {text}
        </div>
      )}
    </div>
  )
}

function ToolCall({ part }: { part: Extract<Part, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false)
  const running = part.status === undefined
  const failed =
    part.status === 'error' || part.status === 'refused' || (part.httpStatus ?? 0) >= 400
  const waiting = part.status === 'needs_approval'
  return (
    <div className="rounded-sm border border-hairline bg-surface-2/60 text-[11.5px]">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
      >
        {running ? (
          <LoaderCircleIcon className="size-3.5 shrink-0 animate-spin text-ink-subtle" />
        ) : failed ? (
          <CircleAlertIcon className="size-3.5 shrink-0 text-pnl-negative" />
        ) : waiting ? (
          <CircleAlertIcon className="size-3.5 shrink-0 text-status-warning" />
        ) : (
          <CheckIcon className="size-3.5 shrink-0 text-pnl-positive" />
        )}
        <span className="shrink-0 text-ink-muted">{TOOL_NAMES[part.tool] ?? part.tool}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-ink">{part.label}</span>
        {part.httpStatus ? (
          <span className={cn('font-mono', failed ? 'text-pnl-negative' : 'text-ink-subtle')}>
            {part.httpStatus}
          </span>
        ) : null}
        {waiting && <span className="text-status-warning">awaiting approval</span>}
        <ChevronRightIcon
          className={cn(
            'size-3 shrink-0 text-ink-subtle transition-transform',
            open && 'rotate-90',
          )}
        />
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-hairline px-2 py-1.5">
          {Object.keys(part.args).length > 0 && (
            <pre className="max-h-40 overflow-auto rounded-xs bg-canvas p-1.5 font-mono text-[11px] text-ink-muted">
              {JSON.stringify(part.args, null, 2)}
            </pre>
          )}
          {part.preview && (
            <pre className="max-h-48 overflow-auto rounded-xs bg-canvas p-1.5 font-mono text-[11px] whitespace-pre-wrap text-ink-muted">
              {part.preview}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

function Proposal({
  part,
  busy,
  onDecide,
}: {
  part: Extract<Part, { kind: 'proposal' }>
  busy: boolean
  onDecide: (p: AgentProposal, approve: boolean) => void
}) {
  const p = part.proposal
  const spends = Object.entries(p.spends).filter(([, v]) => v > 0)
  return (
    <div className="rounded-sm border border-hairline-strong bg-surface-2 p-3">
      <div className="mb-1 text-[11px] tracking-wide text-status-warning uppercase">
        Needs your approval
      </div>
      <div className="font-mono text-body-compact text-ink">{p.label}</div>
      <div className="mt-1 text-body-compact text-ink-muted">{p.summary}</div>
      {(p.effects.length > 0 || spends.length > 0) && (
        <div className="mt-2 flex flex-wrap gap-1">
          {p.effects.map((e) => (
            <Badge key={e} tone="outline">
              {e.replaceAll('_', ' ')}
            </Badge>
          ))}
          {spends.map(([k, v]) => (
            <Badge key={k} tone="warn">
              {k}: {v}
            </Badge>
          ))}
        </div>
      )}
      <details className="mt-2">
        <summary className="cursor-pointer text-[11px] text-ink-subtle">Exact request</summary>
        <pre className="mt-1 max-h-48 overflow-auto rounded-xs bg-canvas p-2 text-[11px] text-ink-muted">
          {JSON.stringify(p.arguments, null, 2)}
        </pre>
      </details>
      {part.decided ? (
        <div
          className={cn(
            'mt-2 flex items-center gap-1 text-body-compact',
            part.decided === 'approved' ? 'text-pnl-positive' : 'text-ink-subtle',
          )}
        >
          {part.decided === 'approved' ? (
            <CheckIcon className="size-3.5" />
          ) : (
            <XIcon className="size-3.5" />
          )}
          {part.decided === 'approved' ? 'Approved' : 'Rejected'}
        </div>
      ) : (
        <div className="mt-3 flex gap-2">
          <Button variant="primary" size="sm" disabled={busy} onClick={() => onDecide(p, true)}>
            Approve
          </Button>
          <Button variant="secondary" size="sm" disabled={busy} onClick={() => onDecide(p, false)}>
            Reject
          </Button>
        </div>
      )}
    </div>
  )
}

const LOOP_TONE: Record<LoopKind, string> = {
  start: 'text-primary',
  continue: 'text-ink-subtle',
  wait: 'text-status-warning',
  await_approval: 'text-status-warning',
  stop: 'text-ink',
}

/** One line of the goal loop between turns: what the judge decided and why. */
function LoopLine({
  kind,
  text,
  until,
}: {
  kind: LoopKind
  text: string
  until?: number | undefined
}) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!until) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [until])
  const left = until ? Math.max(0, Math.round((until - now) / 1000)) : 0
  const Icon = kind === 'wait' ? LoaderCircleIcon : kind === 'stop' ? SquareIcon : TargetIcon
  return (
    <div
      className={cn(
        'flex items-start gap-2 border-l-2 border-hairline pl-2 text-[12px]',
        LOOP_TONE[kind],
      )}
    >
      <Icon
        className={cn('mt-0.5 size-3.5 shrink-0', kind === 'wait' && left > 0 && 'animate-spin')}
      />
      <span className="min-w-0">
        {kind === 'wait' && (
          <span className="num mr-1">{left > 0 ? `Waiting ${left}s.` : 'Checking back in.'}</span>
        )}
        {text}
      </span>
    </div>
  )
}
