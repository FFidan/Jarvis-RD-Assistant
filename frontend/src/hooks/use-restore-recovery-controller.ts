import { useCallback, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  acknowledgeRestore,
  getRestoreStatus,
  requestRestore,
  type RestoreRecoveryRecord,
  type RestoreSource,
} from '@/lib/api/backups';
import {
  clearRestoreRecovery,
  loadRestoreRecovery,
  saveRestoreRecovery,
} from '@/lib/restore-recovery';
import { useMaintenanceStore } from '@/stores/maintenance-store';

const EXPIRED_RECOVERY_MESSAGE =
  'This restore session expired. Sign in as the configured owner or run jarvis-research restore acknowledge <restore-id> on the host.';
const STALE_RECOVERY_MESSAGE =
  'This restore session is no longer usable. Sign in as the configured owner or run jarvis-research restore acknowledge <restore-id> on the host.';

export interface StartRestoreRequest {
  timestamp: string;
  source: RestoreSource;
  allowMissingPdfs: boolean;
  allowUnknownSchema: boolean;
}

/** Own the browser-side restore state machine while the page owns its confirmation UI. */
export function useRestoreRecoveryController() {
  const queryClient = useQueryClient();
  const maintenanceActive = useMaintenanceStore((state) => state.active);
  const [recovery, setRecovery] = useState<RestoreRecoveryRecord | null>(loadRestoreRecovery);
  const [restoringTimestamp, setRestoringTimestamp] = useState<string | null>(
    recovery?.target_timestamp ?? null,
  );
  const [manualStepsNotice, setManualStepsNotice] = useState<string | null>(null);
  const [recoveryIssue, setRecoveryIssue] = useState<string | null>(null);
  const [acknowledgementOpen, setAcknowledgementOpen] = useState(false);

  const discardRecovery = useCallback(() => {
    clearRestoreRecovery();
    setRecovery(null);
  }, []);

  useEffect(() => {
    if (!recovery) return;
    const remainingMs = Date.parse(recovery.expires_at) - Date.now();
    if (remainingMs <= 0) {
      discardRecovery();
      setRecoveryIssue(EXPIRED_RECOVERY_MESSAGE);
      return;
    }
    const timer = window.setTimeout(() => {
      discardRecovery();
      setRecoveryIssue(EXPIRED_RECOVERY_MESSAGE);
    }, Math.min(remainingMs, 2_147_483_647));
    return () => window.clearTimeout(timer);
  }, [discardRecovery, recovery]);

  const trackingRestore = restoringTimestamp !== null || recovery !== null;
  const statusQuery = useQuery({
    queryKey: ['admin', 'restore-status'],
    queryFn: () => getRestoreStatus(recovery?.status_token),
    refetchInterval: trackingRestore ? 3000 : false,
    retry: false,
  });
  const status = statusQuery.data;

  useEffect(() => {
    if (!status) return;
    const quarantine = status.quarantine ?? 'none';
    if (quarantine === 'unreadable') {
      setRestoringTimestamp(null);
      setRecoveryIssue(
        'Restore review state is unreadable. Keep outbound access blocked and inspect it on the host before running jarvis-research restore acknowledge <restore-id>.',
      );
      return;
    }
    if (quarantine === 'awaiting_review') {
      setRestoringTimestamp(null);
      if (
        recovery &&
        (recovery.restore_id !== status.restore_id ||
          recovery.source !== status.source ||
          recovery.source !== 'inbox')
      ) {
        discardRecovery();
        setRecoveryIssue(
          'This tab belongs to a different restore. Sign in as the configured owner or run jarvis-research restore acknowledge <restore-id> on the host.',
        );
      }
      return;
    }
    if (!restoringTimestamp && !recovery) return;
    if (status.state === 'done') {
      if (status.manual_steps_required === true) {
        setManualStepsNotice(
          status.error ??
            'The restore finished but the app is held in maintenance until you recreate the app containers and clear the maintenance markers — see the steps below.',
        );
      } else {
        toast.success('Restore complete. Your data has been restored.');
      }
      void queryClient.invalidateQueries({ queryKey: ['admin', 'restore-points'] });
      setRestoringTimestamp(null);
      discardRecovery();
    } else if (status.state === 'failed') {
      setRestoringTimestamp(null);
      discardRecovery();
    }
  }, [discardRecovery, queryClient, recovery, restoringTimestamp, status]);

  const restoreMutation = useMutation({
    mutationFn: ({
      timestamp,
      source,
      allowMissingPdfs,
      allowUnknownSchema,
    }: StartRestoreRequest) =>
      requestRestore(timestamp, 'RESTORE', source, allowMissingPdfs, allowUnknownSchema),
    onSuccess: (data, { timestamp }) => {
      queryClient.removeQueries({ queryKey: ['admin', 'restore-status'] });
      const nextRecovery: RestoreRecoveryRecord = {
        version: 1,
        restore_id: data.restore_id,
        source: data.source,
        status_token: data.status_token,
        expires_at: data.expires_at,
        target_timestamp: timestamp,
      };
      saveRestoreRecovery(nextRecovery);
      setRecovery(nextRecovery);
      setRecoveryIssue(null);
      setRestoringTimestamp(timestamp);
    },
    onError: (error: unknown) => {
      toast.error(error instanceof Error ? error.message : 'Could not start the restore.');
    },
  });

  const acknowledgementMutation = useMutation({
    mutationFn: ({ restoreId, token }: { restoreId: string; token?: string }) =>
      acknowledgeRestore(
        restoreId,
        'inbox',
        'I HAVE REVIEWED RESTORED CREDENTIALS',
        token,
      ),
    onSuccess: () => {
      discardRecovery();
      setRestoringTimestamp(null);
      setRecoveryIssue(null);
      setAcknowledgementOpen(false);
      toast.success('Restore review acknowledged. Outbound connections are available again.');
      void statusQuery.refetch();
    },
    onError: (error: unknown) => {
      const statusCode =
        typeof error === 'object' && error !== null && 'status' in error
          ? Number(error.status)
          : null;
      if (statusCode === 401 || statusCode === 403 || statusCode === 409) {
        discardRecovery();
        setRecoveryIssue(STALE_RECOVERY_MESSAGE);
      }
      setAcknowledgementOpen(false);
      toast.error(error instanceof Error ? error.message : 'Could not acknowledge restore review.');
    },
  });

  const quarantine = status?.quarantine ?? 'none';
  const quarantineRestoreId = status?.restore_id ?? null;
  const recoveryMatchesQuarantine =
    recovery !== null &&
    quarantine === 'awaiting_review' &&
    recovery.restore_id === quarantineRestoreId &&
    recovery.source === 'inbox' &&
    status?.source === 'inbox';
  const showRestorePanel =
    trackingRestore ||
    status?.state === 'failed' ||
    manualStepsNotice !== null ||
    quarantine !== 'none' ||
    recoveryIssue !== null ||
    maintenanceActive;

  const startRestore = (request: StartRestoreRequest, onSettled: () => void) => {
    restoreMutation.mutate(request, { onSettled });
  };

  const acknowledgeQuarantine = () => {
    if (!quarantineRestoreId) return;
    acknowledgementMutation.mutate({
      restoreId: quarantineRestoreId,
      token: recoveryMatchesQuarantine ? recovery.status_token : undefined,
    });
  };

  return {
    acknowledgementOpen,
    acknowledgementPending: acknowledgementMutation.isPending,
    acknowledgeQuarantine,
    dismissFailed: () => queryClient.removeQueries({ queryKey: ['admin', 'restore-status'] }),
    dismissManual: () => setManualStepsNotice(null),
    manualStepsNotice,
    pollError: statusQuery.isError,
    quarantine,
    quarantineRestoreId,
    recoveryIssue,
    restoringTimestamp,
    setAcknowledgementOpen,
    showRestorePanel,
    startRestore,
    status,
  };
}
