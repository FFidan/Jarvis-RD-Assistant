import { describe, it, expect, vi, beforeEach } from 'vitest';
import { apiFetch, ApiError, searchPreview } from '@/lib/api';

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
