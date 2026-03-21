import { describe, it, expect, vi, beforeEach } from 'vitest';
import { streamSSE, type StreamEvent } from '@/lib/sse';

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
    expect(events[0].type).toBe('token');
    expect(events[0].content).toBe('Hello');
    expect(events[1].content).toBe(' world');
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
    expect(events[0].content).toBe('Hi');
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
    expect(events[0].type).toBe('error');
    expect(events[0].message).toBe('Something failed');
  });
});
