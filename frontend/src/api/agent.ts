/** The in-app agent: a turn, a decision on a proposal, and the action catalog. */

import { http } from './http'

export interface PageContext {
  pathname: string
  title?: string
  area?: string
  scope?: Record<string, unknown> | null
  visible_text: string
}

export interface AgentStep {
  tool: string
  args: Record<string, unknown>
  status: 'ok' | 'executed' | 'needs_approval' | 'refused' | 'error'
  action?: string
  label?: string
  httpStatus?: number
  proposal?: AgentProposal
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

export interface AgentTurn {
  threadId: number
  reply: string
  steps: AgentStep[]
  proposals: AgentProposal[]
  navigate: string | null
}

export const agent = {
  turn: (body: { text: string; thread_id: number | null; context: PageContext }) =>
    http.post<AgentTurn>('/api/agent/turn', body),
  decide: (id: string, body: { approve: boolean; payload_hash: string; context: PageContext }) =>
    http.post<AgentTurn>(`/api/agent/proposals/${encodeURIComponent(id)}/decide`, body),
}
