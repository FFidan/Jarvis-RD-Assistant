import { Activity } from 'lucide-react';
import type { ProjectActivityItem } from '@/types';
import { EmptyState } from '@/components/EmptyState';

/** Map backend `kind` values to display prefix labels. */
const KIND_LABELS: Record<ProjectActivityItem['kind'], string> = {
  added_paper: 'ADDED',
  completed_task: 'COMPLETED TASK',
  completed_milestone: 'COMPLETED MILESTONE',
};

function relativeTime(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} minute${minutes !== 1 ? 's' : ''} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours !== 1 ? 's' : ''} ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} day${days !== 1 ? 's' : ''} ago`;
  const months = Math.floor(days / 30);
  return `${months} month${months !== 1 ? 's' : ''} ago`;
}

interface RecentActivitySectionProps {
  items: ProjectActivityItem[];
}

export function RecentActivitySection({ items }: RecentActivitySectionProps) {
  return (
    <section aria-labelledby="recent-activity-heading">
      <h3
        id="recent-activity-heading"
        className="mb-3 text-xs font-semibold tracking-widest text-muted-foreground uppercase"
      >
        RECENT ACTIVITY
      </h3>

      {items.length === 0 ? (
        <EmptyState
          title="No activity yet"
          description="Activity appears when papers are added or tasks and milestones are completed."
          icon={Activity}
        />
      ) : (
        <ul className="space-y-2">
          {items.map((item, idx) => (
            <li
              key={`${item.kind}-${item.ts}-${idx}`}
              className="flex items-baseline gap-2 rounded-md border p-3 text-sm"
            >
              <span className="shrink-0 text-xs font-semibold tracking-wide text-muted-foreground">
                {KIND_LABELS[item.kind]}
              </span>
              <span className="flex-1 truncate">{item.label}</span>
              <span className="shrink-0 text-xs text-muted-foreground whitespace-nowrap">
                {relativeTime(item.ts)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
