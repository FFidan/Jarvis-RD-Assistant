import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  apiFetch,
  apiFetchRaw,
  ApiError,
  fetchContradictions,
  fetchSystemModels,
  fetchStackHealth,
  batchProcessPapers,
  downloadMyData,
  promoteZoteroNote,
  scanContradictions,
  scanPaperContradictions,
  searchPreview,
  fetchFeed,
  fetchFeedCounts,
} from '@/lib/api';

describe('apiFetch', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('adds Content-Type header to requests (API key injected by nginx)', async () => {
    const mockResponse = { data: 'test' };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await apiFetch('/api/test');

    expect(globalThis.fetch).toHaveBeenCalledWith('/api/test', expect.objectContaining({
      headers: expect.objectContaining({
        'Content-Type': 'application/json',
      }),
    }));
  });

  it('throws ApiError on non-ok response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Not found', { status: 404 }),
    );

    await expect(apiFetch('/api/missing')).rejects.toThrow(ApiError);
    await vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Not found', { status: 404 }),
    );

    try {
      await apiFetch('/api/missing');
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(404);
    }
  });

  it('throws on network error', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(apiFetch('/api/test')).rejects.toThrow('Failed to fetch');
  });

  it('translates timeout AbortError into ApiError(0, timed out)', async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => {
      return new Promise((_resolve, reject) => {
        const signal = init?.signal as AbortSignal | undefined;
        if (signal) {
          signal.addEventListener('abort', () => {
            reject(new DOMException('The user aborted a request.', 'AbortError'));
          });
        }
      });
    });

    const promise = apiFetch('/api/test');
    vi.advanceTimersByTime(300_001); // fire the 5-min timeout
    await expect(promise).rejects.toBeInstanceOf(ApiError);
    const err = (await promise.catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/timed out/i);
    vi.useRealTimers();
  });

  it('re-throws caller-initiated AbortError unchanged (not wrapped as ApiError)', async () => {
    const callerController = new AbortController();
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => {
      return new Promise((_resolve, reject) => {
        const signal = init?.signal as AbortSignal | undefined;
        if (signal) {
          signal.addEventListener('abort', () => {
            reject(new DOMException('The user aborted a request.', 'AbortError'));
          });
        }
      });
    });

    const promise = apiFetch('/api/test', { signal: callerController.signal });
    callerController.abort();
    const err = await promise.catch((e: unknown) => e);
    // Should NOT be an ApiError — it is a raw DOMException
    expect(err).toBeInstanceOf(DOMException);
    expect(err).not.toBeInstanceOf(ApiError);
  });

  it('searchPreview posts to the preview endpoint without side effects', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await searchPreview('Neural ODE', 'semantic_scholar', 10);

    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/search-preview',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          query: 'Neural ODE',
          source_types: ['semantic_scholar'],
          max_results: 10,
          year_from: null,
          year_to: null,
          sort_by: 'relevance',
          author: null,
        }),
      }),
    );
  });

  it('builds contradiction list and scan endpoint requests', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ contradictions: [], total: 0 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await fetchContradictions({ paper_id: 42, status: 'verified', limit: 20 });

    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      '/api/contradictions?paper_id=42&status=verified&limit=20',
      expect.objectContaining({
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    );

    vi.mocked(globalThis.fetch).mockResolvedValue(
      new Response(JSON.stringify({ job_id: 'job-global', status: 'queued' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await scanContradictions({ limit: 10 });
    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      '/api/contradictions/scan',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ limit: 10 }),
      }),
    );

    vi.mocked(globalThis.fetch).mockResolvedValue(
      new Response(JSON.stringify({ job_id: 'job-paper', status: 'queued' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await scanPaperContradictions(42, { limit: 20 });
    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      '/api/papers/42/contradictions/scan',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ limit: 20 }),
      }),
    );
  });

  it('builds Zotero note promotion request', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 7,
          paper_id: 42,
          user_note: 'note',
          highlight_text: 'quote',
          page_number: 2,
          source: 'zotero',
          zotero_annotation_key: 'ANN',
          verification_status: 'verified',
          verified_quote: 'quote',
          verified_page_number: 2,
          promoted_at: '2026-01-02T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    );

    await promoteZoteroNote(7);

    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      '/api/notes/7/promote',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('maps corpus library scope to all_non_trash feed view', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ papers: [], total: 0 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await fetchFeed({ view: 'library', scope: 'corpus', limit: 10, offset: 20 });

    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      '/api/papers/feed?view=all_non_trash&scope=corpus&limit=10&offset=20&include_zotero_notes=true',
      expect.objectContaining({
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    );
  });

  it('preserves explicit library filters when corpus scope is selected', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ papers: [], total: 0 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await fetchFeed({ view: 'library', scope: 'corpus', filter: 'starred', limit: 5, offset: 0 });

    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      '/api/papers/feed?view=starred&scope=corpus&limit=5&offset=0&include_zotero_notes=true',
      expect.objectContaining({
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    );
  });

  it('sends topic_id when topicId is provided', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ papers: [], total: 0 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await fetchFeed({ view: 'library', limit: 10, offset: 0, topicId: 5 });

    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      expect.stringContaining('topic_id=5'),
      expect.anything(),
    );
  });

  it('omits topic_id when topicId is null', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ papers: [], total: 0 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await fetchFeed({ view: 'library', limit: 10, offset: 0, topicId: null });

    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      expect.not.stringContaining('topic_id'),
      expect.anything(),
    );
  });

  it('sends both source_types and topic_id when both are provided', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ papers: [], total: 0 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await fetchFeed({
      view: 'inbox',
      limit: 10,
      offset: 0,
      sourceTypes: 'arxiv',
      topicId: 7,
    });

    const _calls = vi.mocked(globalThis.fetch).mock.calls;
    const url = _calls[_calls.length - 1]?.[0];
    expect(url).toEqual(expect.stringContaining('source_types=arxiv'));
    expect(url).toEqual(expect.stringContaining('topic_id=7'));
  });

  it('sends batch-process limit as a query parameter', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ queued: 0, total_unprocessed: 0, skipped_missing_pdf: 0, job_id: null }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await batchProcessPapers(7);

    const calls = vi.mocked(globalThis.fetch).mock.calls;
    const [url, init] = calls[calls.length - 1] ?? [];
    expect(url).toBe('/api/papers/batch-process?limit=7');
    expect(init).toEqual(expect.objectContaining({ method: 'POST' }));
    expect((init as RequestInit).body).toBeUndefined();
  });
});


describe('downloadMyData', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('downloads the authenticated account export from /api/me/export', async () => {
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const createUrlSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:account-export');
    const revokeUrlSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const createElementSpy = vi.spyOn(document, 'createElement');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('zip', {
        status: 200,
        headers: { 'Content-Disposition': 'attachment; filename="jarvis-data-export.zip"' },
      }),
    );

    await downloadMyData();

    const anchor = createElementSpy.mock.results
      .map((result) => result.value)
      .find((node): node is HTMLAnchorElement => node instanceof HTMLAnchorElement);
    expect(globalThis.fetch).toHaveBeenLastCalledWith(
      '/api/me/export',
      expect.objectContaining({ credentials: 'include' }),
    );
    expect(anchor?.download).toBe('jarvis-data-export.zip');
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(createUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeUrlSpy).toHaveBeenCalledWith('blob:account-export');
  });

  it('uses a neutral filename if a stale server header contains a user id', async () => {
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:account-export');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const createElementSpy = vi.spyOn(document, 'createElement');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('zip', {
        status: 200,
        headers: { 'Content-Disposition': 'attachment; filename="jarvis-export-user-42.zip"' },
      }),
    );

    await downloadMyData();

    const anchor = createElementSpy.mock.results
      .map((result) => result.value)
      .find((node): node is HTMLAnchorElement => node instanceof HTMLAnchorElement);
    expect(anchor?.download).toBe('jarvis-data-export.zip');
  });
});

describe('apiFetchRaw', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('returns raw Response on success', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('binary', { status: 200 }),
    );

    const res = await apiFetchRaw('/api/export/anki/1');
    expect(res).toBeInstanceOf(Response);
    expect(res.status).toBe(200);
  });

  it('throws ApiError on non-ok response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Not found', { status: 404 }),
    );

    let caught: unknown;
    try {
      await apiFetchRaw('/api/export/anki/999');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(404);
  });

  it('translates timeout AbortError into ApiError(0, timed out)', async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => {
      return new Promise((_resolve, reject) => {
        const signal = init?.signal as AbortSignal | undefined;
        if (signal) {
          signal.addEventListener('abort', () => {
            reject(new DOMException('The user aborted a request.', 'AbortError'));
          });
        }
      });
    });

    const promise = apiFetchRaw('/api/export/anki/1');
    vi.advanceTimersByTime(300_001); // fire the 5-min timeout
    await expect(promise).rejects.toBeInstanceOf(ApiError);
    const err = (await promise.catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/timed out/i);
    vi.useRealTimers();
  });

  it('re-throws caller-initiated AbortError unchanged (not wrapped as ApiError)', async () => {
    const callerController = new AbortController();
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => {
      return new Promise((_resolve, reject) => {
        const signal = init?.signal as AbortSignal | undefined;
        if (signal) {
          signal.addEventListener('abort', () => {
            reject(new DOMException('The user aborted a request.', 'AbortError'));
          });
        }
      });
    });

    const promise = apiFetchRaw('/api/export/anki/1', { signal: callerController.signal });
    callerController.abort();
    const err = await promise.catch((e: unknown) => e);
    // Should NOT be an ApiError — it is a raw DOMException
    expect(err).toBeInstanceOf(DOMException);
    expect(err).not.toBeInstanceOf(ApiError);
  });
});

describe('fetchSystemModels', () => {
  it('is exported from api.ts as a function', () => {
    expect(typeof fetchSystemModels).toBe('function');
  });

  it('calls /api/system/models and passes the signal through', async () => {
    const mockData = { status: 'ok', catalog: [], hardware: {} };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockData), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const controller = new AbortController();
    const result = await fetchSystemModels(controller.signal);
    expect(result).toEqual(mockData);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/system/models',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });
});

describe('fetchStackHealth — toStatus degraded branch', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("maps 'degraded' check status to ServiceHealthStatus 'degraded'", async () => {
    // paper_ingestion and learning_engine are ok; one dep reports 'degraded'
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        return new Response(
          JSON.stringify({ status: 'ok', checks: { postgres: 'ok', qdrant: 'degraded', ollama: 'ok', litellm: 'ok', vector: 'ok' } }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      // Public health probes — both ok
      return new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const summary = await fetchStackHealth();

    const qdrantService = summary.services.find((s) => s.name === 'qdrant');
    expect(qdrantService?.status).toBe('degraded');
    expect(summary.degradedCount).toBe(1);
    expect(summary.downCount).toBe(0);
    // Overall rolls up to 'degraded' when any dep is degraded but none are down
    expect(summary.overall).toBe('degraded');
  });
});

describe('fetchStackHealth — unknown-aware rollup', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("internal fails → required deps unknown, public ok → overall NOT 'ok'", async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        // Internal endpoint unreachable → every dep is unknown.
        return new Response(null, { status: 503 });
      }
      // Both public service probes report ok.
      return new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const summary = await fetchStackHealth();

    // Required deps could not be verified → never a false "All healthy".
    expect(summary.overall).not.toBe('ok');
    expect(summary.overall).toBe('unknown');
    // Public services still reported ok from their own probes.
    expect(summary.services.find((s) => s.name === 'paper_ingestion')?.status).toBe('ok');
    expect(summary.services.find((s) => s.name === 'learning_engine')?.status).toBe('ok');
    // Required deps are unknown, not silently healthy.
    expect(summary.services.find((s) => s.name === 'postgres')?.status).toBe('unknown');
    // 'unknown' is not a real down/degraded count.
    expect(summary.downCount).toBe(0);
    expect(summary.degradedCount).toBe(0);
  });

  it("only the optional 'vector' dep unknown → overall stays 'ok'", async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        return new Response(
          JSON.stringify({
            status: 'ok',
            checks: { postgres: 'ok', qdrant: 'ok', ollama: 'ok', litellm: 'ok', vector: 'unknown' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const summary = await fetchStackHealth();

    // Vector is optional — an unknown vector alone never blocks "All healthy".
    expect(summary.overall).toBe('ok');
    expect(summary.services.find((s) => s.name === 'vector')?.status).toBe('unknown');
  });
});

describe('fetchStackHealth — hard deadline (no-response fallback)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('settles to an all-unknown degraded summary when probes hang past the deadline', async () => {
    vi.useFakeTimers();
    // Health probes never respond (network black-hole) — fetch stays pending
    // forever (the request also never hits its own abort here).
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      () => new Promise<Response>(() => {}),
    );

    const promise = fetchStackHealth();
    // Advance past the 5s health deadline so the race resolves to the fallback.
    await vi.advanceTimersByTimeAsync(5001);
    const summary = await promise;

    // Every service reports 'unknown' (structured degraded state, not a hang)
    expect(summary.services.map((s) => s.name)).toEqual([
      'paper_ingestion',
      'learning_engine',
      'postgres',
      'qdrant',
      'ollama',
      'litellm',
      'vector',
    ]);
    expect(summary.services.every((s) => s.status === 'unknown')).toBe(true);
    expect(summary.overall).toBe('unknown');
    // The all-unknown fallback is not a real down/degraded count.
    expect(summary.downCount).toBe(0);
    expect(summary.degradedCount).toBe(0);

    vi.useRealTimers();
  });
});

describe('fetchFeedCounts', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('hits /api/papers/feed/counts with no params when scope is undefined', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ inbox: 0 }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    await fetchFeedCounts();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/papers/feed/counts',
      expect.anything(),
    );
  });

  it('hits /api/papers/feed/counts?scope=library when scope=library', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ inbox: 0 }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    await fetchFeedCounts('library');
    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/papers/feed/counts?scope=library',
      expect.anything(),
    );
  });

  it('hits /api/papers/feed/counts?scope=corpus when scope=corpus', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ inbox: 0 }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    await fetchFeedCounts('corpus');
    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/papers/feed/counts?scope=corpus',
      expect.anything(),
    );
  });
});
