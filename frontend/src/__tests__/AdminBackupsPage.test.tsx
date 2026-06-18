import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AdminBackupsPage } from '@/pages/AdminBackupsPage';

const listBackupsMock = vi.fn();
const getBackupStatusMock = vi.fn();
const triggerBackupMock = vi.fn();
const downloadBackupMock = vi.fn();

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/lib/api/backups', () => ({
  listBackups: () => listBackupsMock(),
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

const _entries = [
  {
    filename: 'jarvis_20260617_120000.sql.gz',
    store: 'jarvis',
    size_bytes: 2048,
    modified_at: new Date().toISOString(),
    encrypted: false,
  },
  {
    filename: 'secrets_20260617_120000.tar.gz.enc',
    store: 'secrets',
    size_bytes: 512,
    modified_at: new Date().toISOString(),
    encrypted: true,
  },
];

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
    listBackupsMock.mockResolvedValue(_entries);
    getBackupStatusMock.mockResolvedValue({
      backup_dir_available: true,
      archive_count: 2,
      last_run_at: new Date().toISOString(),
      trigger_pending: false,
    });
  });

  it('renders archive rows with store + encrypted badge', async () => {
    renderPage();
    expect(await screen.findByText('jarvis_20260617_120000.sql.gz')).toBeInTheDocument();
    expect(screen.getByText('secrets_20260617_120000.tar.gz.enc')).toBeInTheDocument();
    // Scope to the badge <span> — "Encrypted" is also the column header <th>.
    expect(screen.getByText('Encrypted', { selector: 'span' })).toBeInTheDocument();
  });

  it('triggers a backup after confirm', async () => {
    triggerBackupMock.mockResolvedValue({ status: 'scheduled' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('jarvis_20260617_120000.sql.gz');
    await user.click(screen.getByRole('button', { name: /run backup now/i }));
    await user.click(await screen.findByRole('button', { name: /^confirm$/i }));
    await waitFor(() => expect(triggerBackupMock).toHaveBeenCalledTimes(1));
  });

  it('calls downloadBackup for a row', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('jarvis_20260617_120000.sql.gz');
    await user.click(screen.getAllByRole('button', { name: /download/i })[0]!);
    await waitFor(() =>
      expect(downloadBackupMock).toHaveBeenCalledWith('jarvis_20260617_120000.sql.gz'),
    );
  });

  it('shows the read-only restore runbook commands', async () => {
    renderPage();
    expect(await screen.findByText(/DROP DATABASE jarvis/)).toBeInTheDocument();
  });
});
