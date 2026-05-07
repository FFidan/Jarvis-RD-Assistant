import { create } from 'zustand';
import type { ChatMessage, Source } from '@/types';
import type { ConfidenceLevel } from '@/lib/sse';

// ---------------------------------------------------------------------------
// Module-level abort stream registry. Controllers live in a plain Map
// (imperative resource); a parallel Zustand store of chatIds backs the
// reactive `useStreamRegistry` selector so React components can detect
// in-flight streams across hook re-mounts (e.g., navigating away and back
// during an Ask response).
// ---------------------------------------------------------------------------
const activeStreams = new Map<string, AbortController>();

interface StreamRegistryState {
  activeStreamingChats: Set<string>;
  _mark: (chatId: string) => void;
  _unmark: (chatId: string) => void;
}

export const useStreamRegistry = create<StreamRegistryState>()((set) => ({
  activeStreamingChats: new Set<string>(),
  _mark: (chatId) =>
    set((state) => {
      if (state.activeStreamingChats.has(chatId)) return state;
      const next = new Set(state.activeStreamingChats);
      next.add(chatId);
      return { activeStreamingChats: next };
    }),
  _unmark: (chatId) =>
    set((state) => {
      if (!state.activeStreamingChats.has(chatId)) return state;
      const next = new Set(state.activeStreamingChats);
      next.delete(chatId);
      return { activeStreamingChats: next };
    }),
}));

export function registerStream(chatId: string, controller: AbortController): void {
  // Abort any existing stream for this chat before registering the new one
  activeStreams.get(chatId)?.abort();
  activeStreams.set(chatId, controller);
  useStreamRegistry.getState()._mark(chatId);
}

/**
 * Remove a stream from the registry. If `controller` is supplied, only deletes
 * when the registered entry matches — this prevents a slow finally-block from
 * a now-superseded controller from nuking the replacement controller's entry.
 */
export function unregisterStream(chatId: string, controller?: AbortController): void {
  if (controller && activeStreams.get(chatId) !== controller) {
    return;
  }
  activeStreams.delete(chatId);
  useStreamRegistry.getState()._unmark(chatId);
}

/** Abort the active stream for this chat, if any. Safe across remounts. */
export function abortStream(chatId: string): void {
  activeStreams.get(chatId)?.abort();
}

export function abortAllStreams(): void {
  for (const controller of activeStreams.values()) {
    controller.abort();
  }
  activeStreams.clear();
  useStreamRegistry.setState({ activeStreamingChats: new Set<string>() });
}

export interface ConfidencePayload {
  confidence: ConfidenceLevel;
  verified_fraction: number;
  per_sentence: { text: string; verified: boolean }[];
}

interface ChatState {
  chats: Record<string, ChatMessage[]>;
  addMessage: (chatId: string, message: ChatMessage) => void;
  appendToLastMessage: (chatId: string, token: string) => void;
  setLastMessageSources: (chatId: string, sources: Source[]) => void;
  setLastMessageConfidence: (chatId: string, payload: ConfidencePayload) => void;
  removeLastMessageIfEmpty: (chatId: string) => void;
  clearChat: (chatId: string) => void;
}

export const useChatStore = create<ChatState>()((set) => ({
  chats: {},

  addMessage(chatId: string, message: ChatMessage) {
    set((state) => ({
      chats: {
        ...state.chats,
        [chatId]: [...(state.chats[chatId] || []), message],
      },
    }));
  },

  appendToLastMessage(chatId: string, token: string) {
    set((state) => {
      const messages = state.chats[chatId];
      if (!messages || messages.length === 0) return state;
      const last = messages[messages.length - 1];
      if (!last) return state;
      const updated: ChatMessage = { ...last, content: last.content + token };
      return {
        chats: {
          ...state.chats,
          [chatId]: [...messages.slice(0, -1), updated],
        },
      };
    });
  },

  setLastMessageSources(chatId: string, sources: Source[]) {
    set((state) => {
      const messages = state.chats[chatId];
      if (!messages || messages.length === 0) return state;
      const last = messages[messages.length - 1];
      if (!last) return state;
      const updated: ChatMessage = { ...last, sources };
      return {
        chats: {
          ...state.chats,
          [chatId]: [...messages.slice(0, -1), updated],
        },
      };
    });
  },

  removeLastMessageIfEmpty(chatId: string) {
    set((state) => {
      const messages = state.chats[chatId];
      if (!messages || messages.length === 0) return state;
      const last = messages[messages.length - 1];
      if (!last) return state;
      if (last.content !== '') return state;
      return {
        chats: {
          ...state.chats,
          [chatId]: messages.slice(0, -1),
        },
      };
    });
  },

  setLastMessageConfidence(chatId: string, payload: ConfidencePayload) {
    set((state) => {
      const messages = state.chats[chatId];
      if (!messages || messages.length === 0) return state;
      // Find the last assistant message
      const assistantItems = [...messages].map((m, i) => ({ m, i }))
        .filter(({ m }) => m.role === 'assistant');
      const lastAssistantEntry = assistantItems[assistantItems.length - 1];
      if (!lastAssistantEntry) return state;
      const lastAssistantIdx = lastAssistantEntry.i;
      const updated = [...messages];
      const target = updated[lastAssistantIdx];
      if (!target) return state;
      updated[lastAssistantIdx] = {
        role: target.role,
        content: target.content,
        sources: target.sources,
        confidence: payload.confidence,
        verified_fraction: payload.verified_fraction,
        per_sentence: payload.per_sentence,
      };
      return { chats: { ...state.chats, [chatId]: updated } };
    });
  },

  clearChat(chatId: string) {
    set((state) => {
      const { [chatId]: _, ...rest } = state.chats;
      return { chats: rest };
    });
  },
}));
