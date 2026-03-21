import { create } from 'zustand';
import type { ChatMessage, Source } from '@/types';

interface ChatState {
  chats: Record<string, ChatMessage[]>;
  addMessage: (chatId: string, message: ChatMessage) => void;
  appendToLastMessage: (chatId: string, token: string) => void;
  setLastMessageSources: (chatId: string, sources: Source[]) => void;
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

  clearChat(chatId: string) {
    set((state) => {
      const { [chatId]: _, ...rest } = state.chats;
      return { chats: rest };
    });
  },
}));
