/**
 * CorrelationGroup — progressive-disclosure row for a group of events that
 * share the same correlation_id.
 *
 * Summary row shows: correlation_id | event count | span duration | error count.
 * Clicking expands to show the nested event list inline.
 */

import { useState, Fragment } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { LEVEL_BADGE_CLASSES, CATEGORY_BADGE_CLASSES } from './utils';
import { EventDetail } from './EventDetail';
import type { SystemEvent } from '@/lib/logs';

interface CorrelationGroupProps {
  correlationId: string;
  events: SystemEvent[];
  searchText: string;
}

function spanDuration(events: SystemEvent[]): string {
  if (events.length === 0) return '—';
  const ts = events.map((e) => new Date(e.created_at).getTime());
  const ms = Math.max(...ts) - Math.min(...ts);
  if (ms < 1_000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1_000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}

export function CorrelationGroup({
  correlationId,
  events,
  searchText,
}: CorrelationGroupProps) {
  const [expanded, setExpanded] = useState(false);
  const [expandedEventId, setExpandedEventId] = useState<number | null>(null);

  const errorCount = events.filter(
    (e) => e.level === 'error' || e.level === 'critical',
  ).length;

  const firstTs = events.length > 0 && events[0]
    ? new Date(events[0].created_at).toLocaleString()
    : '—';

  // Apply free-text filter within expanded view
  const visibleEvents = searchText
    ? events.filter((e) =>
        e.message.toLowerCase().includes(searchText.toLowerCase()),
      )
    : events;

  return (
    <div className="rounded-md border border-border overflow-hidden">
      {/* Summary row */}
      <button
        data-testid={`group-${correlationId}`}
        className="w-full flex items-center gap-3 px-3 py-2 text-xs hover:bg-muted/30 text-left transition-colors"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}

        {/* Correlation id */}
        <code className="font-mono text-[10px] bg-muted px-1 rounded shrink-0 max-w-[10rem] truncate">
          {correlationId}
        </code>

        {/* Event count */}
        <span className="shrink-0 text-muted-foreground">
          {events.length} event{events.length !== 1 ? 's' : ''}
        </span>

        {/* Span duration */}
        <span className="shrink-0 text-muted-foreground">
          {spanDuration(events)}
        </span>

        {/* Error count badge */}
        {errorCount > 0 && (
          <span className="shrink-0 rounded px-1.5 py-0.5 bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300 font-medium">
            {errorCount} error{errorCount !== 1 ? 's' : ''}
          </span>
        )}

        {/* First timestamp */}
        <span className="ml-auto shrink-0 text-muted-foreground font-mono whitespace-nowrap">
          {firstTs}
        </span>
      </button>

      {/* Expanded event list */}
      {expanded && (
        <div className="border-t border-border divide-y divide-border bg-muted/10">
          {visibleEvents.length === 0 && (
            <p className="px-3 py-2 text-xs text-muted-foreground italic">
              No events match the search filter.
            </p>
          )}
          {visibleEvents.map((ev) => (
            <Fragment key={ev.id}>
              <button
                className="w-full flex items-start gap-3 px-4 py-2 text-xs hover:bg-muted/30 text-left transition-colors"
                onClick={() =>
                  setExpandedEventId((id) => (id === ev.id ? null : ev.id))
                }
                aria-expanded={expandedEventId === ev.id}
              >
                <span className="text-muted-foreground shrink-0 font-mono whitespace-nowrap">
                  {new Date(ev.created_at).toLocaleTimeString()}
                </span>
                <span
                  className={cn(
                    'shrink-0 rounded px-1 py-0.5 font-medium',
                    LEVEL_BADGE_CLASSES[ev.level] ?? '',
                  )}
                >
                  {ev.level}
                </span>
                <span
                  className={cn(
                    'shrink-0 rounded px-1 py-0.5',
                    CATEGORY_BADGE_CLASSES[ev.category] ?? '',
                  )}
                >
                  {ev.category}
                </span>
                <span className="text-muted-foreground shrink-0">{ev.source}</span>
                <span className="break-all flex-1">{ev.message}</span>
              </button>
              {expandedEventId === ev.id && (
                <div className="px-4 pb-3">
                  <EventDetail event={ev} />
                </div>
              )}
            </Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
