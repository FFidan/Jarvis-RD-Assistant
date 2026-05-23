import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { explainPulseCard } from '@/lib/api';
import { Skeleton } from '@/components/ui/skeleton';
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover';
import { cn } from '@/lib/utils';

export interface WhyPopoverProps {
  cardId: number;
  trigger: React.ReactNode;
}

/**
 * Minimal "why this paper?" popover.
 *
 * On open, fetches the per-card reasoning + signal breakdown via TanStack
 * Query (lazy — the query is disabled until `open` flips). Renders the LLM
 * reasoning sentence, a lightweight div-based signal bar chart, and the raw
 * `llm_relevance` / `llm_novelty` scores.
 *
 * Uses @radix-ui/react-popover (via the ui/popover Shadcn wrapper) for
 * focus-trap, ARIA correctness, Escape-to-close, and click-outside handling.
 */
export function WhyPopover({ cardId, trigger }: WhyPopoverProps) {
  const [open, setOpen] = React.useState(false);

  const { data, isLoading } = useQuery({
    queryKey: QUERY_KEYS.pulse.explain(cardId),
    queryFn: () => explainPulseCard(cardId),
    enabled: open,
    staleTime: 60_000,
  });

  const signalEntries = data ? Object.entries(data.signals ?? {}) : [];

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <div onClick={(e) => e.stopPropagation()}>{trigger}</div>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-80"
        aria-label="Why this paper?"
      >
        <h4 className="mb-2 text-sm font-semibold">Why this paper?</h4>
        {isLoading || !data ? (
          <div data-testid="why-popover-skeleton" className="space-y-2">
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-5/6" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        ) : (
          <div className="space-y-3">
            {data.reasoning && (
              <p className="text-xs italic text-muted-foreground">
                {data.reasoning}
              </p>
            )}
            {signalEntries.length > 0 && (
              <div className="space-y-1.5">
                {signalEntries.map(([name, value]) => {
                  const pct = Math.max(0, Math.min(1, value)) * 100;
                  return (
                    <div
                      key={name}
                      data-testid={`why-signal-${name}`}
                      className="space-y-0.5"
                    >
                      <div className="flex justify-between text-[10px] text-muted-foreground">
                        <span>{name}</span>
                        <span>{value.toFixed(2)}</span>
                      </div>
                      <div className="h-1.5 w-full rounded bg-muted">
                        <div
                          className={cn('h-1.5 rounded bg-primary')}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            <div className="flex gap-4 border-t pt-2 text-[11px] text-muted-foreground">
              <span>
                Relevance:{' '}
                <strong className="text-foreground">
                  {data.llm_relevance ?? '—'}
                </strong>
              </span>
              <span>
                Novelty:{' '}
                <strong className="text-foreground">
                  {data.llm_novelty ?? '—'}
                </strong>
              </span>
            </div>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
