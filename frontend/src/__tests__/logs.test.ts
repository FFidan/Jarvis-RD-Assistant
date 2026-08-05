import { describe, it, expect, vi, beforeEach } from 'vitest';
import { toast } from 'sonner';

// Mock sonner — logs.ts → sse-reader.ts now imports handleAuthFailure
// (from @/lib/api/core), which toasts via sonner on a genuine 401.
vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

const logoutMock = vi.fn();
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      getApiKey: vi.fn(() => null),
      isAuthenticated: true,
      logout: logoutMock,
    })),
  },
}));

import { streamCorrelation } from '@/lib/logs';

/** Resolve once the streamCorrelation async loop finishes (onDone fires). */
function runStream(): Promise<void> {
  return new Promise<void>((resolve) => {
    streamCorrelation('corr-1', {
      onEvent: () => {},
      onDone: () => resolve(),
    });
  });
}

describe('streamCorrelation 401 → handleAuthFailure', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.mocked(toast.error).mockClear();
    logoutMock.mockClear();
  });

  it('routes a 401 on the logs stream through handleAuthFailure (toast once, logout) then calls onDone', async () => {
    // Fresh debounce window so the toast always fires.
    vi.spyOn(Date, 'now').mockReturnValue(2_000_000_000_000);
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Unauthorized', { status: 401 }),
    );

    await runStream();

    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringMatching(/session expired/i),
      expect.objectContaining({ duration: 6000 }),
    );
    expect(logoutMock).toHaveBeenCalledTimes(1);
  });

  it('does NOT trigger logout on a non-auth (500) failure but still calls onDone', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(2_000_000_500_000);
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Server error', { status: 500 }),
    );

    await runStream();

    expect(toast.error).not.toHaveBeenCalled();
    expect(logoutMock).not.toHaveBeenCalled();
  });
});
