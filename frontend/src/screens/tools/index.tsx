/** Tools: every tool, two by two.
 *
 * The same card grid as Research Labs, so the two group screens read as one pattern. No
 * "Coming Soon" slots here: the labs are a fixed roster of four and the empty ones say so,
 * while tools are added when something needs one.
 */

import { Link } from '@tanstack/react-router'
import {
  ArrowUpRightIcon,
  LightbulbIcon,
  ListChecksIcon,
  type LucideIcon,
  SlidersHorizontalIcon,
} from 'lucide-react'
import { TOOL_TABS } from '@/shell/nav'
import { Page, PageHeader } from '@/ui/kit'

const ICONS: Record<(typeof TOOL_TABS)[number]['tab'], LucideIcon> = {
  'settings-sampler': SlidersHorizontalIcon,
  'submission-planner': ListChecksIcon,
  tips: LightbulbIcon,
}

/** What each one is for, since a name alone does not say when to reach for it. */
const ABOUT: Record<(typeof TOOL_TABS)[number]['tab'], string> = {
  'settings-sampler': 'Sweep one Alpha across settings to find where it works best.',
  'submission-planner': 'Pick which Alphas to submit, and in what order.',
  tips: "BRAIN's own advice on Turnover, Fitness, Sharpe and Prod Correlation.",
}

const number = (index: number) => String(index + 1).padStart(2, '0')

export function ToolsScreen() {
  return (
    <Page>
      <PageHeader title="Tools" />
      <div className="grid gap-3 sm:grid-cols-2">
        {TOOL_TABS.map((tool, index) => {
          const Icon = ICONS[tool.tab]
          return (
            <Link
              key={tool.tab}
              to={tool.to}
              className="panel-highlight group flex min-h-56 flex-col justify-between rounded-lg border border-hairline bg-surface-1 p-6 transition-colors hover:border-hairline-strong hover:bg-surface-2"
            >
              <div className="flex items-start justify-between">
                <span className="flex size-12 items-center justify-center rounded-md border border-hairline-strong bg-surface-2 text-ink-muted transition-colors group-hover:border-primary group-hover:text-primary">
                  <Icon className="size-6" aria-hidden />
                </span>
                <ArrowUpRightIcon
                  className="size-5 text-ink-tertiary transition-colors group-hover:text-ink"
                  aria-hidden
                />
              </div>
              <div className="flex flex-col gap-1">
                <span className="num text-body-compact text-ink-subtle">{number(index)}</span>
                <h2 className="text-headline font-semibold text-balance text-ink">{tool.label}</h2>
                <p className="max-w-prose text-body text-pretty text-ink-subtle">
                  {ABOUT[tool.tab]}
                </p>
              </div>
            </Link>
          )
        })}
      </div>
    </Page>
  )
}
