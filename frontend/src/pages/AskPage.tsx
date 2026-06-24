/**
 * /ask — Cross-paper reasoning workspace (nav group Ⅳ, Shell/Sidebar spec §3.4).
 *
 * Ask is its own dedicated surface reached via the "Ⅳ Ask" sidebar group.
 * It is NOT folded into the Research Feed tab set.
 *
 * Reuses the existing global cross-paper RAG capability:
 *   - scope='cross-paper' → POST /api/ask/stream
 *   - StreamingChat component (shared with PaperDetail single/cross-paper mode)
 *   - useStreamingChat hook (chat-store backed, survives navigation)
 */

import { MessageCircleQuestion, Sparkles } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { StreamingChat } from '@/components/chat/StreamingChat';
import { Button } from '@/components/ui/button';
import { fetchDashboardMetrics } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';

/** Stable chatId for the cross-paper Ask workspace (session-wide). */
const ASK_CHAT_ID = 'global-ask';

export function AskPage() {
  const { data: metrics, isSuccess } = useQuery({
    queryKey: QUERY_KEYS.dashboard.metrics(),
    queryFn: fetchDashboardMetrics,
  });
  const hasAnalyzedPapers = (metrics?.chunked_papers ?? 0) > 0;
  // Only claim "nothing to ask" once metrics confirm an empty library. While the
  // request is loading or has failed, fall back to the chat workspace rather than
  // a misleading empty-state (failed request -> degraded, never empty).
  const showOnboarding = isSuccess && !hasAnalyzedPapers;

  return (
    <div className="flex flex-col h-full" data-testid="ask-page">
      {/* Page header */}
      <div className="px-6 py-5 border-b border-hair flex-none">
        <div className="flex items-center gap-3">
          <MessageCircleQuestion className="h-5 w-5 text-muted-foreground shrink-0" aria-hidden="true" />
          <div>
            <h1 className="text-xl font-semibold tracking-tight leading-none">Ask</h1>
            <p className="text-xs text-muted-foreground mt-1 italic">
              Cross-paper reasoning and workspace — synthesised from your entire library.
            </p>
          </div>
        </div>
      </div>

      {/* Chat workspace */}
      <div className="flex-1 min-h-0">
        {showOnboarding ? (
          <div
            className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center"
            data-testid="ask-empty-state"
          >
            <Sparkles className="h-8 w-8 text-muted-foreground" aria-hidden="true" />
            <div className="max-w-md space-y-2">
              <h2 className="text-lg font-semibold tracking-tight">Nothing to ask yet</h2>
              <p className="text-sm text-muted-foreground">
                Ask reasons across every analyzed paper in your library to answer questions
                with cited evidence. Import and analyze at least one paper to get started.
              </p>
            </div>
            <Button asChild>
              <Link to="/feed?surface=search">Find papers to analyze</Link>
            </Button>
          </div>
        ) : (
          <StreamingChat
            chatId={ASK_CHAT_ID}
            scope="cross-paper"
            hasAnalyzedPapers={hasAnalyzedPapers}
          />
        )}
      </div>
    </div>
  );
}
