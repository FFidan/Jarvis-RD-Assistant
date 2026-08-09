import { describe, it, expect, vi, beforeEach } from 'vitest';
import { streamSSE, streamAnalyze, type StreamEvent, type AnalyzeEvent } from '@/lib/sse';
import { createSSEReader } from '@/lib/sse-reader';
import { toast } from 'sonner';

// Mock sonner — sse.ts now transitively imports `@/lib/api/core`, which
// imports `toast` from sonner for the session-expired toast.
vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

// Mock auth store — must be defined before importing sse to ensure
// the module-level import in sse.ts resolves to this mock. `isAuthenticated`
// MUST be truthy: handleAuthFailure early-returns (no toast, no logout) when
// the session is not authenticated.
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      isAuthenticated: true,
      logout: vi.fn(),
    })),
  },
}));

function createMockReadableStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let index = 0;
  return new ReadableStream({
    pull(controller) {
      if (index < chunks.length) {
        controller.enqueue(encoder.encode(chunks[index]));
        index++;
      } else {
        controller.close();
      }
    },
  });
}

describe('streamSSE', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('parses token events from SSE stream', async () => {
    const stream = createMockReadableStream([
      'data: {"type":"token","content":"Hello"}\n\n',
      'data: {"type":"token","content":" world"}\n\n',
      'data: [DONE]\n\n',
    ]);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );

    const events: StreamEvent[] = [];
    for await (const event of streamSSE('/api/ask/stream', { question: 'test' })) {
      events.push(event);
    }

    expect(events).toHaveLength(2);
    const ev0 = events[0];
    const ev1 = events[1];
    if (!ev0 || !ev1) throw new Error('test fixture: expected 2 events');
    expect(ev0.type).toBe('token');
    expect(ev0.content).toBe('Hello');
    expect(ev1.content).toBe(' world');
  });

  it('stops on [DONE] sentinel', async () => {
    const stream = createMockReadableStream([
      'data: {"type":"token","content":"Hi"}\n\n',
      'data: [DONE]\n\n',
      'data: {"type":"token","content":"should not appear"}\n\n',
    ]);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );

    const events: StreamEvent[] = [];
    for await (const event of streamSSE('/api/ask/stream', { question: 'test' })) {
      events.push(event);
    }

    expect(events).toHaveLength(1);
    const ev0done = events[0];
    if (!ev0done) throw new Error('test fixture: expected 1 event');
    expect(ev0done.content).toBe('Hi');
  });

  it('yields error events', async () => {
    const stream = createMockReadableStream([
      'data: {"type":"error","message":"Something failed","code":"llm_empty_visible_content"}\n\n',
      'data: [DONE]\n\n',
    ]);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );

    const events: StreamEvent[] = [];
    for await (const event of streamSSE('/api/ask/stream', { question: 'test' })) {
      events.push(event);
    }

    expect(events).toHaveLength(1);
    const ev0err = events[0];
    if (!ev0err) throw new Error('test fixture: expected 1 error event');
    expect(ev0err.type).toBe('error');
    expect(ev0err.message).toBe('Something failed');
    expect(ev0err.code).toBe('llm_empty_visible_content');
  });

  it('calls logout and throws on 401 response', async () => {
    const { useAuthStore } = await import('@/stores/auth-store');
    const logoutMock = vi.fn();
    // Partial AuthState mock — only the fields used by this code path.
    // isAuthenticated must be true or handleAuthFailure early-returns.
    vi.mocked(useAuthStore.getState).mockReturnValue({
      isAuthenticated: true,
      logout: logoutMock,
    } as unknown as ReturnType<typeof useAuthStore.getState>);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Unauthorized', { status: 401 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    await expect(gen.next()).rejects.toThrow('Unauthorized — session ended');
    expect(logoutMock).toHaveBeenCalledOnce();
  });

  it('does NOT call logout and throws Forbidden on 403 response', async () => {
    const { useAuthStore } = await import('@/stores/auth-store');
    const logoutMock = vi.fn();
    // Partial AuthState mock — only the fields used by this code path.
    vi.mocked(useAuthStore.getState).mockReturnValue({
      isAuthenticated: true,
      logout: logoutMock,
    } as unknown as ReturnType<typeof useAuthStore.getState>);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Forbidden', { status: 403 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    await expect(gen.next()).rejects.toThrow('Forbidden — you do not have permission to access this resource');
    expect(logoutMock).not.toHaveBeenCalled();
  });

  it('routes 401 through handleAuthFailure: N parallel 401s toast once, logout per-call', async () => {
    const { useAuthStore } = await import('@/stores/auth-store');
    const logoutMock = vi.fn();
    vi.mocked(useAuthStore.getState).mockReturnValue({
      isAuthenticated: true,
      logout: logoutMock,
    } as unknown as ReturnType<typeof useAuthStore.getState>);
    vi.mocked(toast.error).mockClear();

    // Jump the clock far into the future, well past any prior test's 5s
    // debounce window (which was stamped with the real Date.now), so the first
    // of this burst always clears the gate — order-independent.
    vi.spyOn(Date, 'now').mockReturnValue(4_000_000_000_000);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Unauthorized', { status: 401 }),
    );

    // Fire 3 parallel streamSSE calls that all 401 in the same debounce window.
    const results = await Promise.allSettled(
      [0, 1, 2].map((i) => streamSSE(`/api/ask/stream/${i}`, { question: 'test' }).next()),
    );

    expect(results.every((r) => r.status === 'rejected')).toBe(true);
    // Debounce singleton collapses the burst to one toast …
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringMatching(/session expired/i),
      expect.objectContaining({ duration: 6000 }),
    );
    // … but logout fires once per call (not debounced).
    expect(logoutMock).toHaveBeenCalledTimes(3);
  });

  it('reader.cancel is called in finally block after stream completes', async () => {
    const cancelMock = vi.fn().mockResolvedValue(undefined);
    const stream = createMockReadableStream([
      'data: {"type":"token","content":"Hi"}\n\n',
      'data: [DONE]\n\n',
    ]);
    // Wrap the real reader to spy on cancel
    const originalGetReader = stream.getReader.bind(stream);
    vi.spyOn(stream, 'getReader').mockImplementation(() => {
      const reader = originalGetReader();
      reader.cancel = cancelMock;
      return reader;
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );

    const events: StreamEvent[] = [];
    for await (const event of streamSSE('/api/ask/stream', { question: 'test' })) {
      events.push(event);
    }

    expect(events).toHaveLength(1);
    expect(cancelMock).toHaveBeenCalledOnce();
  });

  it('throws an error containing the status code on non-OK non-auth response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Internal Server Error', { status: 500 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    await expect(gen.next()).rejects.toThrow('500');
  });

  it('keeps the generic message on 500 even when the body carries a detail', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'internal stack trace' }), { status: 500 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    const err = await gen.next().catch((e: unknown) => e as Error);
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toBe('SSE 500: Server error');
  });

  it('appends a string detail from the body on 4xx', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Question too long (max 2000 chars)' }), { status: 422 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    const err = await gen.next().catch((e: unknown) => e as Error);
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toContain('422');
    expect((err as Error).message).toContain('Question too long (max 2000 chars)');
  });

  it('JSON-stringifies an object detail from the body on 4xx', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: { field: 'question', issue: 'required' } }), { status: 400 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    const err = await gen.next().catch((e: unknown) => e as Error);
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toContain('400');
    expect((err as Error).message).toContain('{"field":"question","issue":"required"}');
  });

  it('falls back to the generic message when the 4xx body is not JSON', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('<html>Bad Request</html>', { status: 400 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    const err = await gen.next().catch((e: unknown) => e as Error);
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toBe('SSE 400: Request failed');
  });

  it('keeps the generic message when a 4xx body carries detail: null', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: null }), { status: 422 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    const err = await gen.next().catch((e: unknown) => e as Error);
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toBe('SSE 422: Request failed');
  });

  it('rejects with AbortError when signal is aborted while fetch is in flight', async () => {
    const ac = new AbortController();

    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, opts) => {
      return new Promise((_resolve, reject) => {
        const signal = (opts as RequestInit | undefined)?.signal;
        if (signal?.aborted) {
          reject(new DOMException('The operation was aborted.', 'AbortError'));
          return;
        }
        signal?.addEventListener('abort', () => {
          reject(new DOMException('The operation was aborted.', 'AbortError'));
        });
        // Never resolves on its own — waits for abort.
      });
    });

    const gen = streamSSE('/api/ask/stream', { question: 'test' }, ac.signal);
    const iterPromise = gen.next();

    // Abort after the generator has started awaiting fetch.
    queueMicrotask(() => ac.abort());

    const err = await iterPromise.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(DOMException);
    expect((err as DOMException).name).toBe('AbortError');
  });

  it('flushes trailing data: line when stream ends without trailing newline — streamSSE (3c)', async () => {
    // Final chunk has no trailing \n\n — residual buffer must be flushed on done.
    const stream = createMockReadableStream([
      'data: {"type":"token","content":"partial"}\n\n',
      'data: {"type":"token","content":"last"}',
    ]);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );

    const events: StreamEvent[] = [];
    for await (const event of streamSSE('/api/ask/stream', { question: 'test' })) {
      events.push(event);
    }

    expect(events).toHaveLength(2);
    expect(events[0]?.content).toBe('partial');
    expect(events[1]?.content).toBe('last');
  });

  it('warns and skips malformed frames while yielding subsequent valid frames (streamSSE)', async () => {
    const stream = createMockReadableStream([
      'data: {"type":"token","content":"before"}\n\n',
      'data: not-valid-json{{{broken\n\n',
      'data: {"type":"token","content":42}\n\n',
      'data: {"type":"token","content":"after"}\n\n',
      'data: [DONE]\n\n',
    ]);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const events: StreamEvent[] = [];
    for await (const event of streamSSE('/api/ask/stream', { question: 'test' })) {
      events.push(event);
    }

    // Malformed frame is skipped; both valid frames are yielded.
    expect(events).toHaveLength(2);
    expect(events[0]?.content).toBe('before');
    expect(events[1]?.content).toBe('after');

    // Invalid JSON and schema-invalid JSON are both rejected at the boundary.
    expect(warnSpy).toHaveBeenCalledTimes(2);
    const [label, snippet] = warnSpy.mock.calls[0] as [string, string];
    expect(label).toBe('[sse] malformed frame skipped');
    // Snippet is a truncated string, not the raw exception.
    expect(typeof snippet).toBe('string');
    expect(snippet.length).toBeLessThanOrEqual(120);
  });
});

describe('createSSEReader', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('flushes trailing data: line when stream ends without trailing newline (3c)', async () => {
    const encoder = new TextEncoder();
    // Final chunk has no trailing \n\n — the `data:` line is residual in the buffer when done fires.
    const chunks = [
      'data: first\n\n',
      'data: {"x":1}',
    ];
    let idx = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (idx < chunks.length) {
          controller.enqueue(encoder.encode(chunks[idx]));
          idx++;
        } else {
          controller.close();
        }
      },
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );

    const frames: string[] = [];
    for await (const frame of createSSEReader('/api/test/stream')) {
      frames.push(frame);
    }

    expect(frames).toEqual(['first', '{"x":1}']);
  });

  it('yields raw data-line payloads and stops on [DONE]', async () => {
    const encoder = new TextEncoder();
    const chunks = [
      'data: hello\n\n',
      'data: world\n\n',
      'data: [DONE]\n\n',
      'data: should-not-appear\n\n',
    ];
    let idx = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (idx < chunks.length) {
          controller.enqueue(encoder.encode(chunks[idx]));
          idx++;
        } else {
          controller.close();
        }
      },
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );

    const frames: string[] = [];
    for await (const frame of createSSEReader('/api/test/stream')) {
      frames.push(frame);
    }

    expect(frames).toEqual(['hello', 'world']);
  });
});

describe('streamAnalyze', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('warns and skips malformed frames while yielding subsequent valid frames (streamAnalyze)', async () => {
    const stream = createMockReadableStream([
      'data: {"type":"step","step":"downloading","status":"started"}\n\n',
      'data: }{broken json}\n\n',
      'data: {"type":"complete","paper_id":42}\n\n',
      'data: [DONE]\n\n',
    ]);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const events: AnalyzeEvent[] = [];
    for await (const event of streamAnalyze(42)) {
      events.push(event);
    }

    // Malformed frame is skipped; both valid frames are yielded.
    expect(events).toHaveLength(2);
    expect(events[0]?.type).toBe('step');
    expect(events[1]?.type).toBe('complete');

    // console.warn was called once for the malformed frame.
    expect(warnSpy).toHaveBeenCalledOnce();
    const [label, snippet] = warnSpy.mock.calls[0] as [string, string];
    expect(label).toBe('[sse] malformed frame skipped');
    expect(typeof snippet).toBe('string');
    expect(snippet.length).toBeLessThanOrEqual(120);
  });

  it('parses a skipped step event and carries its reason', async () => {
    const stream = createMockReadableStream([
      'data: {"type":"step","step":"downloading","status":"skipped","reason":"local paper"}\n\n',
      'data: [DONE]\n\n',
    ]);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(stream, { status: 200 }),
    );

    const events: AnalyzeEvent[] = [];
    for await (const event of streamAnalyze(42)) {
      events.push(event);
    }

    expect(events).toHaveLength(1);
    const ev = events[0];
    if (!ev || ev.type !== 'step') throw new Error('test fixture: expected 1 step event');
    expect(ev.status).toBe('skipped');
    expect(ev.reason).toBe('local paper');
  });
});
