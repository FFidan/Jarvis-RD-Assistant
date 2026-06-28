/**
 * Coordinate adapter between JARVIS's stored highlight geometry and the
 * `react-pdf-highlighter-extended` `ScaledPosition` model.
 *
 * Stored shape (canonical DB store, `paper_highlights.rect`):
 *   `{ boundingRect:{x0,y0,x1,y1}, rects:[{x0,y0,x1,y1}] }`
 *   — normalized to [0, 1], TOP-origin. The backend bridge (pypdfium2) already
 *   y-flipped from PDF bottom-origin to top-origin, so there is NO second flip
 *   here (`usePdfCoordinates: false` keeps the library on its top-origin path).
 *
 * Library `Scaled` stores px relative to a `width × height` viewport, top-origin.
 * When projecting to the live viewport the library normalizes by that very
 * `width`/`height` (`scaledToViewport`: `x = viewportW * x1 / width`), so the
 * basis we pass cancels and only the [0, 1] ratio survives — overlays are
 * pixel-identical for any positive basis (proven by the round-trip test). We
 * therefore use a unit basis and avoid an async per-page `getViewport` round
 * trip that would have no visual effect.
 */
import type { Scaled, ScaledPosition } from 'react-pdf-highlighter-extended';
import type { HighlightRect, Rect } from '@/types';

/** Convert a stored normalized rect into the library's `ScaledPosition`. */
export function storedRectToScaledPosition(
  rect: HighlightRect,
  page: number,
  width: number,
  height: number,
): ScaledPosition {
  const toScaled = (r: Rect): Scaled => ({
    x1: r.x0 * width,
    y1: r.y0 * height,
    x2: r.x1 * width,
    y2: r.y1 * height,
    width,
    height,
    pageNumber: page,
  });
  return {
    boundingRect: toScaled(rect.boundingRect),
    rects: rect.rects.map(toScaled),
    usePdfCoordinates: false,
  };
}

/**
 * Reverse a library `ScaledPosition` (e.g. from a user text selection) back to
 * the stored normalized [0, 1] rect + page, so the DB always holds normalized
 * geometry regardless of the zoom level the selection was made at.
 */
export function scaledPositionToStoredRect(pos: ScaledPosition): {
  page: number;
  rect: HighlightRect;
} {
  const toRect = (s: Scaled): Rect => ({
    x0: s.x1 / s.width,
    y0: s.y1 / s.height,
    x1: s.x2 / s.width,
    y1: s.y2 / s.height,
  });
  return {
    page: pos.boundingRect.pageNumber,
    rect: {
      boundingRect: toRect(pos.boundingRect),
      rects: pos.rects.map(toRect),
    },
  };
}
