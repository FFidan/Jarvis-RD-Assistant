/**
 * Unit tests for use-online-status (connectivity gating primitive).
 * Verifies initial read + online→offline→online transitions + SSR safety.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useOnlineStatus } from '@/hooks/use-online-status';

function setNavigatorOnline(value: boolean): void {
  Object.defineProperty(window.navigator, 'onLine', {
    configurable: true,
    get: () => value,
  });
}

afterEach(() => {
  setNavigatorOnline(true);
  vi.restoreAllMocks();
});

describe('useOnlineStatus', () => {
  it('reflects navigator.onLine on initial render', () => {
    setNavigatorOnline(true);
    const { result } = renderHook(() => useOnlineStatus());
    expect(result.current.online).toBe(true);
  });

  it('starts offline when navigator.onLine is false', () => {
    setNavigatorOnline(false);
    const { result } = renderHook(() => useOnlineStatus());
    expect(result.current.online).toBe(false);
  });

  it('transitions online → offline → online via window events', () => {
    setNavigatorOnline(true);
    const { result } = renderHook(() => useOnlineStatus());
    expect(result.current.online).toBe(true);

    act(() => {
      setNavigatorOnline(false);
      window.dispatchEvent(new Event('offline'));
    });
    expect(result.current.online).toBe(false);

    act(() => {
      setNavigatorOnline(true);
      window.dispatchEvent(new Event('online'));
    });
    expect(result.current.online).toBe(true);
  });

  it('removes its event listeners on unmount (no leak)', () => {
    const removeSpy = vi.spyOn(window, 'removeEventListener');
    const { unmount } = renderHook(() => useOnlineStatus());
    unmount();
    const removed = removeSpy.mock.calls.map((c) => c[0]);
    expect(removed).toContain('online');
    expect(removed).toContain('offline');
  });

  it('defaults to online when navigator.onLine is not a boolean (SSR-ish)', () => {
    Object.defineProperty(window.navigator, 'onLine', {
      configurable: true,
      get: () => undefined as unknown as boolean,
    });
    const { result } = renderHook(() => useOnlineStatus());
    expect(result.current.online).toBe(true);
  });
});
