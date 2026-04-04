import { useCallback, useRef, useState } from 'react';
import { streamSSE } from '@/lib/sse';
import { useChatStore } from '@/stores/chat-store';
import type { Source } from '@/types';

interface UseStreamingChatOptions {
  chatId: string;
  /** 'single-paper' uses /api/papers/{paperId}/ask/stream, 'cross-paper' uses /api/ask/stream */
  scope: 'single-paper' | 'cross-paper';
  paperId?: number;
}

export function useStreamingChat({ chatId, scope, paperId }: UseStreamingChatOptions) {
  const { chats, addMessage, appendToLastMessage, setLastMessageSources, clearChat } =
    useChatStore();
  const [phase, setPhase] = useState<'idle' | 'searching' | 'streaming'>('idle');
  const [sources, setSources] = useState<Source[]>([]);
  const abortControllerRef = useRef<AbortController | null>(null);

  const isStreaming = phase !== 'idle';

  const messages = chats[chatId] || [];

  const sendMessage = useCallback(
    async (question: string) => {
      if (isStreaming) return;

      // Add user message
      addMessage(chatId, { role: 'user', content: question });
      // Add empty assistant message to stream into
      addMessage(chatId, { role: 'assistant', content: '' });

      const controller = new AbortController();
      abortControllerRef.current = controller;
      setPhase('searching');
      setSources([]);

      const url =
        scope === 'single-paper'
          ? `/api/papers/${paperId}/ask/stream`
          : '/api/ask/stream';

      const body =
        scope === 'single-paper'
          ? { question }
          : { question, decompose: true };

      try {
        for await (const event of streamSSE(url, body, controller.signal)) {
          if (event.type === 'token' && event.content) {
            setPhase('streaming');
            appendToLastMessage(chatId, event.content);
          } else if (event.type === 'sources' && event.sources) {
            const mapped: Source[] = event.sources.map((s) => ({
              ...s,
              text: s.text || s.content,
            }));
            setSources(mapped);
            setLastMessageSources(chatId, mapped);
          } else if (event.type === 'error') {
            appendToLastMessage(chatId, `\n\n**Error:** ${event.message || 'Unknown error'}`);
          }
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          appendToLastMessage(chatId, `\n\n**Error:** ${(err as Error).message}`);
        }
      } finally {
        setPhase('idle');
        abortControllerRef.current = null;
      }
    },
    [chatId, scope, paperId, phase, addMessage, appendToLastMessage, setLastMessageSources],
  );

  const stopStreaming = useCallback(() => {
    abortControllerRef.current?.abort();
  }, []);

  return {
    messages,
    sources,
    isStreaming,
    phase,
    sendMessage,
    stopStreaming,
    clearChat: () => clearChat(chatId),
  };
}
