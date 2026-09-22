/** The Template Lab draft: the open template, its undo history and the task's settings. */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { DEFAULT_SCOPE } from '@/lib/scope'
import type { LabDraft } from '@/screens/research-labs/lab-task'
import type { TemplateDoc } from '@/screens/research-labs/template/tree'

const UNDO_STEPS = 50

export interface TemplateDraft extends LabDraft {
  /** `preset:<slug>`, a saved template's id, or `null` for a new template. */
  templateId: string | number | null
  name: string
  doc: TemplateDoc | null
  /** Changed since it was opened or saved. */
  dirty: boolean
  /** Earlier trees, newest last. Not kept between visits. */
  past: TemplateDoc[]
}

interface Actions {
  set: (change: Partial<LabDraft>) => void
  open: (templateId: string | number | null, name: string, doc: TemplateDoc) => void
  edit: (doc: TemplateDoc) => void
  undo: () => void
  saved: (templateId: number, name: string) => void
}

export const useTemplateLab = create<TemplateDraft & Actions>()(
  persist(
    (set, get) => ({
      region: DEFAULT_SCOPE.region,
      delay: DEFAULT_SCOPE.delay,
      universe: DEFAULT_SCOPE.universe,
      datasetIds: [],
      cores: 4,
      simulations: null,
      decay: 0,
      vectorOperators: null,
      neutralizations: [],
      templateId: null,
      name: '',
      doc: null,
      dirty: false,
      past: [],
      set: (change) => set(change),
      open: (templateId, name, doc) => set({ templateId, name, doc, dirty: false, past: [] }),
      edit: (doc) => {
        const { doc: current, past } = get()
        set({
          doc,
          dirty: true,
          past: current ? [...past, current].slice(-UNDO_STEPS) : past,
        })
      },
      undo: () => {
        const { past } = get()
        if (past.length === 0) return
        set({ doc: past.at(-1) ?? null, past: past.slice(0, -1), dirty: true })
      },
      saved: (templateId, name) => set({ templateId, name, dirty: false }),
    }),
    {
      name: 'alpha-harness-template-lab',
      version: 1,
      partialize: (s) => ({
        region: s.region,
        delay: s.delay,
        universe: s.universe,
        datasetIds: s.datasetIds,
        cores: s.cores,
        simulations: s.simulations,
        decay: s.decay,
        vectorOperators: s.vectorOperators,
        templateId: s.templateId,
        name: s.name,
        doc: s.doc,
        dirty: s.dirty,
      }),
    },
  ),
)
