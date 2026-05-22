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
  /** Structured error class name from the backend (e.g. "PdfTooLargeError"). Optional — absent until W1.6-I backend ships. */
  error_type?: string | null;
  /** Human-readable detail string from the backend. Optional — absent until W1.6-I backend ships. */
  error_detail?: string | null;
}

export type AnalyzeEvent = AnalyzeStepEvent | AnalyzeCompleteEvent | AnalyzeErrorEvent;

async function* parseSSEFrames(response: Response): AsyncGenerator<string> {
  if (!response.body) {
    throw new Error('Response body is null — streaming not supported');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
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

export async function* streamSSE(
  url: string,
  body: object,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const apiKey = useAuthStore.getState().getApiKey();
  const res = await fetch(url, {
    method: 'POST',
    // WS-2A: include the jarvis_session cookie alongside any X-API-Key.
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    if (res.status === 401) {
      useAuthStore.getState().logout();
      throw new Error('Unauthorized — session ended');
    }
    if (res.status === 403) {
      throw new Error('Forbidden — you do not have permission to access this resource');
    }
    throw new Error(`SSE ${res.status}: ${res.status >= 500 ? 'Server error' : 'Request failed'}`);
  }

  for await (const data of parseSSEFrames(res)) {
    try {
      yield JSON.parse(data) as StreamEvent;
    } catch {
      console.warn('[sse] malformed frame skipped', data.slice(0, 120));
    }
  }
}

/**
 * Stream the compound analyze-paper endpoint (download → process → summarize).
 *
 * Yields AnalyzeEvent objects as the backend progresses through each step.
 */
export async function* streamAnalyze(
  paperId: number,
  signal?: AbortSignal,
): AsyncGenerator<AnalyzeEvent> {
  const apiKey = useAuthStore.getState().getApiKey();
  const res = await fetch(`/api/papers/${paperId}/analyze`, {
    method: 'POST',
    // WS-2A: include the jarvis_session cookie alongside any X-API-Key.
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    },
    body: '{}',
    signal,
  });

  if (!res.ok) {
    if (res.status === 401) {
      useAuthStore.getState().logout();
      throw new Error('Unauthorized — session ended');
    }
    if (res.status === 403) {
      throw new Error('Forbidden — you do not have permission to access this resource');
    }
    throw new Error(`Analyze SSE ${res.status}: ${res.status >= 500 ? 'Server error' : 'Request failed'}`);
  }

  for await (const data of parseSSEFrames(res)) {
    try {
      yield JSON.parse(data) as AnalyzeEvent;
    } catch {
      console.warn('[sse] malformed frame skipped', data.slice(0, 120));
    }
  }
}
