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

import { MessageCircleQuestion } from 'lucide-react';
import { StreamingChat } from '@/components/chat/StreamingChat';

/** Stable chatId for the cross-paper Ask workspace (session-wide). */
const ASK_CHAT_ID = 'global-ask';

export function AskPage() {
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
        <StreamingChat
          chatId={ASK_CHAT_ID}
          scope="cross-paper"
        />
      </div>
    </div>
  );
}
