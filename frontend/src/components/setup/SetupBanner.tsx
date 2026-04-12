import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Rocket, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { getSetupStatus } from '@/lib/api';

const DISMISS_KEY = 'setup-banner-dismissed';

/**
 * Persistent banner surfaced on HomePage while setup is incomplete.
 * Hidden forever once the user clicks Dismiss (localStorage flag).
 */
export function SetupBanner() {
  const navigate = useNavigate();
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(DISMISS_KEY) === 'true';
    } catch {
      return false;
    }
  });

  const { data } = useQuery({
    queryKey: ['setup-status'],
    queryFn: getSetupStatus,
    staleTime: 30_000,
  });

  if (dismissed) return null;
  if (!data || data.setup_completed !== false) return null;

  const handleDismiss = () => {
    try {
      localStorage.setItem(DISMISS_KEY, 'true');
    } catch {
      // ignore (private-mode etc.)
    }
    setDismissed(true);
  };

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
        <Button size="sm" onClick={() => navigate('/setup?step=1')}>
          Resume setup
        </Button>
        <Button size="icon" variant="ghost" onClick={handleDismiss} aria-label="Dismiss">
          <X className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
