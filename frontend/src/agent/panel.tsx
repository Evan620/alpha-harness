/**
 * The in-app agent. It sees the page you are on, can do anything the UI can, and asks before
 * anything that writes, simulates or spends. Toggle with the floating button or ⌘J.
 */

import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate, useRouterState } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { BotIcon, CheckIcon, SendIcon, XIcon } from 'lucide-react'
import { agent, type AgentProposal, type AgentStep, type AgentTurn, type PageContext } from '@/api/agent'
import { errorMessage } from '@/api/http'
import { NAV } from '@/shell/nav'
import { Badge, Button, Spinner, Textarea } from '@/ui/kit'
import { cn } from '@/lib/cn'

type Entry =
  | { kind: 'user'; text: string }
  | { kind: 'agent'; text: string; steps: AgentStep[] }
  | { kind: 'proposal'; proposal: AgentProposal; decided?: 'approved' | 'rejected' }
  | { kind: 'error'; text: string }

const STARTERS = [
  'What is this page for?',
  'Give me a two-minute tour of the platform.',
  'What should I do next to get a submittable alpha?',
]

function usePageContext(): () => PageContext {
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  return () => {
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
}

export function AgentPanel() {
  const [open, setOpen] = useState(false)
  const [entries, setEntries] = useState<Entry[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [threadId, setThreadId] = useState<number | null>(null)
  const context = usePageContext()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const scroller = useRef<HTMLDivElement>(null)
  const pathname = useRouterState({ select: (s) => s.location.pathname })

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

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' })
  }, [entries, busy])

  const absorb = (turn: AgentTurn) => {
    setThreadId(turn.threadId)
    setEntries((prev) => [
      ...prev,
      ...(turn.reply || turn.steps.length ? [{ kind: 'agent' as const, text: turn.reply, steps: turn.steps }] : []),
      ...turn.proposals.map((proposal) => ({ kind: 'proposal' as const, proposal })),
    ])
    if (turn.steps.some((s) => s.status === 'executed' && s.tool === 'call_action')) {
      void queryClient.invalidateQueries()
    }
    if (turn.navigate) void navigate({ to: turn.navigate })
  }

  const send = async (message: string) => {
    const trimmed = message.trim()
    if (!trimmed || busy) return
    setText('')
    setEntries((prev) => [...prev, { kind: 'user', text: trimmed }])
    setBusy(true)
    try {
      absorb(await agent.turn({ text: trimmed, thread_id: threadId, context: context() }))
    } catch (error) {
      setEntries((prev) => [...prev, { kind: 'error', text: errorMessage(error) }])
    } finally {
      setBusy(false)
    }
  }

  const decide = async (proposal: AgentProposal, approve: boolean) => {
    setEntries((prev) =>
      prev.map((e) =>
        e.kind === 'proposal' && e.proposal.id === proposal.id ? { ...e, decided: approve ? 'approved' : 'rejected' } : e,
      ),
    )
    setBusy(true)
    try {
      absorb(
        await agent.decide(proposal.id, {
          approve,
          payload_hash: proposal.payloadHash,
          context: context(),
        }),
      )
      void queryClient.invalidateQueries()
    } catch (error) {
      setEntries((prev) => [...prev, { kind: 'error', text: errorMessage(error) }])
    } finally {
      setBusy(false)
    }
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    void send(text)
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open the agent"
        title="Agent (⌘J)"
        className="fixed right-4 bottom-4 z-40 flex size-11 items-center justify-center rounded-full border border-hairline-strong bg-surface-2 text-ink shadow-lg transition-colors hover:bg-surface-3"
      >
        <BotIcon className="size-5" />
      </button>
    )
  }

  return (
    <aside
      aria-label="Agent"
      className="fixed top-0 right-0 bottom-0 z-40 flex w-[420px] max-w-full flex-col border-l border-hairline bg-surface-1 shadow-xl"
    >
      <header className="flex items-center gap-2 border-b border-hairline px-3 py-2">
        <BotIcon className="size-4 text-ink-muted" />
        <div className="min-w-0 flex-1">
          <div className="text-body font-medium text-ink">Agent</div>
          <div className="truncate text-[11px] text-ink-subtle">Sees: {pathname}</div>
        </div>
        <Button
          variant="ghost"
          size="sm"
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

      <div ref={scroller} className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
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
                onClick={() => void send(s)}
                className="block w-full rounded-sm border border-hairline bg-surface-2 px-3 py-2 text-left text-body text-ink hover:bg-surface-3"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {entries.map((entry, i) => (
          <EntryView key={i} entry={entry} onDecide={decide} busy={busy} />
        ))}
        {busy && (
          <div className="flex items-center gap-2 text-body-compact text-ink-subtle">
            <Spinner className="size-3.5" /> Working…
          </div>
        )}
      </div>

      <form onSubmit={onSubmit} className="flex items-end gap-2 border-t border-hairline p-3">
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send(text)
            }
          }}
          rows={2}
          placeholder="Ask, or tell it what to do…"
          className="min-h-0 flex-1 resize-none py-2"
        />
        <Button type="submit" variant="primary" size="icon" aria-label="Send" disabled={busy || !text.trim()}>
          <SendIcon />
        </Button>
      </form>
    </aside>
  )
}

function EntryView({
  entry,
  onDecide,
  busy,
}: {
  entry: Entry
  onDecide: (p: AgentProposal, approve: boolean) => void
  busy: boolean
}) {
  if (entry.kind === 'user') {
    return (
      <div className="ml-8 rounded-sm bg-surface-3 px-3 py-2 text-body whitespace-pre-wrap text-ink">{entry.text}</div>
    )
  }
  if (entry.kind === 'error') {
    return <div className="rounded-sm border border-hairline px-3 py-2 text-body text-pnl-negative">{entry.text}</div>
  }
  if (entry.kind === 'proposal') {
    const p = entry.proposal
    const spends = Object.entries(p.spends).filter(([, v]) => v > 0)
    return (
      <div className="rounded-sm border border-hairline-strong bg-surface-2 p-3">
        <div className="mb-1 text-[11px] tracking-wide text-status-warning uppercase">Needs your approval</div>
        <div className="font-mono text-body-compact text-ink">{p.label}</div>
        <div className="mt-1 text-body-compact text-ink-muted">{p.summary}</div>
        {p.effects.length > 0 && (
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
        {entry.decided ? (
          <div className={cn('mt-2 flex items-center gap-1 text-body-compact', entry.decided === 'approved' ? 'text-pnl-positive' : 'text-ink-subtle')}>
            {entry.decided === 'approved' ? <CheckIcon className="size-3.5" /> : <XIcon className="size-3.5" />}
            {entry.decided === 'approved' ? 'Approved' : 'Rejected'}
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
  return (
    <div className="space-y-1.5">
      {entry.steps.length > 0 && (
        <details className="text-[11px] text-ink-subtle">
          <summary className="cursor-pointer">
            {entry.steps.length} step{entry.steps.length === 1 ? '' : 's'}
          </summary>
          <ul className="mt-1 space-y-0.5 font-mono">
            {entry.steps.map((s, i) => (
              <li key={i} className={cn(s.status === 'error' || s.status === 'refused' ? 'text-pnl-negative' : '')}>
                {s.tool}
                {s.label ? ` · ${s.label}` : s.action ? ` · ${s.action}` : ''} · {s.status}
                {s.httpStatus ? ` ${s.httpStatus}` : ''}
              </li>
            ))}
          </ul>
        </details>
      )}
      {entry.text && <div className="text-body leading-relaxed whitespace-pre-wrap text-ink">{renderInline(entry.text)}</div>}
    </div>
  )
}

/** **bold** and `code` only; the agent writes short plain answers. */
function renderInline(text: string) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={i}>{part.slice(2, -2)}</strong>
    if (part.startsWith('`') && part.endsWith('`'))
      return (
        <code key={i} className="rounded-xs bg-surface-3 px-1 font-mono text-[12px]">
          {part.slice(1, -1)}
        </code>
      )
    return part
  })
}
