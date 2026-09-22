/**
 * BRAIN's neutralizations, in the two families its own documentation splits them into.
 *
 * The risk family neutralizes an Alpha against a risk model: RAM, the statistical model, the
 * crowding factors, and the fundamental factor models — "In BRAIN Research, fundamental models
 * such as SLOW, FAST, and SLOW_AND_FAST are already represented"
 * (`docs/learn/advanced-topics/getting-started-statistical-risk-neutralized-alphas`). Everything
 * else neutralizes against a group of instruments, or against nothing at all.
 *
 * Which of them a market actually offers is BRAIN's to say, so the groups here are only an
 * ordering: a picker intersects them with the legal values for the scope it is showing.
 */

/** Neutralization against a risk model. */
export const RISK_NEUTRALIZATIONS: readonly string[] = [
  'REVERSION_AND_MOMENTUM',
  'STATISTICAL',
  'CROWDING',
  'SLOW',
  'FAST',
  'SLOW_AND_FAST',
]

export interface NeutralizationGroup {
  id: 'risk' | 'other'
  label: string
  hint: string
}

export const NEUTRALIZATION_GROUPS: readonly NeutralizationGroup[] = [
  {
    id: 'risk',
    label: 'Risk Neutralization',
    hint: 'Neutralized against a risk model: RAM, Statistical, Crowding and the factor models.',
  },
  {
    id: 'other',
    label: 'Other',
    hint: 'Neutralized against a group of instruments, or not neutralized at all.',
  },
]

export const groupOf = (value: string): NeutralizationGroup['id'] =>
  RISK_NEUTRALIZATIONS.includes(value) ? 'risk' : 'other'

/**
 * The legal values for a scope, split into the two families and kept in BRAIN's own order
 * within each. A family the scope offers nothing for is dropped rather than shown empty.
 */
export function splitNeutralizations<T extends { value: string }>(
  available: readonly T[],
): { group: NeutralizationGroup; items: T[] }[] {
  return NEUTRALIZATION_GROUPS.map((group) => ({
    group,
    items: available.filter((choice) => groupOf(choice.value) === group.id),
  })).filter((block) => block.items.length > 0)
}
