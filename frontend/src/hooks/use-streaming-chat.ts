import { useCallback, useEffect, useRef, useState } from 'react';
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
  const {
    chats,
    addMessage,
    appendToLastMessage,
    setLastMessageSources,
    setLastMessageConfidence,
    removeLastMessageIfEmpty,
    clearChat,
  } = useChatStore();
  const [phase, setPhase] = useState<'idle' | 'searching' | 'streaming'>('idle');
  const [sources, setSources] = useState<Source[]>([]);
  const abortControllerRef = useRef<AbortController | null>(null);

  // Keep a ref to phase so sendMessage doesn't need phase in its deps array,
  // preventing unnecessary recreation on every phase change.
  const phaseRef = useRef(phase);
  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  // D.3 — abort SSE on unmount to prevent memory leaks / dangling streams
  useEffect(() => () => abortControllerRef.current?.abort(), []);

  const isStreaming = phase !== 'idle';

  const messages = chats[chatId] || [];

  const sendMessage = useCallback(
    async (question: string) => {
      if (phaseRef.current !== 'idle') return;

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
          } else if (event.type === 'confidence' && event.confidence) {
            setLastMessageConfidence(chatId, {
              confidence: event.confidence,
              verified_fraction: event.verified_fraction ?? 0,
              per_sentence: event.per_sentence ?? [],
            });
          } else if (event.type === 'error') {
            appendToLastMessage(chatId, `\n\n**Error:** ${event.message || 'Unknown error'}`);
          }
        }
      } catch (err) {
        const isAbort = (err as Error).name === 'AbortError';
        if (!isAbort) {
          appendToLastMessage(chatId, `\n\n**Error:** ${(err as Error).message}`);
        }
        // D.2 — if stopped before any token arrived, discard the empty placeholder
        if (isAbort || phaseRef.current === 'searching') {
          removeLastMessageIfEmpty(chatId);
        }
      } finally {
        setPhase('idle');
        abortControllerRef.current = null;
      }
    },
    [chatId, scope, paperId, addMessage, appendToLastMessage, setLastMessageSources, setLastMessageConfidence, removeLastMessageIfEmpty],
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
