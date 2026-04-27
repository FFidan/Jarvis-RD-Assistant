import { create } from 'zustand';
import type { ChatMessage, Source } from '@/types';
import type { ConfidenceLevel } from '@/lib/sse';

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
      return {
        chats: {
          ...state.chats,
          [chatId]: [
            ...messages.slice(0, -1),
            { ...last, content: last.content + token },
          ],
        },
      };
    });
  },

  setLastMessageSources(chatId: string, sources: Source[]) {
    set((state) => {
      const messages = state.chats[chatId];
      if (!messages || messages.length === 0) return state;
      const last = messages[messages.length - 1];
      return {
        chats: {
          ...state.chats,
          [chatId]: [
            ...messages.slice(0, -1),
            { ...last, sources },
          ],
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
      const lastAssistantIdx = assistantItems.length > 0 ? assistantItems[assistantItems.length - 1].i : undefined;
      if (lastAssistantIdx === undefined) return state;
      const updated = [...messages];
      updated[lastAssistantIdx] = {
        ...updated[lastAssistantIdx],
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
