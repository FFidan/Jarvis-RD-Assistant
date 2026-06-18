/**
 * AccessModeSection — admin card for choosing which login method the
 * sign-in screen offers (single-user API-key login vs multi-user magic-link).
 *
 * The choice is applied on the next status poll (the backend reads the saved
 * value live), so there is no restart step.
 *
 * Backed by:
 *  GET  /api/setup/status → getFirstRunStatus() — reads setup_mode
 *  POST /api/setup/mode   → saveSetupMode(mode)
 */

import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { getFirstRunStatus, saveSetupMode } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { errorMessage } from '@/lib/errors';

const MODE_LABELS: Record<'single' | 'multi', string> = {
  single: 'Single-user — the sign-in screen offers API-key login',
  multi: 'Multi-user — the sign-in screen offers magic-link login',
};

export function AccessModeSection() {
  const [pendingMode, setPendingMode] = useState<'single' | 'multi' | null>(null);

  const { data: status, isLoading } = useQuery({
    queryKey: QUERY_KEYS.setup.firstRun(),
    queryFn: getFirstRunStatus,
    staleTime: 60_000,
  });

  const currentMode: 'single' | 'multi' = status?.setup_mode ?? 'single';
  const selectedMode = pendingMode ?? currentMode;

  const saveMut = useMutation({
    mutationFn: saveSetupMode,
    onSuccess: () => {
      setPendingMode(null);
    },
  });

  const handleSave = () => {
    if (selectedMode === currentMode && !pendingMode) return;
    saveMut.mutate(selectedMode);
  };

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading sign-in method…</p>;
  }

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <p className="text-sm text-muted-foreground">
          Choose which login method the sign-in screen offers. Admin invites are
          available in either mode.
        </p>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Mode radio group */}
        <fieldset className="space-y-2">
          <legend className="sr-only">Sign-in method</legend>
          {(['single', 'multi'] as const).map((mode) => (
            <Label
              key={mode}
              className="flex items-start gap-3 cursor-pointer rounded-md border border-hair p-3 has-[:checked]:border-primary"
            >
              <input
                type="radio"
                name="access-mode"
                value={mode}
                checked={selectedMode === mode}
                onChange={() => setPendingMode(mode)}
                className="mt-0.5"
              />
              <span className="text-sm">{MODE_LABELS[mode]}</span>
            </Label>
          ))}
        </fieldset>

        {/* Save button + success feedback */}
        <div className="flex flex-wrap items-center gap-3">
          <Button
            onClick={handleSave}
            disabled={saveMut.isPending || (selectedMode === currentMode && !pendingMode)}
          >
            {saveMut.isPending ? 'Saving…' : 'Save'}
          </Button>

          {saveMut.isSuccess && (
            <p className="text-sm text-green-600 dark:text-green-400">
              Sign-in method updated.
            </p>
          )}
        </div>

        {saveMut.isError && (
          <p className="text-sm text-destructive">
            Could not save: {errorMessage(saveMut.error, 'unknown error')}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
