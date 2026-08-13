import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useRestoreRecoveryController } from '@/hooks/use-restore-recovery-controller';
import { RESTORE_RECOVERY_STORAGE_KEY } from '@/lib/restore-recovery';
import { useMaintenanceStore } from '@/stores/maintenance-store';

const getRestoreStatusMock = vi.fn();
const requestRestoreMock = vi.fn();
const acknowledgeRestoreMock = vi.fn();

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

vi.mock('@/lib/api/backups', () => ({
  acknowledgeRestore: (restoreId: string, source: string, confirm: string, token?: string) =>
    acknowledgeRestoreMock(restoreId, source, confirm, token),
  getRestoreStatus: (token?: string) => getRestoreStatusMock(token),
  requestRestore: (
    timestamp: string,
    confirm: string,
    source: string,
    allowMissingPdfs: boolean,
    allowUnknownSchema: boolean,
  ) => requestRestoreMock(timestamp, confirm, source, allowMissingPdfs, allowUnknownSchema),
}));

const restoreId = '0123456789abcdef0123456789abcdef';

function recoveryRecord(expiresAt = new Date(Date.now() + 60_000).toISOString()) {
  return {
    version: 1,
    restore_id: restoreId,
    source: 'inbox',
    status_token: 'session-only-recovery-token',
    expires_at: expiresAt,
    target_timestamp: '20260617_120000',
  };
}

function restoreStatus(overrides: Record<string, unknown> = {}) {
  return {
    state: 'pending',
    current_step: 'Queued',
    steps: [],
    safety_backup_ts: null,
    started_at: null,
    finished_at: null,
    error: null,
    manual_steps_required: false,
    phase: null,
    restore_id: restoreId,
    source: 'inbox',
    quarantine: 'none',
    ...overrides,
  };
}

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function makeWrapper(queryClient: QueryClient) {
  const Wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
  Wrapper.displayName = 'QueryWrapper';
  return Wrapper;
}

describe('useRestoreRecoveryController', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    useMaintenanceStore.getState().clear();
    getRestoreStatusMock.mockResolvedValue(restoreStatus({ state: 'idle' }));
    requestRestoreMock.mockResolvedValue({
      status: 'scheduled',
      status_token: 'new-session-token',
      restore_id: restoreId,
      source: 'local',
      expires_at: new Date(Date.now() + 60_000).toISOString(),
    });
    acknowledgeRestoreMock.mockResolvedValue({ status: 'acknowledged', restore_id: restoreId });
  });

  it('keeps tracking pending work and expires its tab-scoped capability on time', async () => {
    vi.useFakeTimers();
    try {
      const expiresAt = new Date(Date.now() + 1000).toISOString();
      sessionStorage.setItem(
        RESTORE_RECOVERY_STORAGE_KEY,
        JSON.stringify(recoveryRecord(expiresAt)),
      );
      getRestoreStatusMock.mockResolvedValue(restoreStatus());
      const { result } = renderHook(() => useRestoreRecoveryController(), {
        wrapper: makeWrapper(makeQueryClient()),
      });

      await act(async () => {
        await Promise.resolve();
      });
      expect(result.current.restoringTimestamp).toBe('20260617_120000');

      act(() => vi.advanceTimersByTime(1001));
      expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull();
      expect(result.current.recoveryIssue).toMatch(/restore session expired/i);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ['done', null],
    ['failed', null],
    ['done', 'Recreate the app containers.'],
  ])('handles the %s terminal transition and its manual notice', async (state, manualNotice) => {
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(recoveryRecord()));
    getRestoreStatusMock.mockResolvedValue(
      restoreStatus({
        state,
        error: manualNotice,
        manual_steps_required: manualNotice !== null,
      }),
    );
    const { result } = renderHook(() => useRestoreRecoveryController(), {
      wrapper: makeWrapper(makeQueryClient()),
    });

    await waitFor(() => expect(result.current.restoringTimestamp).toBeNull());
    expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull();
    expect(result.current.manualStepsNotice).toBe(manualNotice);
  });

  it('acknowledges only the matching quarantined restore with the saved capability', async () => {
    const record = recoveryRecord();
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(record));
    getRestoreStatusMock.mockResolvedValue(
      restoreStatus({ state: 'done', quarantine: 'awaiting_review' }),
    );
    const { result } = renderHook(() => useRestoreRecoveryController(), {
      wrapper: makeWrapper(makeQueryClient()),
    });
    await waitFor(() => expect(result.current.quarantine).toBe('awaiting_review'));

    act(() => result.current.acknowledgeQuarantine());

    await waitFor(() =>
      expect(acknowledgeRestoreMock).toHaveBeenCalledWith(
        restoreId,
        'inbox',
        'I HAVE REVIEWED RESTORED CREDENTIALS',
        record.status_token,
      ),
    );
    await waitFor(() =>
      expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull(),
    );
  });

  it('fails closed when a quarantined restore capability is stale', async () => {
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(recoveryRecord()));
    getRestoreStatusMock.mockResolvedValue(
      restoreStatus({ state: 'done', quarantine: 'awaiting_review' }),
    );
    acknowledgeRestoreMock.mockRejectedValue(Object.assign(new Error('expired'), { status: 401 }));
    const { result } = renderHook(() => useRestoreRecoveryController(), {
      wrapper: makeWrapper(makeQueryClient()),
    });
    await waitFor(() => expect(result.current.quarantine).toBe('awaiting_review'));

    act(() => result.current.acknowledgeQuarantine());

    await waitFor(() => expect(result.current.recoveryIssue).toMatch(/no longer usable/i));
    expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull();
  });
});
