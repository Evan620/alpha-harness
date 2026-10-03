/** Vision, the in-app agent: streamed turns and decisions (NDJSON). */

import { ApiError, http, normalise } from './http'

export interface PageContext {
  pathname: string
  title?: string
  area?: string
  scope?: Record<string, unknown> | null
  visible_text: string
}

export interface AgentProposal {
  id: string
  tool: string
  label: string
  summary: string
  detail: string
  effects: string[]
  arguments: Record<string, unknown>
  payloadHash: string
  spends: Record<string, number>
  irreversible: boolean
  status: string
}

export type AgentEvent =
  | { type: 'start'; threadId: number }
  | { type: 'text'; delta: string }
  | { type: 'thinking'; delta: string }
  | { type: 'status'; delta: string }
  | {
      type: 'tool_start'
      id: string
      tool: string
      label: string
      args: Record<string, unknown>
    }
  | {
      type: 'tool_end'
      id: string
      status: string
      httpStatus: number | null
      preview: string
    }
  | { type: 'proposal'; proposal: AgentProposal }
  | { type: 'navigate'; to: string }
  | { type: 'error'; message: string }
  | { type: 'done'; threadId: number }

async function stream(
  path: string,
  body: unknown,
  onEvent: (e: AgentEvent) => void,
  signal?: AbortSignal,
) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'X-Harness-Client': '1', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: signal ?? null,
  })
  if (!response.ok || !response.body) {
    const raw = await response.text()
    let parsed: unknown = raw
    try {
      parsed = JSON.parse(raw)
    } catch {
      // plain text
    }
    throw new ApiError(response.status, normalise(response.status, parsed))
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as AgentEvent)
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer) as AgentEvent)
}

export type PermissionMode = 'ask' | 'auto'

export interface Permissions {
  mode: PermissionMode
  alwaysYours: { action: string; summary: string; tier: string }[]
}

export interface VisionModelOption {
  id: string
  label: string
  provider: string
  summary: string
  rpm: number
  rpd: number
  recommended: boolean
  discovered: boolean
}

export interface ResearchFamily {
  name: string
  status: string
  stopReason: string
  mechanism: string
  proxy: string
  experiments: number
  bestAlphaId: string
  bestFitness: number | null
  noImproveStreak: number
  lastBottleneck: string
  stopDue: string | null
  lessons: string[]
}

export interface ResearchRound {
  number: number
  mode: string
  family: string
  decision: string
  next_action: string
  reason: string
  alpha_id: string
  changed_dimension: string
  bottleneck: string
  lesson: string
  at: number
}

/** Vision's research memory: one hypothesis, one change, one result per goal round. */
export interface ResearchState {
  loop: 'search' | 'improvement'
  required: string[]
  families: ResearchFamily[]
  closedFamilies: { name: string; status: string; reason: string }[]
  recentRounds: ResearchRound[]
}

/** Work Vision chose to monitor; it is woken in that conversation when the work ends. */
export interface VisionWatch {
  id: number
  thread_id: number
  kind: 'task' | 'simulations'
  task_id: number | null
  then: string
  what: string
  minutesLeft: number
}

/** One saved Vision conversation, for /sessions. */
export interface VisionSession {
  id: number
  title: string
  updated: number
  turns: number
  goal: { objective: string; status: string } | null
  running: boolean
}

/** Which model Vision thinks with. Only the person sets it, with /model. */
export interface VisionModel {
  model: string | null
  default: string
  effective: { id: string; label: string; provider: string } | null
  options: VisionModelOption[]
  providers: { id: string; label: string; paid: boolean }[]
}

export interface GoalVerdict {
  turn: number
  verdict: string
  reason: string
  at: number
}

export interface Goal {
  objective: string
  doneWhen: string
  status: string
  running: boolean
  stoppedReason: string
  createdAt: number
  turns: number
  maxTurns: number
  lastVerdict: string
  lastReason: string
  waitUntil: number | null
  threadId: number | null
  history: GoalVerdict[]
  budgets: Record<string, number>
  spent: Record<string, number>
  remaining: Record<string, number | null>
}

/** An event from the backend's own goal loop, with its place in the log. */
export type BusEvent =
  | (AgentEvent & { seq?: number; auto?: boolean; threadId?: number })
  | { type: 'turn_start' | 'turn_end'; seq?: number; auto?: boolean; threadId?: number }
  | {
      type: 'loop'
      kind: 'start' | 'continue' | 'wait' | 'await_approval' | 'stop' | 'status'
      text: string
      until?: number
      seq?: number
      auto?: boolean
    }
  | { type: 'goal'; goal: Goal | null; seq?: number; auto?: boolean }
  | { type: 'ping'; seq?: number; auto?: boolean }

async function follow(
  since: number,
  onEvent: (e: BusEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/agent/events?since=${since}`, { signal })
  if (!response.ok || !response.body) throw new Error(`events ${response.status}`)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) return
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as BusEvent)
  }
}

export interface LearnedRule {
  id: number
  text: string
  evidence: string
  status: 'proposed' | 'accepted' | 'rejected'
  author: string
}

export interface LearnedPlaybook {
  id: number
  name: string
  whenToUse: string
  steps: string
  uses: number
  successes: number
  failures: number
  status: string
}

export const agent = {
  rules: () => http.get<LearnedRule[]>('/api/doctrine'),
  decideRule: (id: number, decision: 'accept' | 'reject') =>
    http.post<{ id: number; status: string; text: string }>(`/api/agent/rules/${id}`, { decision }),
  playbooks: () => http.get<LearnedPlaybook[]>('/api/playbooks'),
  archivePlaybook: (id: number) =>
    http.post<{ id: number; name: string }>(`/api/agent/playbooks/${id}/archive`),
  goal: () => http.get<{ goal: Goal | null; last?: Goal | null }>('/api/agent/goal'),
  setGoal: (body: {
    objective: string
    done_when: string
    brain_simulations: number
    max_turns: number
    thread_id: number | null
    context: PageContext
  }) => http.put<{ goal: Goal }>('/api/agent/goal', body),
  events: follow,
  pauseGoal: () => http.post<{ goal: Goal | null }>('/api/agent/goal/pause'),
  resumeGoal: () => http.post<{ goal: Goal | null }>('/api/agent/goal/resume'),
  clearGoal: () => http.del<{ goal: null }>('/api/agent/goal'),
  permissions: () => http.get<Permissions>('/api/agent/permissions'),
  setPermissions: (mode: PermissionMode) =>
    http.put<Permissions>('/api/agent/permissions', { mode }),
  research: () => http.get<ResearchState>('/api/research/state'),
  sessions: () => http.get<VisionSession[]>('/api/agent/sessions'),
  watches: () => http.get<VisionWatch[]>('/api/agent/watches'),
  cancelWatch: (id: number) => http.del<{ cancelled: number }>(`/api/agent/watches/${id}`),
  session: (id: number) =>
    http.get<{ id: number; entries: unknown[] }>(`/api/agent/sessions/${id}`),
  model: () => http.get<VisionModel>('/api/agent/model'),
  setModel: (body: { model: string | null; provider?: string | null }) =>
    http.put<VisionModel>('/api/agent/model', body),
  turn: (
    body: { text: string; thread_id: number | null; context: PageContext },
    onEvent: (e: AgentEvent) => void,
    signal?: AbortSignal,
  ) => stream('/api/agent/turn', body, onEvent, signal),
  decide: (
    id: string,
    body: { approve: boolean; payload_hash: string; context: PageContext },
    onEvent: (e: AgentEvent) => void,
    signal?: AbortSignal,
  ) => stream(`/api/agent/proposals/${encodeURIComponent(id)}/decide`, body, onEvent, signal),
}
