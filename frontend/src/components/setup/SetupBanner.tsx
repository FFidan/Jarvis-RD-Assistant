import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useNavigate } from 'react-router-dom';
import { Rocket, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { getSetupStatus } from '@/lib/api';
import { useUIStore } from '@/stores/ui-store';

/**
 * Persistent banner surfaced on HomePage while setup is incomplete.
 * Hidden forever once the user clicks Dismiss (Zustand ui-store flag,
 * persisted via localStorage through the `jarvis-ui` key).
 */
export function SetupBanner() {
  const navigate = useNavigate();
  const { setupBannerDismissed, dismissSetupBanner } = useUIStore();

  const { data } = useQuery({
    queryKey: QUERY_KEYS.setup.status(),
    queryFn: getSetupStatus,
    staleTime: 30_000,
  });

  if (setupBannerDismissed) return null;
  if (!data || data.setup_completed !== false) return null;

  return (
    <div className="flex items-start gap-3 rounded-md border border-primary/40 bg-primary/5 p-4">
      <Rocket className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
      <div className="flex-1">
        <p className="font-medium">Finish setting up JARVIS</p>
        <p className="text-sm text-muted-foreground">
          A few quick steps will get your research pipeline running.
        </p>
      </div>
      <div className="flex items-center gap-2">
        {/* The wizard is now gate-rendered (not a /setup route) whenever setup
            is incomplete. Navigate home with ?step=1 — the onboarding gate in
            App.tsx renders OnboardingWizard, which reads ?step= from the URL. */}
        <Button size="sm" onClick={() => navigate('/?step=1')}>
          Resume setup
        </Button>
        <Button size="icon" variant="ghost" onClick={dismissSetupBanner} aria-label="Dismiss">
          <X className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
