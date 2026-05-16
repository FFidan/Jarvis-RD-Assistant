/**
 * paper-scroll-spy.test.tsx
 * Tests for the usePaperScrollSpy hook.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePaperScrollSpy } from '@/hooks/paper-scroll-spy';

// ---- IntersectionObserver mock -------------------------------------------

type IOCallback = (entries: IntersectionObserverEntry[]) => void;
let capturedCallback: IOCallback | null = null;
const observedIds: string[] = [];

class MockIntersectionObserver {
  constructor(callback: IOCallback) {
    capturedCallback = callback;
  }
  observe(el: Element) {
    observedIds.push(el.id);
  }
  disconnect() {
    capturedCallback = null;
    observedIds.length = 0;
  }
}

vi.stubGlobal('IntersectionObserver', MockIntersectionObserver);

function makeEntry(id: string, isIntersecting: boolean): IntersectionObserverEntry {
  const el = document.createElement('section');
  el.id = id;
  document.body.appendChild(el);
  return {
    target: el,
    isIntersecting,
    boundingClientRect: el.getBoundingClientRect(),
    intersectionRatio: isIntersecting ? 1 : 0,
    intersectionRect: el.getBoundingClientRect(),
    rootBounds: null,
    time: Date.now(),
  } as unknown as IntersectionObserverEntry;
}

afterEach(() => {
  document.body.innerHTML = '';
  capturedCallback = null;
  observedIds.length = 0;
});

describe('usePaperScrollSpy', () => {
  it('initializes with the first id as active', () => {
    const ids = ['section-brief', 'section-detailed', 'section-methodology'];
    const { result } = renderHook(() => usePaperScrollSpy(ids));
    expect(result.current).toBe('section-brief');
  });

  it('returns null when ids array is empty', () => {
    const { result } = renderHook(() => usePaperScrollSpy([]));
    expect(result.current).toBeNull();
  });

  it('updates activeId when a section becomes intersecting', () => {
    const ids = ['section-brief', 'section-detailed', 'section-methodology'];

    // Create DOM elements so observer.observe succeeds
    ids.forEach((id) => {
      const el = document.createElement('section');
      el.id = id;
      document.body.appendChild(el);
    });

    const { result } = renderHook(() => usePaperScrollSpy(ids));
    expect(result.current).toBe('section-brief');

    // Fire intersection for the second section
    act(() => {
      capturedCallback?.([makeEntry('section-detailed', true)]);
    });

    expect(result.current).toBe('section-detailed');
  });

  it('picks the topmost visible section in document order', () => {
    const ids = ['section-brief', 'section-detailed', 'section-methodology'];
    ids.forEach((id) => {
      const el = document.createElement('section');
      el.id = id;
      document.body.appendChild(el);
    });

    const { result } = renderHook(() => usePaperScrollSpy(ids));

    // Both sections become visible at the same time
    act(() => {
      capturedCallback?.([
        makeEntry('section-methodology', true),
        makeEntry('section-detailed', true),
      ]);
    });

    // section-detailed comes before section-methodology in the ids array
    expect(result.current).toBe('section-detailed');
  });

  it('falls back to last active when section leaves viewport', () => {
    const ids = ['section-brief', 'section-detailed'];
    ids.forEach((id) => {
      const el = document.createElement('section');
      el.id = id;
      document.body.appendChild(el);
    });

    const { result } = renderHook(() => usePaperScrollSpy(ids));

    // Navigate to section-detailed
    act(() => {
      capturedCallback?.([makeEntry('section-detailed', true)]);
    });
    expect(result.current).toBe('section-detailed');

    // section-detailed leaves viewport but nothing else enters
    act(() => {
      capturedCallback?.([makeEntry('section-detailed', false)]);
    });

    // No visible section — activeId should remain at last known value
    // (the hook does not update when visible set is empty)
    expect(result.current).toBe('section-detailed');
  });
});
