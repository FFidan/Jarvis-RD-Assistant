/**
 * OnboardingTour — first-login guided tour for new users.
 *
 * Fires when:
 *   - User has zero topics AND zero papers in the feed
 *   - AND user_config onboarding_dismissed !== true
 *
 * Steps walk the core happy path:
 *   1. Sidebar "Settings" (Sources sub-section anchor) → connect a source
 *   2. Sidebar "Settings" (Topics sub-section anchor) → define a topic
 *   3. Pulse Deck "Generate" button → run Pulse
 *   4. A Pulse card → rate cards so JARVIS learns
 *
 * Persistence: "Don't show again" writes onboarding.dismissed=true via
 * setConfig (PUT /api/config/onboarding.dismissed).
 */

import { useCallback, useEffect, useState } from 'react';
import Joyride, { ACTIONS, EVENTS, STATUS, type CallBackProps, type Step } from 'react-joyride';
import { useQuery } from '@tanstack/react-query';
import { fetchTopics, fetchFeed, setConfig } from '@/lib/api';

// ── Tour steps ─────────────────────────────────────────────────────────────

const TOUR_STEPS: Step[] = [
  {
    target: '[data-tour-id="sidebar-settings"]',
    title: 'Connect a Source',
    content:
      'Start by connecting a source — arXiv, Semantic Scholar, or OpenAlex — so JARVIS can fetch papers for you. Open Settings and go to the Sources tab.',
    placement: 'right',
    disableBeacon: true,
  },
  {
    target: '[data-tour-id="sidebar-settings"]',
    title: 'Define a Topic',
    content:
      'Define research topics to focus your feed. JARVIS uses these to score and rank every incoming paper. Open Settings and go to the Topics tab.',
    placement: 'right',
    disableBeacon: true,
  },
  {
    target: '[data-tour-id="pulse-generate-btn"]',
    title: 'Run Pulse',
    content:
      'Once you have sources and topics, run Pulse to get your first batch of AI-curated, personalised paper recommendations.',
    placement: 'bottom',
    disableBeacon: true,
  },
  {
    target: '[data-tour-id="pulse-card-first"]',
    title: 'Rate Cards',
    content:
      'Give thumbs up or thumbs down on cards. JARVIS learns from every signal to sharpen future recommendations.',
    placement: 'bottom',
    disableBeacon: true,
  },
];

// ── Trigger-condition hook ─────────────────────────────────────────────────

function useOnboardingEligibility() {
  const topicsQuery = useQuery({
    queryKey: ['topics'],
    queryFn: fetchTopics,
    staleTime: 60_000,
  });

  const feedQuery = useQuery({
    queryKey: ['papers-feed', 'onboarding-check'],
    queryFn: () => fetchFeed({ limit: 1 }),
    staleTime: 60_000,
  });

  const loading = topicsQuery.isLoading || feedQuery.isLoading;
  const zeroTopics = !topicsQuery.data || topicsQuery.data.length === 0;
  const zeroPapers = !feedQuery.data || feedQuery.data.papers.length === 0;

  return { loading, eligible: zeroTopics && zeroPapers };
}

// ── Dismissed-state hook ───────────────────────────────────────────────────

const DISMISSED_KEY = 'onboarding.dismissed';
const LOCAL_STORAGE_KEY = 'jarvis-onboarding-dismissed';

function useDismissedState(): [boolean | null, () => Promise<void>] {
  // Optimistic local check to avoid showing tour briefly then hiding it.
  const [dismissed, setDismissed] = useState<boolean | null>(() => {
    try {
      return localStorage.getItem(LOCAL_STORAGE_KEY) === 'true';
    } catch {
      return null;
    }
  });

  const persist = useCallback(async () => {
    setDismissed(true);
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

  return [dismissed, persist];
}

// ── Component ──────────────────────────────────────────────────────────────

export function OnboardingTour() {
  const [run, setRun] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  const { loading, eligible } = useOnboardingEligibility();
  const [dismissed, persistDismiss] = useDismissedState();

  // Start the tour once eligibility resolves and user hasn't dismissed it.
  useEffect(() => {
    if (!loading && eligible && dismissed === false) {
      // Small delay so the app layout has time to render tour targets.
      const id = setTimeout(() => setRun(true), 800);
      return () => clearTimeout(id);
    }
    return undefined;
  }, [loading, eligible, dismissed]);

  const handleCallback = useCallback(
    (data: CallBackProps) => {
      const { action, index, status, type } = data;

      const isStepEvent =
        type === EVENTS.STEP_AFTER || type === EVENTS.TARGET_NOT_FOUND;
      const isTourDone =
        status === STATUS.FINISHED || status === STATUS.SKIPPED;

      if (isStepEvent) {
        setStepIndex(index + (action === ACTIONS.PREV ? -1 : 1));
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
      steps={TOUR_STEPS}
      callback={handleCallback}
      scrollToFirstStep
      showProgress
      showSkipButton
      locale={{
        back: 'Back',
        close: 'Close',
        last: 'Done',
        next: 'Next',
        skip: "Don't show again",
      }}
      styles={{
        options: {
          primaryColor: 'hsl(var(--primary))',
          zIndex: 10000,
        },
        tooltipContainer: {
          textAlign: 'left',
        },
      }}
    />
  );
}
