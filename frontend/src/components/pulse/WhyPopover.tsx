import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { explainPulseCard } from '@/lib/api';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

export interface WhyPopoverProps {
  cardId: number;
  trigger: React.ReactNode;
}

/**
 * Minimal "why this paper?" popover.
 *
 * On open, fetches the per-card reasoning + signal breakdown via TanStack
 * Query (lazy — the query is disabled until `isOpen` flips). Renders the LLM
 * reasoning sentence, a lightweight div-based signal bar chart, and the raw
 * `llm_relevance` / `llm_novelty` scores.
 *
 * Uses a hand-rolled click-outside popover instead of Radix because no
 * `@radix-ui/react-popover` dependency is installed at the time of writing.
 */
export function WhyPopover({ cardId, trigger }: WhyPopoverProps) {
  const [isOpen, setIsOpen] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['pulse-explain', cardId],
    queryFn: () => explainPulseCard(cardId),
    enabled: isOpen,
    staleTime: 60_000,
  });

  // Click-outside to close.
  React.useEffect(() => {
    if (!isOpen) return;
    const handle = (event: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [isOpen]);

  const signalEntries = data ? Object.entries(data.signals ?? {}) : [];

  return (
    <div ref={containerRef} className="relative inline-block">
      <div
        onClick={(e) => {
          e.stopPropagation();
          setIsOpen((prev) => !prev);
        }}
      >
        {trigger}
      </div>
      {isOpen && (
        <div
          role="dialog"
          aria-label="Why this paper?"
          className="absolute right-0 z-50 mt-2 w-80 rounded-md border bg-popover p-4 text-popover-foreground shadow-md"
          onClick={(e) => e.stopPropagation()}
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
        </div>
      )}
    </div>
  );
}
