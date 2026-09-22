/**
 * Vision's answers as Markdown. App routes become in-app links (no reload), BRAIN and other
 * URLs open in a new tab, and a bare route the model forgot to link is linked anyway.
 */

import { useNavigate } from '@tanstack/react-router'
import { ExternalLinkIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

const ROUTES = 'dashboard|matrix|data|labs|tools|tasks|pool|portfolio|ai|pyramids|alpha'
const BARE_ROUTE = new RegExp(
  `(^|[\\s(])(/(?:${ROUTES})(?:/[\\w\\-]+)*(?:\\?[\\w=&\\-]+)?)(?=[\\s.,;:!)]|$)`,
  'g',
)

/** Link bare routes outside code spans and existing links. */
function linkBareRoutes(text: string): string {
  return text
    .split(/(```[\s\S]*?```|`[^`]*`|\[[^\]]*\]\([^)]*\))/g)
    .map((part, i) =>
      i % 2 === 1
        ? part
        : part.replace(
            BARE_ROUTE,
            (_m, lead: string, route: string) => `${lead}[${route}](${route})`,
          ),
    )
    .join('')
}

function AppLink({ href, children }: { href?: string | undefined; children?: ReactNode }) {
  const navigate = useNavigate()
  if (!href) return <>{children}</>
  if (href.startsWith('/')) {
    const alphaId = /^\/alpha\/([A-Za-z0-9]+)$/.exec(href)?.[1]
    return (
      <>
        <a
          href={href}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey) return
            e.preventDefault()
            void navigate({ to: href })
          }}
          className="text-link underline decoration-hairline-strong underline-offset-2 hover:text-ink hover:decoration-current"
        >
          {children}
        </a>
        {alphaId && (
          <a
            href={`https://platform.worldquantbrain.com/alpha/${alphaId}`}
            target="_blank"
            rel="noopener noreferrer"
            title="Open on BRAIN"
            className="ml-0.5 inline-flex align-middle text-ink-subtle hover:text-ink"
          >
            <ExternalLinkIcon className="size-3" />
          </a>
        )}
      </>
    )
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-baseline gap-0.5 text-link underline decoration-hairline-strong underline-offset-2 hover:text-ink"
    >
      {children}
      <ExternalLinkIcon className="size-3 self-center" />
    </a>
  )
}

const COMPONENTS: Components = {
  a: ({ href, children }) => <AppLink href={href}>{children}</AppLink>,
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => (
    <ul className="my-2 list-disc space-y-1 pl-5 marker:text-ink-subtle">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 list-decimal space-y-1 pl-5 marker:text-ink-subtle">{children}</ol>
  ),
  li: ({ children }) => <li className="pl-0.5">{children}</li>,
  h1: ({ children }) => <h3 className="mt-3 mb-1 text-body font-semibold text-ink">{children}</h3>,
  h2: ({ children }) => <h3 className="mt-3 mb-1 text-body font-semibold text-ink">{children}</h3>,
  h3: ({ children }) => (
    <h4 className="mt-3 mb-1 text-body-compact font-semibold text-ink">{children}</h4>
  ),
  strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
  code: ({ children, className }) =>
    className ? (
      <code className="block overflow-auto rounded-xs bg-canvas p-2 font-mono text-[11.5px] text-ink-muted">
        {children}
      </code>
    ) : (
      <code className="rounded-xs bg-surface-3 px-1 py-px font-mono text-[12px] text-ink">
        {children}
      </code>
    ),
  pre: ({ children }) => <pre className="my-2">{children}</pre>,
  table: ({ children }) => (
    <div className="my-2 overflow-auto rounded-xs border border-hairline">
      <table className="w-full border-collapse text-body-compact">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-hairline bg-surface-2 px-2 py-1 text-left font-medium text-ink-muted">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border-b border-hairline px-2 py-1 align-top">{children}</td>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-hairline-strong pl-3 text-ink-muted">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-3 border-hairline" />,
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="text-body leading-relaxed text-ink">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {linkBareRoutes(text.replace(/\s+—\s+/g, ': ').replace(/—/g, ', '))}
      </ReactMarkdown>
    </div>
  )
}
