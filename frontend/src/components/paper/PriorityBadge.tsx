import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { type PriorityLevel } from '@/types';
import { cn } from '@/lib/utils';

interface PriorityBadgeProps {
  level: PriorityLevel;
}

const priorityStyles: Record<PriorityLevel, string> = {
  'must-read': 'bg-red-100 text-red-800 border-red-200',
  'recommended': 'bg-amber-100 text-amber-800 border-amber-200',
  'background': 'bg-blue-100 text-blue-800 border-blue-200',
  'unscored': 'bg-gray-100 text-gray-600 border-gray-200',
};

const priorityLabels: Record<PriorityLevel, string> = {
  'must-read': 'MUST READ',
  'recommended': 'Recommended',
  'background': 'Background',
  'unscored': 'Not yet ranked',
};

export function PriorityBadge({ level }: PriorityBadgeProps) {
  if (level === 'unscored') {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant="outline" className={cn('shrink-0 text-xs', priorityStyles[level])}>
              Not yet ranked
            </Badge>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs text-xs">
            This paper has not been ranked by the Pulse discovery pipeline yet. Scored papers appear
            in your daily Pulse deck on My Day.
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return (
    <Badge variant="outline" className={cn('shrink-0 text-xs', priorityStyles[level])}>
      {priorityLabels[level]}
    </Badge>
  );
}
