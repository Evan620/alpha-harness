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
  type BusEvent,
  type Goal,
  type PageContext,
  type VisionModel,
  type VisionSession,
} from '@/api/agent'
import { simulations } from '@/api/core'
import { errorMessage } from '@/api/http'
import { cn } from '@/lib/cn'
import { NAV } from '@/shell/nav'
import { Badge, Button, Textarea } from '@/ui/kit'
import { budgetLine, GOAL_KEY, GoalChip, statusOf } from './goal'
import { Markdown } from './markdown'
import { ModelChip } from './model'
import { ModeChip, PermissionsCard } from './permissions'
import { useVision } from './store'
import { WatchChip } from './watch'

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
  | { role: 'output'; text: string }
  | { role: 'loop'; kind: LoopKind; text: string; until?: number }

type LoopKind = 'start' | 'continue' | 'wait' | 'await_approval' | 'stop' | 'status'

const COMMANDS = [
  {
    name: '/goal',
    hint: '<condition> to start, or clear, pause, resume. Alone shows status',
    args: true,
  },
  {
    name: '/rules',
    hint: 'Rules Vision proposed from evidence. accept N or reject N to decide',
    args: true,
  },
  { name: '/playbooks', hint: 'Procedures Vision learned. archive N to retire one', args: true },
  { name: '/permissions', hint: 'Ask first or Auto: whether Vision asks before acting' },
  {
    name: '/model',
    hint: '<id or slug> [provider] to switch, auto to reset. Alone lists',
    args: true,
  },
  { name: '/research', hint: 'Research memory: loop, families, rounds, what is running' },
  { name: '/sessions', hint: 'Pick a saved conversation to switch to' },
  { name: '/watches', hint: 'Work Vision is monitoring. cancel N to stop one', args: true },
  { name: '/resume', hint: '<id> to reopen a session, alone opens the picker', args: true },
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
  //: The /sessions picker: open while non-null; the input filters it.
  const [sessionList, setSessionList] = useState<VisionSession[] | null>(null)
  const [sessionPick, setSessionPick] = useState(0)
  const [busy, setBusy] = useState(false)
  const [, setThreadIdState] = useState<number | null>(null)
  const threadRef = useRef<number | null>(null)
  const setThreadId = (id: number | null) => {
    threadRef.current = id
    setThreadIdState(id)
  }
  //: A goal turn the backend is running. The panel only watches it.
  const [autoBusy, setAutoBusy] = useState(false)
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

  const pushLoop = (kind: LoopKind, text: string, until?: number) =>
    setEntries((prev) => [...prev, { role: 'loop', kind, text, ...(until ? { until } : {}) }])

  /** The person's own turn, streamed straight back over its request. */
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

  // Follow what the backend's goal loop does. It runs with or without this tab, so the
  // panel is a viewer: reopening it replays what happened meanwhile, then carries on live.
  // biome-ignore lint/correctness/useExhaustiveDependencies: one follower per mount
  useEffect(() => {
    const stop = new AbortController()
    let since = -1
    const onBus = (e: BusEvent) => {
      since = Math.max(since, e.seq ?? since)
      if (e.type === 'ping') return
      if (e.type === 'goal') {
        queryClient.setQueryData(GOAL_KEY, { goal: e.goal })
        return
      }
      if (e.type === 'loop') {
        pushLoop(e.kind, e.text, e.until)
        return
      }
      if (!e.auto) return
      if (e.type === 'turn_start') {
        if (e.threadId) setThreadId(e.threadId)
        setAutoBusy(true)
        setEntries((prev) => [...prev, { role: 'agent', parts: [], live: true }])
        return
      }
      if (e.type === 'turn_end') {
        setAutoBusy(false)
        setEntries((prev) =>
          prev.map((en, i) =>
            i === prev.length - 1 && en.role === 'agent' ? { ...en, live: false } : en,
          ),
        )
        void queryClient.invalidateQueries()
        return
      }
      if (e.type === 'navigate') void navigate({ to: e.to })
      setEntries((prev) => {
        const next = [...prev]
        for (let i = next.length - 1; i >= 0; i--) {
          const en = next[i]
          if (en?.role === 'agent' && en.live) {
            next[i] = { ...en, parts: apply(en.parts, e as AgentEvent) }
            break
          }
        }
        return next
      })
    }
    const follow = async () => {
      while (!stop.signal.aborted) {
        try {
          await agent.events(since, onBus, stop.signal)
        } catch {
          // dropped: reconnect from where we left off
        }
        if (!stop.signal.aborted) await new Promise((r) => setTimeout(r, 2000))
      }
    }
    void follow()
    return () => stop.abort()
  }, [])

  const pauseGoal = async () => {
    const res = await agent.pauseGoal().catch(() => null)
    if (res) queryClient.setQueryData(GOAL_KEY, res)
  }

  const resumeGoal = async () => {
    const res = await agent.resumeGoal().catch(() => null)
    if (!res?.goal) pushLoop('status', 'No goal to resume. Start one with /goal <condition>.')
  }

  /** `/goal` alone: where the goal stands, as a line in the conversation. */
  const showGoal = async () => {
    const res = await agent.goal().catch(() => null)
    const g = res?.goal
    if (!g) {
      const last = res?.last
      pushLoop(
        'status',
        last
          ? `No goal running. The last one ended ${statusOf(last.status).label.toLowerCase()} after ${last.turns} of ${last.maxTurns} turns: ${last.objective}. ${last.stoppedReason} Start another with /goal <condition>.`
          : 'No goal set. Start one with /goal <condition>, e.g. /goal find 2 EUR/D1 alphas that pass every check turns=10 sims=500',
      )
      return
    }
    queryClient.setQueryData(GOAL_KEY, res)
    const judge = g.lastReason ? ` Judge: ${g.lastReason}` : ''
    pushLoop(
      'status',
      `${statusOf(g.status).label}, turn ${g.turns} of ${g.maxTurns}, ${budgetLine(g)}. Goal: ${g.objective}.${judge}`,
    )
  }

  const clearGoal = async () => {
    await agent.clearGoal().catch(() => null)
    queryClient.setQueryData(GOAL_KEY, { goal: null })
  }

  /** `/goal <condition>`, with optional turns=N and sims=N anywhere in it. */
  const setGoal = async (text: string) => {
    let turns = 20
    let sims = 0
    const objective = text
      .replace(/\b(turns|sims|simulations)\s*=\s*(\d+)/gi, (_m, key: string, value: string) => {
        if (key.toLowerCase() === 'turns') turns = Math.min(100, Math.max(1, Number(value)))
        else sims = Math.min(5000, Number(value))
        return ''
      })
      .replace(/\s{2,}/g, ' ')
      .trim()
    if (objective.length < 3) {
      pushLoop(
        'status',
        'Say what the goal is, e.g. /goal find 2 EUR/D1 alphas that pass every check',
      )
      return
    }
    try {
      const res = await agent.setGoal({
        objective,
        done_when: '',
        brain_simulations: sims,
        max_turns: turns,
        thread_id: threadRef.current,
        context: readContext(pathname),
      })
      queryClient.setQueryData(GOAL_KEY, res)
      // Carry on in the goal's conversation straight away, so a message sent before the
      // first goal turn is announced still lands in the right place.
      if (res.goal.threadId) setThreadId(res.goal.threadId)
    } catch (error) {
      pushLoop('stop', `Could not set the goal: ${errorMessage(error)}`)
    }
  }

  const goalCommand = (arg: string) => {
    const word = arg.trim().toLowerCase()
    if (!word) return void showGoal()
    if (['clear', 'stop', 'off', 'reset', 'none', 'cancel'].includes(word)) return void clearGoal()
    if (word === 'pause') return void pauseGoal()
    if (word === 'resume') return void resumeGoal()
    if (busy) {
      pushLoop('status', 'Vision is mid-turn. Stop it first, or wait for this turn to finish.')
      return
    }
    void setGoal(arg.trim())
  }

  /** `/rules [accept|reject N]`: what Vision proposed, and the person's decision on it. */
  const rulesCommand = async (arg: string) => {
    const m = /^(accept|reject)\s+#?(\d+)$/i.exec(arg.trim())
    if (m) {
      const decision = (m[1] ?? '').toLowerCase() as 'accept' | 'reject'
      await agent.decideRule(Number(m[2]), decision).catch((e) => pushLoop('stop', errorMessage(e)))
      return
    }
    const rules = await agent.rules().catch(() => [])
    const open = rules.filter((r) => r.status === 'proposed')
    const accepted = rules.filter((r) => r.status === 'accepted')
    if (!rules.length) {
      pushLoop(
        'status',
        'No rules yet. Vision proposes one when the evidence contradicts its doctrine.',
      )
      return
    }
    for (const r of open)
      pushLoop(
        'status',
        `Proposed #${r.id}: ${r.text} Evidence: ${r.evidence} (/rules accept ${r.id} or /rules reject ${r.id})`,
      )
    for (const r of accepted) pushLoop('status', `Accepted #${r.id}: ${r.text}`)
    if (!open.length)
      pushLoop(
        'status',
        `Nothing waiting on you. ${accepted.length} accepted rule(s) steer Vision.`,
      )
  }

  /** `/playbooks [archive N]`: procedures Vision learned from earlier work. */
  const playbooksCommand = async (arg: string) => {
    const m = /^archive\s+#?(\d+)$/i.exec(arg.trim())
    if (m) {
      const res = await agent.archivePlaybook(Number(m[1])).catch((e) => {
        pushLoop('stop', errorMessage(e))
        return null
      })
      if (res) pushLoop('status', `Archived playbook #${res.id}: ${res.name}`)
      return
    }
    const books = await agent.playbooks().catch(() => [])
    if (!books.length) {
      pushLoop('status', 'No playbooks yet. Vision writes one after a goal succeeds.')
      return
    }
    for (const b of books)
      pushLoop(
        'status',
        `#${b.id} ${b.name}: worked ${b.successes}, failed ${b.failures}, used ${b.uses}. ${b.whenToUse}`,
      )
  }

  const showPermissions = () => {
    setEntries((prev) => [...prev.filter((e) => e.role !== 'permissions'), { role: 'permissions' }])
  }

  /** Terminal-style output: plain monospace text in the transcript, no card. */
  const print = (text: string) => setEntries((prev) => [...prev, { role: 'output', text }])

  /** `/model [id|slug] [provider]` or `/model auto`. Alone, prints the model and choices. */
  const modelCommand = async (arg: string) => {
    const [first, provider] = arg.trim().split(/\s+/)
    const line = (m: VisionModel) =>
      `Model: ${m.effective?.label ?? 'none'} (${m.effective?.id ?? '-'}, ${m.effective?.provider ?? '-'})${m.model ? '' : ', automatic'}`
    try {
      if (first) {
        const next = await agent.setModel(
          first.toLowerCase() === 'auto'
            ? { model: null }
            : { model: first, provider: provider ?? null },
        )
        queryClient.setQueryData(['vision', 'model'], next)
        print(line(next))
        return
      }
      const m = await agent.model()
      const width = Math.max(8, ...m.options.map((o) => o.id.length))
      print(
        [
          line(m),
          '/model <id> [provider] to switch, /model auto to reset. Any slug a provider serves works.',
          ...m.options.map(
            (o) =>
              `${o.id === m.effective?.id ? '*' : ' '} ${o.id.padEnd(width)}  ${o.provider.padEnd(7)} ${o.summary}`,
          ),
        ].join('\n'),
      )
    } catch (e) {
      print(`Model not changed: ${errorMessage(e)}`)
    }
  }

  /** `/research`: the research memory, as text. */
  const researchCommand = async () => {
    try {
      const [r, active] = await Promise.all([
        agent.research(),
        simulations.active().catch(() => []),
      ])
      const running = active.length
        ? `${active.length} simulating now (Simulation Matrix, /matrix).`
        : 'Nothing simulating.'
      const lines = [`Research loop: ${r.loop.toUpperCase()}. ${running}`]
      if (r.required.length) lines.push('Required first:', ...r.required.map((x) => `  - ${x}`))
      if (r.families.length) {
        lines.push('Families:')
        for (const f of r.families)
          lines.push(
            `  ${f.name}  [${f.status}]  ${f.experiments} exp, best ${f.bestAlphaId || '-'} fitness ${f.bestFitness ?? '-'}, ${f.noImproveStreak} without gain${f.lastBottleneck ? `, ${f.lastBottleneck}` : ''}`,
            ...(f.mechanism ? [`    ${f.mechanism}`] : []),
          )
      } else lines.push('No families yet. Set a /goal and Vision starts with a hypothesis.')
      if (r.closedFamilies.length)
        lines.push(`Closed: ${r.closedFamilies.map((f) => f.name).join(', ')}`)
      if (r.recentRounds.length)
        lines.push(
          'Recent rounds:',
          ...r.recentRounds.map(
            (x) =>
              `  #${x.number} ${x.mode.replaceAll('_', ' ')}${x.alpha_id ? ` ${x.alpha_id}` : ''}${x.changed_dimension ? ` (${x.changed_dimension})` : ''} -> ${x.next_action || x.decision}`,
          ),
        )
      print(lines.join('\n'))
    } catch (e) {
      print(`Research memory unavailable: ${errorMessage(e)}`)
    }
  }

  /** `/watches [cancel N]`: work Vision is monitoring, as text. */
  const watchesCommand = async (arg: string) => {
    const m = /^cancel\s+#?(\d+)$/i.exec(arg.trim())
    try {
      if (m) {
        await agent.cancelWatch(Number(m[1]))
        return print(`Watch #${m[1]} cancelled.`)
      }
      const rows = await agent.watches()
      if (!rows.length)
        return print('No watches. Vision sets one when it starts work worth waiting on.')
      print(
        [
          'Watches. /watches cancel <id> to stop one.',
          ...rows.map(
            (w) =>
              `  #${w.id}  session #${w.thread_id}  ${w.what}, up to ${w.minutesLeft}m more. Then: ${w.then}`,
          ),
        ].join('\n'),
      )
    } catch (e) {
      print(`Watches unavailable: ${errorMessage(e)}`)
    }
  }

  /** `/sessions`: a picker over saved conversations, newest first, like Claude Code's. */
  const sessionsCommand = async () => {
    try {
      const rows = await agent.sessions()
      if (!rows.length) return print('No saved sessions yet.')
      setSessionList(rows)
      // Start on the newest session you are NOT in: switching is the point of the picker.
      const other = rows.findIndex((r) => r.id !== threadRef.current)
      setSessionPick(Math.max(other, 0))
      setText('')
      requestAnimationFrame(() =>
        document.querySelector<HTMLTextAreaElement>('aside[aria-label=Vision] textarea')?.focus(),
      )
    } catch (e) {
      print(`Sessions unavailable: ${errorMessage(e)}`)
    }
  }

  /** `/resume <id>`: reopen a saved session; the next message carries on in it. */
  const resumeCommand = async (arg: string) => {
    if (!arg.trim()) return void sessionsCommand()
    const id = Number(arg.trim().replace(/^#/, ''))
    if (!Number.isInteger(id) || id <= 0) return print('Usage: /resume <id>, or /sessions to pick')
    if (id === threadRef.current) return print(`You are already in session #${id}.`)
    try {
      const session = await agent.session(id)
      setThreadId(session.id)
      setEntries([
        ...(session.entries as Entry[]),
        { role: 'output', text: `Resumed session #${session.id}. Your next message continues it.` },
      ])
    } catch (e) {
      print(`Could not resume #${id}: ${errorMessage(e)}`)
    }
  }

  const send = (message: string) => {
    const trimmed = message.trim()
    if (!trimmed) return
    // /goal works mid-turn too: clearing or pausing a running loop is the point of it.
    const goal = /^\/goal(?:\s+([\s\S]*))?$/i.exec(trimmed)
    if (goal) {
      setText('')
      return goalCommand(goal[1] ?? '')
    }
    const learned = /^\/(rules|playbooks)(?:\s+([\s\S]*))?$/i.exec(trimmed)
    if (learned) {
      setText('')
      const which = (learned[1] ?? '').toLowerCase()
      return void (which === 'rules' ? rulesCommand : playbooksCommand)(learned[2] ?? '')
    }
    if (busy) return
    setText('')
    // Slash commands are handled here and never reach the model.
    if (trimmed === '/permissions') return showPermissions()
    const command = /^\/(model|resume|watches)(?:\s+([\s\S]*))?$/i.exec(trimmed)
    if (command) {
      const which = (command[1] ?? '').toLowerCase()
      const run =
        which === 'model' ? modelCommand : which === 'watches' ? watchesCommand : resumeCommand
      return void run(command[2] ?? '')
    }
    if (trimmed === '/research') return void researchCommand()
    if (trimmed === '/sessions') return void sessionsCommand()
    if (trimmed === '/new' || trimmed === '/clear') {
      setEntries([])
      setThreadId(null)
      return
    }
    // A goal turn in progress is pre-empted by the backend; the loop judges again after.
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

  const choose = (c: (typeof COMMANDS)[number]) => {
    if ('args' in c && c.args) {
      setText(`${c.name} `)
      requestAnimationFrame(() =>
        document.querySelector<HTMLTextAreaElement>('aside[aria-label=Vision] textarea')?.focus(),
      )
    } else send(c.name)
  }

  // Slash commands: the menu opens on "/" and filters as you type.
  const slash = /^\/\S*$/.test(text) ? text.toLowerCase() : null
  const menu = slash ? COMMANDS.filter((c) => c.name.startsWith(slash)) : []
  const pickIndex = Math.min(pick, Math.max(menu.length - 1, 0))
  const sessionQuery = sessionList ? text.trim().toLowerCase().replace(/^#/, '') : ''
  const sessionRows = (sessionList ?? []).filter(
    (r) =>
      !sessionQuery ||
      String(r.id).startsWith(sessionQuery) ||
      r.title.toLowerCase().includes(sessionQuery),
  )
  const sessionIndex = Math.min(sessionPick, Math.max(sessionRows.length - 1, 0))
  const closeSessions = () => {
    setSessionList(null)
    setText('')
  }
  const openSession = (row: VisionSession | undefined) => {
    if (!row) return
    setSessionList(null)
    setText('')
    void resumeCommand(String(row.id))
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
      className="flex h-full w-[440px] max-w-[50vw] shrink-0 flex-col border-l border-hairline bg-surface-1"
    >
      <header className="flex items-center gap-2 border-b border-hairline px-3 py-2">
        <EyeIcon className="size-4 text-ink-muted" />
        <div className="min-w-0 flex-1">
          <div className="text-body font-medium text-ink">Vision</div>
          <div className="truncate text-[11px] text-ink-subtle">Sees {pathname}</div>
        </div>
        <WatchChip />
        <GoalChip onClick={() => void showGoal()} />
        <ModelChip onClick={() => void modelCommand('')} />
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
              entry.role === 'loop' ? (
                <LoopLine key={i} kind={entry.kind} text={entry.text} until={entry.until} />
              ) : entry.role === 'permissions' ? (
                <PermissionsCard
                  key={i}
                  onDone={() => setEntries((prev) => prev.filter((e) => e.role !== 'permissions'))}
                />
              ) : entry.role === 'output' ? (
                <pre
                  key={i}
                  className="overflow-x-auto font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-ink-muted"
                >
                  {entry.text}
                </pre>
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
        {sessionList && (
          <div
            role="listbox"
            aria-label="Sessions"
            className="absolute right-3 bottom-full left-3 mb-1 max-h-80 overflow-auto rounded-sm border border-hairline-strong bg-surface-2 shadow-lg"
          >
            <div className="px-3 pt-1.5 pb-1 text-[11px] text-ink-subtle">
              Sessions. Type to filter, Enter to open, Esc to close.
            </div>
            {sessionRows.length === 0 && (
              <div className="px-3 py-1.5 text-body-compact text-ink-subtle">No match.</div>
            )}
            {sessionRows.map((r, i) => (
              <button
                key={r.id}
                type="button"
                role="option"
                aria-selected={i === sessionIndex}
                onMouseEnter={() => setSessionPick(i)}
                onMouseDown={(e) => {
                  e.preventDefault()
                  openSession(r)
                }}
                className={cn(
                  'flex w-full items-baseline gap-2 px-3 py-1 text-left font-mono text-[11.5px]',
                  i === sessionIndex ? 'bg-surface-3' : 'hover:bg-surface-3',
                )}
              >
                <span className="w-3 shrink-0 text-ink-subtle">
                  {r.id === threadRef.current ? '>' : ''}
                </span>
                <span className="w-9 shrink-0 text-ink-subtle">#{r.id}</span>
                <span className="w-14 shrink-0 text-ink-subtle">{ago(r.updated)}</span>
                {(r.running || r.goal) && (
                  <span
                    className={cn(
                      'shrink-0',
                      r.running ? 'text-status-warning' : 'text-ink-subtle',
                    )}
                  >
                    {r.running ? 'goal running' : `goal ${r.goal?.status}`}
                  </span>
                )}
                <span className="truncate text-ink">{r.title}</span>
              </button>
            ))}
          </div>
        )}
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
                  choose(c)
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
            setSessionPick(0)
            if (sessionList && e.target.value.startsWith('/')) setSessionList(null)
          }}
          onKeyDown={(e) => {
            if (sessionList) {
              if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault()
                const n = Math.max(sessionRows.length, 1)
                const step = e.key === 'ArrowDown' ? 1 : -1
                setSessionPick((p) => (p + step + n) % n)
                return
              }
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                openSession(sessionRows[sessionIndex])
                return
              }
              if (e.key === 'Escape') {
                e.preventDefault()
                closeSessions()
                return
              }
            }
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
                // Enter on an exact name runs it (bare /goal shows status); otherwise complete.
                if (chosen && e.key === 'Enter' && text.trim().toLowerCase() === chosen.name)
                  send(chosen.name)
                else if (chosen) choose(chosen)
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
          placeholder={
            sessionList
              ? 'Filter sessions by title or #id'
              : 'Ask Vision, or /goal <what to achieve>'
          }
          className="min-h-0 flex-1 resize-none py-2"
        />
        {busy || autoBusy ? (
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
  status: 'text-ink-muted',
  start: 'text-primary',
  continue: 'text-ink-subtle',
  wait: 'text-status-warning',
  await_approval: 'text-status-warning',
  stop: 'text-ink',
}

/** One line of the goal loop between turns: what the judge decided and why. */
function ago(epochSeconds: number): string {
  const s = Math.max(0, Date.now() / 1000 - epochSeconds)
  if (s < 90) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86_400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86_400)}d ago`
}

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
