import { useEffect, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  fetchNudges,
  updateNudge,
  fetchConfig,
  setConfig,
  fetchPulseStats,
} from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { TimeSelect } from '@/components/ui/time-select';
import { EmptyState } from '@/components/EmptyState';
import { Bell } from 'lucide-react';
import { formatDate } from '@/lib/utils';
import type { Nudge, ConfigEntry, PulseStats } from '@/types';

const nudgeLabels: Record<string, string> = {
  research_pulse: 'Background Paper Search',
  review_reminder: 'Flashcard Review Reminder',
  deadline_warning: 'Project Deadline Alert',
  daily_summary: 'Daily Briefing',
  paper_digest: 'Paper Digest',
  author_alert: 'Author Alerts',
};

const nudgeDescriptions: Record<string, string> = {
  research_pulse: 'Automatically searches for new papers matching your topics',
  review_reminder: 'Reminds you when flashcards are due for spaced repetition review',
  deadline_warning: 'Alerts you about upcoming project deadlines',
  daily_summary: 'Morning briefing with new papers, due cards, and project updates',
  paper_digest: 'Weekly digest of the most relevant papers from your sources',
  author_alert: 'Notifies when tracked authors publish new papers',
};

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

const CRON_TOOLTIP =
  'The time of day when Pulse discovery runs automatically. Papers are scored and ranked so your deck is ready when you start your day.';

function cronToHumanReadable(cron: string): string {
  const parts = cron.split(/\s+/);
  if (parts.length < 5) return cron;
  const [minStr, hourStr, , , dowStr] = parts;
  const minute = parseInt(minStr, 10);
  const hour = parseInt(hourStr, 10);
  if (isNaN(minute) || isNaN(hour)) return cron;
  const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  if (dowStr === '*') return `Daily at ${time}`;
  if (dowStr === '1-5') return `Weekdays at ${time}`;
  if (dowStr === '0,6' || dowStr === '6,0') return `Weekends at ${time}`;
  if (dowStr.includes('-') || dowStr.includes(',')) return `Custom days at ${time}`;
  const dow = parseInt(dowStr, 10);
  if (isNaN(dow)) return `Custom days at ${time}`;
  const dayName = DAY_NAMES[dow] ?? dowStr;
  return `Weekly on ${dayName} at ${time}`;
}

function isValidCron(s: string): boolean {
  const parts = s.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  return parts.every((p) => /^[*/0-9,\-]+$/.test(p));
}

function cronToTime(cron: string): string {
  const parts = cron.split(/\s+/);
  const minute = parseInt(parts[0], 10);
  const hour = parseInt(parts[1], 10);
  if (isNaN(minute) || isNaN(hour)) return '09:00';
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

function timeToCron(time: string, originalCron: string): string {
  const [hourStr, minuteStr] = time.split(':');
  const hour = parseInt(hourStr, 10);
  const minute = parseInt(minuteStr, 10);
  if (isNaN(hour) || isNaN(minute)) return originalCron;
  const parts = originalCron.split(/\s+/);
  return `${minute} ${hour} ${parts[2] ?? '*'} ${parts[3] ?? '*'} ${parts[4] ?? '*'}`;
}

function getConfigValue<T>(entries: ConfigEntry[], key: string, fallback: T): T {
  const entry = entries.find((c) => c.key === key);
  return entry !== undefined ? (entry.value as T) : fallback;
}

function PulseSubsection() {
  const queryClient = useQueryClient();
  const cronTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (cronTimeoutRef.current !== null) clearTimeout(cronTimeoutRef.current);
    },
    [],
  );

  const { data: configs = [] } = useQuery({
    queryKey: ['config'],
    queryFn: fetchConfig,
  });

  const { data: stats } = useQuery<PulseStats>({
    queryKey: ['pulse-stats', 1],
    queryFn: () => fetchPulseStats(1),
  });

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['config'] }),
  });

  const enabled = getConfigValue<boolean>(configs, 'pulse.enabled', false);
  const cron = getConfigValue<string>(configs, 'pulse.cron', '0 4 * * *');
  const deckSize = getConfigValue<number>(configs, 'pulse.deck_size', 10);
  const stage2TopK = getConfigValue<number>(configs, 'pulse.stage2_top_k', 50);
  const [localCron, setLocalCron] = useState(cron);

  useEffect(() => {
    setLocalCron(cron);
  }, [cron]);

  const handleToggle = () => {
    setMut.mutate({ key: 'pulse.enabled', value: !enabled });
  };

  const handleCronChange = (value: string) => {
    setLocalCron(value);
    if (cronTimeoutRef.current) clearTimeout(cronTimeoutRef.current);
    if (!isValidCron(value)) return;
    cronTimeoutRef.current = setTimeout(() => {
      setMut.mutate({ key: 'pulse.cron', value });
    }, 400);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Pulse</CardTitle>
        <CardDescription>
          Nightly ranked deck of candidate papers scored by the Pulse pipeline.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center justify-between">
          <Label htmlFor="pulse-enable-toggle">Enable Pulse</Label>
          <button
            id="pulse-enable-toggle"
            type="button"
            role="switch"
            aria-label="Enable Pulse"
            aria-checked={!!enabled}
            onClick={handleToggle}
            disabled={setMut.isPending}
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

        <div className="space-y-1">
          <Label htmlFor="pulse-cron-time" className="flex items-center gap-1">
            Daily run time
            <InfoTooltip content={CRON_TOOLTIP} />
          </Label>
          <TimeSelect
            value={cronToTime(localCron)}
            onChange={(v) => handleCronChange(timeToCron(v, localCron))}
          />
          <p className="text-xs text-muted-foreground">{cronToHumanReadable(localCron)}</p>
        </div>

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
            disabled={setMut.isPending}
            className="w-full accent-primary"
          />
          <p className="text-xs text-muted-foreground">
            Papers in your daily Pulse deck. Larger decks = more variety but longer review.
          </p>
        </div>

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
            disabled={setMut.isPending}
            className="w-full accent-primary"
          />
          <p className="text-xs text-muted-foreground">
            Candidates the LLM reranker evaluates. Higher = better ranking quality but slower.
          </p>
        </div>

        <div className="rounded-md border bg-muted/30 p-3 text-sm">
          <div className="font-medium">Last Pulse run</div>
          {stats ? (
            <div className="mt-1 space-y-1 text-muted-foreground">
              <div>
                Last run:{' '}
                <span className="font-mono">
                  {stats.last_run_at ? formatDate(stats.last_run_at) : 'never'}
                </span>
              </div>
              <div>
                Decks generated: <span className="font-mono">{stats.decks_generated}</span>
              </div>
              {stats.last_error && (
                <Badge variant="destructive" className="mt-1">
                  Error: {stats.last_error}
                </Badge>
              )}
            </div>
          ) : (
            <div className="mt-1 text-xs text-muted-foreground">Loading stats...</div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function AutomationSection() {
  const queryClient = useQueryClient();
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (timeoutRef.current !== null) clearTimeout(timeoutRef.current); }, []);

  const { data: nudges = [], isLoading } = useQuery({
    queryKey: ['nudges'],
    queryFn: fetchNudges,
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<Nudge> }) => updateNudge(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['nudges'] });
    },
  });

  const handleToggle = (nudge: Nudge) => {
    updateMut.mutate({ id: nudge.id, data: { enabled: !nudge.enabled } });
  };

  return (
    <div className="space-y-6">
      <PulseSubsection />

      {isLoading ? (
        <div className="py-8 text-center text-muted-foreground">Loading automation...</div>
      ) : nudges.length === 0 ? (
        <EmptyState
          title="No automation jobs"
          description="Scheduled jobs will appear once configured."
          icon={Bell}
        />
      ) : (
        <div className="space-y-2">
          {nudges.map((nudge) => (
            <Card key={nudge.id}>
              <CardContent className="flex items-center gap-4 p-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">
                      {nudgeLabels[nudge.nudge_type] ?? nudge.nudge_type}
                    </span>
                    <Badge variant={nudge.enabled ? 'default' : 'outline'}>
                      {nudge.enabled ? 'Enabled' : 'Disabled'}
                    </Badge>
                  </div>
                  {nudgeDescriptions[nudge.nudge_type] && (
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {nudgeDescriptions[nudge.nudge_type]}
                    </p>
                  )}
                  <div className="mt-1 flex items-center gap-3 text-sm text-muted-foreground">
                    <span>{cronToHumanReadable(nudge.cron_expression)}</span>
                    <TimeSelect
                      value={cronToTime(nudge.cron_expression)}
                      onChange={(val) => {
                        if (timeoutRef.current) clearTimeout(timeoutRef.current);
                        timeoutRef.current = setTimeout(() => {
                          updateMut.mutate({
                            id: nudge.id,
                            data: { cron_expression: timeToCron(val, nudge.cron_expression) },
                          });
                        }, 300);
                      }}
                    />
                    {nudge.last_fired_at && (
                      <span>Last run: {formatDate(nudge.last_fired_at)}</span>
                    )}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => handleToggle(nudge)}
                  disabled={updateMut.isPending}
                >
                  {nudge.enabled ? 'Disable' : 'Enable'}
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
