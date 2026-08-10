/**
 * Tests for ZoteroPanel — setTimeout timer cleanup.
 *
 * Verifies:
 * - copyKey debounces: calling it twice rapidly cancels the prior timer so
 *   only one pending reset remains.
 * - Unmounting the component clears any pending timer so there is no setState
 *   call on an unmounted component.
 * - Clipboard success: "Copied!" appears and reverts after 2 s.
 * - Clipboard failure: "Copy failed" appears (not "Copied!") and reverts after 2 s.
 *
 * Clipboard mocking strategy: @testing-library/user-event installs its own
 * Clipboard stub on window.navigator.clipboard during setup(). We spy on
 * navigator.clipboard.writeText AFTER userEvent.setup() to control resolve/reject.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { screen, act, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ZoteroPanel } from '@/components/paper/ZoteroPanel';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// --- Module mocks ---

const mocks = vi.hoisted(() => ({
  getLinkage: vi.fn(),
  pushPaper: vi.fn(),
  resync: vi.fn(),
  trackExternalJob: vi.fn(),
  isRunning: vi.fn<(kind: string, payload: Record<string, unknown>) => boolean>(() => false),
}));

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  return createApiMock({
    zoteroGetLinkage: mocks.getLinkage,
    zoteroPushPaper: mocks.pushPaper,
    zoteroResync: mocks.resync,
  });
});

vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (state: {
    trackExternalJob: typeof mocks.trackExternalJob;
    isRunning: typeof mocks.isRunning;
  }) => unknown) => selector({
    trackExternalJob: mocks.trackExternalJob,
    isRunning: mocks.isRunning,
  }),
}));

function renderPanel() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <ZoteroPanel paperId={1} hasProjectLinks />,
    { queryClient },
  );
}

describe('ZoteroPanel — setTimeout timer cleanup', () => {
  beforeEach(() => {
    mocks.getLinkage.mockReset();
    mocks.getLinkage.mockResolvedValue({
      zotero_item_key: 'ABCD1234',
      zotero_citation_key: 'smith2024',
    });
    mocks.pushPaper.mockReset();
    mocks.resync.mockReset();
    mocks.trackExternalJob.mockReset();
    mocks.isRunning.mockReset();
    mocks.isRunning.mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows "Copied!" after clicking copy and hides it after 2 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // userEvent.setup() installs its own clipboard stub on window.navigator.clipboard.
    // Its writeText resolves by default, which is what we want for the success case.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPanel();

    const copyBtn = await screen.findByRole('button', { name: /copy citation key/i });
    await user.click(copyBtn);

    expect(screen.getByText('Copied!')).toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(2000); });

    expect(screen.queryByText('Copied!')).not.toBeInTheDocument();
  });

  it('rapid double-click leaves only one pending timer (prior timer cancelled)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPanel();

    const copyBtn = await screen.findByRole('button', { name: /copy citation key/i });

    // First click — starts timer T1.
    await user.click(copyBtn);
    // Second click before 2 s elapses — must cancel T1 and start T2.
    await user.click(copyBtn);

    // clearTimeout must have been called (to cancel T1).
    expect(clearTimeoutSpy).toHaveBeenCalled();

    // Only one "Copied!" badge visible.
    expect(screen.getAllByText('Copied!')).toHaveLength(1);

    // After 2 s from the second click the badge disappears.
    act(() => { vi.advanceTimersByTime(2000); });
    expect(screen.queryByText('Copied!')).not.toBeInTheDocument();

    clearTimeoutSpy.mockRestore();
  });

  it('clipboard success: shows "Copied!" then reverts after 2 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // userEvent.setup() installs its clipboard stub. Spy on it AFTER setup() so we
    // override the stub's writeText to explicitly resolve.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const clipboardSpy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined);

    renderPanel();

    const copyBtn = await screen.findByRole('button', { name: /copy citation key/i });
    await user.click(copyBtn);

    // Success indicator appears, error indicator absent.
    expect(screen.getByText('Copied!')).toBeInTheDocument();
    expect(screen.queryByText('Copy failed')).not.toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(2000); });

    expect(screen.queryByText('Copied!')).not.toBeInTheDocument();

    clipboardSpy.mockRestore();
  });

  it('clipboard failure: shows "Copy failed" (not "Copied!") then reverts after 2 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // Spy on navigator.clipboard AFTER userEvent.setup() installs its stub so we
    // control the rejection from the stub's writeText.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const clipboardSpy = vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('denied'));

    renderPanel();

    const copyBtn = await screen.findByRole('button', { name: /copy citation key/i });
    await user.click(copyBtn);
    // Flush microtasks so the catch block's setCopyState('error') lands.
    await act(async () => {});

    // Error indicator appears, success indicator absent.
    expect(screen.getByText('Copy failed')).toBeInTheDocument();
    expect(screen.queryByText('Copied!')).not.toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(2000); });

    expect(screen.queryByText('Copy failed')).not.toBeInTheDocument();

    clipboardSpy.mockRestore();
  });

  it('unmounting cancels the pending timer — no state update on unmounted component', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { unmount } = renderPanel();

    const copyBtn = await screen.findByRole('button', { name: /copy citation key/i });
    await user.click(copyBtn);

    // Timer is running. Unmount before it fires.
    unmount();

    // The cleanup useEffect must have called clearTimeout.
    expect(clearTimeoutSpy).toHaveBeenCalled();

    // Advance time past the timer — no throw expected.
    act(() => { vi.advanceTimersByTime(2000); });

    clearTimeoutSpy.mockRestore();
  });
});

describe('ZoteroPanel — background job handoff', () => {
  beforeEach(() => {
    mocks.getLinkage.mockReset();
    mocks.getLinkage.mockResolvedValue({
      zotero_item_key: 'ABCD1234',
      zotero_citation_key: 'smith2024',
    });
    mocks.pushPaper.mockReset();
    mocks.resync.mockReset();
    mocks.trackExternalJob.mockReset();
    mocks.isRunning.mockReset();
    mocks.isRunning.mockReturnValue(false);
  });

  it('tracks a queued push instead of invalidating linkage before completion', async () => {
    mocks.getLinkage.mockResolvedValue({
      zotero_item_key: null,
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    });
    mocks.pushPaper.mockResolvedValue({ job_id: 'push-job-1', status: 'queued' });
    const user = userEvent.setup();
    renderPanel();

    await user.click(await screen.findByRole('button', { name: 'Send to Zotero' }));

    await waitFor(() => expect(mocks.trackExternalJob).toHaveBeenCalledWith({
      jobId: 'push-job-1',
      kind: 'zotero.push',
      payload: { paper_id: 1 },
      status: 'queued',
    }));
    expect(mocks.getLinkage).toHaveBeenCalledTimes(1);
  });

  it('tracks a queued resync through the same job store', async () => {
    mocks.resync.mockResolvedValue({ job_id: 'resync-job-1', status: 'queued' });
    const user = userEvent.setup();
    renderPanel();

    await user.click(await screen.findByTitle('Re-push to Zotero'));

    await waitFor(() => expect(mocks.trackExternalJob).toHaveBeenCalledWith({
      jobId: 'resync-job-1',
      kind: 'zotero.resync',
      payload: { paper_id: 1 },
      status: 'queued',
    }));
  });

  it('keeps push disabled while its tracked job is active', async () => {
    mocks.getLinkage.mockResolvedValue({
      zotero_item_key: null,
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    });
    mocks.isRunning.mockImplementation((kind: string) => kind === 'zotero.push');
    renderPanel();

    expect(await screen.findByRole('button', { name: 'Sending…' })).toBeDisabled();
  });
});
