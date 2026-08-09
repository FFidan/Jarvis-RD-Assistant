import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useRetentionForm } from '@/hooks/use-retention-form';

const getRetentionMock = vi.fn();
const putRetentionMock = vi.fn();

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

vi.mock('@/lib/api/backups', () => ({
  getRetention: () => getRetentionMock(),
  putRetention: (config: unknown) => putRetentionMock(config),
}));

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const Wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
  Wrapper.displayName = 'QueryWrapper';
  return Wrapper;
}

describe('useRetentionForm', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    putRetentionMock.mockImplementation((config: unknown) => Promise.resolve(config));
  });

  it('prohibits saving before authoritative hydration and then tracks edits', async () => {
    let resolveRetention!: (value: { keep_last_n: number | null; max_age_days: number | null }) => void;
    getRetentionMock.mockReturnValue(
      new Promise((resolve) => {
        resolveRetention = resolve;
      }),
    );
    const { result } = renderHook(() => useRetentionForm(), { wrapper: makeWrapper() });

    act(() => result.current.save());
    expect(putRetentionMock).not.toHaveBeenCalled();

    await act(async () => resolveRetention({ keep_last_n: 5, max_age_days: 30 }));
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.keepLastN).toBe('5');
    expect(result.current.maxAgeDays).toBe('30');

    act(() => result.current.setKeepLastN('7'));
    expect(result.current.dirty).toBe(true);
  });

  it('normalizes blank and zero limits to no cap', async () => {
    getRetentionMock.mockResolvedValue({ keep_last_n: 5, max_age_days: 30 });
    const { result } = renderHook(() => useRetentionForm(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => {
      result.current.setKeepLastN('0');
      result.current.setMaxAgeDays('   ');
    });
    act(() => result.current.save());

    await waitFor(() =>
      expect(putRetentionMock).toHaveBeenCalledWith({
        keep_last_n: null,
        max_age_days: null,
      }),
    );
  });

  it('keeps the form unauthoritative when loading fails', async () => {
    getRetentionMock.mockRejectedValue(new Error('network down'));
    const { result } = renderHook(() => useRetentionForm(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.loaded).toBe(false);
    act(() => result.current.save());
    expect(putRetentionMock).not.toHaveBeenCalled();
  });
});
