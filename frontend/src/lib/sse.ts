/**
 * POST-based SSE streaming via fetch + ReadableStream.
 *
 * The backend uses POST for SSE endpoints (not GET), so we cannot use
 * the browser's EventSource API. Instead we read the response body as
 * a stream and parse SSE frames manually.
 *
 * AUTH: the HttpOnly session cookie is included; 401 responses trigger logout.
 */

import { z } from 'zod';
// Import from the leaf module, not the barrel: handleAuthFailure is an internal
// helper and is intentionally NOT re-exported from @/lib/api. Importing the leaf
// also keeps the module graph acyclic.
import { decodeResponseJson, handleAuthFailure } from '@/lib/api/core';
import { apiErrorDetailSchema } from '@/lib/api/schemas/common';

export type ConfidenceLevel = 'HIGH' | 'MEDIUM' | 'LOW' | 'UNVERIFIED';

export interface ConfidenceEvent {
  type: 'confidence';
  confidence: ConfidenceLevel | null;
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
  /** Stable sanitized code for researcher-facing remediation. */
  code?: string;
  // confidence event fields
  confidence?: ConfidenceLevel | null;
  verified_fraction?: number;
  per_sentence?: { text: string; verified: boolean }[];
}

export interface StreamError {
  message: string;
  code?: string;
}

const RAG_HYGIENE_ERROR_COPY =
  'The model did not produce a usable answer. Try again. If it keeps happening, ask an administrator to review the smart model or thinking setting.';
const STREAM_TRANSPORT_ERROR_COPY = 'Something went wrong answering that. Please try again.';

/** Return the one user-facing message used by both transcript and alert. */
export function getStreamErrorCopy(error: StreamError): string {
  if (
    error.code === 'llm_empty_visible_content'
    || error.code === 'llm_visible_work_notes'
  ) {
    return RAG_HYGIENE_ERROR_COPY;
  }
  if (error.code === 'stream_transport_error') {
    return STREAM_TRANSPORT_ERROR_COPY;
  }
  return error.message || 'Unknown streaming error';
}

// --- Analyze Paper SSE types ---

export interface AnalyzeStepEvent {
  type: 'step';
  step: 'downloading' | 'processing' | 'summarizing';
  status: 'started' | 'completed' | 'skipped' | 'failed';
  reason?: string;
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

const streamSourceSchema = z.looseObject({
  chunk_id: z.number().optional(),
  paper_id: z.number().optional(),
  paper_title: z.string().optional(),
  content: z.string().optional(),
  text: z.string().optional(),
  page_number: z.number().nullable().optional(),
  score: z.number(),
});
const verifiedSentenceSchema = z.looseObject({ text: z.string(), verified: z.boolean() });
const streamEventSchema = z.discriminatedUnion('type', [
  z.looseObject({ type: z.literal('token'), content: z.string() }),
  z.looseObject({ type: z.literal('sources'), sources: z.array(streamSourceSchema) }),
  z.looseObject({
    type: z.literal('done'),
    full_answer: z.string(),
    model_used: z.string().nullable().optional(),
  }),
  z.looseObject({ type: z.literal('error'), message: z.string(), code: z.string().optional() }),
  z.looseObject({
    type: z.literal('confidence'),
    confidence: z.enum(['HIGH', 'MEDIUM', 'LOW', 'UNVERIFIED']).nullable(),
    verified_fraction: z.number(),
    per_sentence: z.array(verifiedSentenceSchema),
  }),
]);
const analyzeEventSchema = z.discriminatedUnion('type', [
  z.looseObject({
    type: z.literal('step'),
    step: z.enum(['downloading', 'processing', 'summarizing']),
    status: z.enum(['started', 'completed', 'skipped', 'failed']),
    reason: z.string().optional(),
    chunk_count: z.number().optional(),
    message: z.string().optional(),
  }),
  z.looseObject({ type: z.literal('complete'), paper_id: z.number() }),
  z.looseObject({
    type: z.literal('error'),
    step: z.string(),
    message: z.string(),
    error_code: z.string().nullable().optional(),
    display_message: z.string().nullable().optional(),
    error_type: z.string().nullable().optional(),
    error_detail: z.string().nullable().optional(),
  }),
]);

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
 * frame parse, and schema-validated yield. `streamSSE` and `streamAnalyze` delegate
 * here so the auth/error/parse logic lives in exactly one place.
 *
 * @param errorLabel prefix for the generic non-auth error (`SSE ` / `Analyze SSE `).
 */
async function* _postSSEStream<S extends z.ZodType>(
  url: string,
  body: string | object,
  signal: AbortSignal | undefined,
  errorLabel: string,
  schema: S,
): AsyncGenerator<z.output<S>> {
  const res = await fetch(url, {
    method: 'POST',
    // Include the jarvis_session cookie.
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
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
    // 5xx stays generic — the backend deliberately scrubs server-error bodies.
    if (res.status >= 500) {
      throw new Error(`${errorLabel}${res.status}: Server error`);
    }
    let detail = '';
    try {
      const body = await decodeResponseJson(res, url, apiErrorDetailSchema);
      if (typeof body.detail === 'string') {
        detail = body.detail;
      } else if (body.detail != null) {
        detail = JSON.stringify(body.detail);
      }
    } catch {
      /* keep the generic message */
    }
    throw new Error(`${errorLabel}${res.status}: Request failed${detail ? ` — ${detail}` : ''}`);
  }

  for await (const data of parseSSEFrames(res)) {
    try {
      const payload: unknown = JSON.parse(data);
      const parsed = schema.safeParse(payload);
      if (parsed.success) {
        yield parsed.data;
      } else {
        console.warn('[sse] malformed frame skipped', data.slice(0, 120));
      }
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
  return _postSSEStream(url, body, signal, 'SSE ', streamEventSchema);
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
  return _postSSEStream(
    `/api/papers/${paperId}/analyze`,
    '{}',
    signal,
    'Analyze SSE ',
    analyzeEventSchema,
  );
}
