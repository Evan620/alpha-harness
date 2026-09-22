/** Tips: BRAIN's own advice on what makes an Alpha submittable, one day at a time.
 *
 * The tips shipped in `docs/TIPS.md` since the beginning and were shown nowhere. They are
 * the platform's own words, kept verbatim — this screen only decides which one is today's
 * and lets someone search the rest.
 */

import { ExternalLinkIcon, SearchIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Badge, Empty, Input, LINK, Page, PageHeader, Panel } from '@/ui/kit'
import { TIPS, type Tip, tipOfTheDay } from './tips'

function Links({ tip }: { tip: Tip }) {
  if (tip.links.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-body-compact">
      <span className="text-ink-subtle">Related</span>
      {tip.links.map((link) =>
        link.href ? (
          <a
            key={link.label}
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            className={`${LINK} inline-flex items-center gap-1`}
          >
            {link.label}
            <ExternalLinkIcon className="size-3 shrink-0" aria-hidden />
          </a>
        ) : (
          // BRAIN names these without linking them; shown as plain text rather than
          // invented links to pages that may not exist.
          <span key={link.label} className="text-ink-muted">
            {link.label}
          </span>
        ),
      )}
    </div>
  )
}

export function TipsScreen() {
  const [query, setQuery] = useState('')
  const today = useMemo(() => tipOfTheDay(), [])

  const found = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return TIPS
    return TIPS.filter(
      (tip) =>
        // By number too: the screen labels them, so "12" is a reasonable thing to type.
        String(tip.number) === needle ||
        tip.text.toLowerCase().includes(needle) ||
        tip.links.some((link) => link.label.toLowerCase().includes(needle)),
    )
  }, [query])

  return (
    <Page>
      <PageHeader
        title="Tips"
        description="WorldQuant BRAIN's own advice on Turnover, Fitness, Sharpe and Prod Correlation, in the platform's words. One is picked out each day; the rest are here to search."
      />

      {today && (
        <Panel title="Today's tip" description="The same tip all day, a different one tomorrow.">
          <div className="flex flex-col gap-3">
            <p className="max-w-prose text-title text-pretty text-ink">{today.text}</p>
            <Links tip={today} />
          </div>
        </Panel>
      )}

      <Panel
        title="All tips"
        description={`${TIPS.length} tips, transcribed from the platform.`}
        actions={
          <div className="relative">
            <SearchIcon
              className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-2 size-3.5 text-ink-subtle"
              aria-hidden
            />
            <Input
              className="w-56 pl-7"
              type="search"
              placeholder="Search tips"
              aria-label="Search tips"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        }
      >
        {found.length === 0 ? (
          <Empty title={`Nothing matches “${query}”`} />
        ) : (
          <ul className="flex flex-col gap-3">
            {found.map((tip) => (
              <li
                key={tip.number}
                className="flex flex-col gap-2 border-hairline border-b pb-3 last:border-0 last:pb-0"
              >
                <div className="flex items-start gap-3">
                  <Badge tone="outline" className="mt-0.5 shrink-0">
                    {tip.number}
                  </Badge>
                  <p className="max-w-prose text-body text-pretty text-ink">{tip.text}</p>
                </div>
                <div className="pl-10">
                  <Links tip={tip} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </Page>
  )
}
