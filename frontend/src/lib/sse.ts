/**
 * POST-based SSE streaming via fetch + ReadableStream.
 *
 * The backend uses POST for SSE endpoints (not GET), so we cannot use
 * the browser's EventSource API. Instead we read the response body as
 * a stream and parse SSE frames manually.
 *
 * SECURITY: X-API-Key header is included on every SSE request.
 */

import { useAuthStore } from '@/stores/auth-store';

export interface StreamEvent {
  type: 'token' | 'sources' | 'done' | 'error';
  content?: string;
  full_answer?: string;
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
}

export type AnalyzeEvent = AnalyzeStepEvent | AnalyzeCompleteEvent | AnalyzeErrorEvent;

async function* parseSSEFrames(response: Response): AsyncGenerator<string> {
  if (!response.body) {
    throw new Error('Response body is null — streaming not supported');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

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
}

export async function* streamSSE(
  url: string,
  body: object,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const apiKey = useAuthStore.getState().getApiKey();
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    throw new Error(`SSE ${res.status}: ${await res.text()}`);
  }

  for await (const data of parseSSEFrames(res)) {
    try {
      yield JSON.parse(data) as StreamEvent;
    } catch {
      /* skip malformed SSE frames */
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
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    },
    body: '{}',
    signal,
  });

  if (!res.ok) {
    throw new Error(`Analyze SSE ${res.status}: ${await res.text()}`);
  }

  for await (const data of parseSSEFrames(res)) {
    try {
      yield JSON.parse(data) as AnalyzeEvent;
    } catch {
      /* skip malformed SSE frames */
    }
  }
}
