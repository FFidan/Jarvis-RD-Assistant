import { useEffect, useId, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchNudges, updateNudge, fetchConfig, setConfig } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { TimeSelect } from '@/components/ui/time-select';
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover';
import { EmptyState } from '@/components/EmptyState';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import { Bell, ChevronsUpDown, Check } from 'lucide-react';
import { formatDate } from '@/lib/utils';
import { cronToHumanReadable, cronToTime, isTimeOnlyCron, timeToCron } from '@/lib/cron-utils';
import { onSaveError } from '@/lib/forms/save-error';
import { TIMEZONE_OPTIONS, TIMEZONE_BY_VALUE, TIMEZONE_REGIONS } from '@/lib/timezone-data';
import type { Nudge } from '@/types';

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

/** Nudge types that belong to the "Notification Schedules" group. */
const NOTIFICATION_NUDGE_TYPES = new Set(['daily_summary', 'paper_digest', 'review_reminder']);

/** Nudge types that belong to the "Background Tasks" group. */
const BACKGROUND_NUDGE_TYPES = new Set(['research_pulse', 'author_alert', 'deadline_warning']);


function NudgeRow({
  nudge,
  onToggle,
  onTimeChange,
  isPending,
}: {
  nudge: Nudge;
  onToggle: (nudge: Nudge) => void;
  onTimeChange: (nudge: Nudge, val: string) => void;
  isPending: boolean;
}) {
  // A cron expression with a list/range/step/wildcard minute or hour (multiple
  // daily runs, or hourly) has no single clock time the picker could show —
  // editing it would silently discard the extra runs. Show it as read-only
  // text instead of an inert control.
  const canEditTime = isTimeOnlyCron(nudge.cron_expression);

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardContent className="flex flex-col sm:flex-row items-start sm:items-center gap-4 p-4">
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
          <div className="mt-1 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
            <span>{cronToHumanReadable(nudge.cron_expression)}</span>
            {canEditTime && (
              <TimeSelect
                value={cronToTime(nudge.cron_expression)}
                onChange={(val) => onTimeChange(nudge, val)}
              />
            )}
            {nudge.last_fired_at && (
              <span>Last run: {formatDate(nudge.last_fired_at)}</span>
            )}
          </div>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => onToggle(nudge)}
          disabled={isPending}
        >
          {nudge.enabled ? 'Disable' : 'Enable'}
        </Button>
      </CardContent>
    </Card>
  );
}

export function AutomationSection() {
  const queryClient = useQueryClient();
  const tzSearchId = useId();
  const fetchIntervalId = useId();

  const { data: nudges = [], isLoading, isError: nudgesError } = useQuery({
    queryKey: QUERY_KEYS.account.nudges(),
    queryFn: fetchNudges,
  });

  const { data: configs = [], isPending: configsPending, isError: configsError } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });

  // Config-backed controls stay inert until real server values arrive. While
  // the config query is pending or failed, the fallbacks below (24 hours, UTC)
  // are placeholders, not data — letting a blur or click persist them would
  // overwrite the real server state.
  const configsReady = !configsPending && !configsError;

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<Nudge> }) => updateNudge(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.account.nudges() });
    },
    onError: onSaveError('Could not update this notification schedule'),
  });

  const configMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
    },
    onError: onSaveError('Could not save this setting'),
  });

  const handleToggle = (nudge: Nudge) => {
    updateMut.mutate({ id: nudge.id, data: { enabled: !nudge.enabled } });
  };

  const handleTimeChange = (nudge: Nudge, val: string) => {
    updateMut.mutate({
      id: nudge.id,
      data: { cron_expression: timeToCron(val, nudge.cron_expression) },
    });
  };

  const fetchIntervalEntry = configs.find((e) => e.key === 'automation.fetch_interval_hours');
  const fetchIntervalValue =
    fetchIntervalEntry !== undefined && fetchIntervalEntry.value !== null
      ? Number(fetchIntervalEntry.value)
      : 24;
  // Seed the writable input only from loaded data; until then it stays empty
  // and disabled, and it re-syncs whenever the server value changes (e.g.
  // after page reload).
  const [fetchIntervalInput, setFetchIntervalInput] = useState<number | ''>('');
  useEffect(() => {
    if (configsReady) setFetchIntervalInput(fetchIntervalValue);
  }, [configsReady, fetchIntervalValue]);

  const timezoneEntry = configs.find((e) => e.key === 'user.timezone');
  const timezoneValue = timezoneEntry
    ? (typeof timezoneEntry.value === 'string'
        ? timezoneEntry.value.replace(/^"|"$/g, '')
        : String(timezoneEntry.value))
    : 'UTC';

  // Same containment as the interval input: seed only from loaded data, and
  // keep the local input in sync when the server value changes.
  const [timezoneInput, setTimezoneInput] = useState<string>('');
  useEffect(() => {
    if (configsReady) setTimezoneInput(timezoneValue);
  }, [configsReady, timezoneValue]);

  // Timezone combobox open state
  const [tzOpen, setTzOpen] = useState(false);
  const [tzSearch, setTzSearch] = useState('');

  const filteredTz = tzSearch.trim()
    ? TIMEZONE_OPTIONS.filter((tz) =>
        tz.searchTerms.includes(tzSearch.toLowerCase()),
      )
    : TIMEZONE_OPTIONS;

  const notificationNudges = nudges.filter((n) => NOTIFICATION_NUDGE_TYPES.has(n.nudge_type));
  const backgroundNudges = nudges.filter((n) => BACKGROUND_NUDGE_TYPES.has(n.nudge_type));
  // Nudges not in either known group fall into background tasks
  const otherNudges = nudges.filter(
    (n) => !NOTIFICATION_NUDGE_TYPES.has(n.nudge_type) && !BACKGROUND_NUDGE_TYPES.has(n.nudge_type),
  );

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        Control when JARVIS runs background tasks and sends you notifications. Enable or disable each job and set its schedule here.
      </p>
      {configsError && <QueryErrorState message="Failed to load automation settings." />}
      {isLoading ? (
        <div className="py-8 text-center text-muted-foreground">Loading automation...</div>
      ) : nudgesError ? (
        <QueryErrorState message="Failed to load automation jobs." />
      ) : nudges.length === 0 ? (
        <EmptyState
          title="No automation jobs"
          description="Scheduled jobs will appear once configured."
          icon={Bell}
        />
      ) : (
        <>
          {/* Notification Schedules */}
          <div>
            <h3 className="text-base font-semibold mt-0 mb-2">Notification Schedules</h3>

            {/* Timezone combobox */}
            <Card className="rounded-md border-hair shadow-none mb-3">
              <CardContent className="flex items-center gap-4 p-4">
                <div className="flex-1 min-w-0">
                  <h4 className="text-sm font-medium">Timezone</h4>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Your local timezone for scheduling notifications
                  </p>
                </div>
                <Popover open={tzOpen} onOpenChange={setTzOpen}>
                  <PopoverTrigger asChild>
                    <Button
                      variant="outline"
                      role="combobox"
                      aria-expanded={tzOpen}
                      className="w-full sm:w-64 justify-between font-normal text-left"
                      disabled={!configsReady || configMut.isPending}
                    >
                      <span className="truncate">
                        {configsReady
                          ? (TIMEZONE_BY_VALUE.get(timezoneInput)?.label ?? timezoneInput)
                          : '—'}
                      </span>
                      <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent className="w-72 sm:w-80 p-0" align="end">
                    <div className="flex items-center border-b px-3">
                      <Label htmlFor={tzSearchId} className="sr-only">
                        Search city or timezone
                      </Label>
                      <Input
                        id={tzSearchId}
                        className="h-9 border-0 focus-visible:ring-0 focus-visible:ring-offset-0 shadow-none"
                        placeholder="Search city or timezone..."
                        value={tzSearch}
                        onChange={(e) => setTzSearch(e.target.value)}
                      />
                    </div>
                    <div className="max-h-72 overflow-y-auto">
                      {TIMEZONE_REGIONS.map((region) => {
                        const items = filteredTz.filter((tz) => tz.region === region);
                        if (items.length === 0) return null;
                        return (
                          <div key={region}>
                            <div className="px-3 py-1.5 text-xs font-medium text-muted-foreground bg-muted/40">
                              {region}
                            </div>
                            {items.map((tz) => (
                              <button
                                key={tz.value}
                                className={`flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-accent text-left ${
                                  timezoneInput === tz.value ? 'bg-accent/60' : ''
                                }`}
                                onClick={() => {
                                  setTimezoneInput(tz.value);
                                  configMut.mutate({ key: 'user.timezone', value: tz.value });
                                  setTzOpen(false);
                                  setTzSearch('');
                                }}
                              >
                                <Check
                                  className={`h-4 w-4 shrink-0 ${
                                    timezoneInput === tz.value ? 'opacity-100' : 'opacity-0'
                                  }`}
                                />
                                <span>{tz.label}</span>
                              </button>
                            ))}
                          </div>
                        );
                      })}
                      {filteredTz.length === 0 && (
                        <p className="px-3 py-4 text-center text-sm text-muted-foreground">
                          No timezone found.
                        </p>
                      )}
                    </div>
                  </PopoverContent>
                </Popover>
              </CardContent>
            </Card>

            <div className="space-y-2">
              {notificationNudges.map((nudge) => (
                <NudgeRow
                  key={nudge.id}
                  nudge={nudge}
                  onToggle={handleToggle}
                  onTimeChange={handleTimeChange}
                  isPending={updateMut.isPending}
                />
              ))}
            </div>
          </div>

          {/* Background Tasks */}
          {(backgroundNudges.length > 0 || otherNudges.length > 0) && (
            <div>
              <h3 className="text-base font-semibold mt-6 mb-2">Background Tasks</h3>
              <div className="space-y-2">
                {[...backgroundNudges, ...otherNudges].map((nudge) => (
                  <NudgeRow
                    key={nudge.id}
                    nudge={nudge}
                    onToggle={handleToggle}
                    onTimeChange={handleTimeChange}
                    isPending={updateMut.isPending}
                  />
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {/* Auto-fetch interval — always visible regardless of nudge count */}
      {!isLoading && (
        <div>
          <h3 className="text-base font-semibold mt-0 mb-2">Paper fetching</h3>
          <Card className="rounded-md border-hair shadow-none">
            <CardContent className="flex items-center gap-4 p-4">
              <div className="flex-1 min-w-0">
                <h4 className="text-sm font-medium">Check for new papers every (hours)</h4>
                <p className="text-xs text-muted-foreground mt-0.5">
                  How often the background pipeline fetches new papers from all sources
                </p>
              </div>
              <Label htmlFor={fetchIntervalId} className="sr-only">
                Check for new papers every (hours)
              </Label>
              <Input
                id={fetchIntervalId}
                type="number"
                min={1}
                className="w-24 text-right"
                value={fetchIntervalInput}
                disabled={!configsReady || configMut.isPending}
                onChange={(e) => setFetchIntervalInput(Number(e.target.value))}
                onBlur={() => {
                  // Guard against programmatic blur while disabled: never
                  // persist a value that was not seeded from server data.
                  if (!configsReady || fetchIntervalInput === '') return;
                  const hours = Math.max(1, fetchIntervalInput);
                  setFetchIntervalInput(hours);
                  configMut.mutate({ key: 'automation.fetch_interval_hours', value: hours });
                }}
              />
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
