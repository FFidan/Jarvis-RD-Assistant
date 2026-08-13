import { useCallback, useEffect, useRef, useState } from 'react';
import { getStreamErrorCopy, streamSSE, type StreamError } from '@/lib/sse';
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
  const [streamError, setStreamError] = useState<StreamError | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  // D.3 — AbortController is now stored in the module-level activeStreams map
  // (keyed by chatId) so streams survive component unmount during navigation.
  // The ref here is only used by stopStreaming() to abort imperatively.
  const abortControllerRef = useRef<AbortController | null>(null);

  const lastQuestionRef = useRef<string | null>(null);

  // Keep a ref to phase so sendMessage doesn't need phase in its deps array,
  // preventing unnecessary recreation on every phase change.
  const phaseRef = useRef<Phase>(phase);
  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  // Elapsed-seconds counter: ticks every second while a request is in flight.
  // Keyed on isIdle so the interval starts once on transition to active and
  // is cleared (with a reset) on transition back to idle.
  const isIdle = phase === 'idle';
  useEffect(() => {
    if (isIdle) {
      setElapsedSeconds(0);
      return;
    }
    setElapsedSeconds(0);
    const id = setInterval(() => {
      setElapsedSeconds((s) => s + 1);
    }, 1000);
    return () => { clearInterval(id); };
  }, [isIdle]);

  // Reactive: true whenever ANY hook instance has registered a stream for this
  // chatId, even if this particular instance just remounted (navigation back).
  const isExternallyStreaming = useStreamRegistry((s) => s.activeStreamingChats.has(chatId));
  const isStreaming = phase !== 'idle' || isExternallyStreaming;

  const messages = chats[chatId] || [];

  const sendMessage = useCallback(
    async (question: string) => {
      if (phaseRef.current !== 'idle') return;

      lastQuestionRef.current = question;

      // Capture prior turns BEFORE adding the new user message and the assistant
      // placeholder — both addMessage calls below would otherwise appear in the
      // history we send to the backend.
      const prior = (useChatStore.getState().chats[chatId] || [])
        // Streamed "**Error:**" suffixes are UI-only — never model context.
        .map((m) => ({ role: m.role, content: (m.content.split('\n\n**Error:**')[0] ?? '').trim() }))
        .filter((m) => m.content.length > 0)
        .slice(-6)
        .map((m) => ({
          role: m.role,
          // slice() can split an astral pair; drop a trailing lone surrogate.
          content: m.content.slice(0, 4000).replace(/[\uD800-\uDBFF]$/, ''),
        }));

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
      setStreamError(null);

      const url =
        scope === 'single-paper'
          ? `/api/papers/${paperId}/ask/stream`
          : '/api/ask/stream';

      const body =
        scope === 'single-paper'
          ? { question, history: prior }
          : { question, decompose: true, history: prior };

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
            const error: StreamError = {
              message: event.message || 'Unknown streaming error',
              ...(event.code ? { code: event.code } : {}),
            };
            setStreamError(error);
            appendToLastMessage(chatId, `\n\n**Error:** ${escapeMarkdownInline(getStreamErrorCopy(error))}`);
          }
        }
      } catch (err) {
        const isAbort = (err != null && typeof err === 'object' && 'name' in err)
          ? (err as { name: unknown }).name === 'AbortError'
          : false;
        if (!isAbort) {
          const msg = err instanceof Error ? err.message : String(err);
          const error: StreamError = { message: msg, code: 'stream_transport_error' };
          setStreamError(error);
          appendToLastMessage(chatId, `\n\n**Error:** ${escapeMarkdownInline(getStreamErrorCopy(error))}`);
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

  const retry = useCallback(() => {
    const last = lastQuestionRef.current;
    if (!last) return;
    setStreamError(null);
    void sendMessage(last);
  }, [sendMessage]);

  return {
    messages,
    sources,
    isStreaming,
    phase,
    elapsedSeconds,
    sendMessage,
    stopStreaming,
    retry,
    clearChat: () => { setStreamError(null); clearChat(chatId); },
    modelUsed,
    streamError,
  };
}
