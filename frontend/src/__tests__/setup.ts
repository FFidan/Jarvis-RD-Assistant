import 'fake-indexeddb/auto';
import '@testing-library/jest-dom';
import { afterEach, vi } from 'vitest';

// Radix's FocusScope schedules a zero-delay timer in its unmount cleanup that
// builds a CustomEvent and dispatches it at the container. Testing Library
// unmounts in its own afterEach, but that timer is still queued when the file
// ends, and if the jsdom environment is torn down first the event belongs to a
// dead realm: dispatchEvent rejects it and Vitest reports an uncaught
// TypeError. The run then exits non-zero with every assertion passed, and it
// blames whichever file happened to be running, not the one that armed the
// timer. Yielding one macrotask here drains the timer while the DOM is alive.
//
// Registered before any test file imports Testing Library, so under Vitest's
// reverse hook order this runs after its cleanup, not before.
afterEach(async () => {
  // A test that leaves fake timers installed would never resolve this.
  if (vi.isFakeTimers()) return;
  await new Promise((resolve) => setTimeout(resolve, 0));
});

// pdf.js cannot render under jsdom, so globally stub the in-PDF reader library
// and its worker asset. Tests that exercise the reader (PdfReaderPane.test)
// provide their own richer file-scoped vi.mock, which takes precedence.
vi.mock('react-pdf-highlighter-extended', () => ({
  PdfLoader: () => null,
  PdfHighlighter: () => null,
  TextHighlight: () => null,
  AreaHighlight: () => null,
  MonitoredHighlightContainer: () => null,
  useHighlightContainerContext: () => ({
    highlight: { id: '', position: { boundingRect: {}, rects: [] }, content: {} },
    isScrolledTo: false,
  }),
  usePdfHighlighterContext: () => ({
    getCurrentSelection: () => null,
    setTip: () => {},
    updateTipPosition: () => {},
  }),
}));
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: 'test-worker-url' }));

// jsdom does not implement IntersectionObserver — stub for usePaperScrollSpy and similar.
if (typeof IntersectionObserver === 'undefined') {
  global.IntersectionObserver = class IntersectionObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof IntersectionObserver;
}

// jsdom does not implement ResizeObserver — stub for Radix UI components (Slider, etc.).
if (typeof ResizeObserver === 'undefined') {
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// jsdom does not implement window.matchMedia — stub for useThemeEffect, etc.
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

// jsdom does not implement the Clipboard API — stub so tests can spy on writeText.
if (!navigator.clipboard) {
  Object.defineProperty(navigator, 'clipboard', {
    value: {
      writeText: vi.fn().mockResolvedValue(undefined),
      readText: vi.fn().mockResolvedValue(''),
    },
    configurable: true,
    writable: true,
  });
}

// jsdom does not implement AbortSignal.any — polyfill for tests.
if (typeof AbortSignal.any !== 'function') {
  AbortSignal.any = function any(signals: AbortSignal[]): AbortSignal {
    const controller = new AbortController();
    for (const signal of signals) {
      if (signal.aborted) {
        controller.abort(signal.reason);
        break;
      }
      signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true });
    }
    return controller.signal;
  };
}
