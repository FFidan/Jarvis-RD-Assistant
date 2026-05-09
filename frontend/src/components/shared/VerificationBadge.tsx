import { CheckCircle, AlertTriangle } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';

export interface VerificationBadgeProps {
  variant: 'verified' | 'unverified';
  /** Tooltip text shown on hover. Required for "unverified"; optional for "verified". */
  reason?: string;
  className?: string;
}

/**
 * Small inline badge indicating whether LLM-generated reasoning or a weekly
 * digest theme has been verified against source text by the QuoteVerifier.
 *
 * - `verified`:   subtle green badge with a check icon — the common case; not visually loud.
 * - `unverified`: amber badge with a warning icon + tooltip showing the verifier's reason.
 *
 * Designed to sit inline next to reasoning text in PulseCard and weekly digest
 * theme blocks.
 */
export function VerificationBadge({
  variant,
  reason,
  className,
}: VerificationBadgeProps) {
  if (variant === 'verified') {
    const content = (
      <Badge
        variant="outline"
        className={cn(
          'inline-flex shrink-0 items-center gap-1 border-green-200 bg-green-50 px-1.5 py-0.5 text-[10px] font-medium text-green-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 dark:border-green-800 dark:bg-green-950 dark:text-green-400',
          className,
        )}
        aria-label="Reasoning verified"
      >
        <CheckCircle
          className="h-3 w-3 shrink-0"
          aria-hidden="true"
          data-testid="verification-badge-check-icon"
        />
        Verified
      </Badge>
    );

    if (!reason) {
      return content;
    }

    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{content}</TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs text-xs">
            {reason}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  // unverified variant — always shows tooltip (reason explains why it failed)
  const badgeContent = (
    <Badge
      variant="outline"
      className={cn(
        'inline-flex shrink-0 items-center gap-1 border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-400',
        className,
      )}
      aria-label="Reasoning not verified"
    >
      <AlertTriangle
        className="h-3 w-3 shrink-0"
        aria-hidden="true"
        data-testid="verification-badge-warn-icon"
      />
      Unverified
    </Badge>
  );

  if (!reason) {
    return badgeContent;
  }

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{badgeContent}</TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs text-xs">
          {reason}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
