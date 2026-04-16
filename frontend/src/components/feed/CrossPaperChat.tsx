import { useState } from 'react';
import { StreamingChat } from '@/components/chat/StreamingChat';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { MessageSquare, ChevronDown, ChevronUp } from 'lucide-react';

export function CrossPaperChat() {
  const [expanded, setExpanded] = useState(false);

  return (
    <Card>
      <Button
        variant="ghost"
        className="flex w-full items-center justify-between px-4 py-3"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="flex items-center gap-2 text-sm font-medium">
          <MessageSquare className="h-4 w-4" />
          Ask across all papers
        </span>
        {expanded ? (
          <ChevronUp className="h-4 w-4" />
        ) : (
          <ChevronDown className="h-4 w-4" />
        )}
      </Button>
      {!expanded && (
        <p className="text-xs text-muted-foreground mt-1 px-4 pb-3">Get answers synthesised from your entire library</p>
      )}
      {expanded && (
        <CardContent className="h-[400px] border-t p-0">
          <StreamingChat chatId="cross-paper-rag" scope="cross-paper" />
        </CardContent>
      )}
    </Card>
  );
}
