/** The competition that is running, and where this account stands in it.
 *
 * Shown on the Dashboard rather than as a screen of its own because it is a deadline, and a
 * deadline is only useful where someone already looks every day. It points at the work
 * instead of repeating it: choosing fields is the Data Explorer's job and running the
 * search is a lab's, and neither gets a second copy here.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { TrophyIcon } from 'lucide-react'
import type { components } from '@/api/generated'
import { http } from '@/api/http'
import { fmt } from '@/lib/format'
import { Badge, Button, LINK, Metric, Panel } from '@/ui/kit'

type Competitions = components['schemas']['Competitions']
type Competition = components['schemas']['Competition']

/** A competition scored across every region at once, which is what makes region-agnostic
 *  simulations worth their four-children cost rather than a curiosity. */
const isAllRegion = (c: Competition) => c.name.toLowerCase().includes('all region')

function Deadline({ days }: { days: number | null }) {
  if (days === null) return null
  if (days < 0) return <Badge tone="neutral">Ended</Badge>
  if (days === 0) return <Badge tone="warn">Ends today</Badge>
  return (
    <Badge tone={days <= 7 ? 'warn' : 'neutral'}>
      {fmt.int(days)} {days === 1 ? 'day' : 'days'} left
    </Badge>
  )
}

function One({ competition: c }: { competition: Competition }) {
  const allRegion = isAllRegion(c)
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <TrophyIcon className="size-4 shrink-0 text-primary" aria-hidden />
        <span className="text-title text-ink">{c.name}</span>
        <Deadline days={c.daysLeft} />
        {!c.enrolled && <Badge tone="warn">Not entered</Badge>}
      </div>

      {c.enrolled ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <Metric
            label="Your rank"
            value={c.standing?.rank == null ? '—' : fmt.int(c.standing.rank)}
          />
          <Metric
            label="Alphas scored"
            value={c.standing?.alphas == null ? '—' : fmt.int(c.standing.alphas)}
          />
          <Metric label="Scoring" value={c.scoring ?? '—'} />
        </div>
      ) : (
        <p className="max-w-prose text-body text-ink-subtle">
          {c.signUpDaysLeft !== null && c.signUpDaysLeft >= 0
            ? `Sign-up closes in ${fmt.int(c.signUpDaysLeft)} ${c.signUpDaysLeft === 1 ? 'day' : 'days'}. Entering is done on BRAIN — it accepts an agreement in your name, so Alpha Harness never does it for you.`
            : 'Sign-up has closed for this one.'}
        </p>
      )}

      {allRegion && c.enrolled && (
        // The one place the region-agnostic machinery is worth explaining, because this is
        // the only time of year it pays for itself.
        <p className="max-w-prose text-body text-ink-subtle">
          This one is scored across every region at once. A region-agnostic simulation writes one
          Alpha and runs it in USA, Europe, Asia and Global together — it costs four of your daily
          simulations rather than one, and a child is submittable when it holds up in two regions or
          more.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {allRegion && (
          <>
            <Button size="sm" render={<Link to="/pyramids" />}>
              Sync all-region data
            </Button>
            <Button size="sm" variant="secondary" render={<Link to="/labs/search" />}>
              Start a search
            </Button>
          </>
        )}
        {c.faq && (
          <a href={c.faq} target="_blank" rel="noopener noreferrer" className={LINK}>
            Rules and FAQ
          </a>
        )}
      </div>
    </div>
  )
}

export function CompetitionPanel() {
  const query = useQuery({
    queryKey: ['competitions'],
    queryFn: () => http.get<Competitions>('/api/competitions'),
    // A competition runs for weeks; asking BRAIN on every Dashboard visit spends a request
    // to learn nothing.
    staleTime: 30 * 60 * 1000,
  })

  // Silent when there is nothing on: an empty "no competitions" panel every day of the year
  // is worse than no panel, and this one is never the reason someone opened the Dashboard.
  const live = (query.data?.competitions ?? []).filter(
    (c) => c.daysLeft === null || c.daysLeft >= 0,
  )
  if (live.length === 0) return null

  return (
    <Panel title="Competition">
      <div className="flex flex-col gap-5">
        {live.map((c) => (
          <One key={c.id} competition={c} />
        ))}
      </div>
    </Panel>
  )
}
