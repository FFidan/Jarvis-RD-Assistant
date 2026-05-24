/**
 * Unit tests for use-streaming-chat — D.2 finally-branch empty placeholder removal,
 * D.3 unmount does NOT abort (streams survive navigation); logout DOES abort,
 * and FE-SSE-1 streamError surface.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useChatStore, abortAllStreams } from '@/stores/chat-store';

// ---------------------------------------------------------------------------
// Mock streamSSE before the hook module is imported
// ---------------------------------------------------------------------------
type StreamEvent = { type: string; content?: string; sources?: unknown[] };
type StreamSSEFn = (
  url: string,
  body: unknown,
  signal: AbortSignal,
) => AsyncGenerator<StreamEvent, void, unknown>;
const mockStreamSSE = vi.fn<StreamSSEFn>();

vi.mock('@/lib/sse', () => ({
  streamSSE: (url: string, body: unknown, signal: AbortSignal) =>
    mockStreamSSE(url, body, signal),
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
async function* hangingStream(signal: AbortSignal): AsyncGenerator<StreamEvent, void, unknown> {
  await new Promise<void>((_, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('AbortError', 'AbortError')));
  });
}

// ---------------------------------------------------------------------------
// FE-SSE-1 — streamError surfaced from SSE 'error' event
// ---------------------------------------------------------------------------

describe('use-streaming-chat — FE-SSE-1 streamError', () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it('surfaces streamError when SSE event type=error fires', async () => {
    mockStreamSSE.mockImplementation(async function* () {
      yield { type: 'error', message: 'context too long' };
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'err1', scope: 'cross-paper' }),
    );

    expect(result.current.streamError).toBeNull();

    act(() => {
      void result.current.sendMessage('trigger error');
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    expect(result.current.streamError).toBe('context too long');
  });

  it('clears streamError when sendMessage is called again', async () => {
    // First call: emit an error
    mockStreamSSE.mockImplementationOnce(async function* () {
      yield { type: 'error', message: 'context too long' };
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'err2', scope: 'cross-paper' }),
    );

    act(() => {
      void result.current.sendMessage('first');
    });

    await waitFor(() => expect(result.current.streamError).toBe('context too long'));

    // Second call: normal stream — streamError should be cleared at start
    mockStreamSSE.mockImplementationOnce(async function* () {
      yield { type: 'token', content: 'ok' };
    });

    act(() => {
      void result.current.sendMessage('second');
    });

    // Immediately after sendMessage starts, streamError should clear
    await waitFor(() => expect(result.current.streamError).toBeNull());
  });

  it('uses "Unknown streaming error" fallback when error event has no message', async () => {
    mockStreamSSE.mockImplementation(async function* () {
      yield { type: 'error' };
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'err3', scope: 'cross-paper' }),
    );

    act(() => {
      void result.current.sendMessage('no message error');
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    expect(result.current.streamError).toBe('Unknown streaming error');
  });
});

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
    const initialMessages = useChatStore.getState().chats['c1'];
    if (!initialMessages || initialMessages.length < 2) throw new Error('test fixture: expected 2 messages');
    expect(initialMessages).toHaveLength(2);
    const secondMsg = initialMessages[1];
    if (!secondMsg) throw new Error('test fixture: second message missing');
    expect(secondMsg.content).toBe('');

    // Stop before any token
    act(() => {
      result.current.stopStreaming();
    });

    // Wait for phase to return to idle
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    // Empty assistant placeholder must be removed; only user message remains
    const messages = useChatStore.getState().chats['c1'];
    if (!messages || messages.length < 1) throw new Error('test fixture: expected 1 remaining message');
    const firstMsg = messages[0];
    if (!firstMsg) throw new Error('test fixture: first message missing');
    expect(messages).toHaveLength(1);
    expect(firstMsg.role).toBe('user');
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
    if (!messages || messages.length < 2) throw new Error('test fixture: expected 2 preserved messages');
    const lastMsg = messages[1];
    if (!lastMsg) throw new Error('test fixture: second message missing');
    expect(messages).toHaveLength(2);
    expect(lastMsg.content).toBe('Partial answer');
  });
});

// ---------------------------------------------------------------------------
// D.3 — unmount does NOT abort; streams survive navigation
// ---------------------------------------------------------------------------

describe('use-streaming-chat — D.3 unmount does not abort stream', () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it('does NOT abort the AbortController when the hook unmounts mid-stream', async () => {
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

    // Unmount (navigation away) — stream must continue; signal must stay unaborted
    unmount();

    expect(capturedSignal!.aborted).toBe(false);
  });

  it('aborts all streams when abortAllStreams() is called (logout path)', async () => {
    let capturedSignal: AbortSignal | null = null;
    mockStreamSSE.mockImplementation((_url, _body, signal: AbortSignal) => {
      capturedSignal = signal;
      return hangingStream(signal);
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'c4', scope: 'cross-paper' }),
    );

    act(() => {
      void result.current.sendMessage('logout abort test');
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(true));
    expect(capturedSignal).not.toBeNull();
    expect(capturedSignal!.aborted).toBe(false);

    // Simulate logout aborting all streams
    abortAllStreams();

    expect(capturedSignal!.aborted).toBe(true);
  });

  it('isStreaming stays true on a fresh hook mount while the stream is still in flight (W1.6 review fix)', async () => {
    let capturedSignal: AbortSignal | null = null;
    mockStreamSSE.mockImplementation((_url, _body, signal: AbortSignal) => {
      capturedSignal = signal;
      return hangingStream(signal);
    });

    // Hook A: starts the stream then unmounts (simulates navigating away mid-stream).
    const { result: resultA, unmount: unmountA } = renderHook(() =>
      useStreamingChat({ chatId: 'c5', scope: 'cross-paper' }),
    );
    act(() => {
      void resultA.current.sendMessage('navigation test');
    });
    await waitFor(() => expect(resultA.current.isStreaming).toBe(true));
    unmountA();

    // Stream is still alive in the module-level registry.
    expect(capturedSignal!.aborted).toBe(false);

    // Hook B: fresh mount on the same chatId (simulates navigating back).
    const { result: resultB } = renderHook(() =>
      useStreamingChat({ chatId: 'c5', scope: 'cross-paper' }),
    );
    // The new hook instance MUST reflect that a stream is active so the
    // Stop button stays visible and the UI doesn't lie about being idle.
    expect(resultB.current.isStreaming).toBe(true);

    // Stop via the new hook — should still abort the in-flight stream
    // even though hook B never registered the controller itself.
    act(() => {
      resultB.current.stopStreaming();
    });
    expect(capturedSignal!.aborted).toBe(true);
  });
});
