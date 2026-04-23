/**
 * Tests for ZoteroSection (Settings → Zotero).
 *
 * Covers FE-002: zoteroPollNow returns { job_id: string; status: string }
 * and the "Sync now" button wires the response into the job store via
 * trackExternalJob so progress is tracked and the zotero-library query
 * is invalidated on job success.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ZoteroSection } from '@/components/settings/ZoteroSection';

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
      getApiKey: vi.fn(() => 'test-key'),
      logout: vi.fn(),
    })),
  },
}));

const { fetchConfig, zoteroPollNow } = await import('@/lib/api');
const { useJobStore } = await import('@/stores/job-store');

// Config with Zotero configured and poll enabled so the "Sync now" button renders
const CONFIGURED_CONFIG = [
  { key: 'zotero.api_key', value: 'abc123' },
  { key: 'zotero.user_id', value: '1234567' },
  { key: 'zotero.library_type', value: 'user' },
  { key: 'zotero.auto_push_on_star', value: 'false' },
  { key: 'zotero.poll_enabled', value: 'true' },
  { key: 'zotero.poll_cron', value: '0 * * * *' },
];

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ZoteroSection />
        </MemoryRouter>
      </QueryClientProvider>,
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
    } as ReturnType<typeof useJobStore.getState>);

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

  it('does not throw when zoteroPollNow rejects (silently ignored)', async () => {
    vi.mocked(zoteroPollNow).mockRejectedValue(new Error('network error'));
    const user = userEvent.setup();
    renderSection();

    const btn = await screen.findByRole('button', { name: /sync now/i });
    // Should not throw — error is swallowed
    await expect(user.click(btn)).resolves.toBeUndefined();
  });
});

