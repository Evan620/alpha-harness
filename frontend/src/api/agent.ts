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

export const agent = {
  goal: () => http.get<{ goal: Goal | null }>('/api/agent/goal'),
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
