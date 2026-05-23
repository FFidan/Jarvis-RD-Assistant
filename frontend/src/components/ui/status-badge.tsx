/**
 * StatusBadge — renders an OK / Degraded / Failed badge with an optional tooltip.
 */
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { Badge } from '@/components/ui/badge';

interface StatusBadgeProps {
  status: 'ok' | 'degraded' | 'failed';
  tooltip?: string;
}

export function StatusBadge({ status, tooltip }: StatusBadgeProps) {
  if (status === 'failed') {
    return <Badge variant="destructive">Failed</Badge>;
  }

  const badgeEl =
    status === 'degraded' ? (
      <Badge
        variant="outline"
        className="text-[var(--status-warn)] border-[var(--status-warn)] cursor-default"
      >
        Degraded
      </Badge>
    ) : (
      <Badge
        variant="outline"
        className="text-[var(--status-ok)] border-[var(--status-ok)] cursor-default"
      >
        OK
      </Badge>
    );

  if (!tooltip) return badgeEl;

  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>{badgeEl}</TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs text-xs">
          {tooltip}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
