/**
 * PulseSection tests
 *
 * vi.mock factories use vi.hoisted() for any values needed in the factory
 * closure so there are no TDZ issues with module-level consts.
 * Each describe block gets a fresh QueryClient.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { PulseSection } from '@/components/settings/PulseSection';
import type { PulseStats, SystemCapabilities } from '@/types';

// ---------------------------------------------------------------------------
// Inline fixtures (avoid module-const TDZ in vi.mock factories)
// ---------------------------------------------------------------------------

// (debug fixture is inlined in vi.mock factory below to avoid TDZ)

// ---------------------------------------------------------------------------
// Module mock
// ---------------------------------------------------------------------------

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({ key: '', value: null }),
    fetchPulseStats: vi.fn(),
    fetchPulseDebug: vi.fn().mockResolvedValue({
      deck_date: '2026-04-17',
      card_count: 5,
      degraded_reason: null,
      source_counts: { arxiv: 12, pubmed: 8 },
      source_diagnostics: {},
      topic_embeddings: [{ key: 'topic.1.embedding', dim: 1024, ok: true, non_null: true }],
      top_cards: [],
      classifier_available: false,
      classifier_sample_count: null,
      classifier_feature_names: [],
      classifier_auc: null,
      classifier_auc_degradation_reason: null,
      classifier_degradation_reason: null,
    }),
    createJob: vi.fn().mockResolvedValue({ job_id: 'pulse-job-1', status: 'queued' }),
    listJobs: vi.fn().mockResolvedValue([]),
    getSystemCapabilities: vi.fn().mockResolvedValue({ networkx: true, scikit_learn: true }),
    patchSourceConfig: vi.fn().mockResolvedValue({ ok: true }),
    clearSourceCooldown: vi.fn().mockResolvedValue({ ok: true }),
  };
});

// Mock auth store — default: regular (non-admin) user.
// Use eslint-disable for the any cast; the real AuthState is wide but we only
// need the user field to drive isAdmin logic.
vi.mock('@/stores/auth-store', () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  useAuthStore: vi.fn((selector: (s: any) => unknown) =>
    selector({ user: { id: 1, email: 'test@example.com', role: 'user' } }),
  ),
}));

const {
  fetchConfig,
  fetchPulseStats,
  setConfig,
  createJob,
  getSystemCapabilities,
  patchSourceConfig,
  clearSourceCooldown,
} = await import('@/lib/api');

const { useAuthStore: useAuthStoreMock } = await import('@/stores/auth-store');
const mockUseAuthStore = vi.mocked(useAuthStoreMock);

// ---------------------------------------------------------------------------
// Render helper — per-test QueryClient so cache never bleeds between tests
// ---------------------------------------------------------------------------

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PulseSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openAdvancedTuning(user = userEvent.setup()) {
  const button = await screen.findByRole('button', { name: /advanced tuning/i });
  expect(button).toHaveAttribute('aria-expanded', 'false');
  await user.click(button);
  expect(button).toHaveAttribute('aria-expanded', 'true');
}

// ---------------------------------------------------------------------------
// Shared base data
// ---------------------------------------------------------------------------

const baseStats: PulseStats = {
  window_days: 1,
  decks_generated: 3,
  avg_candidates: 42,
  avg_llm_calls: 15,
  avg_duration_s: 12.3,
  last_run_at: '2026-04-10T04:00:00Z',
  last_error: null,
  degraded_reason: null,
};

const capableSystem: SystemCapabilities = { networkx: true, scikit_learn: true, structured_output_enforced: true };
const incapableSystem: SystemCapabilities = { networkx: false, scikit_learn: false, structured_output_enforced: false };

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PulseSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'pulse.enabled', value: false },
      { key: 'pulse.cron', value: '0 4 * * *' },
      { key: 'pulse.deck_size', value: 10 },
      { key: 'pulse.stage2_top_k', value: 40 },
    ]);
    vi.mocked(fetchPulseStats).mockResolvedValue(baseStats);
    vi.mocked(getSystemCapabilities).mockResolvedValue(capableSystem);
    // Default: non-admin user
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    mockUseAuthStore.mockImplementation((selector: (s: any) => unknown) =>
      selector({ user: { id: 1, email: 'test@example.com', role: 'user' } }),
    );
  });

  // ── Basic rendering ────────────────────────────────────────────────────────

  it('renders the Pulse enable toggle', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: /pulse/i })).toBeInTheDocument();
    });
  });

  it('calls setConfig with pulse.enabled when toggled', async () => {
    const user = userEvent.setup();
    renderSection();
    const toggle = await screen.findByRole('switch', { name: /pulse/i });
    await user.click(toggle);
    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith('pulse.enabled', true);
    });
  });

  it('renders last-run status with stats data', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/decks generated/i)).toBeInTheDocument();
    });
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  // ── Badge state tests ──────────────────────────────────────────────────────

  it('shows Failed badge (red) when last_error is set', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      decks_generated: 0,
      last_run_at: null,
      last_error: 'scoring pipeline exploded',
      degraded_reason: null,
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('Failed')).toBeInTheDocument();
    });
    expect(screen.getByText(/scoring pipeline exploded/i)).toBeInTheDocument();
  });

  it('shows Degraded badge (amber) when degraded_reason is set and last_error is null', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      last_error: null,
      degraded_reason: 'optional Pulse Phase 2 signals unavailable: no networkx',
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('Degraded')).toBeInTheDocument();
    });
    const badge = screen.getByText('Degraded');
    expect(badge).toBeInTheDocument();
  });

  it('Degraded badge tooltip content includes the degraded_reason text', async () => {
    const degradedMsg = 'optional Pulse Phase 2 signals unavailable: no networkx';
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      last_error: null,
      degraded_reason: degradedMsg,
    });
    const user = userEvent.setup();
    renderSection();
    const badge = await screen.findByText('Degraded');
    await user.hover(badge);
    await waitFor(() => {
      const matches = screen.getAllByText(degradedMsg);
      expect(matches.length).toBeGreaterThan(0);
    });
  });

  it('shows OK badge (green) when both last_error and degraded_reason are null', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      last_error: null,
      degraded_reason: null,
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('OK')).toBeInTheDocument();
    });
  });

  // ── Double-tooltip fix: gate trigger wraps ONLY the slider, not the ⓘ ─────

  it('gate-tooltip trigger for classifier wraps only the slider input, not the InfoTooltip', async () => {
    // Capability missing → gate is visible
    vi.mocked(getSystemCapabilities).mockResolvedValue(incapableSystem);
    renderSection();
    await openAdvancedTuning();

    const gateTrigger = await screen.findByTestId('gate-tooltip-trigger-classifier');
    // The gate trigger should contain the slider input
    const slider = gateTrigger.querySelector('input[type="range"]');
    expect(slider).not.toBeNull();
    // The ⓘ InfoTooltip button must NOT be inside the gate trigger
    // (InfoTooltip renders a <button> with aria-label containing "info" or similar)
    const infoButtons = gateTrigger.querySelectorAll('button');
    expect(infoButtons.length).toBe(0);
  });

  it('gate-tooltip trigger for citation_pagerank wraps only the slider input', async () => {
    vi.mocked(getSystemCapabilities).mockResolvedValue(incapableSystem);
    renderSection();
    await openAdvancedTuning();

    const gateTrigger = await screen.findByTestId('gate-tooltip-trigger-citation_pagerank');
    const slider = gateTrigger.querySelector('input[type="range"]');
    expect(slider).not.toBeNull();
    const infoButtons = gateTrigger.querySelectorAll('button');
    expect(infoButtons.length).toBe(0);
  });

  // ── Capability-driven gate: no nag when capability present ────────────────

  it('does NOT render gate-tooltip trigger for classifier when scikit_learn is available', async () => {
    vi.mocked(getSystemCapabilities).mockResolvedValue(capableSystem);
    renderSection();
    await openAdvancedTuning();
    // Wait for sliders to be rendered
    await screen.findByTestId('weight-slider-classifier');
    expect(screen.queryByTestId('gate-tooltip-trigger-classifier')).toBeNull();
  });

  it('does NOT render gate-tooltip trigger for citation_pagerank when networkx is available', async () => {
    vi.mocked(getSystemCapabilities).mockResolvedValue(capableSystem);
    renderSection();
    await openAdvancedTuning();
    await screen.findByTestId('weight-slider-citation_pagerank');
    expect(screen.queryByTestId('gate-tooltip-trigger-citation_pagerank')).toBeNull();
  });

  // ── Capability-driven gate: nag when capability missing ───────────────────

  it('renders gate-tooltip trigger for classifier when scikit_learn is FALSE', async () => {
    vi.mocked(getSystemCapabilities).mockResolvedValue({ networkx: true, scikit_learn: false, structured_output_enforced: false });
    renderSection();
    await openAdvancedTuning();
    await waitFor(() => {
      expect(screen.getByTestId('gate-tooltip-trigger-classifier')).toBeInTheDocument();
    });
  });

  it('renders gate-tooltip trigger for citation_pagerank when networkx is FALSE', async () => {
    vi.mocked(getSystemCapabilities).mockResolvedValue({ networkx: false, scikit_learn: true, structured_output_enforced: false });
    renderSection();
    await openAdvancedTuning();
    await waitFor(() => {
      expect(screen.getByTestId('gate-tooltip-trigger-citation_pagerank')).toBeInTheDocument();
    });
  });

  it('gate tooltip for classifier contains plain-language message about scikit-learn', async () => {
    vi.mocked(getSystemCapabilities).mockResolvedValue({ networkx: true, scikit_learn: false, structured_output_enforced: false });
    const user = userEvent.setup();
    renderSection();
    await openAdvancedTuning(user);
    const trigger = await screen.findByTestId('gate-tooltip-trigger-classifier');
    await user.hover(trigger);
    await waitFor(() => {
      expect(screen.getAllByText(/scikit-learn/i).length).toBeGreaterThan(0);
    });
  });

  it('gate tooltip for citation_pagerank contains plain-language message about networkx', async () => {
    vi.mocked(getSystemCapabilities).mockResolvedValue({ networkx: false, scikit_learn: true, structured_output_enforced: false });
    const user = userEvent.setup();
    renderSection();
    await openAdvancedTuning(user);
    const trigger = await screen.findByTestId('gate-tooltip-trigger-citation_pagerank');
    await user.hover(trigger);
    await waitFor(() => {
      expect(screen.getAllByText(/networkx/i).length).toBeGreaterThan(0);
    });
  });

  // ── Fail-safe: capability loading/error treats signals as unavailable (fail closed) ──

  it('shows gate triggers for optional signals when getSystemCapabilities returns error', async () => {
    vi.mocked(getSystemCapabilities).mockRejectedValue(new Error('network error'));
    renderSection();
    await openAdvancedTuning();
    // Fail closed: a failed capabilities query surfaces the gate rather than hiding it
    await waitFor(() => {
      expect(screen.getByTestId('gate-tooltip-trigger-classifier')).toBeInTheDocument();
    });
    expect(screen.getByTestId('gate-tooltip-trigger-citation_pagerank')).toBeInTheDocument();
  });

  // ── Presets ───────────────────────────────────────────────────────────────

  it('renders the Presets control area when advanced tuning is open', async () => {
    renderSection();
    await openAdvancedTuning();
    const presets = await screen.findByTestId('weight-presets');
    expect(presets).toBeInTheDocument();
  });

  it('clicking Balanced preset calls setConfig with pulse.weights', async () => {
    const user = userEvent.setup();
    renderSection();
    await openAdvancedTuning(user);
    const balancedBtn = await screen.findByTestId('preset-balanced');
    await user.click(balancedBtn);
    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith(
        'pulse.weights',
        expect.objectContaining({ embedding: 0.2, llm_relevance: 0.3 }),
      );
    });
  });

  it('clicking Semantic-first preset sets higher embedding and topic weights', async () => {
    const user = userEvent.setup();
    renderSection();
    await openAdvancedTuning(user);
    const btn = await screen.findByTestId('preset-semantic-first');
    await user.click(btn);
    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith(
        'pulse.weights',
        expect.objectContaining({ embedding: 0.4, topic: 0.35 }),
      );
    });
  });

  it('clicking Freshness-first preset sets higher recency weight', async () => {
    const user = userEvent.setup();
    renderSection();
    await openAdvancedTuning(user);
    const btn = await screen.findByTestId('preset-freshness-first');
    await user.click(btn);
    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith(
        'pulse.weights',
        expect.objectContaining({ recency: 0.4 }),
      );
    });
  });

  // ── Normalize button always visible ───────────────────────────────────────

  it('renders Normalize to 1.0 button even when sum is within range', async () => {
    renderSection();
    await openAdvancedTuning();
    const normalizeBtn = await screen.findByTestId('normalize-button');
    expect(normalizeBtn).toBeInTheDocument();
  });

  // ── Optional signals visually grouped ────────────────────────────────────

  it('shows "Optional signals" group label in advanced tuning', async () => {
    renderSection();
    await openAdvancedTuning();
    await waitFor(() => {
      expect(screen.getByText(/optional signals/i)).toBeInTheDocument();
    });
  });

  // ── Slider rendering ───────────────────────────────────────────────────────

  it('hides scoring weight sliders until Advanced tuning is expanded', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /advanced tuning/i })).toBeInTheDocument();
    });
    expect(screen.queryByLabelText(/semantic similarity weight/i)).not.toBeInTheDocument();
  });

  it('renders scoring weight sliders after Advanced tuning is expanded', async () => {
    renderSection();
    await openAdvancedTuning();
    expect(screen.getByLabelText(/semantic similarity weight/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/topic match weight/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/relevance score weight/i)).toBeInTheDocument();
    expect(screen.getAllByLabelText(/liked papers weight/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/l2 negative-feedback penalty/i)).toBeInTheDocument();
  });

  // ── Generate Pulse button ──────────────────────────────────────────────────

  it('renders Generate Pulse now button', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /generate pulse now/i })).toBeInTheDocument();
    });
  });

  // ── Source diagnostics ────────────────────────────────────────────────────

  it('renders source diagnostics for rate-limited and unconfigured sources', async () => {
    const user = userEvent.setup();
    const { fetchPulseDebug } = await import('@/lib/api');
    vi.mocked(fetchPulseDebug).mockResolvedValueOnce({
      deck_date: '2026-05-06',
      card_count: 0,
      degraded_reason: 'No Pulse candidates returned; arXiv rate limit reached.',
      source_counts: {},
      source_diagnostics: {
        arxiv: {
          status: 'rate_limit',
          message: 'arxiv returned HTTP 429',
          status_code: 429,
          retry_after_s: 60,
          settings_hint: null,
        },
        openalex: {
          status: 'unconfigured',
          message: 'OpenAlex needs contact settings.',
          status_code: null,
          retry_after_s: null,
          settings_hint: 'Set OPENALEX_EMAIL for polite pool access.',
        },
      },
      topic_embeddings: [],
      top_cards: [],
      classifier_available: false,
      classifier_sample_count: null,
      classifier_feature_names: [],
      classifier_auc: null,
      classifier_auc_degradation_reason: null,
      classifier_degradation_reason: null,
    });

    renderSection();
    const diagnostics = await screen.findByRole('button', { name: /diagnostics/i });
    await user.click(diagnostics);

    expect(await screen.findByText(/source diagnostics/i)).toBeInTheDocument();
    expect(screen.getByText(/arxiv returned HTTP 429/i)).toBeInTheDocument();
    expect(screen.getByText(/retry after 60s/i)).toBeInTheDocument();
    expect(screen.getByText(/set OPENALEX_EMAIL/i)).toBeInTheDocument();
  });

  // ── Config/stats error states ─────────────────────────────────────────────

  it('renders config failures explicitly and disables settings mutations', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchConfig).mockRejectedValue(new Error('config unavailable'));

    renderSection();

    const toggle = await screen.findByRole('switch', { name: /pulse/i });
    await waitFor(() => {
      expect(screen.getByText(/pulse settings unavailable/i)).toBeInTheDocument();
    });
    expect(toggle).toBeDisabled();
    await user.click(toggle);
    expect(vi.mocked(setConfig)).not.toHaveBeenCalled();
  });

  it('renders stats failures explicitly and disables manual Pulse generation', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchPulseStats).mockRejectedValue(new Error('stats unavailable'));

    renderSection();

    const generateButton = await screen.findByRole('button', { name: /generate pulse now/i });
    await waitFor(() => {
      expect(screen.getByText(/pulse stats unavailable/i)).toBeInTheDocument();
    });
    expect(generateButton).toBeDisabled();
    await user.click(generateButton);
    expect(vi.mocked(createJob)).not.toHaveBeenCalled();
  });

  // ── Empty-state coaching ──────────────────────────────────────────────────

  it('shows empty-state coaching note when decks_generated is 0 and no error', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      decks_generated: 0,
      last_run_at: null,
      last_error: null,
      degraded_reason: null,
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/pulse needs a populated library/i)).toBeInTheDocument();
    });
  });

  // ── Heading-rhythm ────────────────────────────────────────────────────────

  it('shows the CardDescription lead-in text', async () => {
    renderSection();
    await waitFor(() => {
      expect(
        screen.getByText(/nightly ranked deck of candidate papers scored by the pulse pipeline/i),
      ).toBeInTheDocument();
    });
  });

  it('does not render a heading-level "Pulse" title inside the card', async () => {
    renderSection();
    await screen.findByRole('switch', { name: /pulse/i });
    expect(screen.queryByRole('heading', { name: 'Pulse' })).toBeNull();
  });

  // ── Inline source config (admin-only) ─────────────────────────────────────

  it('does NOT show source config inputs for non-admin users', async () => {
    // Default mock is non-admin
    renderSection();
    await screen.findByRole('switch', { name: /pulse/i });
    expect(screen.queryByLabelText(/openalex contact email/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/semantic scholar api key/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /clear arxiv cooldown/i })).not.toBeInTheDocument();
  });

  it('shows source config inputs for admin users', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    mockUseAuthStore.mockImplementation((selector: (s: any) => unknown) =>
      selector({ user: { id: 1, email: 'admin@example.com', role: 'admin' } }),
    );
    renderSection();
    await waitFor(() => {
      expect(screen.getByLabelText(/openalex contact email/i)).toBeInTheDocument();
    });
    expect(screen.getByLabelText(/semantic scholar api key/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /clear arxiv cooldown/i })).toBeInTheDocument();
  });

  it('calls patchSourceConfig for openalex with the entered email (admin)', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    mockUseAuthStore.mockImplementation((selector: (s: any) => unknown) =>
      selector({ user: { id: 1, email: 'admin@example.com', role: 'admin' } }),
    );
    const user = userEvent.setup();
    renderSection();
    const emailInput = await screen.findByLabelText(/openalex contact email/i);
    await user.type(emailInput, 'contact@lab.org');
    const [saveBtn] = screen.getAllByRole('button', { name: /save/i });
    expect(saveBtn).toBeTruthy();
    await user.click(saveBtn!);
    await waitFor(() => {
      expect(vi.mocked(patchSourceConfig)).toHaveBeenCalledWith('openalex', {
        email: 'contact@lab.org',
      });
    });
  });

  it('calls clearSourceCooldown for arxiv when the button is clicked (admin)', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    mockUseAuthStore.mockImplementation((selector: (s: any) => unknown) =>
      selector({ user: { id: 1, email: 'admin@example.com', role: 'admin' } }),
    );
    const user = userEvent.setup();
    renderSection();
    const clearBtn = await screen.findByRole('button', { name: /clear arxiv cooldown/i });
    await user.click(clearBtn);
    await waitFor(() => {
      expect(vi.mocked(clearSourceCooldown)).toHaveBeenCalledWith('arxiv');
    });
  });
});
