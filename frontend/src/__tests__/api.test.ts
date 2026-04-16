import { describe, it, expect, vi, beforeEach } from 'vitest';
import { apiFetch, apiFetchRaw, ApiError, searchPreview } from '@/lib/api';

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
          source: 'semantic_scholar',
          max_results: 10,
        }),
      }),
    );
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
