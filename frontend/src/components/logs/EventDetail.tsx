import type { SystemEvent } from '@/lib/logs';
import { useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { ExternalLink } from 'lucide-react';

interface EventDetailProps {
  event: SystemEvent;
}

export function EventDetail({ event }: EventDetailProps) {
  const navigate = useNavigate();

  const hasContext =
    event.context && Object.keys(event.context).length > 0;

  return (
    <div className="px-4 py-3 bg-muted/40 rounded-md space-y-3 text-sm">
      {event.correlation_id && (
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground">Correlation ID:</span>
          <code className="font-mono text-xs bg-muted px-1 rounded">
            {event.correlation_id}
          </code>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={() =>
              navigate(`/logs?tab=events&correlation=${event.correlation_id}`)
            }
          >
            <ExternalLink className="h-3 w-3 mr-1" />
            View correlated events
          </Button>
        </div>
      )}

      {hasContext && (
        <div>
          <div className="text-muted-foreground mb-1">Context:</div>
          <pre className="text-xs font-mono bg-muted p-2 rounded overflow-x-auto whitespace-pre-wrap break-all">
            {JSON.stringify(event.context, null, 2)}
          </pre>
        </div>
      )}

      {!hasContext && !event.correlation_id && (
        <p className="text-muted-foreground italic">No additional context</p>
      )}
    </div>
  );
}
