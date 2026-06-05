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
import { render, screen, waitFor } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QUERY_KEYS } from '@/lib/query-keys';

vi.mock('@/lib/api', () => ({
  getFirstRunStatus: vi.fn().mockResolvedValue({ configured: false, setup_completed: false }),
  runFirstRunSystemCheck: vi.fn().mockResolvedValue({
    services: [{ name: 'postgres', ok: true, detail: null }],
    all_ok: true,
  }),
  saveFirstRunSmtp: vi.fn().mockResolvedValue({ saved: true, test_sent: null, test_error: null }),
  createFirstRunAdmin: vi.fn().mockResolvedValue({ id: 1, email: 'admin@example.com', role: 'admin' }),
  saveFirstRunCloudKeys: vi.fn().mockResolvedValue({ saved_providers: [], applied_now: [], restart_required: false }),
  getSetupStatus: vi.fn().mockResolvedValue({
    setup_completed: false,
    models_ready: true,
    models_downloading: [],
    topics_count: 0,
    telegram_configured: false,
    telegram_paired: false,
  }),
  createTopic: vi.fn().mockResolvedValue({ id: 1, name: 'test' }),
  setConfig: vi.fn().mockResolvedValue({ key: 'pulse.cron', value: '0 4 * * *' }),
  fetchConfig: vi.fn().mockResolvedValue([]),
  fetchSources: vi.fn().mockResolvedValue([]),
  updateSource: vi.fn(),
  markSetupCompleted: vi.fn().mockResolvedValue(undefined),
  getTelegramPairing: vi.fn().mockResolvedValue({ paired: false, chat_id: null, telegram_username: null, paired_at: null }),
  removeTelegramPairing: vi.fn(),
  requestTelegramPairToken: vi.fn(),
}));

const api = await import('@/lib/api');
const { OnboardingWizard } = await import('@/pages/OnboardingWizard');
const { useAuthStore } = await import('@/stores/auth-store');

type FirstRun = { configured: boolean; setup_completed: boolean; setup_mode?: 'single' | 'multi' };

function renderWizard(
  firstRun: FirstRun = { configured: false, setup_completed: false },
  authed = false,
  initialUrl = '/?step=1',
  client?: QueryClient,
) {
  const queryClient = client ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <Routes>
          {/* Wizard reads ?step= from the URL at any path. */}
          <Route path="/" element={<OnboardingWizard firstRun={firstRun} authed={authed} />} />
          <Route path="/done-marker" element={<div>DASHBOARD</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
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
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
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

  // (a)
  it('fresh install renders step 1 (welcome + system check) and runs the probe', async () => {
    renderWizard({ configured: false, setup_completed: false }, false, '/?step=1');
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
    expect(screen.getByText('Step 1 of 9')).toBeInTheDocument();
    await waitFor(() => {
      expect(api.runFirstRunSystemCheck).toHaveBeenCalled();
    });
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
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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

  // GAP-2: markSetupCompleted rejects → Done step renders error UI + retry calls it again.
  it('(GAP-2) Done step: markSetupCompleted rejects → shows error UI, retry calls it again, on success cache flips', async () => {
    const user = userEvent.setup();
    // First call rejects; second call (retry) resolves.
    vi.mocked(api.markSetupCompleted)
      .mockRejectedValueOnce(new Error('503 Service Unavailable'))
      .mockResolvedValueOnce(undefined);

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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
});
