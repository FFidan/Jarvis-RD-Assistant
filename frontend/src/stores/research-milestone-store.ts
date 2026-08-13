import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const RESEARCH_MILESTONE_STORE_KEY = 'jarvis-research-milestones';

export type ResearchMilestone = 'save' | 'analyze';

interface ResearchMilestoneState {
  completed: Record<ResearchMilestone, boolean>;
  advancedCueDismissed: boolean;
  recordMilestone: (milestone: ResearchMilestone) => void;
  dismissAdvancedCue: () => void;
}

const EMPTY_MILESTONES: Record<ResearchMilestone, boolean> = {
  save: false,
  analyze: false,
};

/** Device-local first-use milestones; no research data or user identity is stored. */
export const useResearchMilestoneStore = create<ResearchMilestoneState>()(
  persist(
    (set) => ({
      completed: { ...EMPTY_MILESTONES },
      advancedCueDismissed: false,
      recordMilestone(milestone) {
        set((state) => ({
          completed: { ...state.completed, [milestone]: true },
        }));
      },
      dismissAdvancedCue() {
        set({ advancedCueDismissed: true });
      },
    }),
    {
      name: RESEARCH_MILESTONE_STORE_KEY,
      partialize: (state) => ({
        completed: state.completed,
        advancedCueDismissed: state.advancedCueDismissed,
      }),
    },
  ),
);
