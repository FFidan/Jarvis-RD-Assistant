import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AdminBackupsPage } from '@/pages/AdminBackupsPage';
import { RESTORE_RECOVERY_STORAGE_KEY } from '@/lib/restore-recovery';
import { toast } from 'sonner';

import { useMaintenanceStore } from '@/stores/maintenance-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const getRestorePointsMock = vi.fn();
const getInboxRestorePointsMock = vi.fn();
const getBackupStatusMock = vi.fn();
const triggerBackupMock = vi.fn();
const downloadBackupMock = vi.fn();
const requestRestoreMock = vi.fn();
const getRestoreStatusMock = vi.fn();
const deleteRestorePointMock = vi.fn();
const getRetentionMock = vi.fn();
const putRetentionMock = vi.fn();
const acknowledgeRestoreMock = vi.fn();

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/lib/api/backups', () => ({
  getRestorePoints: () => getRestorePointsMock(),
  getInboxRestorePoints: () => getInboxRestorePointsMock(),
  getBackupStatus: () => getBackupStatusMock(),
  triggerBackup: () => triggerBackupMock(),
  downloadBackup: (name: string) => downloadBackupMock(name),
  requestRestore: (
    timestamp: string,
    confirm: string,
    source?: string,
    allowMissingPdfs?: boolean,
    allowUnknownSchema?: boolean,
  ) => requestRestoreMock(timestamp, confirm, source, allowMissingPdfs, allowUnknownSchema),
  getRestoreStatus: (token?: string) => getRestoreStatusMock(token),
  acknowledgeRestore: (restoreId: string, source: string, confirm: string, token?: string) =>
    acknowledgeRestoreMock(restoreId, source, confirm, token),
  deleteRestorePoint: (timestamp: string, confirm: string) =>
    deleteRestorePointMock(timestamp, confirm),
  getRetention: () => getRetentionMock(),
  putRetention: (config: unknown) => putRetentionMock(config),
}));

let _mockRole: 'user' | 'admin' = 'admin';
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: (selector?: (s: { user: { role: 'user' | 'admin' } | null }) => unknown) => {
    const state = { user: { role: _mockRole } };
    return selector ? selector(state) : state;
  },
}));

const _restorePoints = {
  retention_days: 14,
  last_run: { attempted_at: new Date().toISOString(), succeeded: true, stores: {} },
  restore_points: [
    {
      timestamp: '20260617_120000',
      created_at: new Date().toISOString(),
      stores: ['jarvis', 'pdfs', 'secrets'],
      qdrant_collections: ['kg_entities'],
      complete: true,
      has_pdfs: true,
      legacy_missing_pdfs: false,
      encrypted: true,
      total_size_bytes: 2560,
      app_version: '1.0.0',
      schema_version: 97,
      compat: 'same',
      files: [
        {
          filename: 'jarvis_20260617_120000.sql.gz',
          store: 'jarvis',
          size_bytes: 2048,
          encrypted: false,
        },
        {
          filename: 'secrets_20260617_120000.tar.gz.enc',
          store: 'secrets',
          size_bytes: 512,
          encrypted: true,
        },
        {
          filename: 'pdfs_20260617_120000.tar.gz.enc',
          store: 'pdfs',
          size_bytes: 1024,
          encrypted: true,
        },
      ],
    },
  ],
};

const _okStatus = {
  backup_dir_available: true,
  archive_count: 2,
  last_run_at: new Date().toISOString(),
  trigger_pending: false,
  last_attempt_at: new Date().toISOString(),
  last_run_succeeded: true,
};

const _runningRestore = {
  state: 'running',
  current_step: 'Restoring database',
  steps: [
    { name: 'Safety backup', status: 'done' },
    { name: 'Restoring database', status: 'running' },
    { name: 'Restoring API-key store', status: 'pending' },
    { name: 'Restoring search index', status: 'pending' },
    { name: 'Finishing up', status: 'pending' },
  ],
  safety_backup_ts: '20260617_115900',
  started_at: new Date().toISOString(),
  finished_at: null,
  error: null,
};

const _restoreId = '0123456789abcdef0123456789abcdef';

function recoveryRecord(
  overrides: Partial<{
    version: 1;
    restore_id: string;
    source: 'local' | 'inbox';
    status_token: string;
    expires_at: string;
    target_timestamp: string;
  }> = {},
) {
  return {
    version: 1 as const,
    restore_id: _restoreId,
    source: 'inbox' as const,
    status_token: 'session-only-recovery-token',
    expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    target_timestamp: '20260617_120000',
    ...overrides,
  };
}

const _newerPoints = {
  ..._restorePoints,
  restore_points: [{ ..._restorePoints.restore_points[0], compat: 'newer' }],
};

function renderPage() {
  const qc = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter initialEntries={['/admin/backups']}>
      <Routes>
        <Route path="/admin/backups" element={<AdminBackupsPage />} />
        <Route path="/" element={<div>HOME</div>} />
      </Routes>
    </MemoryRouter>,
    { queryClient: qc },
  );
}

describe('AdminBackupsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    localStorage.clear();
    useMaintenanceStore.getState().clear();
    _mockRole = 'admin';
    getRestorePointsMock.mockResolvedValue(_restorePoints);
    getInboxRestorePointsMock.mockResolvedValue([]);
    getBackupStatusMock.mockResolvedValue(_okStatus);
    requestRestoreMock.mockResolvedValue({
      status: 'scheduled',
      status_token: 'test-bearer-token',
      restore_id: _restoreId,
      source: 'local',
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    });
    acknowledgeRestoreMock.mockResolvedValue({ status: 'acknowledged', restore_id: _restoreId });
    getRestoreStatusMock.mockResolvedValue(_runningRestore);
    deleteRestorePointMock.mockResolvedValue({ status: 'scheduled' });
    getRetentionMock.mockResolvedValue({ keep_last_n: null, max_age_days: 14 });
    putRetentionMock.mockResolvedValue({ keep_last_n: 5, max_age_days: 30 });
  });

  it('shows a loading status, not "not available", while the status query is pending', () => {
    getBackupStatusMock.mockReturnValue(new Promise(() => {}));
    renderPage();
    const statusLine = screen.getByTestId('backup-status');
    expect(statusLine).toHaveTextContent('Checking backup status…');
    expect(statusLine).not.toHaveTextContent('not available');
  });

  it('shows "Backup storage is not available." when the dir is unavailable', async () => {
    getBackupStatusMock.mockResolvedValue({ ..._okStatus, backup_dir_available: false });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('backup-status')).toHaveTextContent(
        'Backup storage is not available.',
      ),
    );
  });

  it('shows a failure warning when the last run did not succeed', async () => {
    getBackupStatusMock.mockResolvedValue({ ..._okStatus, last_run_succeeded: false });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('backup-status')).toHaveTextContent(
        'Last backup attempt failed — check the backup service.',
      ),
    );
    expect(screen.getByTestId('backup-status')).toHaveTextContent(/\(\d+m ago\)/);
  });

  it('shows a degraded status (not "No backups yet.") when the status probe fails', async () => {
    getBackupStatusMock.mockRejectedValue(new Error('status down'));
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('backup-status')).toHaveTextContent('Backup status unavailable.'),
    );
    expect(screen.getByTestId('backup-status')).not.toHaveTextContent('No backups yet.');
  });

  it('renders a restore-point card with store badges and an expander that reveals files', async () => {
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByTestId('restore-point-card')).toBeInTheDocument();
    expect(
      screen.getByText(/backups include databases, PDF files, the search index/i),
    ).toBeInTheDocument();
    expect(screen.getByText('Main database')).toBeInTheDocument();
    expect(screen.getByText('PDF files')).toBeInTheDocument();
    expect(screen.getByText('Data keys')).toBeInTheDocument();
    expect(screen.getByText('Qdrant: kg_entities')).toBeInTheDocument();
    expect(screen.getByText('Kept for 14 days')).toBeInTheDocument();

    // Files are collapsed by default; the expander reveals them.
    expect(screen.queryByText('jarvis_20260617_120000.sql.gz')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /details/i }));
    expect(await screen.findByText('jarvis_20260617_120000.sql.gz')).toBeInTheDocument();
    expect(screen.getByText('secrets_20260617_120000.tar.gz.enc')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /download/i }).length).toBeGreaterThan(0);
  });

  it('triggers a backup after confirm', async () => {
    triggerBackupMock.mockResolvedValue({ status: 'scheduled' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /run backup now/i }));
    await user.click(await screen.findByRole('button', { name: /^confirm$/i }));
    await waitFor(() => expect(triggerBackupMock).toHaveBeenCalledTimes(1));
  });

  it('calls downloadBackup for a file', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /details/i }));
    await user.click((await screen.findAllByRole('button', { name: /download/i }))[0]!);
    await waitFor(() =>
      expect(downloadBackupMock).toHaveBeenCalledWith('jarvis_20260617_120000.sql.gz'),
    );
  });

  it('shows the manual restore runbook reframed as the advanced fallback', async () => {
    renderPage();
    expect(await screen.findByText(/Manual restore \(advanced\)/i)).toBeInTheDocument();
    expect(screen.getByText(/DROP DATABASE jarvis/)).toBeInTheDocument();
  });

  it('enables Restore for a complete same/older restore point', async () => {
    renderPage();
    await screen.findByTestId('restore-point-card');
    const restoreBtn = screen.getByRole('button', { name: /restore to this point/i });
    expect(restoreBtn).toBeEnabled();
  });

  it('disables Restore with a caption for a newer restore point', async () => {
    getRestorePointsMock.mockResolvedValue(_newerPoints);
    renderPage();
    await screen.findByTestId('restore-point-card');
    expect(screen.getByRole('button', { name: /restore to this point/i })).toBeDisabled();
    expect(
      screen.getByText(/newer than the current app version — update first/i),
    ).toBeInTheDocument();
  });

  it('runs the typed-RESTORE confirm flow and calls requestRestore', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));

    const input = await screen.findByLabelText(/type RESTORE to confirm/i);
    // Confirm stays disabled until the exact word is typed.
    expect(screen.getByRole('button', { name: /^restore$/i })).toBeDisabled();
    await user.type(input, 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    await waitFor(() =>
      expect(requestRestoreMock).toHaveBeenCalledWith(
        '20260617_120000',
        'RESTORE',
        'local',
        false,
        false,
      ),
    );
  });

  it('blocks a current restore point whose PDF archive is missing', async () => {
    getRestorePointsMock.mockResolvedValue({
      ..._restorePoints,
      restore_points: [
        {
          ..._restorePoints.restore_points[0],
          stores: ['jarvis', 'secrets'],
          complete: false,
          has_pdfs: false,
          legacy_missing_pdfs: false,
        },
      ],
    });

    renderPage();
    const card = await screen.findByTestId('restore-point-card');
    expect(within(card).getByRole('button', { name: /restore to this point/i })).toBeDisabled();
    expect(within(card).getByText(/missing its PDF archive/i)).toBeInTheDocument();
    expect(requestRestoreMock).not.toHaveBeenCalled();
  });

  it('shows an unavoidable PDF-loss warning before an older backup can be restored', async () => {
    getRestorePointsMock.mockResolvedValue({
      ..._restorePoints,
      restore_points: [
        {
          ..._restorePoints.restore_points[0],
          stores: ['jarvis', 'secrets'],
          compat: 'older',
          has_pdfs: false,
          legacy_missing_pdfs: true,
        },
      ],
    });
    const user = userEvent.setup();

    renderPage();
    await user.click(
      within(await screen.findByTestId('restore-point-card')).getByRole('button', {
        name: /restore to this point/i,
      }),
    );

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/does not include PDF files/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/remove the PDF files currently stored/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/papers may still appear in JARVIS/i)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /^restore$/i })).toBeDisabled();
    expect(requestRestoreMock).not.toHaveBeenCalled();
  });

  it('opts in to an empty PDF set only for the selected older backup', async () => {
    getRestorePointsMock.mockResolvedValue({
      ..._restorePoints,
      restore_points: [
        {
          ..._restorePoints.restore_points[0],
          stores: ['jarvis', 'secrets'],
          compat: 'older',
          has_pdfs: false,
          legacy_missing_pdfs: true,
        },
      ],
    });
    const user = userEvent.setup();

    renderPage();
    await user.click(
      within(await screen.findByTestId('restore-point-card')).getByRole('button', {
        name: /restore to this point/i,
      }),
    );
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    await waitFor(() =>
      expect(requestRestoreMock).toHaveBeenCalledWith(
        '20260617_120000',
        'RESTORE',
        'local',
        true,
        false,
      ),
    );
  });

  it('acknowledges a restore point that records no usable schema version', async () => {
    getRestorePointsMock.mockResolvedValue({
      ..._restorePoints,
      restore_points: [
        { ..._restorePoints.restore_points[0], schema_version: 0, compat: 'unknown' },
      ],
    });
    const user = userEvent.setup();

    renderPage();
    const card = await screen.findByTestId('restore-point-card');
    expect(within(card).getByText(/predates schema recording/i)).toBeInTheDocument();
    await user.click(within(card).getByRole('button', { name: /restore to this point/i }));

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByTestId('restore-unknown-schema-warning')).toBeInTheDocument();
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(within(dialog).getByRole('button', { name: /^restore$/i }));

    await waitFor(() =>
      expect(requestRestoreMock).toHaveBeenCalledWith(
        '20260617_120000',
        'RESTORE',
        'local',
        false,
        true,
      ),
    );
  });

  it('polls getRestoreStatus and renders persona-friendly progress steps after starting', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    expect(await screen.findByTestId('restore-progress')).toBeInTheDocument();
    expect(await screen.findByText('Safety backup')).toBeInTheDocument();
    expect(screen.getByText('Finishing up')).toBeInTheDocument();
    await waitFor(() => expect(getRestoreStatusMock).toHaveBeenCalled());
  });

  it('shows an explanatory empty state (not an error) when no inbox backups are staged', async () => {
    renderPage();
    const section = await screen.findByTestId('inbox-restore-section');
    expect(within(section).getByText(/restore from another jarvis/i)).toBeInTheDocument();
    expect(screen.getByTestId('inbox-empty')).toHaveTextContent(/no off-host backups staged/i);
  });

  it('lists inbox restore points and triggers an inbox restore through the RESTORE confirm', async () => {
    getInboxRestorePointsMock.mockResolvedValue([
      {
        timestamp: '20260701_030000',
        complete: true,
        has_secrets: true,
        has_key: true,
        has_pdfs: true,
        legacy_missing_pdfs: false,
      },
      {
        timestamp: '20260630_020000',
        complete: false,
        has_secrets: false,
        has_key: false,
        has_pdfs: true,
        legacy_missing_pdfs: false,
      },
    ]);
    const user = userEvent.setup();
    renderPage();
    const section = await screen.findByTestId('inbox-restore-section');
    const items = within(section).getAllByTestId('inbox-restore-point');
    expect(items).toHaveLength(2);
    // The incomplete/keyless point cannot be triggered; its hint explains why.
    const incomplete = items[1]!;
    expect(within(incomplete).getByText('Incomplete')).toBeInTheDocument();
    expect(within(incomplete).getByRole('button', { name: /restore to this point/i })).toBeDisabled();
    expect(within(incomplete).getByText(/missing a required database archive/i)).toBeInTheDocument();

    // The complete + keyed point restores through the shared typed-RESTORE confirm.
    const ready = items[0]!;
    await user.click(within(ready).getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    await waitFor(() =>
      expect(requestRestoreMock).toHaveBeenCalledWith(
        '20260701_030000',
        'RESTORE',
        'inbox',
        false,
        false,
      ),
    );
  });

  it('explains that a missing data-key archive is refused before data changes', async () => {
    getInboxRestorePointsMock.mockResolvedValue([
      {
        timestamp: '20260701_040000',
        complete: true,
        has_secrets: false,
        has_key: true,
        has_pdfs: true,
        legacy_missing_pdfs: false,
      },
    ]);

    renderPage();
    const section = await screen.findByTestId('inbox-restore-section');
    expect(
      within(section).getByText(/will not change data without it/i),
    ).toBeInTheDocument();
    expect(within(section).queryByText(/after swapping the databases/i)).not.toBeInTheDocument();
  });

  it('requires the same PDF-loss opt-in for an older inbox backup', async () => {
    getInboxRestorePointsMock.mockResolvedValue([
      {
        timestamp: '20250501_030000',
        complete: true,
        has_secrets: true,
        has_key: true,
        has_pdfs: false,
        legacy_missing_pdfs: true,
      },
    ]);
    const user = userEvent.setup();

    renderPage();
    const section = await screen.findByTestId('inbox-restore-section');
    await user.click(within(section).getByRole('button', { name: /restore to this point/i }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/remove the PDF files currently stored/i)).toBeInTheDocument();
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(within(dialog).getByRole('button', { name: /^restore$/i }));

    await waitFor(() =>
      expect(requestRestoreMock).toHaveBeenCalledWith(
        '20250501_030000',
        'RESTORE',
        'inbox',
        true,
        false,
      ),
    );
  });

  it('blocks an inbox restore with no data-key archive', async () => {
    // Reject the archive before any data changes and explain what is missing.
    getInboxRestorePointsMock.mockResolvedValue([
      {
        timestamp: '20260701_030000',
        complete: true,
        has_secrets: false,
        has_key: true,
        has_pdfs: true,
        legacy_missing_pdfs: false,
      },
    ]);
    renderPage();
    const section = await screen.findByTestId('inbox-restore-section');
    const point = within(section).getByTestId('inbox-restore-point');
    expect(within(point).getByText('No data keys')).toBeInTheDocument();
    expect(within(point).getByRole('button', { name: /restore to this point/i })).toBeDisabled();
    expect(within(point).getByText(/no data-key archive/i)).toBeInTheDocument();
    expect(requestRestoreMock).not.toHaveBeenCalled();
  });

  it('authenticates restore-status polling with the saved restore token', async () => {
    requestRestoreMock.mockResolvedValue({
      status: 'scheduled',
      status_token: 'poll-bearer-42',
      restore_id: _restoreId,
      source: 'local',
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    // The poll must present the bearer token so it survives the DB swap.
    await waitFor(() => expect(getRestoreStatusMock).toHaveBeenCalledWith('poll-bearer-42'));
  });

  it('persists the bound recovery record in sessionStorage only', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    await waitFor(() => expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).not.toBeNull());
    const stored = JSON.parse(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY) ?? '{}');
    expect(stored).toMatchObject({
      version: 1,
      restore_id: _restoreId,
      source: 'local',
      status_token: 'test-bearer-token',
      target_timestamp: '20260617_120000',
    });
    expect(localStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull();
  });

  it('resumes bound status polling from sessionStorage after remount', async () => {
    const stored = recoveryRecord();
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(stored));

    const first = renderPage();
    await waitFor(() =>
      expect(getRestoreStatusMock).toHaveBeenCalledWith(stored.status_token),
    );
    first.unmount();
    getRestoreStatusMock.mockClear();

    renderPage();

    await waitFor(() =>
      expect(getRestoreStatusMock).toHaveBeenCalledWith(stored.status_token),
    );
    expect(screen.getByTestId('restore-progress')).toBeInTheDocument();
  });

  it('retains the token through quarantined completion and never renders it', async () => {
    const stored = recoveryRecord();
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(stored));
    getRestoreStatusMock.mockResolvedValue({
      ..._runningRestore,
      state: 'done',
      current_step: null,
      steps: [],
      finished_at: new Date().toISOString(),
      restore_id: _restoreId,
      source: 'inbox',
      quarantine: 'awaiting_review',
    });

    renderPage();

    expect(await screen.findByTestId('restore-quarantine')).toHaveTextContent(
      /review restored connections/i,
    );
    expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).not.toBeNull();
    expect(screen.queryByText(stored.status_token)).not.toBeInTheDocument();
  });

  it('acknowledges the exact quarantined restore and then clears sessionStorage', async () => {
    const stored = recoveryRecord();
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(stored));
    getRestoreStatusMock.mockResolvedValue({
      ..._runningRestore,
      state: 'done',
      current_step: null,
      steps: [],
      restore_id: _restoreId,
      source: 'inbox',
      quarantine: 'awaiting_review',
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: /acknowledge review/i }));
    await user.type(
      screen.getByLabelText(/type I HAVE REVIEWED RESTORED CREDENTIALS to confirm/i),
      'I HAVE REVIEWED RESTORED CREDENTIALS',
    );
    await user.click(screen.getByRole('button', { name: /^acknowledge$/i }));

    await waitFor(() =>
      expect(acknowledgeRestoreMock).toHaveBeenCalledWith(
        _restoreId,
        'inbox',
        'I HAVE REVIEWED RESTORED CREDENTIALS',
        stored.status_token,
      ),
    );
    await waitFor(() =>
      expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull(),
    );
  });

  it('drops an expired recovery record before polling with its token', async () => {
    const stored = recoveryRecord({ expires_at: new Date(Date.now() - 1000).toISOString() });
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(stored));

    renderPage();

    await waitFor(() =>
      expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull(),
    );
    expect(getRestoreStatusMock).not.toHaveBeenCalledWith(stored.status_token);
  });

  it.each([
    ['prior restore', { restore_id: 'fedcba9876543210fedcba9876543210', source: 'inbox' }],
    ['wrong source', { restore_id: _restoreId, source: 'local' }],
  ])('rejects a %s recovery record and falls back to owner or host recovery', async (_label, bound) => {
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(recoveryRecord()));
    getRestoreStatusMock.mockResolvedValue({
      ..._runningRestore,
      state: 'done',
      current_step: null,
      steps: [],
      quarantine: 'awaiting_review',
      ...bound,
    });

    renderPage();

    expect(await screen.findByTestId('restore-quarantine')).toHaveTextContent(
      /configured owner.*jarvis-research restore acknowledge/i,
    );
    await waitFor(() =>
      expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull(),
    );
  });

  it('treats a replayed restore token as terminal and shows owner fallback', async () => {
    const stored = recoveryRecord();
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, JSON.stringify(stored));
    getRestoreStatusMock.mockResolvedValue({
      ..._runningRestore,
      state: 'done',
      current_step: null,
      steps: [],
      restore_id: _restoreId,
      source: 'inbox',
      quarantine: 'awaiting_review',
    });
    acknowledgeRestoreMock.mockRejectedValue(Object.assign(new Error('invalid'), { status: 401 }));
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: /acknowledge review/i }));
    await user.type(
      screen.getByLabelText(/type I HAVE REVIEWED RESTORED CREDENTIALS to confirm/i),
      'I HAVE REVIEWED RESTORED CREDENTIALS',
    );
    await user.click(screen.getByRole('button', { name: /^acknowledge$/i }));

    await waitFor(() =>
      expect(sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY)).toBeNull(),
    );
    expect(await screen.findByTestId('restore-quarantine')).toHaveTextContent(
      /configured owner.*jarvis-research restore acknowledge/i,
    );
  });

  it('drives the guided recovery view (live step, aria-live) while maintenance is active', async () => {
    getRestoreStatusMock.mockResolvedValue(_runningRestore);
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    // The mid-restore 503 interceptor flips the app into maintenance.
    act(() => useMaintenanceStore.getState().setMaintenance(true, 30));

    const panel = await screen.findByTestId('restore-progress');
    // Never a blank 503: the guided view shows the live step + list, announced live.
    expect(panel).toHaveAttribute('aria-live', 'polite');
    expect(within(panel).getByText('Safety backup')).toBeInTheDocument();
    expect(within(panel).getAllByText('Restoring database').length).toBeGreaterThan(0);
  });

  it('shows a degraded restoring panel (not empty, no logout) when status errors mid-restore', async () => {
    getRestoreStatusMock.mockRejectedValue(new Error('503 Service Unavailable'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    expect(await screen.findByTestId('restore-degraded')).toHaveTextContent(
      /briefly unavailable while the database is restored/i,
    );
    // Degraded, not a bounce to /login and not an empty state.
    expect(screen.queryByText('HOME')).not.toBeInTheDocument();
    expect(screen.getByTestId('restore-point-card')).toBeInTheDocument();
  });

  it('keeps tracking when the first status poll is still idle (does not clear early)', async () => {
    // The sidecar takes a few seconds to pick up the request and write the first
    // status; an initial idle must NOT end tracking — the progress panel persists.
    getRestoreStatusMock.mockResolvedValue({
      state: 'idle',
      current_step: null,
      steps: [],
      safety_backup_ts: null,
      started_at: null,
      finished_at: null,
      error: null,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    expect(await screen.findByTestId('restore-progress')).toBeInTheDocument();
    await waitFor(() => expect(getRestoreStatusMock).toHaveBeenCalled());
    // Old behaviour cleared tracking on idle -> panel vanished. It must persist.
    expect(screen.getByTestId('restore-progress')).toBeInTheDocument();
  });

  it('disables every other restore point while one restore is in progress', async () => {
    getRestorePointsMock.mockResolvedValue({
      ..._restorePoints,
      restore_points: [
        _restorePoints.restore_points[0],
        {
          ..._restorePoints.restore_points[0],
          timestamp: '20260616_120000',
          created_at: '2026-06-16T12:00:00Z',
        },
      ],
    });
    getRestoreStatusMock.mockResolvedValue({
      state: 'pending',
      current_step: 'Queued',
      steps: [],
      safety_backup_ts: null,
      started_at: null,
      finished_at: null,
      error: null,
    });
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('restore-point-card')).toHaveLength(2));
    const [firstRestoreBtn] = screen.getAllByRole('button', { name: /restore to this point/i });
    if (!firstRestoreBtn) throw new Error('expected at least one restore button');
    await user.click(firstRestoreBtn);
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    // No concurrent destructive restore: every remaining "Restore to this point"
    // CTA is disabled while one restore is in flight.
    await waitFor(() => {
      const others = screen.queryAllByRole('button', { name: /^restore to this point$/i });
      expect(others.length).toBeGreaterThan(0);
      for (const b of others) expect(b).toBeDisabled();
    });
  });

  it('shows a "one more step" notice and no success toast when a restore finishes held in maintenance', async () => {
    getRestoreStatusMock.mockResolvedValue({
      state: 'done',
      current_step: null,
      steps: [],
      safety_backup_ts: null,
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      error:
        'The stack stays in maintenance until you recreate the app containers and clear the markers.',
      manual_steps_required: true,
      phase: 'maintenance-held',
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    const notice = await screen.findByTestId('restore-manual-steps');
    expect(notice).toHaveTextContent(/one more step/i);
    expect(notice).toHaveTextContent(/recreate the app containers/i);
    // A held restore must never claim success.
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('still shows the success toast when a restore finishes clean (manual_steps_required false)', async () => {
    getRestoreStatusMock.mockResolvedValue({
      state: 'done',
      current_step: null,
      steps: [],
      safety_backup_ts: null,
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      error: null,
      manual_steps_required: false,
      phase: null,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Restore complete. Your data has been restored.'),
    );
    expect(screen.queryByTestId('restore-manual-steps')).not.toBeInTheDocument();
  });

  it('a second restore does not fire a premature success toast from the stale done state', async () => {
    // Two restore points so we can run two separate restores in sequence.
    getRestorePointsMock.mockResolvedValue({
      ..._restorePoints,
      restore_points: [
        _restorePoints.restore_points[0]!,
        {
          ..._restorePoints.restore_points[0]!,
          timestamp: '20260616_120000',
          created_at: '2026-06-16T12:00:00Z',
        },
      ],
    });

    // Restore #1 resolves immediately as 'done', leaving terminal state in the cache.
    getRestoreStatusMock.mockResolvedValue({
      state: 'done',
      current_step: null,
      steps: [],
      safety_backup_ts: null,
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      error: null,
    });

    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('restore-point-card')).toHaveLength(2));

    // --- Restore #1 ---
    const [firstBtn] = screen.getAllByRole('button', { name: /restore to this point/i });
    if (!firstBtn) throw new Error('expected at least one restore button');
    await user.click(firstBtn);
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    // Wait for the legitimate success toast from restore #1.
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        'Restore complete. Your data has been restored.',
      ),
    );
    const toastCallsAfterRestore1 = vi.mocked(toast.success).mock.calls.length;

    // Switch the status mock to 'running' for restore #2 — if the stale 'done'
    // cache is not evicted before re-enabling, the effect fires prematurely and
    // calls toast.success again before the running state is ever fetched.
    getRestoreStatusMock.mockResolvedValue({
      state: 'running',
      current_step: 'Restoring database',
      steps: [{ name: 'Restoring database', status: 'running' }],
      safety_backup_ts: null,
      started_at: new Date().toISOString(),
      finished_at: null,
      error: null,
    });

    // After restore #1 completes, restoringTimestamp resets to null and the
    // restore buttons become re-enabled. Wait for that transition.
    await waitFor(() => {
      const btns = screen.getAllByRole('button', { name: /restore to this point/i });
      expect(btns.some((b) => !b.hasAttribute('disabled'))).toBe(true);
    });

    // --- Restore #2 ---
    const enabledBtn = screen
      .getAllByRole('button', { name: /restore to this point/i })
      .find((b) => !b.hasAttribute('disabled'));
    if (!enabledBtn) throw new Error('expected an enabled restore button for restore #2');
    await user.click(enabledBtn);
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    // After the mutation fires, onSuccess runs synchronously in the mock (resolved
    // value). Without the fix, the stale 'done' cache causes an extra success toast
    // here before the new fetch arrives.
    await waitFor(() => expect(requestRestoreMock).toHaveBeenCalledTimes(2));
    expect(vi.mocked(toast.success).mock.calls.length).toBe(toastCallsAfterRestore1);

    // Monitoring must stay active — the restore-progress panel must be visible.
    await waitFor(() =>
      expect(screen.getByTestId('restore-progress')).toBeInTheDocument(),
    );
  });

  it('labels the count as restore points, not archives', async () => {
    renderPage();
    await screen.findByTestId('restore-point-card');
    expect(screen.getByTestId('backup-status')).toHaveTextContent('1 restore point');
    expect(screen.getByTestId('backup-status')).not.toHaveTextContent('archive');
  });

  it('shows a "Backup running…" row while a backup is pending', async () => {
    getBackupStatusMock.mockResolvedValue({ ..._okStatus, trigger_pending: true });
    renderPage();
    expect(await screen.findByTestId('backup-running')).toHaveTextContent(/backup running/i);
  });

  it('hides the backup-running row when no backup is pending', async () => {
    renderPage();
    await screen.findByTestId('restore-point-card');
    expect(screen.queryByTestId('backup-running')).not.toBeInTheDocument();
  });

  it('runs the typed-DELETE confirm flow and calls deleteRestorePoint', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    // Only the card Delete button exists while the dialog is closed.
    await user.click(screen.getByRole('button', { name: /^delete$/i }));

    const dialog = await screen.findByRole('alertdialog');
    const input = await screen.findByLabelText(/type DELETE to confirm/i);
    // The dialog's confirm stays disabled until the exact word is typed.
    expect(within(dialog).getByRole('button', { name: /^delete$/i })).toBeDisabled();
    await user.type(input, 'DELETE');
    await user.click(within(dialog).getByRole('button', { name: /^delete$/i }));

    await waitFor(() =>
      expect(deleteRestorePointMock).toHaveBeenCalledWith('20260617_120000', 'DELETE'),
    );
  });

  it('disables Delete for the restore point currently being restored', async () => {
    getRestoreStatusMock.mockResolvedValue({
      state: 'pending',
      current_step: 'Queued',
      steps: [],
      safety_backup_ts: null,
      started_at: null,
      finished_at: null,
      error: null,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('restore-point-card');
    await user.click(screen.getByRole('button', { name: /restore to this point/i }));
    await user.type(await screen.findByLabelText(/type RESTORE to confirm/i), 'RESTORE');
    await user.click(screen.getByRole('button', { name: /^restore$/i }));

    // Can't delete the point a restore is using.
    await waitFor(() => expect(screen.getByRole('button', { name: /^delete$/i })).toBeDisabled());
  });

  it('loads and saves the retention policy', async () => {
    const user = userEvent.setup();
    renderPage();
    const keepInput = await screen.findByLabelText(/keep most recent restore points/i);
    const ageInput = screen.getByLabelText(/maximum age in days/i);
    // Seeded from getRetention (keep_last_n null -> blank, max_age_days 14).
    await waitFor(() => expect(ageInput).toHaveValue(14));

    await user.clear(keepInput);
    await user.type(keepInput, '5');
    await user.clear(ageInput);
    await user.type(ageInput, '30');
    await user.click(screen.getByRole('button', { name: /save retention policy/i }));

    await waitFor(() =>
      expect(putRetentionMock).toHaveBeenCalledWith({ keep_last_n: 5, max_age_days: 30 }),
    );
  });

  it('treats a retention value of 0 as "no cap" (null), never a 0-day window', async () => {
    const user = userEvent.setup();
    renderPage();
    const keepInput = await screen.findByLabelText(/keep most recent restore points/i);
    const ageInput = screen.getByLabelText(/maximum age in days/i);

    await user.clear(keepInput);
    await user.type(keepInput, '0');
    await user.clear(ageInput);
    await user.type(ageInput, '0');
    await user.click(screen.getByRole('button', { name: /save retention policy/i }));

    // A 0-day age window would delete everything but the last day; 0 kept points
    // is meaningless. Both collapse to null so the sidecar keeps its default.
    await waitFor(() =>
      expect(putRetentionMock).toHaveBeenCalledWith({ keep_last_n: null, max_age_days: null }),
    );
  });

  it('disables Save while the retention policy is still loading, then enables it once hydrated', async () => {
    const user = userEvent.setup();
    let resolveRetention!: (v: { keep_last_n: number | null; max_age_days: number | null }) => void;
    getRetentionMock.mockReturnValue(
      new Promise((resolve) => {
        resolveRetention = resolve;
      }),
    );
    renderPage();

    // Still loading: the form is present but Save must be disabled, and clicking
    // a disabled button must never reach putRetention with the blank defaults.
    const saveButton = await screen.findByRole('button', { name: /save retention policy/i });
    expect(saveButton).toBeDisabled();
    await user.click(saveButton);
    expect(putRetentionMock).not.toHaveBeenCalled();

    // Resolve the query: fields hydrate from the real policy and Save unlocks.
    await act(async () => {
      resolveRetention({ keep_last_n: 5, max_age_days: 30 });
    });

    await waitFor(() =>
      expect(screen.getByLabelText(/keep most recent restore points/i)).toHaveValue(5),
    );
    await waitFor(() => expect(saveButton).not.toBeDisabled());

    await user.click(saveButton);
    await waitFor(() =>
      expect(putRetentionMock).toHaveBeenCalledWith({ keep_last_n: 5, max_age_days: 30 }),
    );
  });

  it('shows an error and Retry instead of an editable form when the retention policy fails to load', async () => {
    const user = userEvent.setup();
    getRetentionMock.mockRejectedValue(new Error('network down'));
    renderPage();

    expect(await screen.findByText(/could not load the retention policy/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/keep most recent restore points/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /save retention policy/i })).not.toBeInTheDocument();
    expect(putRetentionMock).not.toHaveBeenCalled();

    getRetentionMock.mockResolvedValue({ keep_last_n: 5, max_age_days: 30 });
    await user.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() =>
      expect(screen.getByLabelText(/keep most recent restore points/i)).toHaveValue(5),
    );
    expect(screen.getByRole('button', { name: /save retention policy/i })).not.toBeDisabled();
  });
});
