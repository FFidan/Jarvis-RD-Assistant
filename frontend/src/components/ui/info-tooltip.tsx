import * as React from 'react';
import { Info } from 'lucide-react';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';

export interface InfoTooltipProps {
  content: React.ReactNode;
  side?: 'top' | 'right' | 'bottom' | 'left';
  className?: string;
  triggerElement?: 'button' | 'span';
}

/**
 * Generic `(i)` info icon that shows a tooltip on hover/focus.
 *
 * Wraps a Shadcn `Tooltip` in a `TooltipProvider` so it can be dropped in
 * anywhere without requiring a global provider. Keyboard-focusable and
 * labelled for screen readers.
 */
export function InfoTooltip({
  content,
  side = 'top',
  className,
  triggerElement = 'button',
}: InfoTooltipProps) {
  const triggerClassName = cn(
    'inline-flex items-center justify-center rounded-full text-muted-foreground hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring',
    className,
  );
  const icon = <Info className="h-3.5 w-3.5" aria-hidden="true" />;

  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          {triggerElement === 'span' ? (
            <span
              aria-label="More info"
              className={triggerClassName}
              onClick={(event) => event.stopPropagation()}
              onMouseDown={(event) => event.stopPropagation()}
            >
              {icon}
            </span>
          ) : (
            <button type="button" aria-label="More info" className={triggerClassName}>
              {icon}
            </button>
          )}
        </TooltipTrigger>
        <TooltipContent side={side} className="max-w-xs text-xs">
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
