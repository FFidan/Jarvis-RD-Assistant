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
import { toast } from 'sonner';
import { cronToHumanReadable, cronToTime, timeToCron } from '@/lib/cron-utils';
import { useSyncedState } from './use-synced-state';
import { useDebouncedConfig } from './use-debounced-config';
import { CRON_TOOLTIP } from './pulse-constants';
import type { ConfigEntry } from '@/types';

function isValidCron(s: string): boolean {
  const parts = s.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  return parts.every((p) => /^[*/0-9,-]+$/.test(p));
}

function getConfigValue<T>(entries: ConfigEntry[], key: string, fallback: T): T {
  const entry = entries.find((c) => c.key === key);
  return entry !== undefined ? (entry.value as T) : fallback;
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
  const deckSize = getConfigValue<number>(configs, 'pulse.deck_size', 10);
  const stage2TopK = getConfigValue<number>(configs, 'pulse.stage2_top_k', 40);
  const lookbackDaysConfig = getConfigValue<number>(configs, 'pulse.lookback_days', 7);
  const startupGraceConfig = getConfigValue<number>(configs, 'pulse.startup_grace_seconds', 0);

  const [localCron, setLocalCron] = useSyncedState(cron);
  const [lookbackDays, setLookbackDays] = useSyncedState(lookbackDaysConfig);
  const [startupGrace, setStartupGrace] = useSyncedState(startupGraceConfig);

  const debouncedCron = useDebouncedConfig(
    ({ value }) => setMut.mutate({ key: 'pulse.cron', value }),
    400,
  );

  const handleToggle = () => {
    if (settingsControlsDisabled) return;
    setMut.mutate({ key: 'pulse.enabled', value: !enabled });
  };

  const handleCronChange = (value: string) => {
    if (settingsControlsDisabled) return;
    setLocalCron(value);
    if (!isValidCron(value)) return;
    debouncedCron({ key: 'pulse.cron', value });
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
          <TimeSelect
            value={cronToTime(localCron)}
            onChange={(v) => handleCronChange(timeToCron(v, localCron))}
            disabled={settingsControlsDisabled}
          />
          <p className="text-xs text-muted-foreground">{cronToHumanReadable(localCron)}</p>
        </div>

        {/* Deck size */}
        <div className="space-y-1">
          <Label htmlFor="pulse-deck-size" className="flex items-center justify-between">
            <span>Deck size</span>
            <span className="text-muted-foreground text-sm font-normal">{deckSize}</span>
          </Label>
          <input
            id="pulse-deck-size"
            type="range"
            min={5}
            max={30}
            step={5}
            value={deckSize}
            onChange={(e) =>
              setMut.mutate({ key: 'pulse.deck_size', value: parseInt(e.target.value, 10) })
            }
            disabled={settingsControlsDisabled}
            className="w-full accent-primary"
          />
          <p className="text-xs text-muted-foreground">
            Papers in your daily Pulse deck. Larger decks = more variety but longer review.
          </p>
        </div>

        {/* Stage-2 ranking candidates */}
        <div className="space-y-1">
          <Label htmlFor="pulse-stage2-top-k" className="flex items-center justify-between">
            <span>Ranking candidates</span>
            <span className="text-muted-foreground text-sm font-normal">{stage2TopK}</span>
          </Label>
          <input
            id="pulse-stage2-top-k"
            type="range"
            min={20}
            max={100}
            step={10}
            value={stage2TopK}
            onChange={(e) =>
              setMut.mutate({ key: 'pulse.stage2_top_k', value: parseInt(e.target.value, 10) })
            }
            disabled={settingsControlsDisabled}
            className="w-full accent-primary"
          />
          <p className="text-xs text-muted-foreground">
            Candidates the LLM reranker evaluates. Higher = better ranking quality but slower.
          </p>
        </div>

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
              {
                onError: (err) =>
                  toast.error('Failed to update lookback window', {
                    description: err instanceof Error ? err.message : 'Unknown error',
                  }),
              },
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
              {
                onError: (err) =>
                  toast.error('Failed to update startup grace', {
                    description: err instanceof Error ? err.message : 'Unknown error',
                  }),
              },
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
