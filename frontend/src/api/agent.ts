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

export const agent = {
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
