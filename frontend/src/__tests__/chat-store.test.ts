/**
 * Unit tests for chat-store actions — D.2 removeLastMessageIfEmpty + W4.3 stable message ids
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore } from '@/stores/chat-store';

const CHAT_ID = 'test-chat';

function resetStore() {
  useChatStore.setState({ chats: {} });
}

describe('chat-store — removeLastMessageIfEmpty (D.2)', () => {
  beforeEach(resetStore);

  it('no-op when chat has no messages', () => {
    useChatStore.getState().removeLastMessageIfEmpty(CHAT_ID);
    const messages = useChatStore.getState().chats[CHAT_ID];
    expect(messages).toBeUndefined();
  });

  it('no-op when last message has content', () => {
    useChatStore.getState().addMessage(CHAT_ID, { role: 'user', content: 'hello' });
    useChatStore.getState().addMessage(CHAT_ID, { role: 'assistant', content: 'world' });

    useChatStore.getState().removeLastMessageIfEmpty(CHAT_ID);

    const messages = useChatStore.getState().chats[CHAT_ID];
    expect(messages).toBeDefined();
    if (!messages) return;
    expect(messages).toHaveLength(2);
    expect(messages[1]?.content).toBe('world');
  });

  it('removes last message when it is empty string', () => {
    useChatStore.getState().addMessage(CHAT_ID, { role: 'user', content: 'a question' });
    useChatStore.getState().addMessage(CHAT_ID, { role: 'assistant', content: '' });

    useChatStore.getState().removeLastMessageIfEmpty(CHAT_ID);

    const messages = useChatStore.getState().chats[CHAT_ID];
    expect(messages).toBeDefined();
    if (!messages) return;
    expect(messages).toHaveLength(1);
    expect(messages[0]?.role).toBe('user');
  });

  it('removes only the last message, preserving prior content', () => {
    useChatStore.getState().addMessage(CHAT_ID, { role: 'user', content: 'first question' });
    useChatStore.getState().addMessage(CHAT_ID, { role: 'assistant', content: 'first answer' });
    useChatStore.getState().addMessage(CHAT_ID, { role: 'user', content: 'second question' });
    useChatStore.getState().addMessage(CHAT_ID, { role: 'assistant', content: '' });

    useChatStore.getState().removeLastMessageIfEmpty(CHAT_ID);

    const messages = useChatStore.getState().chats[CHAT_ID];
    expect(messages).toBeDefined();
    if (!messages) return;
    expect(messages).toHaveLength(3);
    expect(messages[2]?.content).toBe('second question');
  });

  it('no-op when last message has a single space (non-empty string)', () => {
    useChatStore.getState().addMessage(CHAT_ID, { role: 'assistant', content: ' ' });

    useChatStore.getState().removeLastMessageIfEmpty(CHAT_ID);

    const messages = useChatStore.getState().chats[CHAT_ID];
    expect(messages).toHaveLength(1);
  });

  it('does not affect other chats when removing from one chat', () => {
    const OTHER = 'other-chat';
    useChatStore.getState().addMessage(CHAT_ID, { role: 'assistant', content: '' });
    useChatStore.getState().addMessage(OTHER, { role: 'assistant', content: 'has content' });

    useChatStore.getState().removeLastMessageIfEmpty(CHAT_ID);

    const mainMessages = useChatStore.getState().chats[CHAT_ID];
    const otherMessages = useChatStore.getState().chats[OTHER];
    expect(mainMessages).toHaveLength(0);
    expect(otherMessages).toBeDefined();
    if (!otherMessages) return;
    expect(otherMessages).toHaveLength(1);
    expect(otherMessages[0]?.content).toBe('has content');
  });
});

describe('chat-store — stable message ids (W4.3)', () => {
  beforeEach(resetStore);

  it('auto-assigns a non-empty id when message is added without one', () => {
    useChatStore.getState().addMessage(CHAT_ID, { role: 'user', content: 'hi' });
    const messages = useChatStore.getState().chats[CHAT_ID];
    expect(messages).toHaveLength(1);
    expect(typeof messages?.[0]?.id).toBe('string');
    expect(messages?.[0]?.id.length).toBeGreaterThan(0);
  });

  it('preserves an explicitly provided id', () => {
    useChatStore.getState().addMessage(CHAT_ID, { id: 'my-stable-id', role: 'user', content: 'hi' });
    const messages = useChatStore.getState().chats[CHAT_ID];
    expect(messages?.[0]?.id).toBe('my-stable-id');
  });

  it('each message gets a unique id', () => {
    useChatStore.getState().addMessage(CHAT_ID, { role: 'user', content: 'first' });
    useChatStore.getState().addMessage(CHAT_ID, { role: 'assistant', content: 'second' });
    const messages = useChatStore.getState().chats[CHAT_ID];
    expect(messages).toHaveLength(2);
    const id0 = messages?.[0]?.id;
    const id1 = messages?.[1]?.id;
    expect(id0).toBeTruthy();
    expect(id1).toBeTruthy();
    expect(id0).not.toBe(id1);
  });

  it('id survives appendToLastMessage (content-update preserves identity)', () => {
    useChatStore.getState().addMessage(CHAT_ID, { role: 'assistant', content: 'Hel' });
    const idBefore = useChatStore.getState().chats[CHAT_ID]?.[0]?.id;

    useChatStore.getState().appendToLastMessage(CHAT_ID, 'lo');

    const idAfter = useChatStore.getState().chats[CHAT_ID]?.[0]?.id;
    expect(idAfter).toBe(idBefore);
    expect(useChatStore.getState().chats[CHAT_ID]?.[0]?.content).toBe('Hello');
  });
});
