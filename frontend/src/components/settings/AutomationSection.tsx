import { useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchNudges, updateNudge } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { TimeSelect } from '@/components/ui/time-select';
import { EmptyState } from '@/components/EmptyState';
import { Bell } from 'lucide-react';
import { formatDate } from '@/lib/utils';
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

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

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
