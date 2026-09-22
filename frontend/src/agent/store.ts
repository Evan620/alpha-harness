/** Whether Vision's sidebar is open. Shared with the shell, which trades it off against the nav. */

import { create } from 'zustand'

interface VisionState {
  open: boolean
  setOpen: (open: boolean) => void
  toggle: () => void
}

export const useVision = create<VisionState>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
  toggle: () => set((s) => ({ open: !s.open })),
}))
