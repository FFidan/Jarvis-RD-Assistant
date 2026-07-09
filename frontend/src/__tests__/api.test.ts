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
import { useMaintenanceStore } from '@/stores/maintenance-store';

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

describe('apiFetch — 503 maintenance interceptor', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useMaintenanceStore.getState().clear();
  });

  it('sets maintenance active on a 503 with the machine-readable "Restore in progress" detail', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Restore in progress', retry_after: 45 }), {
        status: 503,
        headers: { 'Content-Type': 'application/json', 'retry-after': '45' },
      }),
    );

    await expect(apiFetch('/api/test')).rejects.toThrow(ApiError);

    const state = useMaintenanceStore.getState();
    expect(state.active).toBe(true);
    expect(state.retryAfterS).toBe(45);
  });

  it('does NOT set maintenance for a 503 with a different (non-machine-readable) detail', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'something else' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(apiFetch('/api/test')).rejects.toThrow(ApiError);
    expect(useMaintenanceStore.getState().active).toBe(false);
  });

  it('does not throw a parse error and does not set maintenance for a non-JSON 503 body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('<html>Bad Gateway</html>', { status: 503 }),
    );

    await expect(apiFetch('/api/test')).rejects.toThrow(ApiError);
    expect(useMaintenanceStore.getState().active).toBe(false);
  });

  it('does not clear maintenance state on a 2xx response (health endpoints are maintenance-exempt)', async () => {
    useMaintenanceStore.getState().setMaintenance(true, 30);
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await apiFetch('/health/paper_ingestion/internal');

    expect(useMaintenanceStore.getState().active).toBe(true);
  });

  it('preserves `since` across repeated setMaintenance calls during one restore', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Restore in progress' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await apiFetch('/api/test').catch(() => undefined);
    const firstSince = useMaintenanceStore.getState().since;
    expect(firstSince).not.toBeNull();

    await apiFetch('/api/test').catch(() => undefined);
    expect(useMaintenanceStore.getState().since).toBe(firstSince);
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
      // Liveness probes — both ok
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
    const calledUrls = vi.mocked(globalThis.fetch).mock.calls.map(([url]) => String(url));
    expect(calledUrls).toContain('/health/paper_ingestion/live');
    expect(calledUrls).toContain('/health/learning_engine/live');
  });
});

describe('fetchStackHealth — degraded 503 internal body is informative', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("503 'degraded' body with maintenance:true → overall 'maintenance' (the restore DB-reload window)", async () => {
    // During STEP 5 of a restore the jarvis DB is mid-reload (ALLOW_CONNECTIONS
    // false / dropped), so the internal health endpoint returns HTTP 503
    // (degraded) — but its body still reports maintenance:true. The rollup must
    // reflect 'maintenance', not fall to all-'unknown'.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        return new Response(
          JSON.stringify({
            status: 'degraded',
            checks: { postgres: 'unavailable', qdrant: 'ok', ollama: 'ok', litellm: 'ok', vector: 'ok' },
            maintenance: true,
            version: '1.0.4',
          }),
          { status: 503, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const summary = await fetchStackHealth();

    expect(summary.overall).toBe('maintenance');
    expect(summary.maintenance).toBe(true);
    expect(summary.version).toBe('1.0.4');
  });

  it("503 'degraded' body without maintenance surfaces the real degraded deps (not all-unknown)", async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        return new Response(
          JSON.stringify({
            status: 'degraded',
            checks: { postgres: 'ok', qdrant: 'unavailable', ollama: 'ok', litellm: 'ok', vector: 'ok' },
            maintenance: false,
          }),
          { status: 503, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const summary = await fetchStackHealth();

    // The real 'unavailable' qdrant → 'down' is surfaced, not masked as all-unknown.
    expect(summary.services.find((s) => s.name === 'qdrant')?.status).toBe('down');
    expect(summary.overall).toBe('down');
    expect(summary.maintenance).toBe(false);
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
      // Both service liveness probes report ok.
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

  it('keeps service rows alive when dependency readiness is degraded', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        return new Response(null, { status: 503 });
      }
      if (u.includes('/health/paper_ingestion/live') || u.includes('/health/learning_engine/live')) {
        return new Response(JSON.stringify({ status: 'ok' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(null, { status: 404 });
    });

    const summary = await fetchStackHealth();

    expect(summary.services.find((s) => s.name === 'paper_ingestion')?.status).toBe('ok');
    expect(summary.services.find((s) => s.name === 'learning_engine')?.status).toBe('ok');
    expect(summary.services.find((s) => s.name === 'litellm')?.status).toBe('unknown');
    expect(summary.overall).toBe('unknown');
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

  it("payload missing one dep key → only that dep 'unknown', others intact", async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        return new Response(
          JSON.stringify({
            status: 'ok',
            // 'ollama' key absent from the payload
            checks: { postgres: 'ok', qdrant: 'ok', litellm: 'ok', vector: 'ok' },
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

    expect(summary.services.find((s) => s.name === 'ollama')?.status).toBe('unknown');
    expect(summary.services.find((s) => s.name === 'postgres')?.status).toBe('ok');
    expect(summary.services.find((s) => s.name === 'qdrant')?.status).toBe('ok');
    expect(summary.services.find((s) => s.name === 'litellm')?.status).toBe('ok');
    // ollama is a required dep — the rollup honestly stays off 'ok'.
    expect(summary.overall).toBe('unknown');
  });
});

describe('fetchStackHealth — maintenance rollup', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("payload maintenance:true → overall 'maintenance' even with services down", async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        return new Response(
          JSON.stringify({
            status: 'ok',
            checks: { postgres: 'ok', qdrant: 'ok', ollama: 'ok', litellm: 'ok', vector: 'ok' },
            maintenance: true,
            version: '1.0.4',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      // Liveness probes 503 during the restore window.
      return new Response(null, { status: 503 });
    });

    const summary = await fetchStackHealth();

    // Maintenance takes precedence over the down rollup — the stack is
    // intentionally offline, not broken.
    expect(summary.overall).toBe('maintenance');
    expect(summary.maintenance).toBe(true);
    expect(summary.version).toBe('1.0.4');
    // Per-service statuses stay truthful underneath the rollup.
    expect(summary.downCount).toBe(2);
  });

  it('payload without a maintenance flag keeps the normal rollup', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/health/paper_ingestion/internal')) {
        return new Response(
          JSON.stringify({
            status: 'ok',
            checks: { postgres: 'ok', qdrant: 'ok', ollama: 'ok', litellm: 'ok', vector: 'ok' },
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

    expect(summary.overall).toBe('ok');
    expect(summary.maintenance).toBeUndefined();
    expect(summary.version).toBeUndefined();
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
    // The all-unknown fallback is not a real down/degraded count…
    expect(summary.downCount).toBe(0);
    expect(summary.degradedCount).toBe(0);
    // …and the maintenance state is UNKNOWN (undefined), not a definitive
    // "not in maintenance" — a timeout must never satisfy the banner's
    // `maintenance === false` clear-check, or it would flip-flop mid-restore.
    expect(summary.maintenance).toBeUndefined();
    expect(summary.version).toBeUndefined();

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
