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
  XIcon,
} from 'lucide-react'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { type AgentEvent, type AgentProposal, agent, type PageContext } from '@/api/agent'
import { errorMessage } from '@/api/http'
import { cn } from '@/lib/cn'
import { NAV } from '@/shell/nav'
import { Badge, Button, Textarea } from '@/ui/kit'
import { Markdown } from './markdown'

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

type Entry = { role: 'user'; text: string } | { role: 'agent'; parts: Part[]; live: boolean }

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
  const [open, setOpen] = useState(false)
  const [entries, setEntries] = useState<Entry[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [threadId, setThreadId] = useState<number | null>(null)
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
        setOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run on every streamed update to follow the tail
  useEffect(() => {
    if (pinned.current) scroller.current?.scrollTo({ top: scroller.current.scrollHeight })
  }, [entries])

  const run = async (
    starter: (onEvent: (e: AgentEvent) => void, signal: AbortSignal) => Promise<void>,
  ) => {
    setBusy(true)
    pinned.current = true
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
    try {
      await starter((e) => {
        if (e.type === 'start' || e.type === 'done') setThreadId(e.threadId)
        if (e.type === 'navigate') void navigate({ to: e.to })
        if (e.type === 'tool_end' && e.status === 'executed') wrote = true
        update((parts) => apply(parts, e))
      }, controller.signal)
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
  }

  const send = (message: string) => {
    const trimmed = message.trim()
    if (!trimmed || busy) return
    setText('')
    setEntries((prev) => [...prev, { role: 'user', text: trimmed }])
    void run((onEvent, signal) =>
      agent.turn(
        { text: trimmed, thread_id: threadId, context: readContext(pathname) },
        onEvent,
        signal,
      ),
    )
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
      className="fixed top-0 right-0 bottom-0 z-40 flex w-[460px] max-w-full flex-col border-l border-hairline bg-surface-1 shadow-xl"
    >
      <header className="flex items-center gap-2 border-b border-hairline px-3 py-2">
        <EyeIcon className="size-4 text-ink-muted" />
        <div className="min-w-0 flex-1">
          <div className="text-body font-medium text-ink">Vision</div>
          <div className="truncate text-[11px] text-ink-subtle">Sees {pathname}</div>
        </div>
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

      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
        }}
        className="min-h-0 flex-1 space-y-4 overflow-auto px-3 py-3"
      >
        {entries.length === 0 && (
          <div className="space-y-2">
            <p className="text-body text-ink-muted">
              I can see this page and do anything you can do in the app. Anything that writes, runs
              simulations or spends LLM budget waits for your Approve.
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
          entry.role === 'user' ? (
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

      <form onSubmit={onSubmit} className="flex items-end gap-2 border-t border-hairline p-3">
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
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
            onClick={() => abort.current?.abort()}
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
