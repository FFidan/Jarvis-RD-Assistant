import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AdminBackupsPage } from '@/pages/AdminBackupsPage';

import { useMaintenanceStore } from '@/stores/maintenance-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const createUploadGrantMock = vi.fn();
const uploadRestoreFileMock = vi.fn();
const getRestorePointsMock = vi.fn();
const getInboxRestorePointsMock = vi.fn();
const getBackupStatusMock = vi.fn();
const getRestoreStatusMock = vi.fn();
const getRetentionMock = vi.fn();

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/lib/api/backups', () => ({
  getRestorePoints: () => getRestorePointsMock(),
  getInboxRestorePoints: () => getInboxRestorePointsMock(),
  getBackupStatus: () => getBackupStatusMock(),
  triggerBackup: vi.fn(),
  downloadBackup: vi.fn(),
  requestRestore: vi.fn(),
  getRestoreStatus: (token?: string) => getRestoreStatusMock(token),
  deleteRestorePoint: vi.fn(),
  getRetention: () => getRetentionMock(),
  putRetention: vi.fn(),
  createUploadGrant: () => createUploadGrantMock(),
  uploadRestoreFile: (
    ...args: [string, Blob, string, ((percent: number) => void)?, AbortSignal?]
  ) => uploadRestoreFileMock(...args),
}));

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: (selector?: (s: { user: { role: 'admin' } } ) => unknown) => {
    const state = { user: { role: 'admin' as const } };
    return selector ? selector(state) : state;
  },
}));

function renderPage() {
  const qc = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter initialEntries={['/admin/backups']}>
      <Routes>
        <Route path="/admin/backups" element={<AdminBackupsPage />} />
      </Routes>
    </MemoryRouter>,
    { queryClient: qc },
  );
}

/** Render the page, mint a grant, and return the mounted upload section. */
async function openSectionWithGrant(user: ReturnType<typeof userEvent.setup>) {
  renderPage();
  const section = await screen.findByTestId('offhost-upload-section');
  await user.click(within(section).getByTestId('generate-upload-grant'));
  await within(section).findByTestId('upload-grant-countdown');
  return section;
}

describe('OffHostUploadSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMaintenanceStore.getState().clear();
    getRestorePointsMock.mockResolvedValue({
      retention_days: null,
      last_run: null,
      restore_points: [],
    });
    getInboxRestorePointsMock.mockResolvedValue([]);
    getBackupStatusMock.mockResolvedValue({
      backup_dir_available: true,
      archive_count: 0,
      last_run_at: null,
      trigger_pending: false,
      last_attempt_at: null,
      last_run_succeeded: null,
    });
    getRetentionMock.mockResolvedValue({ keep_last_n: null, max_age_days: null });
    getRestoreStatusMock.mockResolvedValue({ state: 'idle' });
    createUploadGrantMock.mockResolvedValue({ grant_token: 'grant-tok-1', expires_in_seconds: 1800 });
    uploadRestoreFileMock.mockResolvedValue(undefined);
  });

  it('the real api module exports createUploadGrant and uploadRestoreFile', async () => {
    const actual =
      await vi.importActual<typeof import('@/lib/api/backups')>('@/lib/api/backups');
    expect(typeof actual.createUploadGrant).toBe('function');
    expect(typeof actual.uploadRestoreFile).toBe('function');
  });

  it('mints an upload grant and shows the 30-minute expiry countdown', async () => {
    const user = userEvent.setup();
    const section = await openSectionWithGrant(user);
    expect(createUploadGrantMock).toHaveBeenCalledTimes(1);
    expect(within(section).getByTestId('upload-grant-countdown')).toHaveTextContent(
      /30:00|29:5\d/,
    );
  });

  it('uploads an allowlisted file with the grant token and refreshes the staged inbox', async () => {
    const user = userEvent.setup();
    const section = await openSectionWithGrant(user);
    expect(getInboxRestorePointsMock).toHaveBeenCalledTimes(1);

    const file = new File(['dump'], 'jarvis_20260617_120000.sql.gz');
    await user.upload(within(section).getByTestId('upload-file-input'), file);
    await user.click(within(section).getByTestId('upload-start'));

    await waitFor(() =>
      expect(uploadRestoreFileMock).toHaveBeenCalledWith(
        'jarvis_20260617_120000.sql.gz',
        expect.any(File),
        'grant-tok-1',
        expect.any(Function),
      ),
    );
    // Completion invalidates ['admin','backups','inbox'] so the staged set refetches.
    await waitFor(() => expect(getInboxRestorePointsMock).toHaveBeenCalledTimes(2));
    expect(await within(section).findByText('Uploaded')).toBeInTheDocument();
  });

  it('uploads the picked key file under the literal operator_key name', async () => {
    const user = userEvent.setup();
    const section = await openSectionWithGrant(user);

    const key = new File(['k'], 'my-downloaded-key.txt');
    await user.upload(within(section).getByTestId('operator-key-input'), key);
    await user.click(within(section).getByTestId('upload-start'));

    await waitFor(() =>
      expect(uploadRestoreFileMock).toHaveBeenCalledWith(
        'operator_key',
        expect.any(File),
        'grant-tok-1',
        expect.any(Function),
      ),
    );
  });

  it('rejects a non-allowlisted filename before any PUT', async () => {
    const user = userEvent.setup();
    const section = await openSectionWithGrant(user);

    const evil = new File(['x'], 'evil_20260617_120000.sql.gz');
    await user.upload(within(section).getByTestId('upload-file-input'), evil);

    expect(await within(section).findByTestId('upload-rejected')).toHaveTextContent(
      'evil_20260617_120000.sql.gz',
    );
    expect(within(section).queryAllByTestId('upload-file-row')).toHaveLength(0);
    expect(within(section).getByTestId('upload-start')).toBeDisabled();
    expect(uploadRestoreFileMock).not.toHaveBeenCalled();
  });

  // Pins the client ALLOWED_FILENAME_RE against the canonical shapes the sidecar's
  // _FILENAME_RE (services/restore_uploader/uploader.py) accepts, one per alternative,
  // so the mirrored regexes cannot silently drift apart.
  it('accepts one canonical filename of each sidecar allowlist shape', async () => {
    const user = userEvent.setup();
    const section = await openSectionWithGrant(user);

    const names = [
      'jarvis_20260101_000000.sql.gz',
      'litellm_20260101_000000.sql.gz.enc',
      'secrets_20260101_000000.tar.gz',
      'qdrant_papers_20260101_000000.snapshot',
      'manifest_20260101_000000.json',
      'operator_key',
    ];
    await user.upload(
      within(section).getByTestId('upload-file-input'),
      names.map((name) => new File(['x'], name)),
    );

    const rows = await within(section).findAllByTestId('upload-file-row');
    expect(rows.map((row) => row.querySelector('.font-mono')?.textContent)).toEqual(names);
    expect(within(section).queryByTestId('upload-rejected')).not.toBeInTheDocument();
    expect(within(section).getByTestId('upload-start')).toBeEnabled();
  });

  it('shows the failure with its error message and Retry re-invokes the upload', async () => {
    const user = userEvent.setup();
    const section = await openSectionWithGrant(user);
    uploadRestoreFileMock.mockRejectedValueOnce(
      new Error('Upload grant invalid or expired — generate a new grant, then retry.'),
    );

    const file = new File(['dump'], 'jarvis_20260617_120000.sql.gz');
    await user.upload(within(section).getByTestId('upload-file-input'), file);
    await user.click(within(section).getByTestId('upload-start'));

    const row = await within(section).findByTestId('upload-file-row');
    expect(
      await within(row).findByText(
        'Upload grant invalid or expired — generate a new grant, then retry.',
      ),
    ).toBeInTheDocument();
    const retry = within(row).getByRole('button', { name: 'Retry' });
    expect(uploadRestoreFileMock).toHaveBeenCalledTimes(1);

    await user.click(retry);

    await waitFor(() => expect(uploadRestoreFileMock).toHaveBeenCalledTimes(2));
    expect(await within(row).findByText('Uploaded')).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });
});
