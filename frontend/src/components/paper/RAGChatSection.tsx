import { useState } from 'react';
import { StreamingChat } from '@/components/chat/StreamingChat';
import { Separator } from '@/components/ui/separator';
import { Label } from '@/components/ui/label';

interface RAGChatSectionProps {
  paperId: number;
}

export function RAGChatSection({ paperId }: RAGChatSectionProps) {
  const [scope, setScope] = useState<'single-paper' | 'cross-paper'>('single-paper');

  const chatId =
    scope === 'single-paper'
      ? `paper-${paperId}`
      : `paper-${paperId}-cross`;

  return (
    <div className="space-y-4">
      <Separator />
      <h3 className="text-lg font-semibold">Ask about this paper</h3>

      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-3">
          <Label className="text-sm font-medium">Scope:</Label>
          <div className="flex rounded-md border">
            <button
              type="button"
              className={`px-3 py-1.5 text-sm transition-colors ${
                scope === 'single-paper'
                  ? 'bg-primary text-primary-foreground'
                  : 'hover:bg-muted'
              }`}
              onClick={() => setScope('single-paper')}
            >
              This paper
            </button>
            <button
              type="button"
              className={`px-3 py-1.5 text-sm transition-colors ${
                scope === 'cross-paper'
                  ? 'bg-primary text-primary-foreground'
                  : 'hover:bg-muted'
              }`}
              onClick={() => setScope('cross-paper')}
            >
              All papers
            </button>
          </div>
        </div>
      </div>

      <div className="h-[400px] rounded-md border">
        <StreamingChat
          chatId={chatId}
          scope={scope}
          paperId={paperId}
        />
      </div>
    </div>
  );
}
