import { describe, it, expect, vi, beforeEach } from 'vitest';
import { streamSSE, streamAnalyze, type StreamEvent, type AnalyzeEvent } from '@/lib/sse';
import { createSSEReader } from '@/lib/sse-reader';

// Mock auth store — must be defined before importing sse to ensure
// the module-level import in sse.ts resolves to this mock.
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      getApiKey: vi.fn(() => null),
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
      'data: {"type":"error","message":"Something failed"}\n\n',
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
  });

  it('calls logout and throws on 401 response', async () => {
    const { useAuthStore } = await import('@/stores/auth-store');
    const logoutMock = vi.fn();
    // Partial AuthState mock — only the fields used by this code path.
    vi.mocked(useAuthStore.getState).mockReturnValue({
      getApiKey: vi.fn(() => null),
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
      getApiKey: vi.fn(() => null),
      logout: logoutMock,
    } as unknown as ReturnType<typeof useAuthStore.getState>);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Forbidden', { status: 403 }),
    );

    const gen = streamSSE('/api/ask/stream', { question: 'test' });
    await expect(gen.next()).rejects.toThrow('Forbidden — you do not have permission to access this resource');
    expect(logoutMock).not.toHaveBeenCalled();
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

  it('warns and skips malformed frames while yielding subsequent valid frames (streamSSE)', async () => {
    const stream = createMockReadableStream([
      'data: {"type":"token","content":"before"}\n\n',
      'data: not-valid-json{{{broken\n\n',
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

    // console.warn was called once for the malformed frame.
    expect(warnSpy).toHaveBeenCalledOnce();
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
});
