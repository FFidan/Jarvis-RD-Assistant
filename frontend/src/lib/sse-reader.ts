/**
 * GET-based SSE reader utility.
 *
 * Yields raw `data:` payload strings from a server-sent-events stream received
 * over a plain GET request.  Handles chunked buffering, split-on-double-newline
 * framing, the `[DONE]` terminator sentinel, and reader cancellation in the
 * finally block.
 *
 * Scope: GET-only, no retry / backoff (those are the caller's responsibility).
 */

export async function* createSSEReader(
  url: string,
  init?: { headers?: HeadersInit; signal?: AbortSignal },
): AsyncGenerator<string, void, void> {
  const res = await fetch(url, {
    method: 'GET',
    credentials: 'include',
    headers: init?.headers,
    signal: init?.signal,
  });

  if (!res.ok || !res.body) {
    throw new Error(`SSE GET failed: ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by blank lines (\n\n).
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (data === '[DONE]') return;
        yield data;
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
}
