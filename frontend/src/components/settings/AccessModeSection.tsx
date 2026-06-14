/**
 * AccessModeSection — admin card for switching between single-user and
 * multi-user access mode.
 *
 * Shows:
 *  - A radio group offering "Single-user" vs "Multi-user".
 *  - Defaults to the current value from getFirstRunStatus().setup_mode.
 *  - Save button calls saveSetupMode(mode).
 *  - A persistent amber "pending restart" pill (survives reloads via
 *    localStorage) shown until the API-reported mode matches the saved mode.
 *  - An actionable restart instruction with the exact compose command.
 *
 * Backed by:
 *  GET  /api/setup/status → getFirstRunStatus() — reads setup_mode
 *  POST /api/setup/mode   → saveSetupMode(mode)
 */

import { useEffect, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { getFirstRunStatus, saveSetupMode } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { errorMessage } from '@/lib/errors';

const MODE_LABELS: Record<'single' | 'multi', string> = {
  single: 'Single-user — only the admin account can log in',
  multi: 'Multi-user — invite additional accounts via magic-link',
};

const PENDING_MODE_KEY = 'jarvis-access-mode-pending';

function readPendingMode(): 'single' | 'multi' | null {
  try {
    const raw = localStorage.getItem(PENDING_MODE_KEY);
    return raw === 'single' || raw === 'multi' ? raw : null;
  } catch {
    return null;
  }
}

function writePendingMode(mode: 'single' | 'multi' | null): void {
  try {
    if (mode === null) {
      localStorage.removeItem(PENDING_MODE_KEY);
    } else {
      localStorage.setItem(PENDING_MODE_KEY, mode);
    }
  } catch {
    // ignore storage errors — the pill is a best-effort hint
  }
}

export function AccessModeSection() {
  const [pendingMode, setPendingMode] = useState<'single' | 'multi' | null>(null);
  // The mode saved-but-not-yet-applied (running services still read the old
  // value until restarted). Persisted so the pill survives a page reload.
  const [pendingRestartMode, setPendingRestartMode] = useState<'single' | 'multi' | null>(
    readPendingMode,
  );

  const { data: status, isLoading } = useQuery({
    queryKey: QUERY_KEYS.setup.firstRun(),
    queryFn: getFirstRunStatus,
    staleTime: 60_000,
  });

  const currentMode: 'single' | 'multi' = status?.setup_mode ?? 'single';
  const selectedMode = pendingMode ?? currentMode;

  // Once the API reports the saved mode, the restart has landed — clear the pill.
  useEffect(() => {
    if (pendingRestartMode && status?.setup_mode === pendingRestartMode) {
      setPendingRestartMode(null);
      writePendingMode(null);
    }
  }, [pendingRestartMode, status?.setup_mode]);

  const saveMut = useMutation({
    mutationFn: saveSetupMode,
    onSuccess: (data) => {
      setPendingMode(null);
      if (data.restart_required) {
        setPendingRestartMode(data.mode);
        writePendingMode(data.mode);
      } else {
        setPendingRestartMode(null);
        writePendingMode(null);
      }
    },
  });

  const handleSave = () => {
    if (selectedMode === currentMode && !pendingMode) return;
    saveMut.mutate(selectedMode);
  };

  const restartPending = pendingRestartMode !== null && pendingRestartMode !== currentMode;

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
        {restartPending && (
          <p
            role="status"
            className="inline-flex items-center gap-2 rounded-full border border-amber-400 bg-amber-50 px-3 py-1 text-xs font-medium text-amber-700 dark:border-amber-500/60 dark:bg-amber-500/10 dark:text-amber-300"
          >
            Mode change pending — restart required
          </p>
        )}

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
            {errorMessage(saveMut.error, 'unknown error')}
          </p>
        )}

        {restartPending && (
          <p className="text-xs text-muted-foreground border-t border-hair pt-3">
            To apply, an administrator runs{' '}
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.7rem]">
              docker compose restart paper_ingestion learning_engine
            </code>{' '}
            on the server.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
