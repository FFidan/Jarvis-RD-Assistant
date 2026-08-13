/**
 * Tests for ZoteroSection (Settings → Zotero).
 *
 * Covers: zoteroPollNow returns { job_id: string; status: string }
 * and the "Sync now" button wires the response into the job store via
 * trackExternalJob so progress is tracked and the zotero-library query
 * is invalidated on job success.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { ZoteroSection } from '@/components/settings/ZoteroSection';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// --- Module mocks ---

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({ key: '', value: null }),
    zoteroTest: vi.fn(),
    zoteroPollNow: vi.fn(),
    listJobs: vi.fn().mockResolvedValue([]),
    cancelJob: vi.fn(),
    getJob: vi.fn(),
  };
});

vi.mock('@/stores/job-store', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/stores/job-store')>();
  return {
    ...orig,
    useJobStore: {
      ...orig.useJobStore,
      getState: vi.fn(() => ({
        trackExternalJob: vi.fn(),
        jobs: {},
        activeAborts: {},
      })),
    },
  };
});

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      logout: vi.fn(),
    })),
  },
}));

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

const { fetchConfig, setConfig, zoteroPollNow, zoteroTest } = await import('@/lib/api');
const { useJobStore } = await import('@/stores/job-store');

// Config with Zotero configured and poll enabled so the "Sync now" button renders
const CONFIGURED_CONFIG = [
  { key: 'zotero.api_key', value: 'abc123' },
  { key: 'zotero.user_id', value: '1234567' },
  { key: 'zotero.library_type', value: 'user' },
  { key: 'zotero.auto_push_on_star', value: 'false' },
  { key: 'zotero.poll_enabled', value: 'true' },
  { key: 'zotero.poll_cron', value: '0 * * * *' },
  { key: 'zotero.allowed_private_hosts', value: ['zotero.lan'] },
];

function renderSection() {
  const queryClient = createTestQueryClient();
  return {
    ...renderWithProviders(
      <MemoryRouter>
        <ZoteroSection />
      </MemoryRouter>,
      { queryClient },
    ),
  };
}

describe('ZoteroSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue(CONFIGURED_CONFIG);
  });

  it('renders the Sync now button when poll is enabled', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /sync now/i })).toBeInTheDocument();
    });
  });

  it('renders the blank form, not an error, when config loads empty', async () => {
    vi.mocked(fetchConfig).mockResolvedValue([]);
    renderSection();
    expect(await screen.findByLabelText('API Key')).toBeInTheDocument();
    expect(screen.queryByText('Failed to load Zotero settings.')).toBeNull();
  });

  it('shows an error message, not a blank form, when config fails to load', async () => {
    vi.mocked(fetchConfig).mockRejectedValue(new Error('network down'));
    renderSection();
    expect(await screen.findByText('Failed to load Zotero settings.')).toBeInTheDocument();
    expect(screen.queryByLabelText('API Key')).toBeNull();
  });

  it('calls zoteroPollNow on Sync now click', async () => {
    vi.mocked(zoteroPollNow).mockResolvedValue({ job_id: 'jid-123', status: 'queued' });
    const user = userEvent.setup();
    renderSection();

    const btn = await screen.findByRole('button', { name: /sync now/i });
    await user.click(btn);

    await waitFor(() => {
      expect(vi.mocked(zoteroPollNow)).toHaveBeenCalledTimes(1);
    });
  });

  it('calls trackExternalJob with job_id and kind=zotero.poll after Sync now', async () => {
    vi.mocked(zoteroPollNow).mockResolvedValue({ job_id: 'jid-123', status: 'queued' });
    const trackExternalJob = vi.fn();
    vi.mocked(useJobStore.getState).mockReturnValue({
      trackExternalJob,
      jobs: {},
      activeAborts: {},
    } as unknown as ReturnType<typeof useJobStore.getState>);

    const user = userEvent.setup();
    renderSection();

    const btn = await screen.findByRole('button', { name: /sync now/i });
    await user.click(btn);

    await waitFor(() => {
      expect(trackExternalJob).toHaveBeenCalledWith({
        jobId: 'jid-123',
        kind: 'zotero.poll',
        payload: {},
        status: 'queued',
      });
    });
  });

  it('offers immediate library sync after changing Zotero library scope', async () => {
    vi.mocked(fetchConfig).mockResolvedValue(
      CONFIGURED_CONFIG.map((entry) =>
        entry.key === 'zotero.poll_enabled' ? { ...entry, value: 'false' } : entry,
      ),
    );
    vi.mocked(setConfig).mockResolvedValue({ key: 'zotero.library_type', value: 'group' });
    vi.mocked(zoteroPollNow).mockResolvedValue({ job_id: 'jid-scope', status: 'queued' });
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByLabelText('Group library'));

    expect(await screen.findByText(/Library identity changed/i)).toBeInTheDocument();
    const syncButton = screen.getByRole('button', { name: /run library sync now/i });
    await user.click(syncButton);

    await waitFor(() => {
      expect(vi.mocked(zoteroPollNow)).toHaveBeenCalledTimes(1);
    });
  });

  it('does not throw when zoteroPollNow rejects and shows an error', async () => {
    const { toast } = await import('sonner');
    vi.mocked(zoteroPollNow).mockRejectedValue(new Error('network error'));
    const user = userEvent.setup();
    renderSection();

    const btn = await screen.findByRole('button', { name: /sync now/i });
    await expect(user.click(btn)).resolves.toBeUndefined();
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Zotero sync failed to queue.');
    });
  });

  it('keeps Zotero user ID and group ID as distinct fields for group libraries', async () => {
    vi.mocked(fetchConfig).mockResolvedValue(
      CONFIGURED_CONFIG.map((entry) =>
        entry.key === 'zotero.library_type' ? { ...entry, value: 'group' } : entry,
      ),
    );

    renderSection();

    expect(await screen.findByLabelText('User ID')).toBeInTheDocument();
    expect(await screen.findByLabelText('Group ID')).toBeInTheDocument();
  });

  it('writes Zotero boolean settings as booleans, not strings', async () => {
    const user = userEvent.setup();
    renderSection();

    const autoPush = await screen.findByRole('switch', { name: 'Auto-push on star' });
    expect(screen.getByRole('switch', { name: 'Enable Zotero → JARVIS sync' })).toBeInTheDocument();
    await user.click(autoPush);

    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith('zotero.auto_push_on_star', true);
    });
  });

  it('keeps Test connection disabled until credentials are present', async () => {
    vi.mocked(fetchConfig).mockResolvedValue(
      CONFIGURED_CONFIG.map((entry) =>
        entry.key === 'zotero.user_id' ? { ...entry, value: '' } : entry,
      ),
    );

    renderSection();

    expect(await screen.findByRole('button', { name: /test connection/i })).toBeDisabled();
    expect(vi.mocked(zoteroTest)).not.toHaveBeenCalled();
  });

  it('shows "Connected" when the connection test succeeds', async () => {
    vi.mocked(zoteroTest).mockResolvedValue({ success: true });
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /test connection/i }));

    expect(await screen.findByText('Connected')).toBeInTheDocument();
  });

  it('shows the error from a failed connection test', async () => {
    vi.mocked(zoteroTest).mockResolvedValue({
      success: false,
      error: 'Zotero API key or user ID not configured',
    });
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /test connection/i }));

    expect(
      await screen.findByText('Zotero API key or user ID not configured'),
    ).toBeInTheDocument();
  });

  it('does not save the custom cron schedule until Save is clicked', async () => {
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Advanced' }));
    const cronInput = await screen.findByLabelText('Custom sync schedule (cron)');
    await user.clear(cronInput);
    await user.type(cronInput, '15 3 1 * 2');
    await user.tab(); // blur must not save

    expect(vi.mocked(setConfig)).not.toHaveBeenCalledWith('zotero.poll_cron', expect.anything());

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith('zotero.poll_cron', '15 3 1 * 2');
    });
  });

  it('keeps the Save button disabled until a field is edited', async () => {
    const user = userEvent.setup();
    renderSection();

    const saveButton = await screen.findByRole('button', { name: 'Save' });
    expect(saveButton).toBeDisabled();

    const apiKeyInput = screen.getByLabelText('API Key');
    await user.clear(apiKeyInput);
    await user.type(apiKeyInput, 'newkey123');

    expect(saveButton).toBeEnabled();
  });

  it('saves the Better BibTeX hosts as a list when Save is clicked', async () => {
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Advanced' }));
    const hostsInput = await screen.findByLabelText('Better BibTeX hosts');
    expect(hostsInput).toHaveValue('zotero.lan');

    await user.clear(hostsInput);
    await user.type(hostsInput, 'zotero.lan, 192.168.1.50');
    await user.tab(); // blur must not save

    expect(vi.mocked(setConfig)).not.toHaveBeenCalledWith(
      'zotero.allowed_private_hosts',
      expect.anything(),
    );

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith('zotero.allowed_private_hosts', [
        'zotero.lan',
        '192.168.1.50',
      ]);
    });
  });

  it('shows a toast when saving a Zotero setting fails', async () => {
    const { toast } = await import('sonner');
    vi.mocked(setConfig).mockRejectedValue(new Error('network error'));
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Advanced' }));
    const hostsInput = await screen.findByLabelText('Better BibTeX hosts');
    await user.clear(hostsInput);
    await user.type(hostsInput, 'zotero.lan, newhost.example');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('network error');
    });
  });

  it('issues zero config writes when mounting with any stored schedule value and polling enabled', async () => {
    const scheduleValues = ['0 * * * *', '0 */4 * * *', '30 7 * * *', '0 9 * * 1', '15 3 1 * 2'];

    for (const cron of scheduleValues) {
      vi.mocked(setConfig).mockClear();
      vi.mocked(fetchConfig).mockResolvedValue(
        CONFIGURED_CONFIG.map((entry) =>
          entry.key === 'zotero.poll_cron' ? { ...entry, value: cron } : entry,
        ),
      );

      const { unmount } = renderSection();
      await screen.findByRole('button', { name: /sync now/i });

      expect(vi.mocked(setConfig)).not.toHaveBeenCalled();
      unmount();
    }
  });
});
