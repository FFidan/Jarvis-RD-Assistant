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

/** Error thrown by createSSEReader on a non-ok response, carrying the HTTP status. */
export class SSEGetError extends Error {
  constructor(public status: number) {
    super(`SSE GET failed: ${status}`);
    this.name = 'SSEGetError';
  }
}

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
    // Carry the HTTP status so callers can route auth failures centrally.
    // The message is preserved verbatim for back-compatible callers that
    // string-match `SSE GET failed: <status>` (e.g. the job store).
    throw new SSEGetError(res.status);
  }

  yield* readSSEFrames(res.body.getReader());
}
