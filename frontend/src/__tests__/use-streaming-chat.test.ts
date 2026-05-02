/**
 * Unit tests for use-streaming-chat — D.2 finally-branch empty placeholder removal
 * and D.3 unmount abort.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useChatStore } from '@/stores/chat-store';

// ---------------------------------------------------------------------------
// Mock streamSSE before the hook module is imported
// ---------------------------------------------------------------------------
const mockStreamSSE = vi.fn<() => AsyncGenerator<never, void, unknown>>();

vi.mock('@/lib/sse', () => ({
  streamSSE: (...args: unknown[]) => mockStreamSSE(...args),
}));

// Stub useAuthStore so streamSSE mock doesn't need it (sse.ts imports it at top-level)
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: () => ({ apiKey: 'test-key' }),
  },
}));

// Import hook AFTER mocks are in place
const { useStreamingChat } = await import('@/hooks/use-streaming-chat');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resetStore() {
  useChatStore.setState({ chats: {} });
}

/** Returns an async generator that aborts when the given signal fires */
async function* hangingStream(signal: AbortSignal): AsyncGenerator<never, void, unknown> {
  await new Promise<void>((_, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('AbortError', 'AbortError')));
  });
}

/** Returns an async generator that yields nothing and resolves immediately */
async function* emptyStream(): AsyncGenerator<never, void, unknown> {
  // no tokens — generator ends immediately (simulates instant done with no content)
}

// ---------------------------------------------------------------------------
// D.2 — empty placeholder removed on AbortError (Stop before any token)
// ---------------------------------------------------------------------------

describe('use-streaming-chat — D.2 empty placeholder removal', () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it('removes empty assistant message when Stop is clicked before any token arrives', async () => {
    // Stream hangs until aborted
    mockStreamSSE.mockImplementation((_url, _body, signal: AbortSignal) => hangingStream(signal));

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'c1', scope: 'cross-paper' }),
    );

    // Start sending a message (adds user + empty assistant messages)
    act(() => {
      void result.current.sendMessage('test question');
    });

    // Wait until isStreaming is true (phase = searching)
    await waitFor(() => expect(result.current.isStreaming).toBe(true));

    // At this point we have 2 messages: user + empty assistant
    expect(useChatStore.getState().chats['c1']).toHaveLength(2);
    expect(useChatStore.getState().chats['c1'][1].content).toBe('');

    // Stop before any token
    act(() => {
      result.current.stopStreaming();
    });

    // Wait for phase to return to idle
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    // Empty assistant placeholder must be removed; only user message remains
    const messages = useChatStore.getState().chats['c1'];
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe('user');
  });

  it('preserves partial content when Stop arrives after some tokens', async () => {
    // Stream yields one token then hangs
    mockStreamSSE.mockImplementation(async function* (_url, _body, signal: AbortSignal) {
      yield { type: 'token', content: 'Partial answer' };
      // Now hang until aborted
      await new Promise<void>((_, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('AbortError', 'AbortError')));
      });
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'c2', scope: 'cross-paper' }),
    );

    act(() => {
      void result.current.sendMessage('what is X?');
    });

    // Wait until at least one token is appended (streaming phase)
    await waitFor(() => {
      const msgs = useChatStore.getState().chats['c2'] ?? [];
      const last = msgs[msgs.length - 1];
      return last?.content === 'Partial answer';
    });

    act(() => {
      result.current.stopStreaming();
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    // Partial content must be preserved
    const messages = useChatStore.getState().chats['c2'];
    expect(messages).toHaveLength(2);
    expect(messages[1].content).toBe('Partial answer');
  });
});

// ---------------------------------------------------------------------------
// D.3 — unmount aborts the SSE stream
// ---------------------------------------------------------------------------

describe('use-streaming-chat — D.3 unmount cleanup', () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it('aborts the AbortController when the hook unmounts mid-stream', async () => {
    let capturedSignal: AbortSignal | null = null;
    mockStreamSSE.mockImplementation((_url, _body, signal: AbortSignal) => {
      capturedSignal = signal;
      return hangingStream(signal);
    });

    const { result, unmount } = renderHook(() =>
      useStreamingChat({ chatId: 'c3', scope: 'cross-paper' }),
    );

    act(() => {
      void result.current.sendMessage('unmount test');
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(true));
    expect(capturedSignal).not.toBeNull();
    expect(capturedSignal!.aborted).toBe(false);

    // Unmount — the cleanup useEffect should fire abort()
    unmount();

    expect(capturedSignal!.aborted).toBe(true);
  });
});
