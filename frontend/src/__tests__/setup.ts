import '@testing-library/jest-dom';

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
