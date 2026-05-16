/**
 * Unit tests for sw-cache-policy (Wave 3 P1a).
 *
 * The SECURITY-CRITICAL assertions here are the NON-GOAL denials: chat/RAG,
 * discovery/process/embedding, streams, mutations, exports MUST NEVER be
 * runtime-cached. Read surfaces (library/detail/notes-read/extractions/stats)
 * MUST be cacheable.
 */
import { describe, it, expect } from 'vitest';
import { isCacheableApiRequest } from '@/lib/sw-cache-policy';

describe('isCacheableApiRequest — offline-capable READ surfaces (cacheable)', () => {
  const cacheableGets = [
    '/api/papers',
    '/api/papers/',
    '/api/papers?status=inbox',
    '/api/papers/brief',
    '/api/papers/brief?search=neural',
    '/api/papers/feed?surface=library',
    '/api/papers/feed/counts',
    '/api/papers/123',
    '/api/papers/123?include=summary',
    '/api/papers/123/notes',
    '/api/papers/123/notes?source=zotero',
    '/api/extractions/table?template_id=4',
    '/api/dashboard/metrics',
    '/api/stats',
  ];
  for (const url of cacheableGets) {
    it(`GET ${url} → cacheable`, () => {
      expect(isCacheableApiRequest('GET', url)).toBe(true);
    });
  }
});

describe('isCacheableApiRequest — NON-GOAL endpoints (NEVER cacheable)', () => {
  const nonGoalGets = [
    // RAG / chat / cross-paper Q&A
    '/api/ask/stream',
    '/api/ask',
    '/api/papers/123/ask/stream',
    '/api/papers/123/analyze',
    '/api/chat',
    // discovery / fetch / process / embedding (model layer)
    '/api/discover',
    '/api/discover?q=transformers',
    '/api/generate',
    '/api/summarize/123',
    '/api/process-pdf/123',
    '/api/papers/batch-process',
    '/api/papers/process_batch',
    '/api/papers/batch-summarize?limit=10',
    '/api/extract-entities/123',
    '/api/extractions/batch',
    '/api/contradictions?query=x',
    '/api/reembed',
    // streams
    '/api/jobs/abc/stream',
    // exports / downloads / raw assets
    '/api/export/anki/5',
    '/api/download-pdf/123',
    '/api/snapshots/123/2',
    // auth lifecycle
    '/api/auth/logout',
    '/api/auth/verify',
  ];
  for (const url of nonGoalGets) {
    it(`GET ${url} → NOT cacheable`, () => {
      expect(isCacheableApiRequest('GET', url)).toBe(false);
    });
  }
});

describe('isCacheableApiRequest — method + non-API guards', () => {
  it('rejects non-GET methods even on safelisted paths', () => {
    for (const m of ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']) {
      expect(isCacheableApiRequest(m, '/api/papers/123')).toBe(false);
      expect(isCacheableApiRequest(m, '/api/papers/123/notes')).toBe(false);
    }
  });

  it('mutations on paper sub-resources are not cacheable (PUT save/skip/etc.)', () => {
    expect(isCacheableApiRequest('PUT', '/api/papers/123/save')).toBe(false);
    expect(isCacheableApiRequest('PUT', '/api/papers/123/skip')).toBe(false);
  });

  it('non-/api/ paths are not handled by the API runtime cache', () => {
    expect(isCacheableApiRequest('GET', '/index.html')).toBe(false);
    expect(isCacheableApiRequest('GET', '/assets/index-abc123.js')).toBe(false);
    expect(isCacheableApiRequest('GET', '/')).toBe(false);
  });

  it('unknown/unsafelisted /api/ GETs default to NOT cacheable (default-deny)', () => {
    expect(isCacheableApiRequest('GET', '/api/config')).toBe(false);
    expect(isCacheableApiRequest('GET', '/api/admin/users')).toBe(false);
    expect(isCacheableApiRequest('GET', '/api/pulse/today')).toBe(false);
  });

  it('handles absolute URLs (origin stripped)', () => {
    expect(
      isCacheableApiRequest('GET', 'https://app.example.com/api/papers/9'),
    ).toBe(true);
    expect(
      isCacheableApiRequest('GET', 'https://app.example.com/api/ask/stream'),
    ).toBe(false);
  });
});
