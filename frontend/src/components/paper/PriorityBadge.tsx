import { Badge } from '@/components/ui/badge';
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
  'unscored': 'Unscored',
};

export function PriorityBadge({ level }: PriorityBadgeProps) {
  return (
    <Badge variant="outline" className={cn('shrink-0 text-xs', priorityStyles[level])}>
      {priorityLabels[level]}
    </Badge>
  );
}
