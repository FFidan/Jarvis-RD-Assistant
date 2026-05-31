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

import { readSSEFrames } from '@/lib/sse';

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

  yield* readSSEFrames(res.body.getReader());
}
