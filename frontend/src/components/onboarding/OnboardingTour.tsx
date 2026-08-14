/**
 * OnboardingTour — first-login guided tour for new users.
 *
 * Fires when:
 *   - User has zero papers in the feed
 *   - AND user_config onboarding_dismissed !== true
 *
 * Steps match the public quick start for every researcher:
 *   1. Discover a paper
 *   2. Save it to Papers
 *   3. Analyze it
 *   4. Ask across the papers you saved
 *
 * Persistence: "Don't show again" writes onboarding.dismissed=true via
 * setConfig (PUT /api/config/onboarding.dismissed).
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ACTIONS,
  EVENTS,
  Joyride,
  STATUS,
  type Controls,
  type EventData,
  type Step,
} from 'react-joyride';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchConfig, fetchFeed, setConfig } from '@/lib/api';

// ── Tour steps ─────────────────────────────────────────────────────────────

const STEP_DISCOVER: Step = {
  target: '[data-tour-id="sidebar-discover"]',
  title: 'Discover Papers',
  content:
    'Open Discover to search your enabled literature sources by title or keyword.',
  placement: 'right',
  skipBeacon: true,
};

const STEP_SAVE: Step = {
  target: '[data-tour-id~="sidebar-library"]',
  title: 'Save to Papers',
  content:
    'Save a useful result to Papers so it becomes part of your research workspace.',
  placement: 'right',
  skipBeacon: true,
};

const STEP_ANALYZE: Step = {
  target: '[data-tour-id~="sidebar-analyze"]',
  title: 'Analyze a Paper',
  content:
    'Open a saved paper from Papers and choose Analyze to download, parse, and summarize it.',
  placement: 'right',
  skipBeacon: true,
};

const STEP_ASK: Step = {
  target: '[data-tour-id="sidebar-ask"]',
  title: 'Ask Across Your Papers',
  content:
    'After analysis, use Ask for evidence-grounded questions across the papers you saved.',
  placement: 'right',
  skipBeacon: true,
};

const RESEARCH_WORKFLOW_STEPS = [STEP_DISCOVER, STEP_SAVE, STEP_ANALYZE, STEP_ASK];

function isNarrowViewport(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(max-width: 767px)').matches
  );
}

function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(isNarrowViewport);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia('(max-width: 767px)');
    const update = () => setNarrow(query.matches);
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);

  return narrow;
}

// ── Trigger-condition hook ─────────────────────────────────────────────────

function useOnboardingEligibility() {
  const feedQuery = useQuery({
    queryKey: QUERY_KEYS.feed.onboardingCheck(),
    queryFn: () => fetchFeed({ limit: 1 }),
    staleTime: 60_000,
  });

  const loading = feedQuery.isLoading;
  const zeroPapers = !feedQuery.data || feedQuery.data.papers.length === 0;

  return { loading, eligible: feedQuery.isSuccess && zeroPapers };
}

// ── Dismissed-state hook ───────────────────────────────────────────────────

const DISMISSED_KEY = 'onboarding.dismissed';
const LOCAL_STORAGE_KEY = 'jarvis-onboarding-dismissed';

function useDismissedState(): [boolean | null, () => Promise<void>] {
  // Optimistic local check to avoid showing tour briefly then hiding it.
  const [locallyDismissed, setLocallyDismissed] = useState(() => {
    try {
      return localStorage.getItem(LOCAL_STORAGE_KEY) === 'true';
    } catch {
      return false;
    }
  });
  const configQuery = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
    enabled: !locallyDismissed,
    staleTime: 60_000,
  });

  const remotelyDismissed = configQuery.data?.some(
    (entry) => entry.key === DISMISSED_KEY && entry.value === true,
  ) ?? false;
  const dismissed = locallyDismissed || remotelyDismissed;
  const resolvedDismissed = !locallyDismissed && configQuery.isLoading ? null : dismissed;

  const persist = useCallback(async () => {
    setLocallyDismissed(true);
    try {
      localStorage.setItem(LOCAL_STORAGE_KEY, 'true');
    } catch {
      // ignore storage errors
    }
    // Best-effort server-side persistence; failure doesn't break the UI.
    try {
      await setConfig(DISMISSED_KEY, true);
    } catch {
      // ignore — local flag already set
    }
  }, []);

  return [resolvedDismissed, persist];
}

// ── Component ──────────────────────────────────────────────────────────────

// Default export required for React.lazy() in AppShell.
// Named re-export preserved for direct imports (e.g. tests).
export default function OnboardingTour() {
  const [run, setRun] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  const { loading, eligible } = useOnboardingEligibility();
  const [dismissed, persistDismiss] = useDismissedState();
  const narrowViewport = useNarrowViewport();

  const steps = useMemo<Step[]>(() => {
    if (!narrowViewport) return RESEARCH_WORKFLOW_STEPS;
    return RESEARCH_WORKFLOW_STEPS.map((step) => ({
      ...step,
      target: 'body',
      placement: 'center',
      content: `${String(step.content)} Open the navigation menu to choose the named destination.`,
    }));
  }, [narrowViewport]);

  // Start the tour once eligibility resolves and user hasn't dismissed it.
  useEffect(() => {
    if (!loading && eligible && dismissed === false) {
      // Small delay so the app layout has time to render tour targets.
      const id = setTimeout(() => setRun(true), 800);
      return () => clearTimeout(id);
    }
    return undefined;
  }, [loading, eligible, dismissed]);

  const handleEvent = useCallback(
    (data: EventData, controls: Controls) => {
      const { action, index, status, type } = data;

      const isStepEvent =
        type === EVENTS.STEP_AFTER || type === EVENTS.TARGET_NOT_FOUND;
      const isTourDone =
        status === STATUS.FINISHED || status === STATUS.SKIPPED;

      if (isStepEvent) {
        const nextIndex = index + (action === ACTIONS.PREV ? -1 : 1);
        const { size } = controls.info();
        setStepIndex(Math.min(Math.max(nextIndex, 0), size));
      } else if (isTourDone) {
        setRun(false);
        setStepIndex(0);
        // Persist dismissal on both finish and skip.
        void persistDismiss();
      }
    },
    [persistDismiss],
  );

  // Don't mount joyride until we know we should show it (avoids SSR/hydration
  // noise and extra DOM nodes for users who have already dismissed the tour).
  if (loading || !eligible || dismissed !== false) {
    return null;
  }

  return (
    <Joyride
      continuous
      run={run}
      stepIndex={stepIndex}
      steps={steps}
      onEvent={handleEvent}
      scrollToFirstStep
      locale={{
        back: 'Back',
        close: 'Close',
        last: 'Done',
        next: 'Next',
        skip: "Don't show again",
      }}
      options={{
        buttons: ['back', 'skip', 'primary'],
        primaryColor: 'hsl(var(--primary))',
        showProgress: true,
        zIndex: 10000,
      }}
      styles={{
        tooltipContainer: {
          textAlign: 'left',
        },
      }}
    />
  );
}

// Named re-export so tests can import { OnboardingTour } without change.
export { OnboardingTour };
