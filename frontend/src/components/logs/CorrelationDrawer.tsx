import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { getCorrelation } from '@/lib/logs';
import type { SystemEvent } from '@/lib/logs';
import { LEVEL_COLORS } from './utils';

interface CorrelationDrawerProps {
  correlationId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function CorrelationDrawer({
  correlationId,
  open,
  onOpenChange,
}: CorrelationDrawerProps) {
  const { data: events, isLoading } = useQuery({
    queryKey: QUERY_KEYS.logs.correlation(correlationId!),
    queryFn: () => getCorrelation(correlationId!),
    enabled: !!correlationId && open,
  });

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="sm:max-w-xl w-full overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="text-sm font-mono truncate">
            {correlationId ?? 'Correlation chain'}
          </SheetTitle>
        </SheetHeader>

        <div className="mt-4">
          {isLoading && (
            <p className="text-sm text-muted-foreground">Loading events…</p>
          )}
          {!isLoading && (!events || events.length === 0) && (
            <p className="text-sm text-muted-foreground">No events found.</p>
          )}
          {events && events.length > 0 && (
            <Timeline events={events} />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function Timeline({ events }: { events: SystemEvent[] }) {
  return (
    <ol className="relative border-l border-border ml-3 space-y-4">
      {events.map((ev) => (
        <li key={ev.id} className="ml-4">
          <span
            className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full border border-background"
            style={{ backgroundColor: LEVEL_COLORS[ev.level] ?? '#6b7280' }}
          />
          <div className="text-xs text-muted-foreground">
            {new Date(ev.created_at).toLocaleString()}
          </div>
          <div className="text-sm font-medium">{ev.message}</div>
          <div className="text-xs text-muted-foreground">
            {ev.source} · {ev.category}
          </div>
          {ev.context && Object.keys(ev.context).length > 0 && (
            <pre className="mt-1 text-xs font-mono bg-muted p-2 rounded overflow-x-auto whitespace-pre-wrap break-all">
              {JSON.stringify(ev.context, null, 2)}
            </pre>
          )}
        </li>
      ))}
    </ol>
  );
}
