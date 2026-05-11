/**
 * ErrorSparkLine — small Recharts line chart showing errors-per-minute
 * over the last hour. Data is aggregated client-side from events returned
 * by the logs endpoint; no backend changes needed.
 */

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';
import type { SystemEvent } from '@/lib/logs';

interface SparkBucket {
  /** Label shown on x-axis, e.g. "10:45" */
  label: string;
  errors: number;
}

/**
 * Bucket events by minute into a 60-slot array covering the last hour.
 * Only error/critical levels are counted.
 */
export function buildSparkBuckets(events: SystemEvent[]): SparkBucket[] {
  const now = Date.now();
  const buckets: SparkBucket[] = Array.from({ length: 60 }, (_, i) => {
    const minuteStart = now - (59 - i) * 60_000;
    const d = new Date(minuteStart);
    const label = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    return { label, errors: 0 };
  });

  for (const ev of events) {
    if (ev.level !== 'error' && ev.level !== 'critical') continue;
    const ts = new Date(ev.created_at).getTime();
    const minutesAgo = Math.floor((now - ts) / 60_000);
    if (minutesAgo < 0 || minutesAgo >= 60) continue;
    const idx = 59 - minutesAgo;
    const bucket = buckets[idx];
    if (bucket) bucket.errors += 1;
  }

  return buckets;
}

interface ErrorSparkLineProps {
  events: SystemEvent[];
}

export function ErrorSparkLine({ events }: ErrorSparkLineProps) {
  const data = buildSparkBuckets(events);
  const hasErrors = data.some((b) => b.errors > 0);

  if (!hasErrors) {
    return (
      <div className="rounded-md border border-border bg-muted/20 px-4 py-3 flex items-center gap-2">
        <span className="h-2 w-2 rounded-full bg-green-500" />
        <span className="text-xs text-muted-foreground">
          No errors in the last hour
        </span>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-border bg-muted/20 px-4 py-3">
      <div className="text-xs text-muted-foreground mb-2 font-medium">
        Errors per minute — last 60 min
      </div>
      <div data-testid="error-sparkline" className="h-16">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <XAxis
              dataKey="label"
              tick={false}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              width={24}
              tick={{ fontSize: 10 }}
              axisLine={false}
              tickLine={false}
              allowDecimals={false}
            />
            <Tooltip
              contentStyle={{ fontSize: 11 }}
              formatter={(value) => [Number(value), 'errors']}
            />
            <Line
              type="monotone"
              dataKey="errors"
              stroke="#ef4444"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
