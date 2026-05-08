/**
 * Logs API client and types.
 *
 * All functions follow the same fetch/auth patterns as lib/api.ts.
 */

import { useAuthStore } from '@/stores/auth-store';
import { apiFetch } from '@/lib/api';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SystemEvent {
  id: number;
  created_at: string;
  level: 'debug' | 'info' | 'warning' | 'error' | 'critical';
  category: 'error' | 'job' | 'source' | 'auth' | 'config' | 'infra';
  source: string;
  message: string;
  context: Record<string, unknown>;
  correlation_id: string | null;
}

export interface LogsListResponse {
  events: SystemEvent[];
  next_cursor: number | null;
}

export interface LogsSummary {
  by_level: Record<string, number>;
  by_category: Record<string, number>;
  total: number;
}

export interface ListEventsParams {
  level?: string;
  category?: string;
  source?: string;
  since?: string;
  until?: string;
  cursor?: number;
  limit?: number;
  q?: string;
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

export async function listEvents(params: ListEventsParams = {}): Promise<LogsListResponse> {
  const qs = new URLSearchParams();
  if (params.level) qs.set('level', params.level);
  if (params.category) qs.set('category', params.category);
  if (params.source) qs.set('source', params.source);
  if (params.since) qs.set('since', params.since);
  if (params.until) qs.set('until', params.until);
  if (params.cursor != null) qs.set('cursor', String(params.cursor));
  if (params.limit != null) qs.set('limit', String(params.limit));
  if (params.q) qs.set('q', params.q);
  const query = qs.toString();
  return apiFetch<LogsListResponse>(`/api/logs/events${query ? `?${query}` : ''}`);
}

export async function getEvent(id: number): Promise<SystemEvent> {
  return apiFetch<SystemEvent>(`/api/logs/events/${id}`);
}

export async function getSummary(): Promise<LogsSummary> {
  return apiFetch<LogsSummary>('/api/logs/summary');
}

export async function getCorrelation(correlationId: string): Promise<SystemEvent[]> {
  return apiFetch<SystemEvent[]>(`/api/logs/correlation/${encodeURIComponent(correlationId)}`);
}

export async function getLogsSources(): Promise<string[]> {
  return apiFetch<string[]>('/api/logs/sources');
}

// ---------------------------------------------------------------------------
// SSE streaming for a correlation chain
// ---------------------------------------------------------------------------

export interface StreamCorrelationOpts {
  since?: number;
  onEvent: (e: SystemEvent) => void;
  onDone: () => void;
}

export function streamCorrelation(
  correlationId: string,
  opts: StreamCorrelationOpts,
): { close: () => void } {
  const apiKey = useAuthStore.getState().getApiKey();
  const controller = new AbortController();

  const qs = new URLSearchParams();
  if (opts.since != null) qs.set('since', String(opts.since));
  const query = qs.toString();
  const url = `/api/logs/stream/${encodeURIComponent(correlationId)}${query ? `?${query}` : ''}`;

  (async () => {
    try {
      const res = await fetch(url, {
        method: 'GET',
        headers: apiKey ? { 'X-API-Key': apiKey } : {},
        signal: controller.signal,
      });

      if (!res.ok || !res.body) {
        opts.onDone();
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() ?? '';
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const raw = line.slice(6).trim();
            if (raw === '[DONE]') {
              opts.onDone();
              return;
            }
            try {
              opts.onEvent(JSON.parse(raw) as SystemEvent);
            } catch {
              /* skip malformed frames */
            }
          }
        }
      } finally {
        await reader.cancel().catch(() => {});
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      // swallow other errors — caller sees onDone
    }
    opts.onDone();
  })();

  return { close: () => controller.abort() };
}
