/**
 * BRAIN's own simulation tips, read straight out of `docs/TIPS.md`.
 *
 * Imported as text at build time rather than copied into the app: `docs/` is the source of
 * truth for everything transcribed from the platform, and a second copy here would be a
 * second thing to keep in step. Vite inlines it, so the tips ship inside the bundle the
 * backend already serves and no file has to be found at runtime.
 */

import source from '../../../../../docs/TIPS.md?raw'

export interface TipLink {
  label: string
  /** Absent for a related item BRAIN names without linking, e.g. "trade_when Operator". */
  href?: string
}

export interface Tip {
  /** BRAIN's own numbering, kept so a tip can be talked about. */
  number: number
  text: string
  links: TipLink[]
}

/** A markdown link, or a bare name. One `Related:` line mixes both:
 *  `Related: [backfilling](…), ts_backfill, group_backfill`. The bare branch excludes `[`
 *  so it cannot start on the space before a link and swallow it as plain text. */
const ITEM = /\[([^\]]+)\]\(([^)]+)\)|([^,[]+)/g

/** One `Related:` line into its parts, linked or not.
 *
 * Both shapes in one pass rather than links-or-names: taking the links and stopping drops
 * the bare names that follow them, and BRAIN writes at least one tip that way. */
function related(line: string): TipLink[] {
  const body = line.slice('Related:'.length).trim()
  if (!body) return []
  const links: TipLink[] = []
  for (const match of body.matchAll(ITEM)) {
    const [, label, href, bare] = match
    if (label && href) links.push({ label: label.trim(), href })
    else if (bare?.trim()) links.push({ label: bare.trim() })
  }
  return links
}

function parse(markdown: string): Tip[] {
  const tips: Tip[] = []
  let current: Tip | null = null
  for (const raw of markdown.split('\n')) {
    const line = raw.trim()
    const heading = /^##\s+Tip\s+(\d+)/.exec(line)
    if (heading) {
      current = { number: Number(heading[1]), text: '', links: [] }
      tips.push(current)
      continue
    }
    if (!current || !line || line.startsWith('#')) continue
    if (line.startsWith('Related:')) current.links = related(line)
    // Paragraphs join with a space: a tip is one or two sentences and reads as a block.
    else current.text = current.text ? `${current.text} ${line}` : line
  }
  return tips.filter((tip) => tip.text.length > 0)
}

export const TIPS: Tip[] = parse(source)

/**
 * The tip for a given day, the same one all day and a different one tomorrow.
 *
 * Keyed on the date rather than picked at random so that "today's tip" means something:
 * two people comparing notes on the same day see the same tip, and a reload does not
 * shuffle it away mid-read.
 */
export function tipOfTheDay(today: Date = new Date()): Tip | undefined {
  if (TIPS.length === 0) return undefined
  // UTC components, not local ones: the promise above is that two people comparing notes
  // on the same day see the same tip, and local components would break that across
  // timezones while still looking correct to each of them.
  const day = Math.floor(
    Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()) / 86_400_000,
  )
  return TIPS[day % TIPS.length]
}
