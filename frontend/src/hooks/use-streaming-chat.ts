import { useCallback, useEffect, useRef, useState } from 'react';
import { streamSSE } from '@/lib/sse';
import { escapeMarkdownInline } from '@/lib/markdown-escape';
import {
  useChatStore,
  useStreamRegistry,
  registerStream,
  unregisterStream,
  abortStream,
} from '@/stores/chat-store';
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
  type Phase = 'idle' | 'searching' | 'streaming';
  const [phase, setPhase] = useState<Phase>('idle');
  const [sources, setSources] = useState<Source[]>([]);
  const [modelUsed, setModelUsed] = useState<string | null>(null);
  // D.3 — AbortController is now stored in the module-level activeStreams map
  // (keyed by chatId) so streams survive component unmount during navigation.
  // The ref here is only used by stopStreaming() to abort imperatively.
  const abortControllerRef = useRef<AbortController | null>(null);

  // Keep a ref to phase so sendMessage doesn't need phase in its deps array,
  // preventing unnecessary recreation on every phase change.
  const phaseRef = useRef<Phase>(phase);
  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  // Reactive: true whenever ANY hook instance has registered a stream for this
  // chatId, even if this particular instance just remounted (navigation back).
  const isExternallyStreaming = useStreamRegistry((s) => s.activeStreamingChats.has(chatId));
  const isStreaming = phase !== 'idle' || isExternallyStreaming;

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
      // Register in module-level map so the stream survives navigation
      registerStream(chatId, controller);
      setPhase('searching');
      setSources([]);
      setModelUsed(null);

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
          } else if (event.type === 'done') {
            setModelUsed(event.model_used ?? null);
          } else if (event.type === 'error') {
            appendToLastMessage(chatId, `\n\n**Error:** ${escapeMarkdownInline(event.message || 'Unknown error')}`);
          }
        }
      } catch (err) {
        const isAbort = (err != null && typeof err === 'object' && 'name' in err)
          ? (err as { name: unknown }).name === 'AbortError'
          : false;
        if (!isAbort) {
          appendToLastMessage(chatId, `\n\n**Error:** ${escapeMarkdownInline(err instanceof Error ? err.message : String(err))}`);
        }
        // D.2 — if stopped before any token arrived, discard the empty placeholder
        // (cast: TS narrows phaseRef.current to its initial 'idle' literal from useRef<Phase>(phase) inference)
        if (isAbort || (phaseRef.current as Phase) === 'searching') {
          removeLastMessageIfEmpty(chatId);
        }
      } finally {
        setPhase('idle');
        abortControllerRef.current = null;
        // Identity check: only clear the registry entry if we still own it.
        // If a newer sendMessage call replaced our controller, leave the
        // newer registration intact.
        unregisterStream(chatId, controller);
      }
    },
    [chatId, scope, paperId, addMessage, appendToLastMessage, setLastMessageSources, setLastMessageConfidence, removeLastMessageIfEmpty],
  );

  const stopStreaming = useCallback(() => {
    // Local ref first (fast path for streams started by this hook instance).
    abortControllerRef.current?.abort();
    // Also abort by chatId so the Stop button works after navigation back —
    // this hook instance may not have started the stream that's still flowing.
    abortStream(chatId);
  }, [chatId]);

  return {
    messages,
    sources,
    isStreaming,
    phase,
    sendMessage,
    stopStreaming,
    clearChat: () => clearChat(chatId),
    modelUsed,
  };
}
