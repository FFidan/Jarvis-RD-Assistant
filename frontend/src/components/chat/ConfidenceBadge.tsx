import { Badge } from '@/components/ui/badge';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import type { ConfidenceLevel } from '@/lib/sse';

interface ConfidenceBadgeProps {
  confidence: ConfidenceLevel;
  verified_fraction: number;
  per_sentence: { text: string; verified: boolean }[];
}

const confidenceStyles: Record<ConfidenceLevel, string> = {
  HIGH: 'bg-green-100 text-green-800 border-green-200 hover:bg-green-200 cursor-pointer',
  MEDIUM: 'bg-yellow-100 text-yellow-800 border-yellow-200 hover:bg-yellow-200 cursor-pointer',
  LOW: 'bg-orange-100 text-orange-800 border-orange-200 hover:bg-orange-200 cursor-pointer',
  UNVERIFIED: 'bg-red-100 text-red-800 border-red-200 hover:bg-red-200 cursor-pointer',
};

const confidenceLabels: Record<ConfidenceLevel, string> = {
  HIGH: 'Verified',
  MEDIUM: 'Mostly verified',
  LOW: 'Partially verified',
  UNVERIFIED: 'Unverified',
};

export function ConfidenceBadge({ confidence, verified_fraction: _, per_sentence }: ConfidenceBadgeProps) {
  const verifiedCount = per_sentence.filter((s) => s.verified).length;
  const totalCount = per_sentence.length;
  const tooltipText =
    totalCount > 0
      ? `${verifiedCount} of ${totalCount} sentences verified against sources`
      : 'Answer verification confidence';

  const badge = (
    <Badge variant="outline" className={cn('shrink-0 text-xs', confidenceStyles[confidence])}>
      {confidenceLabels[confidence]}
    </Badge>
  );

  if (totalCount === 0) {
    // No per-sentence data — just show tooltip, no dialog
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{badge}</TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs text-xs">
            {tooltipText}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  const unverifiedSentences = per_sentence.filter((s) => !s.verified);

  return (
    <Dialog>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DialogTrigger asChild>{badge}</DialogTrigger>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs text-xs">
            {tooltipText} — click for details
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Answer Verification Details</DialogTitle>
          <DialogDescription>
            Sentence-level verification is checked against retrieved source text.
          </DialogDescription>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          {verifiedCount} of {totalCount} sentences verified against source documents.
        </p>
        {unverifiedSentences.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              Unverified sentences
            </p>
            <ul className="space-y-1 max-h-64 overflow-y-auto">
              {unverifiedSentences.map((s, i) => (
                <li
                  key={i}
                  className="text-sm px-3 py-2 rounded-md bg-red-50 border border-red-100 text-red-900"
                >
                  {s.text}
                </li>
              ))}
            </ul>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
