import { useState, useRef, useEffect, type FormEvent } from 'react';
import { useStreamingChat } from '@/hooks/use-streaming-chat';
import { ChatMessage } from '@/components/chat/ChatMessage';
import { SourcesAccordion } from '@/components/chat/SourcesAccordion';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { RotateCcw, Send, Square, Trash2 } from 'lucide-react';

interface StreamingChatProps {
  chatId: string;
  scope: 'single-paper' | 'cross-paper';
  paperId?: number;
  /**
   * Whether the library contains at least one analyzed (chunked) paper.
   * When false the input area is wrapped in a tooltip explaining the prerequisite.
   * Defaults to true so existing callers without the prop are unaffected.
   */
  hasAnalyzedPapers?: boolean;
}

export function StreamingChat({ chatId, scope, paperId, hasAnalyzedPapers = true }: StreamingChatProps) {
  const { messages, sources, isStreaming, phase, sendMessage, stopStreaming, clearChat, retry, streamError, elapsedSeconds } =
    useStreamingChat({ chatId, scope, paperId });
  // First question of this chat: the warm-up hint only applies before any prior answer.
  const isFirstQuestion = messages.filter((m) => m.role === 'user').length <= 1;
  const [input, setInput] = useState('');
  const [tipOpen, setTipOpen] = useState(false);
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
        {/* D.4 — Clear chat button at top-right of message area */}
        <div className="flex justify-end mb-2">
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={messages.length === 0}
                aria-label="Clear chat"
                className="gap-1.5 text-muted-foreground"
              >
                <Trash2 className="h-4 w-4" />
                Clear chat
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Clear all messages?</AlertDialogTitle>
                <AlertDialogDescription>
                  This will delete the entire chat history. This cannot be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={clearChat} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
                  Confirm
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>

        <div className="space-y-4">
          {messages.length === 0 && (
            <p className="text-center text-sm text-muted-foreground">
              Ask a question to get started
            </p>
          )}
          {messages.map((msg, i) => (
            <div key={msg.id}>
              {/* D.1 — pass phase so in-bubble spinner reflects search vs stream */}
              <ChatMessage
                message={msg}
                isLoading={isStreaming && i === messages.length - 1 && msg.role === 'assistant'}
                phase={phase}
                elapsedSeconds={elapsedSeconds}
                isFirstQuestion={isFirstQuestion}
              />
              {msg.role === 'assistant' && msg.sources && msg.sources.length > 0 && (
                <SourcesAccordion sources={msg.sources} />
              )}
            </div>
          ))}
          {/* D.1 — bottom banner removed; phase is shown in-bubble instead */}
          {isStreaming && sources.length > 0 && (
            <SourcesAccordion sources={sources} />
          )}
        </div>
      </ScrollArea>

      {/* SSE stream error banner — friendly copy + Retry (degraded, never empty) */}
      {streamError && (
        <div
          role="alert"
          className="mx-4 mb-2 flex items-center justify-between gap-3 rounded bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          <span>Something went wrong answering that. Please try again.</span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={retry}
            disabled={isStreaming}
            className="gap-1.5"
            aria-label="Retry last question"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            Retry
          </Button>
        </div>
      )}

      {/* Input */}
      <div className="border-t p-4">
        <TooltipProvider>
          <Tooltip open={!hasAnalyzedPapers && tipOpen} onOpenChange={setTipOpen}>
            <TooltipTrigger asChild>
              {/* span wrapper needed so tooltip fires even when inner inputs are disabled */}
              <span className="block">
                <form onSubmit={handleSubmit} className="flex gap-2">
                  <Textarea
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    placeholder="Ask a question..."
                    className="min-h-[40px] max-h-[120px] resize-none"
                    disabled={!hasAnalyzedPapers}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        handleSubmit(e);
                      }
                    }}
                  />
                  <div className="flex flex-col gap-1">
                    {isStreaming ? (
                      <Button type="button" variant="destructive" size="icon" onClick={stopStreaming} aria-label="Stop streaming">
                        <Square className="h-4 w-4" />
                      </Button>
                    ) : (
                      <Button type="submit" size="icon" disabled={!input.trim() || !hasAnalyzedPapers} aria-label="Send message">
                        <Send className="h-4 w-4" />
                      </Button>
                    )}
                  </div>
                </form>
              </span>
            </TooltipTrigger>
            {!hasAnalyzedPapers && (
              <TooltipContent side="top" className="text-xs">
                Analyze at least one paper first
              </TooltipContent>
            )}
          </Tooltip>
        </TooltipProvider>
      </div>
    </div>
  );
}
