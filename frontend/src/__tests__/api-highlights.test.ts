import { describe, it, expect, vi, beforeEach } from 'vitest';
import { listHighlights, createHighlight } from '@/lib/api';
import type { Highlight, HighlightCreate } from '@/types';

describe('highlights api', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('listHighlights GETs the paper-scoped collection and returns the array', async () => {
    const rows: Highlight[] = [
      {
        id: 1,
        paper_id: 7,
        page: 2,
        rect: {
          boundingRect: { x0: 0.1, y0: 0.2, x1: 0.3, y1: 0.4 },
          rects: [{ x0: 0.1, y0: 0.2, x1: 0.3, y1: 0.4 }],
        },
        note: 'hi',
        color: 'yellow',
        quote: 'q',
        created_at: '2026-01-01T00:00:00Z',
      },
    ];
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(rows), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const result = await listHighlights(7);

    expect(spy).toHaveBeenCalledWith('/api/papers/7/highlights', expect.any(Object));
    expect(result).toEqual(rows);
  });

  it('createHighlight POSTs the JSON body and returns the created highlight', async () => {
    const body: HighlightCreate = {
      page: 2,
      rect: { boundingRect: { x0: 0.1, y0: 0.2, x1: 0.3, y1: 0.4 }, rects: [] },
      note: 'note',
    };
    const created: Highlight = {
      id: 9,
      paper_id: 7,
      page: 2,
      rect: body.rect,
      note: 'note',
      color: null,
      quote: null,
      created_at: '2026-01-01T00:00:00Z',
    };
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(created), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const result = await createHighlight(7, body);

    expect(spy).toHaveBeenCalledWith(
      '/api/papers/7/highlights',
      expect.objectContaining({ method: 'POST', body: JSON.stringify(body) }),
    );
    expect(result).toEqual(created);
  });
});
