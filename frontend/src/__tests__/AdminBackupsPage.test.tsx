import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AdminBackupsPage } from '@/pages/AdminBackupsPage';

const getRestorePointsMock = vi.fn();
const getBackupStatusMock = vi.fn();
const triggerBackupMock = vi.fn();
const downloadBackupMock = vi.fn();

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/lib/api/backups', () => ({
  getRestorePoints: () => getRestorePointsMock(),
  getBackupStatus: () => getBackupStatusMock(),
  triggerBackup: () => triggerBackupMock(),
  downloadBackup: (name: string) => downloadBackupMock(name),
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
      stores: ['jarvis', 'secrets'],
      qdrant_collections: ['kg_entities'],
      complete: true,
      encrypted: true,
      total_size_bytes: 2560,
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

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/admin/backups']}>
        <Routes>
          <Route path="/admin/backups" element={<AdminBackupsPage />} />
          <Route path="/" element={<div>HOME</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AdminBackupsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    getRestorePointsMock.mockResolvedValue(_restorePoints);
    getBackupStatusMock.mockResolvedValue(_okStatus);
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
    expect(screen.getByText('Main database')).toBeInTheDocument();
    expect(screen.getByText('Secrets')).toBeInTheDocument();
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

  it('shows the read-only restore runbook commands', async () => {
    renderPage();
    expect(await screen.findByText(/DROP DATABASE jarvis/)).toBeInTheDocument();
  });
});
