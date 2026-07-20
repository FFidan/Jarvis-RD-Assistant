import { describe, expect, it, vi } from 'vitest';

const entryMocks = vi.hoisted(() => {
  const authState = { isAuthenticated: false };
  return {
    authState,
    render: vi.fn(),
    registerServiceWorker: vi.fn(),
    requestPersistentStorage: vi.fn(),
    subscribe: vi.fn(),
    subscriber: undefined as (() => void) | undefined,
  };
});

vi.mock('react-dom/client', () => ({
  createRoot: () => ({ render: entryMocks.render }),
}));

vi.mock('@/providers/AppProviders', () => ({ AppProviders: () => null }));
vi.mock('@/App', () => ({ App: () => null }));
vi.mock('@/lib/pwa', () => ({
  registerServiceWorker: entryMocks.registerServiceWorker,
  requestPersistentStorage: entryMocks.requestPersistentStorage,
}));
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: () => entryMocks.authState,
    subscribe: entryMocks.subscribe.mockImplementation((listener: () => void) => {
      entryMocks.subscriber = listener;
      return vi.fn();
    }),
  },
}));

describe('PWA entry wiring', () => {
  it('waits for authentication before requesting persistent storage', async () => {
    await import('@/main');

    expect(entryMocks.registerServiceWorker).toHaveBeenCalledOnce();
    expect(entryMocks.requestPersistentStorage).not.toHaveBeenCalled();
    expect(entryMocks.subscribe).toHaveBeenCalledOnce();

    entryMocks.authState.isAuthenticated = true;
    entryMocks.subscriber?.();

    expect(entryMocks.requestPersistentStorage).toHaveBeenCalledOnce();
  });
});
