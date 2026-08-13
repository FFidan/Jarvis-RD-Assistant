/**
 * Unified OnboardingWizard tests (Task A2 — wizard consolidation).
 *
 * Covers:
 *   (a) fresh install (configured=false, setup_completed=false) renders step 1.
 *   (b) admin-create flips authed and advances past the admin step.
 *   (c) configured=true + setup_completed=false SKIPS the admin step.
 *   (d) finishing sets setup_completed via setQueryData and navigates to '/'
 *       WITHOUT bouncing back to the wizard (BUG-2 regression).
 *   (e) the Telegram step button shows 'Next' when getTelegramPairing returns
 *       {paired:true} (BUG-1).
 *   (f) topic/automation CTAs reflect server state on mount (SETUP-STEP-STATE).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useJobStore } from '@/stores/job-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

function LocationDisplay() {
  const location = useLocation();
  return (
    <>
      <span data-testid="location-search">{location.search}</span>
      <span data-testid="location-hash">{location.hash}</span>
    </>
  );
}

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  return createApiMock({
  getFirstRunStatus: async () => ({ configured: false, setup_completed: false }),
  runFirstRunSystemCheck: async () => ({
    services: [{ name: 'postgres', ok: true, detail: null }],
    all_ok: true,
  }),
  saveFirstRunSmtp: async () => ({ saved: true, test_sent: null, test_error: null }),
  getSmtpConfig: async () => ({
    host: null,
    port: null,
    user: null,
    from_email: null,
    reply_to: null,
    from_name: null,
    has_password: false,
    restart_required: false,
    deliverable: true,
    issues: [],
  }),
  createFirstRunAdmin: async () => ({ id: 1, email: 'admin@example.com', role: 'admin' }),
  saveFirstRunCloudKeys: async () => ({ saved_providers: [], applied_now: [], restart_required: false }),
  getSetupStatus: async () => ({
    setup_completed: false,
    models_ready: true,
    models_downloading: [],
    topics_count: 0,
    telegram_configured: false,
    telegram_paired: false,
  }),
  createTopic: async () => ({ id: 1, name: 'test' }),
  setConfig: async () => ({ key: 'pulse.cron', value: '0 4 * * *' }),
  fetchConfig: async () => ([]),
  fetchSources: async () => ([]),
  updateSource: vi.fn(),
  markSetupCompleted: async () => (undefined),
  getTelegramPairing: async () => ({ paired: false, chat_id: null, telegram_username: null, paired_at: null }),
  removeTelegramPairing: vi.fn(),
  requestTelegramPairToken: vi.fn(),
  });
});

vi.mock('@/lib/api/pulse', () => ({
  generatePulseNow: vi.fn(async () => ({ job_id: 'pulse-job-1', status: 'queued' })),
}));

const api = await import('@/lib/api');
const pulseApi = await import('@/lib/api/pulse');
// The barrel above is fully mocked; ApiError comes from the un-mocked core so
// it is the same class AdminStep checks with `instanceof`.
const { ApiError } = await import('@/lib/api/core');
const { OnboardingWizard, isRemotePlainHttp } = await import('@/pages/OnboardingWizard');
const { useAuthStore } = await import('@/stores/auth-store');
const { resetAuthState } = await import('@/__tests__/auth-test-utils');

type FirstRun = {
  configured: boolean;
  setup_completed: boolean;
  setup_mode?: 'single' | 'multi';
  hw_tier_current?: string | null;
  recommended_backend?: string | null;
  gpu_vendor?: string;
};

function renderWizard(
  firstRun: FirstRun = { configured: false, setup_completed: false },
  authed = false,
  initialUrl = '/?step=1',
  client?: QueryClient,
) {
  const queryClient = client ?? createTestQueryClient();
  const utils = renderWithProviders(
    <MemoryRouter initialEntries={[initialUrl]}>
      <Routes>
        {/* Wizard reads ?step= from the URL at any path. */}
        <Route
          path="/"
          element={
            <>
              <LocationDisplay />
              <OnboardingWizard firstRun={firstRun} authed={authed} />
            </>
          }
        />
        <Route path="/done-marker" element={<div>DASHBOARD</div>} />
      </Routes>
    </MemoryRouter>,
    { queryClient },
  );
  return { ...utils, queryClient };
}

// Total steps when admin step is present (fresh install) = 9; the URL ?step is
// 1-based into the effective sequence:
//   1 welcome, 2 smtp, 3 admin, 4 cloud, 5 topic, 6 automation, 7 sources,
//   8 telegram, 9 done.
// When configured (admin skipped) the sequence is 8 long:
//   1 welcome, 2 smtp, 3 cloud, 4 topic, 5 automation, 6 sources, 7 telegram, 8 done.

describe('OnboardingWizard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    resetAuthState();
    // Every `authed=true` fixture in this file represents a signed-in admin
    // (the wizard's original assumption before topic-step gating existed);
    // individual tests override the role where the gating itself is under test.
    useAuthStore.setState({
      isAuthenticated: true,
      user: { id: 1, email: 'admin@example.com', role: 'admin' },
    });
    useJobStore.getState()._reset();
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({ configured: false, setup_completed: false });
    vi.mocked(api.getSetupStatus).mockResolvedValue({
      setup_completed: false,
      models_ready: true,
      models_downloading: [],
      topics_count: 0,
      telegram_configured: false,
      telegram_paired: false,
    });
    vi.mocked(api.getTelegramPairing).mockResolvedValue({
      paired: false,
      chat_id: null,
      telegram_username: null,
      paired_at: null,
    });
    vi.mocked(api.fetchConfig).mockResolvedValue([]);
  });

  it('treats raw-IP HTTP as diagnostics while preserving localhost HTTP', () => {
    expect(isRemotePlainHttp({ protocol: 'http:', hostname: '10.0.0.17' })).toBe(true);
    expect(isRemotePlainHttp({ protocol: 'http:', hostname: 'jarvis.lan' })).toBe(true);
    expect(isRemotePlainHttp({ protocol: 'http:', hostname: 'localhost' })).toBe(false);
    expect(isRemotePlainHttp({ protocol: 'http:', hostname: '127.0.0.1' })).toBe(false);
    expect(isRemotePlainHttp({ protocol: 'https:', hostname: 'jarvis.example.ts.net' })).toBe(false);
  });



  it('single-user first run puts admin creation before optional SMTP', async () => {
    renderWizard(
      { configured: false, setup_completed: false, setup_mode: 'single' },
      false,
      '/?step=2',
    );

    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();
    expect(screen.queryByText('SMTP relay')).not.toBeInTheDocument();
    expect(screen.getByText('Step 2 of 9')).toBeInTheDocument();
  });

  it('Done step offers first discovery and tracks the queued pulse job', async () => {
    const user = userEvent.setup();
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(QUERY_KEYS.setup.firstRun(), { configured: true, setup_completed: false });

    renderWizard({ configured: true, setup_completed: false }, true, '/?step=8', queryClient);

    expect(await screen.findByText('Start discovery')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /discover papers now/i }));

    await waitFor(() => {
      expect(pulseApi.generatePulseNow).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(useJobStore.getState().jobs['pulse-job-1']?.kind).toBe('pulse.generate');
    });
  });

  it('Done step tracks a queued discovery job', async () => {
    const user = userEvent.setup();
    vi.mocked(pulseApi.generatePulseNow).mockResolvedValueOnce({ job_id: 'pulse-job-running', status: 'queued' });
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(QUERY_KEYS.setup.firstRun(), { configured: true, setup_completed: false });

    renderWizard({ configured: true, setup_completed: false }, true, '/?step=8', queryClient);

    expect(await screen.findByText('Start discovery')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /discover papers now/i }));

    await waitFor(() => {
      expect(useJobStore.getState().jobs['pulse-job-running']?.status).toBe('queued');
    });
  });

  it('Done step keeps dashboard navigation available after discovery rate-limit errors', async () => {
    const user = userEvent.setup();
    vi.mocked(pulseApi.generatePulseNow).mockRejectedValueOnce(new Error('429 Too Many Requests'));
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(QUERY_KEYS.setup.firstRun(), { configured: true, setup_completed: false });

    renderWizard({ configured: true, setup_completed: false }, true, '/?step=8', queryClient);

    expect(await screen.findByText('Start discovery')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /discover papers now/i }));

    expect(await screen.findByText(/429 Too Many Requests/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go to dashboard/i })).toBeEnabled();
  });

  it('Done step keeps dashboard navigation available when discovery is already running', async () => {
    const user = userEvent.setup();
    vi.mocked(pulseApi.generatePulseNow).mockRejectedValueOnce(new Error('409 Conflict'));
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(QUERY_KEYS.setup.firstRun(), { configured: true, setup_completed: false });

    renderWizard({ configured: true, setup_completed: false }, true, '/?step=8', queryClient);

    expect(await screen.findByText('Start discovery')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /discover papers now/i }));

    expect(await screen.findByText(/409 Conflict/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go to dashboard/i })).toBeEnabled();
  });

  it('multi-user first run preserves optional SMTP before admin creation', async () => {
    renderWizard(
      { configured: false, setup_completed: false, setup_mode: 'multi' },
      false,
      '/?step=2',
    );

    expect(await screen.findByText('SMTP relay')).toBeInTheDocument();
    expect(screen.getByText(/one-time sign-in links/i)).toHaveTextContent(/Admin → Users/);
    expect(screen.queryByText(/stdout/i)).not.toBeInTheDocument();
    expect(screen.queryByText('Create your admin account')).not.toBeInTheDocument();
    expect(screen.getByText('Step 2 of 9')).toBeInTheDocument();
  });

  // (a)
  it('fresh install renders step 1 (welcome + system check) and runs the probe', async () => {
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=1');
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
    expect(screen.getByText('Step 1 of 9')).toBeInTheDocument();
    await waitFor(() => {
      expect(api.runFirstRunSystemCheck).toHaveBeenCalled();
    });
  });

  // 2B.1: the welcome step renders the already-fetched firstRun hardware
  // fields (tier/backend/vendor) with zero new fetches.
  it('welcome step shows the detected hardware tier, backend, and GPU vendor from firstRun', async () => {
    renderWizard(
      {
        configured: false,
        setup_completed: false,
        hw_tier_current: 'tier-2',
        recommended_backend: 'ollama',
        gpu_vendor: 'nvidia',
      },
      false,
      '/?step=1',
    );
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
    const hardware = screen.getByTestId('detected-hardware');
    expect(hardware).toHaveTextContent('tier-2');
    expect(hardware).toHaveTextContent('ollama');
    expect(hardware).toHaveTextContent('nvidia');
  });

  it('welcome step omits the detected-hardware block when firstRun has no hw_tier_current', async () => {
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=1');
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
    expect(screen.queryByTestId('detected-hardware')).not.toBeInTheDocument();
  });

  // (b) admin-create flips authed and advances past the admin step.
  it('admin step calls createFirstRunAdmin, stores the session, and advances', async () => {
    const user = userEvent.setup();
    // Start directly on the admin step (step 3 in the fresh-install sequence).
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=3');

    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();
    await user.type(screen.getByLabelText(/admin email/i), 'admin@example.com');
    await user.click(screen.getByRole('button', { name: /create admin & sign in/i }));

    await waitFor(() => {
      expect(api.createFirstRunAdmin).toHaveBeenCalled();
      expect(vi.mocked(api.createFirstRunAdmin).mock.calls[0]?.[0]).toBe('admin@example.com');
    });
    // Session mirrored into the auth store.
    await waitFor(() => {
      const state = useAuthStore.getState();
      expect(state.isAuthenticated).toBe(true);
      expect(state.user?.role).toBe('admin');
    });
    // Advanced to the next step (Cloud LLM keys).
    expect(await screen.findByText(/Cloud LLM keys/i)).toBeInTheDocument();
  });

  it('admin step explains the no-SMTP family invite and passkey path', async () => {
    renderWizard({ configured: false, setup_completed: false, setup_mode: 'multi' }, false, '/?step=3');

    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();
    expect(screen.getByText(/one-time sign-in link/i)).toHaveTextContent(/without email/i);
    expect(screen.getByText(/one-time sign-in link/i)).toHaveTextContent(/passkey/i);
  });

  // (c) configured=true skips the admin step → step 3 is Cloud, not Admin.
  it('configured install skips the admin step (step 3 is Cloud LLM, not Admin)', async () => {
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=3');
    expect(await screen.findByText(/Cloud LLM keys/i)).toBeInTheDocument();
    expect(screen.queryByText('Create your admin account')).not.toBeInTheDocument();
    // Effective sequence is 8 steps long when the admin step is skipped.
    expect(screen.getByText('Step 3 of 8')).toBeInTheDocument();
  });

  // (d) finishing sets setup_completed via setQueryData (no bounce — BUG-2).
  it('Done step marks completion, writes setup_completed to the firstRun cache, and navigates home', async () => {
    const queryClient = createTestQueryClient();
    // Seed the firstRun cache the way the gate would have populated it.
    queryClient.setQueryData(QUERY_KEYS.setup.firstRun(), { configured: true, setup_completed: false });

    // Done is step 8 in the configured (admin-skipped) sequence.
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=8', queryClient);

    await waitFor(() => {
      expect(api.markSetupCompleted).toHaveBeenCalledTimes(1);
    });
    // BUG-2 regression: the firstRun cache the gate reads must flip to
    // setup_completed=true BEFORE navigation, so the gate does not bounce the
    // user back into the wizard.
    await waitFor(() => {
      const cached = queryClient.getQueryData<FirstRun>(QUERY_KEYS.setup.firstRun());
      expect(cached?.setup_completed).toBe(true);
    });
  });

  // (e) Telegram step button shows 'Next' when paired (BUG-1).
  it('Telegram step button shows Next when getTelegramPairing reports paired', async () => {
    vi.mocked(api.getTelegramPairing).mockResolvedValue({
      paired: true,
      chat_id: 123,
      telegram_username: 'me',
      paired_at: '2026-01-01T00:00:00Z',
    });
    // Telegram is step 7 in the configured (admin-skipped) sequence.
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=7');
    expect(await screen.findByText(/Pair Telegram/i)).toBeInTheDocument();
    // The footer advance button reads "Next" (not "Skip for now") when paired.
    expect(await screen.findByRole('button', { name: /^next$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /skip for now/i })).not.toBeInTheDocument();
  });

  it('Telegram step button shows Skip for now when not paired', async () => {
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=7');
    expect(await screen.findByText(/Pair Telegram/i)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /skip for now/i })).toBeInTheDocument();
  });

  // (f) topic CTA reflects server state on mount (SETUP-STEP-STATE).
  it('topic step shows Next (not Skip) when the server already has topics', async () => {
    vi.mocked(api.getSetupStatus).mockResolvedValue({
      setup_completed: false,
      models_ready: true,
      models_downloading: [],
      topics_count: 2,
      telegram_configured: false,
      telegram_paired: false,
    });
    // Topic is step 4 in the configured (admin-skipped) sequence.
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=4');
    expect(await screen.findByText('Your first research topic')).toBeInTheDocument();
    // The advance button reads "Next" because topics_count > 0.
    expect(await screen.findByRole('button', { name: /^next$/i })).toBeInTheDocument();
  });

  // (f) automation CTA reflects persisted config on mount (SETUP-STEP-STATE).
  it('automation step shows Next + seeds saved badge when pulse config is persisted', async () => {
    vi.mocked(api.fetchConfig).mockResolvedValue([
      { key: 'pulse.cron', value: '30 6 * * *' },
      { key: 'pulse.enabled', value: true },
    ]);
    // Automation is step 5 in the configured (admin-skipped) sequence.
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=5');
    expect(await screen.findByText('Automation schedule')).toBeInTheDocument();
    // The advance button reads "Next" because config is already persisted.
    expect(await screen.findByRole('button', { name: /^next$/i })).toBeInTheDocument();
  });

  // (F3) a partial save (cron persisted, enabling Pulse failed) reports the
  // truth: the config query still refreshes, but neither the green indicator
  // nor the footer's "Next" flip may claim a save that did not fully happen.
  it('automation step: partial save invalidates config but withholds the saved indicator', async () => {
    const user = userEvent.setup();
    vi.mocked(api.setConfig).mockImplementation((key: string, value: unknown) =>
      key === 'pulse.enabled'
        ? Promise.reject(new Error('boom'))
        : Promise.resolve({ key, value } as never),
    );
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=5');
    expect(await screen.findByText('Automation schedule')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /save schedule/i }));

    expect(await screen.findByText(/enabling pulse failed/i)).toBeInTheDocument();
    expect(screen.queryByText('Saved')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /skip for now/i })).toBeInTheDocument();
    // The config query still refetches even though the save was only partial.
    await waitFor(() => {
      expect(api.fetchConfig).toHaveBeenCalledTimes(2);
    });
  });

  // A clean save shows the green indicator with no partial warning.
  it('automation step: a clean save shows the saved indicator and no warning', async () => {
    const user = userEvent.setup();
    vi.mocked(api.setConfig).mockResolvedValue({ key: 'pulse.cron', value: '0 4 * * *' });
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=5');
    expect(await screen.findByText('Automation schedule')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /save schedule/i }));

    expect(await screen.findByText('Saved')).toBeInTheDocument();
    expect(screen.queryByText(/enabling pulse failed/i)).not.toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /^next$/i })).toBeInTheDocument();
  });

  // Task 19 (F4): the topic step's admin-only API means an authenticated
  // non-admin's wizard must not offer it.
  it('hides the topic step for an authenticated non-admin', async () => {
    useAuthStore.setState({
      isAuthenticated: true,
      user: { id: 2, email: 'member@example.com', role: 'user' },
    });
    // Topic would be step 4 if offered; without it, step 4 is automation.
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=4');
    expect(await screen.findByText('Automation schedule')).toBeInTheDocument();
    expect(screen.queryByText('Your first research topic')).not.toBeInTheDocument();
  });

  it('shows the topic step for an authenticated admin', async () => {
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=4');
    expect(await screen.findByText('Your first research topic')).toBeInTheDocument();
  });

  // The !authed term: during first run the operator is not yet authenticated,
  // so the wizard must still offer the topic step to the admin-to-be.
  it('shows the topic step for an unauthenticated first-run wizard', async () => {
    // No session exists yet — a real first-run visitor, not the admin default
    // this file's beforeEach seeds for the authed=true fixtures.
    resetAuthState();
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=5');
    expect(await screen.findByText('Your first research topic')).toBeInTheDocument();
  });

  // GAP-2: markSetupCompleted rejects → Done step renders error UI + retry calls it again.
  it('(GAP-2) Done step: markSetupCompleted rejects → shows error UI, retry calls it again, on success cache flips', async () => {
    const user = userEvent.setup();
    // First call rejects; second call (retry) resolves.
    vi.mocked(api.markSetupCompleted)
      .mockRejectedValueOnce(new Error('503 Service Unavailable'))
      .mockResolvedValueOnce(undefined);

    const queryClient = createTestQueryClient();
    queryClient.setQueryData(QUERY_KEYS.setup.firstRun(), { configured: true, setup_completed: false });

    // Done is step 8 in the configured (admin-skipped) sequence.
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=8', queryClient);

    // Wait for the first call to fail and the error UI to appear.
    await waitFor(() => {
      expect(screen.getByText('Setup completion failed')).toBeInTheDocument();
    });
    // The actual error message should be shown (not just the static fallback).
    expect(screen.getByText(/503 Service Unavailable/i)).toBeInTheDocument();

    // Click the Retry button.
    await user.click(screen.getByRole('button', { name: /retry/i }));

    // On retry success the cache must flip to setup_completed=true.
    await waitFor(() => {
      const cached = queryClient.getQueryData<FirstRun>(QUERY_KEYS.setup.firstRun());
      expect(cached?.setup_completed).toBe(true);
    });
    expect(api.markSetupCompleted).toHaveBeenCalledTimes(2);
  });

  // GAP-4: SMTP step — filling host + from_email and clicking Continue triggers saveFirstRunSmtp,
  // then advances. If already saved, Continue does NOT call save again.
  it('(GAP-4) SMTP step Continue saves dirty form before advancing', async () => {
    const user = userEvent.setup();
    vi.mocked(api.saveFirstRunSmtp).mockResolvedValue({ saved: true, test_sent: null, test_error: null });

    // SMTP is step 2 in both sequences.
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=2');
    expect(await screen.findByText('SMTP relay')).toBeInTheDocument();

    // Fill the required fields (host + from_email makes canSave=true).
    await user.type(screen.getByLabelText(/host/i), 'smtp.example.com');
    await user.type(screen.getByLabelText(/from address/i), 'jarvis@example.com');

    // Click Continue — should trigger save then advance.
    await user.click(screen.getByRole('button', { name: /continue/i }));

    await waitFor(() => {
      expect(api.saveFirstRunSmtp).toHaveBeenCalledTimes(1);
    });
    // Advance must happen only after save success (the mock resolves, so next step renders).
    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();
  });

  it('(GAP-4) SMTP step Continue does NOT re-save when already saved', async () => {
    const user = userEvent.setup();
    vi.mocked(api.saveFirstRunSmtp).mockResolvedValue({ saved: true, test_sent: null, test_error: null });

    renderWizard({ configured: false, setup_completed: false }, false, '/?step=2');
    expect(await screen.findByText('SMTP relay')).toBeInTheDocument();

    await user.type(screen.getByLabelText(/host/i), 'smtp.example.com');
    await user.type(screen.getByLabelText(/from address/i), 'jarvis@example.com');

    // Click the explicit Save button first.
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(api.saveFirstRunSmtp).toHaveBeenCalledTimes(1));

    // Now click Continue — should NOT call save a second time.
    await user.click(screen.getByRole('button', { name: /continue/i }));
    // Allow any async work to settle.
    await waitFor(() => {
      expect(screen.queryByText('SMTP relay')).not.toBeInTheDocument();
    });
    expect(api.saveFirstRunSmtp).toHaveBeenCalledTimes(1);
  });

  // SMTP Continue must NOT advance a half-filled form: it stays disabled until
  // host + a valid from-address are present (Skip is the optional-out).
  it('SMTP Continue is disabled until host + a valid from-address are filled', async () => {
    const user = userEvent.setup();
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=2');
    expect(await screen.findByText('SMTP relay')).toBeInTheDocument();

    const continueBtn = screen.getByRole('button', { name: /continue/i });
    // Empty form → disabled.
    expect(continueBtn).toBeDisabled();

    // Host only (no from-address) → still disabled.
    await user.type(screen.getByLabelText(/host/i), 'smtp.example.com');
    expect(continueBtn).toBeDisabled();

    // Host + malformed email → still disabled.
    await user.type(screen.getByLabelText(/from address/i), 'not-an-email');
    expect(continueBtn).toBeDisabled();

    // Host + valid email → enabled.
    await user.clear(screen.getByLabelText(/from address/i));
    await user.type(screen.getByLabelText(/from address/i), 'jarvis@example.com');
    expect(continueBtn).toBeEnabled();
  });

  // FE-UIB-05: invalid port → inline error + Continue disabled; valid port clears error.
  it('(FE-UIB-05) SMTP port: non-empty invalid value shows inline error and disables Continue', async () => {
    const user = userEvent.setup();
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=2');
    expect(await screen.findByText('SMTP relay')).toBeInTheDocument();

    // Fill host + from-address so canSave would be true if port were valid.
    await user.type(screen.getByLabelText(/host/i), 'smtp.example.com');
    await user.type(screen.getByLabelText(/from address/i), 'jarvis@example.com');

    const portInput = screen.getByLabelText(/^port$/i);
    const continueBtn = screen.getByRole('button', { name: /continue/i });

    // Non-numeric input → error message + Continue disabled.
    await user.clear(portInput);
    await user.type(portInput, 'abc');
    expect(await screen.findByText(/port must be a number between 1 and 65535/i)).toBeInTheDocument();
    expect(continueBtn).toBeDisabled();

    // Out-of-range (0) → error + disabled.
    await user.clear(portInput);
    await user.type(portInput, '0');
    expect(screen.getByText(/port must be a number between 1 and 65535/i)).toBeInTheDocument();
    expect(continueBtn).toBeDisabled();

    // Out-of-range (65536) → error + disabled.
    await user.clear(portInput);
    await user.type(portInput, '65536');
    expect(screen.getByText(/port must be a number between 1 and 65535/i)).toBeInTheDocument();
    expect(continueBtn).toBeDisabled();

    // Valid port (465) → no error + Continue enabled.
    await user.clear(portInput);
    await user.type(portInput, '465');
    expect(screen.queryByText(/port must be a number between 1 and 65535/i)).not.toBeInTheDocument();
    expect(continueBtn).toBeEnabled();

    // Empty port (reset to default) → no error + Continue still enabled.
    await user.clear(portInput);
    expect(screen.queryByText(/port must be a number between 1 and 65535/i)).not.toBeInTheDocument();
    expect(continueBtn).toBeEnabled();
  });

  // GAP-5: Welcome step "Skip setup" while !authed + showAdminStep=true → navigates to admin step,
  // does NOT call markSetupCompleted.
  it('(GAP-5) Welcome step Skip setup while unauthed navigates to admin step without calling markSetupCompleted', async () => {
    const user = userEvent.setup();
    // Fresh install: showAdminStep = true (configured=false).
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=1');
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /skip setup/i }));

    // Should navigate to the admin step (step 3 in the 9-step sequence).
    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();
    // markSetupCompleted must NOT be called (can't mark completed without a session).
    expect(api.markSetupCompleted).not.toHaveBeenCalled();
  });

  // The setup token is captured then stripped from the URL on mount.
  it('captures setup_token from the URL and strips it from the address bar on mount', async () => {
    renderWizard({ configured: false, setup_completed: false }, false, '/?setup_token=test-tok&step=1');
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId('location-search').textContent).not.toContain('setup_token');
    });
    // The step query survives the strip.
    expect(screen.getByTestId('location-search').textContent).toContain('step=1');
  });

  // The token also arrives as a URL fragment (#setup_token=…), which — unlike
  // the query form — never reaches the wire or server access logs. It is
  // captured and stripped from the address bar on mount.
  it('captures setup_token from the URL fragment and strips it from the address bar on mount', async () => {
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=1#setup_token=hash-tok');
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
    // The fragment token is stored for the bootstrap write…
    await waitFor(() => {
      expect(sessionStorage.getItem('jarvis_setup_token')).toBe('hash-tok');
    });
    // …and removed from the address bar so it never lingers in history.
    await waitFor(() => {
      expect(screen.getByTestId('location-hash').textContent).not.toContain('setup_token');
    });
    // The step query survives the strip.
    expect(screen.getByTestId('location-search').textContent).toContain('step=1');
  });

  // The captured token is forwarded to the first-run WRITE call.
  it('forwards the setup token as the X-Setup-Token arg to createFirstRunAdmin', async () => {
    const user = userEvent.setup();
    // Admin is step 3 in the fresh-install sequence.
    renderWizard({ configured: false, setup_completed: false }, false, '/?setup_token=test-tok&step=3');
    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();

    await user.type(screen.getByLabelText(/admin email/i), 'admin@example.com');
    await user.click(screen.getByRole('button', { name: /create admin & sign in/i }));

    await waitFor(() => {
      expect(vi.mocked(api.createFirstRunAdmin).mock.calls[0]?.[1]).toBe('test-tok');
    });
  });

  // No token in the URL → no token forwarded (regression guard).
  it('passes no setup token to createFirstRunAdmin when the URL has none', async () => {
    const user = userEvent.setup();
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=3');
    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();

    await user.type(screen.getByLabelText(/admin email/i), 'admin@example.com');
    await user.click(screen.getByRole('button', { name: /create admin & sign in/i }));

    await waitFor(() => {
      expect(api.createFirstRunAdmin).toHaveBeenCalled();
    });
    expect(vi.mocked(api.createFirstRunAdmin).mock.calls[0]?.[1]).toBeNull();
  });

  // M2 refresh-recovery (a): token persisted in sessionStorage but absent from
  // the URL (the post-refresh case) is still forwarded to the first-run write.
  it('(M2) recovers the setup token from sessionStorage after a refresh and forwards it', async () => {
    const user = userEvent.setup();
    // Simulate a refresh: the token survives in sessionStorage, URL is clean.
    sessionStorage.setItem('jarvis_setup_token', 'stored-tok');
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=3');
    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();

    await user.type(screen.getByLabelText(/admin email/i), 'admin@example.com');
    await user.click(screen.getByRole('button', { name: /create admin & sign in/i }));

    await waitFor(() => {
      expect(vi.mocked(api.createFirstRunAdmin).mock.calls[0]?.[1]).toBe('stored-tok');
    });
  });

  // M2 refresh-recovery (b): a URL-sourced token is mirrored into sessionStorage
  // (so it survives a subsequent refresh).
  it('(M2) persists a URL setup token into sessionStorage on mount', async () => {
    renderWizard({ configured: false, setup_completed: false }, false, '/?setup_token=url-tok&step=1');
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
    await waitFor(() => {
      expect(sessionStorage.getItem('jarvis_setup_token')).toBe('url-tok');
    });
  });

  // M2 hygiene: completing setup clears the one-time token from sessionStorage.
  it('(M2) clears the setup token from sessionStorage once setup completes', async () => {
    sessionStorage.setItem('jarvis_setup_token', 'stored-tok');
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(QUERY_KEYS.setup.firstRun(), { configured: true, setup_completed: false });

    // Done is step 8 in the configured (admin-skipped) sequence — it marks
    // completion on mount, which runs markFirstRunCompleted → clears the token.
    renderWizard({ configured: true, setup_completed: false }, true, '/?step=8', queryClient);

    await waitFor(() => {
      expect(api.markSetupCompleted).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(sessionStorage.getItem('jarvis_setup_token')).toBeNull();
    });
  });

  // A token-gate 403 while this browser holds no token (second device /
  // incognito) surfaces an inline "Setup token" paste field.
  it('renders a setup-token input when admin creation 403s and no token is held', async () => {
    const user = userEvent.setup();
    vi.mocked(api.createFirstRunAdmin).mockRejectedValueOnce(
      new ApiError(403, '{"detail":"Invalid or missing setup token"}'),
    );
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=3');
    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();

    await user.type(screen.getByLabelText(/admin email/i), 'admin@example.com');
    await user.click(screen.getByRole('button', { name: /create admin & sign in/i }));

    expect(await screen.findByLabelText(/setup token/i)).toBeInTheDocument();
  });

  // Pasting the token retries the create with it and advances on success.
  it('retries admin creation with the pasted setup token and advances', async () => {
    const user = userEvent.setup();
    vi.mocked(api.createFirstRunAdmin)
      .mockRejectedValueOnce(new ApiError(403, '{"detail":"Invalid or missing setup token"}'))
      .mockResolvedValueOnce({ id: 1, email: 'admin@example.com', role: 'admin' });
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=3');
    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();

    await user.type(screen.getByLabelText(/admin email/i), 'admin@example.com');
    await user.click(screen.getByRole('button', { name: /create admin & sign in/i }));

    const tokenInput = await screen.findByLabelText(/setup token/i);
    await user.type(tokenInput, 'pasted-tok');
    await user.click(screen.getByRole('button', { name: /create admin & sign in/i }));

    await waitFor(() => {
      expect(vi.mocked(api.createFirstRunAdmin).mock.calls[1]?.[1]).toBe('pasted-tok');
    });
    expect(await screen.findByText(/Cloud LLM keys/i)).toBeInTheDocument();
  });
});
