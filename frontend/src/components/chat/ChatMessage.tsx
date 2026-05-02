import type { ChatMessage as ChatMessageType } from '@/types';
import { cn } from '@/lib/utils';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { ConfidenceBadge } from '@/components/chat/ConfidenceBadge';
import { AlertTriangle, Loader2 } from 'lucide-react';

interface ChatMessageProps {
  message: ChatMessageType;
  isLoading?: boolean;
  /** When isLoading is true, phase drives the spinner caption */
  phase?: 'idle' | 'searching' | 'streaming';
}

export function ChatMessage({ message, isLoading, phase }: ChatMessageProps) {
  const isUser = message.role === 'user';

  return (
    <div className={cn('flex', isUser ? 'justify-end' : 'justify-start')}>
      <div
        className={cn(
          'max-w-[80%] rounded-lg px-4 py-2',
          isUser
            ? 'bg-primary text-primary-foreground'
            : 'bg-muted text-foreground',
        )}
      >
        {isUser ? (
          <p className="text-sm whitespace-pre-wrap">{message.content}</p>
        ) : message.content ? (
          <>
            {message.confidence && message.confidence !== 'HIGH' && (
              <div className="mb-2 flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  Some answer sentences were not verified against retrieved sources. Check the
                  verification details before relying on them.
                </span>
              </div>
            )}
            <MarkdownContent
              unverifiedSentences={(message.per_sentence ?? [])
                .filter((s) => !s.verified)
                .map((s) => s.text)}
            >
              {message.content}
            </MarkdownContent>
            {message.confidence && (
              <div className="mt-2">
                <ConfidenceBadge
                  confidence={message.confidence}
                  verified_fraction={message.verified_fraction ?? 0}
                  per_sentence={message.per_sentence ?? []}
                />
              </div>
            )}
          </>
        ) : isLoading ? (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span className="text-sm">
              {phase === 'searching' ? 'Searching paper chunks…' : 'Generating response…'}
            </span>
          </div>
        ) : null}
      </div>
    </div>
  );
}
