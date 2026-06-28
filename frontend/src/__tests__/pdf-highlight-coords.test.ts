import { describe, it, expect } from 'vitest';
import {
  storedRectToScaledPosition,
  scaledPositionToStoredRect,
} from '@/components/paper/pdf-highlight-coords';
import type { HighlightRect } from '@/types';

const sample: HighlightRect = {
  boundingRect: { x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.35 },
  rects: [
    { x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.25 },
    { x0: 0.1, y0: 0.26, x1: 0.4, y1: 0.35 },
  ],
};

describe('pdf-highlight-coords adapter', () => {
  // The library re-normalizes by the Scaled width/height, so any positive basis
  // must round-trip to the same normalized rect (zoom/scale independence).
  it.each([
    [612, 792],
    [1, 1],
    [2000, 1000],
  ])('round-trips storedRect → ScaledPosition → storedRect (basis %ix%i)', (w, h) => {
    const pos = storedRectToScaledPosition(sample, 3, w, h);
    const back = scaledPositionToStoredRect(pos);

    expect(back.page).toBe(3);
    expect(back.rect.boundingRect.x0).toBeCloseTo(sample.boundingRect.x0, 10);
    expect(back.rect.boundingRect.y0).toBeCloseTo(sample.boundingRect.y0, 10);
    expect(back.rect.boundingRect.x1).toBeCloseTo(sample.boundingRect.x1, 10);
    expect(back.rect.boundingRect.y1).toBeCloseTo(sample.boundingRect.y1, 10);

    expect(back.rect.rects).toHaveLength(sample.rects.length);
    sample.rects.forEach((expected, i) => {
      const got = back.rect.rects[i];
      expect(got).toBeDefined();
      expect(got?.x0).toBeCloseTo(expected.x0, 10);
      expect(got?.y0).toBeCloseTo(expected.y0, 10);
      expect(got?.x1).toBeCloseTo(expected.x1, 10);
      expect(got?.y1).toBeCloseTo(expected.y1, 10);
    });
  });

  it('stays on the library top-origin path (no second y-flip)', () => {
    const pos = storedRectToScaledPosition(sample, 1, 612, 792);
    // usePdfCoordinates:false keeps the viewport (top-origin) projection — the
    // backend already flipped pypdfium2 bottom-origin to top-origin.
    expect(pos.usePdfCoordinates).toBe(false);
    // A top-region rect (small y0) stays near the top (small y1), and y ordering
    // is preserved — a double flip would invert this.
    expect(pos.boundingRect.y1).toBeCloseTo(0.2 * 792, 6);
    expect(pos.boundingRect.y2).toBeCloseTo(0.35 * 792, 6);
    expect(pos.boundingRect.y1).toBeLessThan(pos.boundingRect.y2);
  });

  it('preserves the (1-indexed) page number both directions', () => {
    const pos = storedRectToScaledPosition(sample, 7, 100, 100);
    expect(pos.boundingRect.pageNumber).toBe(7);
    expect(pos.rects.every((r) => r.pageNumber === 7)).toBe(true);
    expect(scaledPositionToStoredRect(pos).page).toBe(7);
  });
});
