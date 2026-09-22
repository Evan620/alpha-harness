import {
  ChartPieIcon,
  DatabaseIcon,
  FlaskConicalIcon,
  Grid3x3Icon,
  LayersIcon,
  LayoutGridIcon,
  ListChecksIcon,
  RefreshCwIcon,
  SparklesIcon,
  WrenchIcon,
} from 'lucide-react'

/** Sub-tabs, in display order. Screens import these so the sidebar, ⌘K and tab bars agree. */
export const POOL_TABS = [
  { tab: 'stored', label: 'Stored' },
  { tab: 'submittable', label: 'Submittable' },
] as const

export const AI_TABS = [
  { tab: 'providers', label: 'Providers' },
  { tab: 'keys', label: 'Keys' },
  { tab: 'budget', label: 'Budget' },
  { tab: 'prompts', label: 'Prompts' },
  { tab: 'assistant', label: 'Assistant' },
] as const

/** Shown nested under Research Labs in the sidebar, each with its own route. */
export const LAB_TABS = [
  { tab: 'search', label: 'Search Lab', to: '/labs/search' },
  { tab: 'template', label: 'Template Lab', to: '/labs/template' },
  { tab: 'evolution', label: 'Evolution Lab', to: '/labs/evolution' },
  { tab: 'power-pool', label: 'LLM Power Pool Lab', to: '/labs/power-pool' },
] as const

/** Shown nested under Tools in the sidebar, each with its own route. */
export const TOOL_TABS = [
  { tab: 'settings-sampler', label: 'Settings Sampler', to: '/tools/settings-sampler' },
  { tab: 'submission-planner', label: 'Submission Planner', to: '/tools/submission-planner' },
  { tab: 'tips', label: 'Tips', to: '/tools/tips' },
] as const

export const NAV = [
  {
    to: '/dashboard',
    area: 'dashboard',
    label: 'Dashboard',
    icon: LayoutGridIcon,
    tabs: [],
  },
  {
    to: '/matrix',
    area: 'matrix',
    label: 'Simulation Matrix',
    icon: Grid3x3Icon,
    tabs: [],
  },
  {
    to: '/data',
    area: 'data',
    label: 'Data Explorer',
    icon: DatabaseIcon,
    tabs: [],
  },
  {
    to: '/labs',
    area: 'labs',
    label: 'Research Labs',
    icon: FlaskConicalIcon,
    tabs: LAB_TABS,
    nested: true,
  },
  {
    to: '/tools',
    area: 'tools',
    label: 'Tools',
    icon: WrenchIcon,
    tabs: TOOL_TABS,
    nested: true,
  },
  {
    to: '/tasks',
    area: 'tasks',
    label: 'Tasks',
    icon: ListChecksIcon,
    tabs: [],
  },
  {
    to: '/pool',
    area: 'pool',
    label: 'Alphas',
    icon: LayersIcon,
    tabs: POOL_TABS,
  },
  {
    to: '/portfolio',
    area: 'portfolio',
    label: 'Portfolio',
    icon: ChartPieIcon,
    tabs: [],
  },
  {
    to: '/ai',
    area: 'ai',
    label: 'LLM Integration',
    icon: SparklesIcon,
    tabs: AI_TABS,
  },
  {
    to: '/pyramids',
    area: 'pyramids',
    label: 'Sync with BRAIN',
    icon: RefreshCwIcon,
    tabs: [],
  },
] as const
