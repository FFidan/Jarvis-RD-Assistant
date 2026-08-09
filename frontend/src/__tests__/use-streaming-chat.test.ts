/**
 * Unit tests for use-streaming-chat — D.2 finally-branch empty placeholder removal,
 * D.3 unmount does NOT abort (streams survive navigation); logout DOES abort,
 * FE-SSE-1 streamError surface, and U1-fe elapsed-seconds timer.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useChatStore, abortAllStreams } from '@/stores/chat-store';

// ---------------------------------------------------------------------------
// Mock streamSSE before the hook module is imported
// ---------------------------------------------------------------------------
type StreamEvent = { type: string; content?: string; sources?: unknown[]; message?: string; code?: string };
type StreamSSEFn = (
  url: string,
  body: unknown,
  signal: AbortSignal,
) => AsyncGenerator<StreamEvent, void, unknown>;
const mockStreamSSE = vi.fn<StreamSSEFn>();

vi.mock('@/lib/sse', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/sse')>();
  return {
    ...actual,
    streamSSE: (url: string, body: unknown, signal: AbortSignal) =>
      mockStreamSSE(url, body, signal),
  };
});

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

  it('maps a known hygiene code to the same actionable transcript and error state', async () => {
    mockStreamSSE.mockImplementation(async function* () {
      yield {
        type: 'error',
        message: 'The model did not return a usable final answer. Please try again.',
        code: 'llm_empty_visible_content',
      };
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'err1', scope: 'cross-paper' }),
    );

    expect(result.current.streamError).toBeNull();

    act(() => {
      void result.current.sendMessage('trigger error');
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    const actionable = 'The model did not produce a usable answer. Try again. If it keeps happening, ask an administrator to review the smart model or thinking setting.';
    expect(result.current.streamError).toEqual({
      message: 'The model did not return a usable final answer. Please try again.',
      code: 'llm_empty_visible_content',
    });
    expect(useChatStore.getState().chats.err1?.slice(-1)[0]?.content).toContain(actionable);
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

    await waitFor(() => expect(result.current.streamError).toEqual({ message: 'context too long' }));

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

    expect(result.current.streamError).toEqual({ message: 'Unknown streaming error' });
  });
});

// ---------------------------------------------------------------------------
// catch-block sets streamError on non-abort transport errors
// ---------------------------------------------------------------------------

describe('use-streaming-chat — catch-block setStreamError', () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it('sets streamError when the SSE generator throws a non-abort error', async () => {
    mockStreamSSE.mockImplementation(async function* () {
      throw new Error('network failure');
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'cf1', scope: 'cross-paper' }),
    );

    expect(result.current.streamError).toBeNull();

    act(() => {
      void result.current.sendMessage('trigger transport error');
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    expect(result.current.streamError).toEqual({
      message: 'network failure',
      code: 'stream_transport_error',
    });
    const transcript = useChatStore.getState().chats.cf1?.slice(-1)[0]?.content ?? '';
    expect(transcript).toContain('Something went wrong answering that. Please try again.');
    expect(transcript).not.toContain('network failure');
  });
});

// ---------------------------------------------------------------------------
// clearChat resets streamError
// ---------------------------------------------------------------------------

describe('use-streaming-chat — clearChat resets streamError', () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it('resets streamError to null when clearChat is called', async () => {
    mockStreamSSE.mockImplementation(async function* () {
      yield { type: 'error', message: 'some error' };
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'cf2', scope: 'cross-paper' }),
    );

    act(() => {
      void result.current.sendMessage('trigger error');
    });

    await waitFor(() => expect(result.current.streamError).toEqual({ message: 'some error' }));

    act(() => {
      result.current.clearChat();
    });

    expect(result.current.streamError).toBeNull();
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

  it('isStreaming stays true on a fresh hook mount while the stream is still in flight (review fix)', async () => {
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

// ---------------------------------------------------------------------------
// U1-fe — elapsedSeconds timer
// ---------------------------------------------------------------------------

describe('use-streaming-chat — U1-fe elapsedSeconds timer', () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
    // shouldAdvanceTime=true: fake timers auto-advance wall-clock so that
    // waitFor polling (which uses setTimeout internally) still resolves.
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('increments each second while streaming then resets to 0 on stop', async () => {
    mockStreamSSE.mockImplementation((_url, _body, signal: AbortSignal) => hangingStream(signal));

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'timer1', scope: 'cross-paper' }),
    );

    expect(result.current.elapsedSeconds).toBe(0);

    act(() => { void result.current.sendMessage('time me'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(true));

    expect(result.current.elapsedSeconds).toBe(0);

    act(() => { vi.advanceTimersByTime(1000); });
    expect(result.current.elapsedSeconds).toBe(1);

    act(() => { vi.advanceTimersByTime(4000); });
    expect(result.current.elapsedSeconds).toBe(5);

    // Stop stream — elapsed should reset to 0
    act(() => { result.current.stopStreaming(); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));
    expect(result.current.elapsedSeconds).toBe(0);
  });

  it('resets elapsedSeconds to 0 when stream ends normally', async () => {
    mockStreamSSE.mockImplementation(async function* () {
      yield { type: 'token', content: 'hello' };
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'timer2', scope: 'cross-paper' }),
    );

    act(() => { void result.current.sendMessage('quick question'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));
    expect(result.current.elapsedSeconds).toBe(0);
  });

  it('does not leak the interval after unmount mid-stream', async () => {
    mockStreamSSE.mockImplementation((_url, _body, signal: AbortSignal) => hangingStream(signal));

    const { result, unmount } = renderHook(() =>
      useStreamingChat({ chatId: 'timer3', scope: 'cross-paper' }),
    );

    act(() => { void result.current.sendMessage('leak test'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(true));

    act(() => { vi.advanceTimersByTime(2000); });
    expect(result.current.elapsedSeconds).toBe(2);

    // Unmount — React cleanup should clear the interval
    unmount();

    // Advancing time after unmount must not throw (stray setElapsedSeconds calls on
    // an unmounted hook would produce a React act() warning / error).
    act(() => { vi.advanceTimersByTime(5000); });
  });
});

// ---------------------------------------------------------------------------
// C1 — history sent in the POST body
// ---------------------------------------------------------------------------

describe('use-streaming-chat — C1 conversation history in POST body', () => {
  /** Capture bodies posted to streamSSE across calls */
  const capturedBodies: unknown[] = [];

  beforeEach(() => {
    resetStore();
    capturedBodies.length = 0;
    vi.clearAllMocks();
    mockStreamSSE.mockImplementation(async function* (_url, body) {
      capturedBodies.push(body);
      yield { type: 'token', content: 'reply' };
    });
  });

  it('sends history:[] on the first question (no prior turns)', async () => {
    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'hist-first', scope: 'cross-paper' }),
    );

    act(() => { void result.current.sendMessage('What is this paper about?'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    expect(capturedBodies).toHaveLength(1);
    const body = capturedBodies[0] as { history: unknown[] };
    expect(body.history).toEqual([]);
  });

  it('excludes the just-added user message and assistant placeholder from history', async () => {
    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'hist-excl', scope: 'cross-paper' }),
    );

    // First turn: history=[]
    act(() => { void result.current.sendMessage('First question'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    // Second turn: history should contain exactly the first user + assistant
    // exchange, NOT the new user message or the new assistant placeholder
    act(() => { void result.current.sendMessage('Follow-up question'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    expect(capturedBodies).toHaveLength(2);
    const secondBody = capturedBodies[1] as { history: { role: string; content: string }[] };
    expect(secondBody.history).toHaveLength(2);
    expect(secondBody.history[0]).toMatchObject({ role: 'user', content: 'First question' });
    expect(secondBody.history[1]).toMatchObject({ role: 'assistant', content: 'reply' });

    // The new user message "Follow-up question" must NOT appear in history
    const historyContents = secondBody.history.map((h) => h.content);
    expect(historyContents).not.toContain('Follow-up question');
  });

  it('sends at most 6 prior turns when the chat has more than 6', async () => {
    // Pre-populate the store with 8 turns (4 exchanges)
    useChatStore.setState({
      chats: {
        'hist-limit': [
          { id: '1', role: 'user', content: 'Q1' },
          { id: '2', role: 'assistant', content: 'A1' },
          { id: '3', role: 'user', content: 'Q2' },
          { id: '4', role: 'assistant', content: 'A2' },
          { id: '5', role: 'user', content: 'Q3' },
          { id: '6', role: 'assistant', content: 'A3' },
          { id: '7', role: 'user', content: 'Q4' },
          { id: '8', role: 'assistant', content: 'A4' },
        ],
      },
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'hist-limit', scope: 'cross-paper' }),
    );

    act(() => { void result.current.sendMessage('Q5'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    const body = capturedBodies[0] as { history: { role: string; content: string }[] };
    expect(body.history).toHaveLength(6);
    // Should be the LAST 6 (turns 3–8: Q2 A2 Q3 A3 Q4 A4)
    expect(body.history[0]).toMatchObject({ role: 'user', content: 'Q2' });
    expect(body.history[5]).toMatchObject({ role: 'assistant', content: 'A4' });
  });

  it('strips streamed error suffixes from history and drops error-only messages', async () => {
    useChatStore.setState({
      chats: {
        'hist-err': [
          { id: '1', role: 'user', content: 'Q1' },
          { id: '2', role: 'assistant', content: 'partial answer\n\n**Error:** Request timed out' },
          { id: '3', role: 'user', content: 'Q2' },
          { id: '4', role: 'assistant', content: '\n\n**Error:** Unknown streaming error' },
        ],
      },
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'hist-err', scope: 'cross-paper' }),
    );

    act(() => { void result.current.sendMessage('Q3'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    const body = capturedBodies[0] as { history: { role: string; content: string }[] };
    // Error-only assistant message dropped entirely; suffix stripped from the partial one.
    expect(body.history).toHaveLength(3);
    expect(body.history[1]).toMatchObject({ role: 'assistant', content: 'partial answer' });
    for (const turn of body.history) {
      expect(turn.content).not.toContain('**Error:**');
    }
  });

  it('filters out whitespace-only / empty-content messages before sending', async () => {
    // Pre-populate with one legit exchange and one whitespace-only assistant message
    useChatStore.setState({
      chats: {
        'hist-filter': [
          { id: '1', role: 'user', content: 'Real question' },
          { id: '2', role: 'assistant', content: '   ' }, // whitespace-only (streaming placeholder)
        ],
      },
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'hist-filter', scope: 'cross-paper' }),
    );

    act(() => { void result.current.sendMessage('Next question'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    const body = capturedBodies[0] as { history: { role: string; content: string }[] };
    // The whitespace-only assistant message must be excluded; only the real user turn survives
    expect(body.history).toHaveLength(1);
    expect(body.history[0]).toMatchObject({ role: 'user', content: 'Real question' });
  });

  it('includes history:prior in single-paper scope POST body', async () => {
    // Pre-populate with one exchange
    useChatStore.setState({
      chats: {
        'hist-single': [
          { id: '1', role: 'user', content: 'Paper question' },
          { id: '2', role: 'assistant', content: 'Paper answer' },
        ],
      },
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'hist-single', scope: 'single-paper', paperId: 42 }),
    );

    act(() => { void result.current.sendMessage('Follow up on paper'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    const body = capturedBodies[0] as { question: string; history: { role: string; content: string }[] };
    expect(body.history).toHaveLength(2);
    expect(body.history[0]).toMatchObject({ role: 'user', content: 'Paper question' });
    expect(body.history[1]).toMatchObject({ role: 'assistant', content: 'Paper answer' });
    // decompose must NOT be in single-paper body
    expect(body).not.toHaveProperty('decompose');
  });

  it('truncates content to 4000 chars per turn', async () => {
    const longContent = 'x'.repeat(5000);
    useChatStore.setState({
      chats: {
        'hist-truncate': [
          { id: '1', role: 'user', content: longContent },
          { id: '2', role: 'assistant', content: 'short' },
        ],
      },
    });

    const { result } = renderHook(() =>
      useStreamingChat({ chatId: 'hist-truncate', scope: 'cross-paper' }),
    );

    act(() => { void result.current.sendMessage('Next'); });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));

    const body = capturedBodies[0] as { history: { role: string; content: string }[] };
    expect(body.history[0]?.content).toHaveLength(4000);
    expect(body.history[1]?.content).toBe('short');
  });
});
