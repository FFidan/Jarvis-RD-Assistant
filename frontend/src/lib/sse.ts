/**
 * POST-based SSE streaming via fetch + ReadableStream.
 *
 * The backend uses POST for SSE endpoints (not GET), so we cannot use
 * the browser's EventSource API. Instead we read the response body as
 * a stream and parse SSE frames manually.
 *
 * SECURITY: X-API-Key header is included on every SSE request.
 * AUTH: 401/403 responses trigger automatic logout.
 */

import { useAuthStore } from '@/stores/auth-store';
// Import from the leaf module, not the barrel: handleAuthFailure is an internal
// helper and is intentionally NOT re-exported from @/lib/api. Importing the leaf
// also keeps the module graph acyclic.
import { handleAuthFailure } from '@/lib/api/core';

export type ConfidenceLevel = 'HIGH' | 'MEDIUM' | 'LOW' | 'UNVERIFIED';

export interface ConfidenceEvent {
  type: 'confidence';
  confidence: ConfidenceLevel;
  verified_fraction: number;
  per_sentence: { text: string; verified: boolean }[];
}

export interface StreamEvent {
  type: 'token' | 'sources' | 'done' | 'error' | 'confidence';
  content?: string;
  full_answer?: string;
  model_used?: string | null;
  sources?: Array<{
    chunk_id?: number;
    paper_id?: number;
    paper_title?: string;
    content?: string;
    text?: string;
    page_number?: number | null;
    score: number;
  }>;
  message?: string;
  // confidence event fields
  confidence?: ConfidenceLevel;
  verified_fraction?: number;
  per_sentence?: { text: string; verified: boolean }[];
}

// --- Analyze Paper SSE types ---

export interface AnalyzeStepEvent {
  type: 'step';
  step: 'downloading' | 'processing' | 'summarizing';
  status: 'started' | 'completed' | 'failed';
  chunk_count?: number;
  message?: string;
}

export interface AnalyzeCompleteEvent {
  type: 'complete';
  paper_id: number;
}

export interface AnalyzeErrorEvent {
  type: 'error';
  step: string;
  message: string;
  /** Stable sanitized code intended for display and support correlation. */
  error_code?: string | null;
  /** Sanitized user-facing message. Prefer this over raw backend internals when present. */
  display_message?: string | null;
  /** Structured error class name from the backend (e.g. "PdfTooLargeError"). Optional — absent until the structured-error backend ships. */
  error_type?: string | null;
  /** Human-readable detail string from the backend. Optional — absent until the structured-error backend ships. */
  error_detail?: string | null;
}

export type AnalyzeEvent = AnalyzeStepEvent | AnalyzeCompleteEvent | AnalyzeErrorEvent;

/**
 * Core SSE frame parser: reads from any Uint8Array reader, splits on newlines,
 * yields each `data:` payload, and flushes any residual un-terminated line
 * when the stream ends (flush invariant: a producer that omits the trailing
 * blank line still delivers the final frame).
 */
export async function* readSSEFrames(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): AsyncGenerator<string> {
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (!done) {
        buffer += decoder.decode(value, { stream: true });
      }
      const lines = buffer.split('\n');
      // When not done, the partial trailing line stays buffered for the next read.
      // When done, lines is NOT popped, so a residual un-terminated `data:` line
      // is flushed below rather than discarded.
      buffer = done ? '' : (lines.pop() ?? '');
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (data === '[DONE]') return;
        yield data;
      }
      if (done) return;
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
}

async function* parseSSEFrames(response: Response): AsyncGenerator<string> {
  if (!response.body) {
    throw new Error('Response body is null — streaming not supported');
  }
  yield* readSSEFrames(response.body.getReader());
}

/**
 * Shared POST-SSE driver: authenticated POST, ok-check (with auth routing),
 * frame parse, and JSON.parse-yield. `streamSSE` and `streamAnalyze` delegate
 * here so the auth/error/parse logic lives in exactly one place.
 *
 * @param errorLabel prefix for the generic non-auth error (`SSE ` / `Analyze SSE `).
 */
async function* _postSSEStream<T>(
  url: string,
  body: string | object,
  signal: AbortSignal | undefined,
  errorLabel: string,
): AsyncGenerator<T> {
  const apiKey = useAuthStore.getState().getApiKey();
  const res = await fetch(url, {
    method: 'POST',
    // Include the jarvis_session cookie alongside any X-API-Key.
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    },
    body: typeof body === 'string' ? body : JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    if (res.status === 401) {
      // Centralized auth handling: debounced session-expired toast + logout.
      handleAuthFailure(401);
      throw new Error('Unauthorized — session ended');
    }
    if (res.status === 403) {
      // 403 is permission-denied for an authenticated user — no logout.
      throw new Error('Forbidden — you do not have permission to access this resource');
    }
    throw new Error(
      `${errorLabel}${res.status}: ${res.status >= 500 ? 'Server error' : 'Request failed'}`,
    );
  }

  for await (const data of parseSSEFrames(res)) {
    try {
      yield JSON.parse(data) as T;
    } catch {
      console.warn('[sse] malformed frame skipped', data.slice(0, 120));
    }
  }
}

export function streamSSE(
  url: string,
  body: object,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  return _postSSEStream<StreamEvent>(url, body, signal, 'SSE ');
}

/**
 * Stream the compound analyze-paper endpoint (download → process → summarize).
 *
 * Yields AnalyzeEvent objects as the backend progresses through each step.
 */
export function streamAnalyze(
  paperId: number,
  signal?: AbortSignal,
): AsyncGenerator<AnalyzeEvent> {
  return _postSSEStream<AnalyzeEvent>(`/api/papers/${paperId}/analyze`, '{}', signal, 'Analyze SSE ');
}
