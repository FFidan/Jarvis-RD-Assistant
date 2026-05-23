/**
 * PulseRunStatusCard — last-run status badge, stats table, generate button, diagnostics,
 * and admin source config panel.
 */
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError } from '@/lib/api';
import { useJobStore } from '@/stores/job-store';
import { formatDate } from '@/lib/utils';
import { StatusBadge } from '@/components/ui/status-badge';
import type { PulseStats } from '@/types';
import { QUERY_KEYS } from '@/lib/query-keys';
import { DiagnosticsPanel } from '../DiagnosticsPanel';
import { SourceConfigPanel } from '../SourceConfigPanel';

interface PulseRunStatusCardProps {
  stats: PulseStats | undefined;
  statsError: boolean;
  statsUnavailable: boolean;
  settingsUnavailable: boolean;
  isAdmin: boolean;
}

export function PulseRunStatusCard({
  stats,
  statsError,
  statsUnavailable,
  settingsUnavailable,
  isAdmin,
}: PulseRunStatusCardProps) {
  const queryClient = useQueryClient();
  const { startJob, hasRunning } = useJobStore();
  const isPulseRunning = hasRunning('pulse.generate');

  let statusBadge: React.ReactNode = null;
  if (stats) {
    if (stats.last_error) {
      statusBadge = <Badge variant="destructive">Failed</Badge>;
    } else if (stats.degraded_reason) {
      statusBadge = <StatusBadge status="degraded" tooltip={stats.degraded_reason} />;
    } else {
      statusBadge = (
        <StatusBadge
          status="ok"
          tooltip={stats.last_run_at ? `Last run: ${formatDate(stats.last_run_at)}` : 'No runs yet'}
        />
      );
    }
  }

  return (
    <Card className="rounded-md border-hair shadow-none" data-testid="pulse-status-card">
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          Last Pulse run
          {statusBadge}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {stats ? (
          <div className="space-y-1 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Last run</span>
              <span className="font-mono">
                {stats.last_run_at ? formatDate(stats.last_run_at) : 'never'}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Decks generated</span>
              <span className="font-mono">{stats.decks_generated}</span>
            </div>
            {stats.last_error && (
              <div className="pt-1">
                <Badge variant="destructive" className="text-xs">
                  {stats.last_error}
                </Badge>
              </div>
            )}
            {stats.decks_generated === 0 && !stats.last_error && (
              <p className="rounded-md border border-muted bg-muted/20 px-3 py-2 text-xs text-muted-foreground mt-2">
                No Pulse deck yet. Pulse needs a populated library with topics set and at least
                one working source. Enable Pulse above and run it once to get started.
              </p>
            )}
          </div>
        ) : statsError ? (
          <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            Pulse stats unavailable. Generation is disabled until stats load.
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">Loading stats…</p>
        )}

        <Button
          onClick={() => {
            startJob('pulse.generate', {}).catch((err: unknown) => {
              if (err instanceof ApiError && err.status === 409) {
                toast.info('Pulse is already running. Your deck will be ready shortly.');
              } else if (err instanceof ApiError && err.status === 429) {
                toast.error('Rate limit reached. Try again in a minute.');
              } else {
                toast.error('Failed to start Pulse generation.');
              }
            });
          }}
          disabled={isPulseRunning || statsUnavailable || settingsUnavailable}
          className="w-full"
        >
          {isPulseRunning ? (
            <span className="flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" />
              Generating…
            </span>
          ) : (
            'Generate Pulse now'
          )}
        </Button>

        <DiagnosticsPanel />

        <SourceConfigPanel
          isAdmin={isAdmin}
          onArxivCooldownCleared={() => {
            void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.pulse.debug() });
            void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.pulse.statsAll() });
          }}
        />
      </CardContent>
    </Card>
  );
}
