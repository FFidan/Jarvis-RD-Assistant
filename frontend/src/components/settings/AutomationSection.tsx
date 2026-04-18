import { useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchNudges, updateNudge, fetchConfig, setConfig } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { TimeSelect } from '@/components/ui/time-select';
import { EmptyState } from '@/components/EmptyState';
import { Bell } from 'lucide-react';
import { formatDate } from '@/lib/utils';
import { cronToHumanReadable, cronToTime, timeToCron } from '@/lib/cron-utils';
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
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleTimeChange = (val: string) => {
    if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => {
      onTimeChange(nudge, val);
    }, 300);
  };

  return (
    <Card>
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
              onChange={handleTimeChange}
            />
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

  const { data: nudges = [], isLoading } = useQuery({
    queryKey: ['nudges'],
    queryFn: fetchNudges,
  });

  const { data: configs = [] } = useQuery({
    queryKey: ['config'],
    queryFn: fetchConfig,
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<Nudge> }) => updateNudge(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['nudges'] });
    },
  });

  const configMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['config'] });
    },
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

  const timezoneEntry = configs.find((e) => e.key === 'user.timezone');
  const timezoneValue = timezoneEntry
    ? (typeof timezoneEntry.value === 'string'
        ? timezoneEntry.value.replace(/^"|"$/g, '')
        : String(timezoneEntry.value))
    : 'UTC';

  const notificationNudges = nudges.filter((n) => NOTIFICATION_NUDGE_TYPES.has(n.nudge_type));
  const backgroundNudges = nudges.filter((n) => BACKGROUND_NUDGE_TYPES.has(n.nudge_type));
  // Nudges not in either known group fall into background tasks
  const otherNudges = nudges.filter(
    (n) => !NOTIFICATION_NUDGE_TYPES.has(n.nudge_type) && !BACKGROUND_NUDGE_TYPES.has(n.nudge_type),
  );

  return (
    <div className="space-y-6">
      {isLoading ? (
        <div className="py-8 text-center text-muted-foreground">Loading automation...</div>
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

            {/* Timezone field */}
            <Card className="mb-3">
              <CardContent className="flex items-center gap-4 p-4">
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm">Timezone</div>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Your local timezone for scheduling notifications (e.g. Europe/Berlin, America/New_York)
                  </p>
                </div>
                <Input
                  className="w-52"
                  defaultValue={timezoneValue}
                  onBlur={(e) => {
                    const val = e.target.value.trim();
                    if (val && val !== timezoneValue) {
                      configMut.mutate({ key: 'user.timezone', value: val });
                    }
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      const val = (e.target as HTMLInputElement).value.trim();
                      if (val && val !== timezoneValue) {
                        configMut.mutate({ key: 'user.timezone', value: val });
                      }
                    }
                  }}
                  disabled={configMut.isPending}
                  placeholder="UTC"
                />
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
    </div>
  );
}
