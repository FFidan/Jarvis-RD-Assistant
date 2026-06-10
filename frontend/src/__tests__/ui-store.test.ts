import { describe, it, expect, beforeEach } from 'vitest';
import { useUIStore, UI_STORE_KEY } from '@/stores/ui-store';

describe('ui-store onboardingCelebrated', () => {
  beforeEach(() => {
    localStorage.clear();
    useUIStore.getState()._reset();
  });

  it('defaults to false', () => {
    expect(useUIStore.getState().onboardingCelebrated).toBe(false);
  });

  it('markOnboardingCelebrated sets the flag true', () => {
    useUIStore.getState().markOnboardingCelebrated();
    expect(useUIStore.getState().onboardingCelebrated).toBe(true);
  });

  it('persists onboardingCelebrated to localStorage', () => {
    useUIStore.getState().markOnboardingCelebrated();
    const raw = localStorage.getItem(UI_STORE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).state.onboardingCelebrated).toBe(true);
  });

  it('_reset clears the flag back to false', () => {
    useUIStore.getState().markOnboardingCelebrated();
    expect(useUIStore.getState().onboardingCelebrated).toBe(true);
    useUIStore.getState()._reset();
    expect(useUIStore.getState().onboardingCelebrated).toBe(false);
  });
});
