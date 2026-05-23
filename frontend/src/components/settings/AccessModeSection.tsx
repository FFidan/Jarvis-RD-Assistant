/**
 * AccessModeSection — admin card for switching between single-user and
 * multi-user access mode.
 *
 * Shows:
 *  - A select (or radio) offering "Single-user" vs "Multi-user".
 *  - Defaults to the current value from getFirstRunStatus().setup_mode.
 *  - Save button calls saveSetupMode(mode).
 *  - Persistent note that changing mode requires an application restart.
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

const MODE_LABELS: Record<'single' | 'multi', string> = {
  single: 'Single-user — only the admin account can log in',
  multi: 'Multi-user — invite additional accounts via magic-link',
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
    return <p className="text-sm text-muted-foreground">Loading access mode…</p>;
  }

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <p className="text-sm text-muted-foreground">
          Control whether JARVIS allows additional user accounts beyond the admin.
        </p>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Mode radio group */}
        <fieldset className="space-y-2">
          <legend className="sr-only">Access mode</legend>
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

          {saveMut.isSuccess && saveMut.data && (
            <p className="text-sm text-green-600 dark:text-green-400">
              {saveMut.data.restart_required
                ? 'Saved — restart required for the change to take effect.'
                : 'Access mode updated.'}
            </p>
          )}
        </div>

        {saveMut.isError && (
          <p className="text-sm text-destructive">
            Could not save:{' '}
            {saveMut.error instanceof Error ? saveMut.error.message : 'unknown error'}
          </p>
        )}

        {/* Persistent restart note */}
        <p className="text-xs text-muted-foreground border-t border-hair pt-3">
          Changing access mode requires an application restart by an administrator.
        </p>
      </CardContent>
    </Card>
  );
}
