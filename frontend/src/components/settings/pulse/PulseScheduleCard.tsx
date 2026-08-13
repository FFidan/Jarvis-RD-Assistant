/**
 * PulseScheduleCard — Pulse enable toggle, cron time, deck size, ranking candidates,
 * lookback window, and startup grace settings.
 */
import type { UseMutationResult } from '@tanstack/react-query';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { TimeSelect } from '@/components/ui/time-select';
import { ConfigSlider } from '@/components/ui/config-slider';
import { cronToHumanReadable, cronToTime, isTimeOnlyCron, timeToCron } from '@/lib/cron-utils';
import { useSyncedState } from './use-synced-state';
import { CRON_TOOLTIP } from './pulse-constants';
import { getConfigValue } from './pulse-utils';
import { onSaveError } from '@/lib/forms/save-error';
import type { ConfigEntry } from '@/types';

function isValidCron(s: string): boolean {
  const parts = s.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  return parts.every((p) => /^[*/0-9,-]+$/.test(p));
}

interface PulseScheduleCardProps {
  configs: ConfigEntry[];
  setMut: UseMutationResult<unknown, Error, { key: string; value: unknown }>;
  settingsUnavailable: boolean;
  settingsControlsDisabled: boolean;
}

export function PulseScheduleCard({
  configs,
  setMut,
  settingsUnavailable,
  settingsControlsDisabled,
}: PulseScheduleCardProps) {
  const enabled = getConfigValue<boolean>(configs, 'pulse.enabled', false);
  const cron = getConfigValue<string>(configs, 'pulse.cron', '0 4 * * *');
  const deckSizeConfig = getConfigValue<number>(configs, 'pulse.deck_size', 10);
  const stage2TopKConfig = getConfigValue<number>(configs, 'pulse.stage2_top_k', 40);
  const lookbackDaysConfig = getConfigValue<number>(configs, 'pulse.lookback_days', 7);
  const startupGraceConfig = getConfigValue<number>(configs, 'pulse.startup_grace_seconds', 0);

  const [localCron, setLocalCron] = useSyncedState(cron);
  const [deckSize, setDeckSize] = useSyncedState(deckSizeConfig);
  const [stage2TopK, setStage2TopK] = useSyncedState(stage2TopKConfig);
  const [lookbackDays, setLookbackDays] = useSyncedState(lookbackDaysConfig);
  const [startupGrace, setStartupGrace] = useSyncedState(startupGraceConfig);

  // pulse.cron accepts more than a single daily clock time (e.g. "0 8,20 * * *"
  // for two runs a day). The picker below can only show and edit a single time.
  const cronIsTimeOnly = isTimeOnlyCron(cron);

  const handleToggle = () => {
    if (settingsControlsDisabled) return;
    setMut.mutate(
      { key: 'pulse.enabled', value: !enabled },
      { onError: onSaveError('Could not update whether Pulse is enabled') },
    );
  };

  const handleCronChange = (value: string) => {
    if (settingsControlsDisabled) return;
    setLocalCron(value);
    if (!isValidCron(value)) return;
    setMut.mutate(
      { key: 'pulse.cron', value },
      { onError: onSaveError('Could not update the daily run time') },
    );
  };

  return (
    <Card className="rounded-md border-hair shadow-none" data-testid="pulse-schedule-card">
      <CardHeader>
        <CardDescription>
          Nightly ranked deck of candidate papers scored by the Pulse pipeline.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Enable toggle */}
        <div className="flex items-center justify-between">
          <Label htmlFor="pulse-enable-toggle">Enable Pulse</Label>
          <button
            id="pulse-enable-toggle"
            type="button"
            role="switch"
            aria-label="Enable Pulse"
            aria-checked={!!enabled}
            onClick={handleToggle}
            disabled={settingsControlsDisabled}
            className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${
              enabled ? 'bg-primary' : 'bg-input'
            }`}
          >
            <span
              className={`pointer-events-none block h-5 w-5 rounded-full bg-background shadow-lg ring-0 transition-transform ${
                enabled ? 'translate-x-5' : 'translate-x-0'
              }`}
            />
          </button>
        </div>

        {/* Daily run time */}
        <div className="space-y-1">
          <Label htmlFor="pulse-cron-time" className="flex items-center gap-1">
            Daily run time
            <InfoTooltip content={CRON_TOOLTIP} />
          </Label>
          {cronIsTimeOnly ? (
            <>
              <TimeSelect
                value={cronToTime(localCron)}
                onChange={(v) => handleCronChange(timeToCron(v, localCron))}
                disabled={settingsControlsDisabled}
              />
              <p className="text-xs text-muted-foreground">{cronToHumanReadable(localCron)}</p>
            </>
          ) : (
            <p className="text-xs text-muted-foreground" data-testid="pulse-cron-readonly">
              This schedule runs more than once a day ({cron}), so it can&apos;t be shown or
              changed with the time picker.
            </p>
          )}
        </div>

        {/* Deck size */}
        <ConfigSlider
          id="pulse-deck-size"
          label="Deck size"
          value={deckSize}
          min={5}
          max={30}
          step={5}
          disabled={settingsControlsDisabled}
          onLocalChange={(v) => setDeckSize(v)}
          onCommit={(v) =>
            setMut.mutate(
              { key: 'pulse.deck_size', value: v },
              { onError: onSaveError('Could not update the deck size') },
            )
          }
          description="Papers in your daily Pulse deck. Larger decks = more variety but longer review."
        />

        {/* Stage-2 ranking candidates */}
        <ConfigSlider
          id="pulse-stage2-top-k"
          label="Ranking candidates"
          value={stage2TopK}
          min={20}
          max={100}
          step={10}
          disabled={settingsControlsDisabled}
          onLocalChange={(v) => setStage2TopK(v)}
          onCommit={(v) =>
            setMut.mutate(
              { key: 'pulse.stage2_top_k', value: v },
              { onError: onSaveError('Could not update the number of ranking candidates') },
            )
          }
          description="Candidates the LLM reranker evaluates. Higher = better ranking quality but slower."
        />

        {/* Lookback window */}
        <ConfigSlider
          id="pulse-lookback-days"
          label="Lookback window"
          value={lookbackDays}
          min={1}
          max={90}
          step={1}
          unit="d"
          infoTooltip="How many days back Pulse looks for candidate papers from each source. Longer windows surface more papers but may increase discovery time."
          description="Days of paper history each source scans per Pulse run. Default 7."
          disabled={settingsControlsDisabled}
          onLocalChange={(v) => setLookbackDays(v)}
          onCommit={(v) =>
            setMut.mutate(
              { key: 'pulse.lookback_days', value: v },
              { onError: onSaveError('Could not update the lookback window') },
            )
          }
        />

        {/* Startup grace */}
        <ConfigSlider
          id="pulse-startup-grace"
          label="Startup grace"
          value={startupGrace}
          min={0}
          max={300}
          step={5}
          unit="s"
          infoTooltip="Seconds to wait before each source's first outbound HTTP burst after process start. Lets containers warm up. Default 0 (disabled)."
          description="Warmup pause before first HTTP burst. Default 0s (disabled)."
          disabled={settingsControlsDisabled}
          onLocalChange={(v) => setStartupGrace(v)}
          onCommit={(v) =>
            setMut.mutate(
              { key: 'pulse.startup_grace_seconds', value: v },
              { onError: onSaveError('Could not update the startup grace period') },
            )
          }
        />

        {settingsUnavailable && (
          <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            Pulse settings unavailable. Settings controls are disabled until configuration loads.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
