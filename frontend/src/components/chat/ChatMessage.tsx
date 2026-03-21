import type { ChatMessage as ChatMessageType } from '@/types';
import { cn } from '@/lib/utils';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { Loader2 } from 'lucide-react';

interface ChatMessageProps {
  message: ChatMessageType;
  isLoading?: boolean;
}

export function ChatMessage({ message, isLoading }: ChatMessageProps) {
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
          <MarkdownContent>{message.content}</MarkdownContent>
        ) : isLoading ? (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span className="text-sm">Generating response...</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}
