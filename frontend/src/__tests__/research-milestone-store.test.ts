import { beforeEach, describe, expect, it } from 'vitest';
import {
  RESEARCH_MILESTONE_STORE_KEY,
  useResearchMilestoneStore,
} from '@/stores/research-milestone-store';

function resetStore() {
  useResearchMilestoneStore.setState({
    completed: { save: false, analyze: false },
    advancedCueDismissed: false,
  });
}

describe('research milestone store', () => {
  beforeEach(() => {
    localStorage.clear();
    resetStore();
  });

  it('records Save and Analyze independently and persists only milestone guidance state', () => {
    useResearchMilestoneStore.getState().recordMilestone('save');
    expect(useResearchMilestoneStore.getState().completed).toEqual({
      save: true,
      analyze: false,
    });

    useResearchMilestoneStore.getState().recordMilestone('analyze');
    const persisted = JSON.parse(localStorage.getItem(RESEARCH_MILESTONE_STORE_KEY)!);
    expect(persisted.state).toEqual({
      completed: { save: true, analyze: true },
      advancedCueDismissed: false,
    });
  });

  it('persists dismissal without clearing completed milestones', () => {
    useResearchMilestoneStore.getState().recordMilestone('save');
    useResearchMilestoneStore.getState().dismissAdvancedCue();

    expect(useResearchMilestoneStore.getState()).toMatchObject({
      completed: { save: true, analyze: false },
      advancedCueDismissed: true,
    });
  });
});
