/**
 * DiagnosticsPanel — collapsible panel showing Pulse run diagnostics (lazy-fetched).
 * Extracted from PulseSection.tsx.
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchPulseDebug } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react';
import type { PulseDebugInfo } from '@/types';

export function DiagnosticsPanel() {
  const [open, setOpen] = useState(false);
  const { data, isLoading, isError, refetch } = useQuery<PulseDebugInfo>({
    queryKey: ['pulse-debug'],
    queryFn: fetchPulseDebug,
    enabled: open,
    staleTime: 30_000,
  });

  return (
    <div className="rounded-md border">
      <button
        type="button"
        className="flex w-full items-center gap-2 p-3 text-sm font-medium hover:bg-muted/30 transition-colors"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        Diagnostics
      </button>

      {open && (
        <div className="border-t p-3 space-y-4">
          {isLoading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading diagnostics…
            </div>
          )}
          {isError && (
            <div className="space-y-2">
              <p className="text-sm text-destructive">
                Failed to load diagnostics (no deck generated yet?).
              </p>
              <Button variant="outline" size="sm" onClick={() => void refetch()}>
                Retry
              </Button>
            </div>
          )}
          {data && (
            <>
              <div>
                <div className="text-xs font-semibold text-muted-foreground mb-1">
                  Deck: {data.deck_date} — {data.card_count} cards
                  {data.degraded_reason && (
                    <Badge
                      variant="outline"
                      className="ml-2 text-[var(--status-warn)] border-[var(--status-warn)]"
                    >
                      {data.degraded_reason}
                    </Badge>
                  )}
                </div>
              </div>

              {/* Per-source counts */}
              {Object.keys(data.source_counts).length > 0 && (
                <div>
                  <p className="text-xs font-semibold mb-1">Source candidate counts</p>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
                    {Object.entries(data.source_counts).map(([src, count]) => (
                      <div key={src} className="flex justify-between">
                        <span className="text-muted-foreground">{src}</span>
                        <span className="font-mono">{count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {data.source_diagnostics && Object.keys(data.source_diagnostics).length > 0 && (
                <div>
                  <p className="text-xs font-semibold mb-1">Source diagnostics</p>
                  <div className="max-h-64 space-y-1 overflow-y-auto pr-1 text-xs">
                    {Object.entries(data.source_diagnostics).map(([src, diagnostic]) => (
                      <div key={src} className="rounded border bg-muted/20 p-2">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="min-w-0 truncate font-medium">{src}</span>
                          <Badge
                            variant="outline"
                            className={
                              diagnostic.status === 'ok'
                                ? 'border-[var(--status-ok)] text-[var(--status-ok)]'
                                : diagnostic.status === 'rate_limit'
                                  ? 'border-[var(--status-warn)] text-[var(--status-warn)]'
                                  : 'border-muted-foreground text-muted-foreground'
                            }
                          >
                            {diagnostic.status}
                          </Badge>
                        </div>
                        <p className="mt-1 break-words text-muted-foreground">
                          {diagnostic.message}
                        </p>
                        {(diagnostic.retry_after_s || diagnostic.status_code) && (
                          <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                            {diagnostic.status_code ? `HTTP ${diagnostic.status_code}` : ''}
                            {diagnostic.status_code && diagnostic.retry_after_s ? ' · ' : ''}
                            {diagnostic.retry_after_s
                              ? `retry after ${diagnostic.retry_after_s}s`
                              : ''}
                          </p>
                        )}
                        {diagnostic.settings_hint && (
                          <p className="mt-1 break-words text-[var(--status-warn)]">
                            {diagnostic.settings_hint}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Topic embedding health */}
              {data.topic_embeddings.length > 0 && (
                <div>
                  <p className="text-xs font-semibold mb-1">Topic embedding health</p>
                  <div className="space-y-0.5 text-xs">
                    {data.topic_embeddings.map((te) => (
                      <div key={te.key} className="flex items-center gap-2">
                        <span
                          className={`inline-block h-2 w-2 rounded-full ${te.ok ? 'bg-green-500' : 'bg-red-500'}`}
                        />
                        <span className="font-mono text-muted-foreground truncate max-w-[200px]">
                          {te.key}
                        </span>
                        <span>
                          {te.ok ? `dim=${te.dim}` : te.non_null ? 'wrong dim' : 'null'}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Top-N signals table */}
              {data.top_cards.length > 0 && (
                <div>
                  <p className="text-xs font-semibold mb-1">Top cards (rank order)</p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs border-collapse">
                      <thead>
                        <tr className="text-muted-foreground text-left">
                          <th className="pr-2 pb-1 font-medium">Title</th>
                          <th className="pr-2 pb-1 font-mono font-medium">Score</th>
                          <th className="pr-2 pb-1 font-mono font-medium">Rel</th>
                          <th className="pb-1 font-mono font-medium">Nov</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.top_cards.map((card) => (
                          <tr key={card.card_id} className="border-t border-muted">
                            <td className="pr-2 py-0.5 max-w-[200px] truncate">{card.title}</td>
                            <td className="pr-2 py-0.5 font-mono">
                              {card.final_score.toFixed(3)}
                            </td>
                            <td className="pr-2 py-0.5 font-mono">
                              {card.llm_relevance !== null ? card.llm_relevance.toFixed(2) : '—'}
                            </td>
                            <td className="py-0.5 font-mono">
                              {card.llm_novelty !== null ? card.llm_novelty.toFixed(2) : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
