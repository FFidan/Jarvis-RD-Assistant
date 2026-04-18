import { useState, useRef, useEffect, type FormEvent } from 'react';
import { useStreamingChat } from '@/hooks/use-streaming-chat';
import { ChatMessage } from '@/components/chat/ChatMessage';
import { SourcesAccordion } from '@/components/chat/SourcesAccordion';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Loader2, Send, Square, Trash2 } from 'lucide-react';

interface StreamingChatProps {
  chatId: string;
  scope: 'single-paper' | 'cross-paper';
  paperId?: number;
}

export function StreamingChat({ chatId, scope, paperId }: StreamingChatProps) {
  const { messages, sources, isStreaming, phase, sendMessage, stopStreaming, clearChat } =
    useStreamingChat({ chatId, scope, paperId });
  const [input, setInput] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll on new content
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || isStreaming) return;
    setInput('');
    sendMessage(trimmed);
  }

  return (
    <div className="flex h-full flex-col">
      {/* Messages */}
      <ScrollArea className="flex-1 p-4" ref={scrollRef}>
        <div className="space-y-4">
          {messages.length === 0 && (
            <p className="text-center text-sm text-muted-foreground">
              Ask a question to get started
            </p>
          )}
          {messages.map((msg, i) => (
            <div key={`${msg.role}:${msg.content.slice(0, 8)}:${i}`}>
              <ChatMessage
                message={msg}
                isLoading={isStreaming && i === messages.length - 1 && msg.role === 'assistant'}
              />
              {msg.role === 'assistant' && msg.sources && msg.sources.length > 0 && (
                <SourcesAccordion sources={msg.sources} />
              )}
            </div>
          ))}
          {phase === 'searching' && (
            <div className="flex items-center gap-2 px-4 py-3 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>Searching paper chunks and thinking...</span>
            </div>
          )}
          {isStreaming && sources.length > 0 && (
            <SourcesAccordion sources={sources} />
          )}
        </div>
      </ScrollArea>

      {/* Input */}
      <div className="border-t p-4">
        <form onSubmit={handleSubmit} className="flex gap-2">
          <Textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask a question..."
            className="min-h-[40px] max-h-[120px] resize-none"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSubmit(e);
              }
            }}
          />
          <div className="flex flex-col gap-1">
            {isStreaming ? (
              <Button type="button" variant="destructive" size="icon" onClick={stopStreaming}>
                <Square className="h-4 w-4" />
              </Button>
            ) : (
              <Button type="submit" size="icon" disabled={!input.trim()}>
                <Send className="h-4 w-4" />
              </Button>
            )}
            <Button type="button" variant="ghost" size="icon" onClick={clearChat}>
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
