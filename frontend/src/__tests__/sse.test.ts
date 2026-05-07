import { describe, it, expect, vi, beforeEach } from 'vitest';
import { streamSSE, type StreamEvent } from '@/lib/sse';

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

  it('calls logout and throws on 403 response', async () => {
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
    await expect(gen.next()).rejects.toThrow('Unauthorized — session ended');
    expect(logoutMock).toHaveBeenCalledOnce();
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
});
