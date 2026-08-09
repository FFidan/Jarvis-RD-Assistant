/**
 * Logs API client and types.
 *
 * All functions follow the same fetch/auth patterns as lib/api.ts.
 */

import { apiFetchJson } from '@/lib/api/core';
import {
  logsListResponseSchema,
  logsSummarySchema,
  systemEventSchema,
} from '@/lib/api/schemas/logs';
// Leaf import (not the barrel): handleAuthFailure is an internal helper, not
// part of @/lib/api's public surface.
import { handleAuthFailure } from '@/lib/api/core';
import { createSSEReader, SSEGetError } from '@/lib/sse-reader';
import { z } from 'zod';

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
  return apiFetchJson(`/api/logs/events${query ? `?${query}` : ''}`, logsListResponseSchema);
}

export async function getEvent(id: number): Promise<SystemEvent> {
  return apiFetchJson(`/api/logs/events/${id}`, systemEventSchema);
}

export async function getSummary(opts?: { excludeInfra?: boolean }): Promise<LogsSummary> {
  const qs = opts?.excludeInfra ? '?exclude_infra=1' : '';
  return apiFetchJson(`/api/logs/summary${qs}`, logsSummarySchema);
}

export async function getCorrelation(correlationId: string): Promise<SystemEvent[]> {
  return apiFetchJson(`/api/logs/correlation/${encodeURIComponent(correlationId)}`, systemEventSchema.array());
}

export async function getLogsSources(): Promise<string[]> {
  return apiFetchJson('/api/logs/sources', z.string().array());
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
  const controller = new AbortController();

  const qs = new URLSearchParams();
  if (opts.since != null) qs.set('since', String(opts.since));
  const query = qs.toString();
  const url = `/api/logs/stream/${encodeURIComponent(correlationId)}${query ? `?${query}` : ''}`;

  (async () => {
    try {
      for await (const raw of createSSEReader(url, {
        signal: controller.signal,
      })) {
        try {
          const event = systemEventSchema.safeParse(JSON.parse(raw));
          if (event.success) opts.onEvent(event.data);
        } catch {
          /* skip malformed frames */
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      // Route a genuine auth failure through the centralized handler
      // (debounced session-expired toast + logout) before surfacing onDone.
      if (err instanceof SSEGetError) handleAuthFailure(err.status);
      // swallow other errors — caller sees onDone
    }
    opts.onDone();
  })();

  return { close: () => controller.abort() };
}
