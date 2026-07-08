/**
 * SettingsPage RBAC regression tests (2-pane IA).
 *
 * The tab-bar is replaced with a §-grouped rail (SettingsRail). These tests
 * verify that:
 *  - The Settings heading still renders.
 *  - Personal §-sections (Account, Integrations, Research) are always visible.
 *  - System §-sections (Sources, Models, System) are hidden for non-admin users.
 *  - Non-admin deep-link to a system section → redirected to default (research/topics).
 *  - Admin users see all sections.
 *  - Default landing is research/topics (Research → Topics).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { SettingsPage } from '@/pages/SettingsPage';
import { SettingsDetailPane } from '@/components/settings/SettingsDetailPane';
import { useAuthStore } from '@/stores/auth-store';
import * as api from '@/lib/api';

// ---------------------------------------------------------------------------
// Mock all api calls used by settings sections
// ---------------------------------------------------------------------------
vi.mock('@/lib/api', () => ({
  fetchTopics: vi.fn().mockResolvedValue([]),
  createTopic: vi.fn(),
  updateTopic: vi.fn(),
  deleteTopic: vi.fn(),
  fetchMySubscriptions: vi.fn().mockResolvedValue([]),
  subscribeToTopic: vi.fn().mockResolvedValue(undefined),
  unsubscribeFromTopic: vi.fn().mockResolvedValue(undefined),
  fetchSources: vi.fn().mockResolvedValue([]),
  updateSource: vi.fn(),
  reorderSources: vi.fn(),
  fetchTrackedAuthors: vi.fn().mockResolvedValue([]),
  createTrackedAuthor: vi.fn(),
  updateTrackedAuthor: vi.fn(),
  deleteTrackedAuthor: vi.fn(),
  autoDetectAuthors: vi.fn(),
  checkTrackedAuthors: vi.fn(),
  fetchConfig: vi.fn().mockResolvedValue([]),
  setConfig: vi.fn(),
  fetchNudges: vi.fn().mockResolvedValue([]),
  updateNudge: vi.fn(),
  fetchExtractionTemplates: vi.fn().mockResolvedValue([]),
  createExtractionTemplate: vi.fn(),
  deleteExtractionTemplate: vi.fn(),
  checkHealth: vi.fn(),
  fetchPulseStats: vi.fn().mockResolvedValue({ last_run_at: null, decks_generated: 0, last_error: null }),
  fetchPulseDebug: vi.fn().mockResolvedValue({
    deck_date: '2026-04-17',
    card_count: 5,
    degraded_reason: null,
    source_counts: {},
    topic_embeddings: [],
    top_cards: [],
    classifier_available: false,
    classifier_sample_count: null,
    classifier_feature_names: [],
    classifier_auc: null,
    classifier_auc_degradation_reason: null,
    classifier_degradation_reason: null,
  }),
  createJob: vi.fn().mockResolvedValue({ job_id: 'test-job-id', status: 'queued' }),
  listJobs: vi.fn().mockResolvedValue([]),
  getSetupStatus: vi.fn().mockResolvedValue({
    setup_completed: true,
    models_ready: true,
    models_downloading: [],
    topics_count: 0,
    telegram_configured: false,
    telegram_paired: false,
  }),
  fetchAccount: vi.fn().mockResolvedValue({
    id: 1,
    email: 'test@example.com',
    role: 'admin',
    display_name: 'Test User',
    created_at: '2025-01-01T00:00:00Z',
    last_login_at: null,
  }),
  updateAccount: vi.fn(),
  downloadMyData: vi.fn().mockResolvedValue(undefined),
  confirmEmailChange: vi.fn(),
  apiFetch: vi.fn(),
  getTelegramBotToken: vi.fn().mockResolvedValue({ has_token: false }),
  saveTelegramBotToken: vi.fn().mockResolvedValue(undefined),
  fetchSystemModels: vi.fn().mockResolvedValue({ hardware: undefined, catalog: [] }),
  // AIPanel is now mounted as the advanced disclosure inside the LLM Models page.
  getAISettings: vi.fn().mockResolvedValue({
    hw_tier: 'cpu',
    recommended_backend: 'ollama',
    recommended_model: 'qwen3:1.7b',
    configured_backend: 'ollama',
    configured_model: 'qwen3:1.7b',
    candidates_for_tier: [{ backend: 'ollama', model: 'qwen3:1.7b', rank: 1 }],
    candidate_issues: [],
  }),
  redetectHW: vi.fn(),
  getFirstRunStatus: vi.fn().mockResolvedValue({ configured: true, hw_tier_changed: false }),
  dismissBanner: vi.fn().mockResolvedValue(undefined),
}));

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

function renderSettingsPage(initialSearch = '') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/settings${initialSearch}`]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderSettingsPageAs(role: 'admin' | 'user' | null, initialSearch = '') {
  if (role !== null) {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: null,
      user: { id: 1, email: 'test@example.com', role },
    });
  } else {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'test-key',
      user: null,
    });
  }
  return renderSettingsPage(initialSearch);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('SettingsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'test-key',
      user: null,
    });
  });

  it('renders the settings heading', () => {
    renderSettingsPage();
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
  });

  it('renders Account section in nav for any authenticated user', async () => {
    renderSettingsPage();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Profile & Email/i })).toBeInTheDocument(),
    );
  });

  it('renders Research section nav items for any authenticated user', async () => {
    renderSettingsPage();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Topics/i })).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: /Authors/i })).toBeInTheDocument();
  });

  it('hides Sources nav items for non-admin (role=user)', async () => {
    renderSettingsPageAs('user');
    // Wait for render to settle via heading role (avoids multiple-match with breadcrumb)
    await waitFor(() => screen.getByRole('heading', { name: 'Settings' }));
    // Sources section (header + nav item) should not be visible
    expect(screen.queryByText('Sources')).not.toBeInTheDocument();
  });

  it('hides System nav items for non-admin (role=user)', async () => {
    renderSettingsPageAs('user');
    await waitFor(() => screen.getByRole('heading', { name: 'Settings' }));
    expect(screen.queryByText('System')).not.toBeInTheDocument();
  });

  it('shows Sources section header for admin', async () => {
    renderSettingsPageAs('admin');
    await waitFor(() => expect(screen.getAllByText('Sources').length).toBeGreaterThan(0));
  });

  it('shows Models and System section headers for admin', async () => {
    renderSettingsPageAs('admin');
    await waitFor(() => expect(screen.getByText('Models')).toBeInTheDocument());
    expect(screen.getByText('System')).toBeInTheDocument();
  });

  it('defaults to Research / Topics content pane', async () => {
    renderSettingsPage();
    // The detail pane heading should say "Topics"
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Topics', level: 2 })).toBeInTheDocument(),
    );
  });

  it('navigating to Account / Profile & Email rail item shows detail heading', async () => {
    const user = userEvent.setup();
    renderSettingsPage();
    await waitFor(() => screen.getByRole('button', { name: /Profile & Email/i }));
    await user.click(screen.getByRole('button', { name: /Profile & Email/i }));
    // Detail pane h2 heading should update
    await waitFor(() =>
      expect(
        screen.getByRole('heading', { name: /Profile & Email/i, level: 2 }),
      ).toBeInTheDocument(),
    );
  });

  it('non-admin deep-link to system section redirects to default (Topics heading)', async () => {
    renderSettingsPageAs('user', '?section=sources&item=arxiv');
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Topics', level: 2 })).toBeInTheDocument(),
    );
  });

  it('does not render Recommendations section (removed in legacy cleanup)', () => {
    renderSettingsPage();
    expect(screen.queryByText(/Recommendations/i)).not.toBeInTheDocument();
  });

  it('shows API-key-only session (null user) same as non-admin — hides system sections', async () => {
    renderSettingsPageAs(null);
    await waitFor(() => screen.getByRole('heading', { name: 'Settings' }));
    expect(screen.queryByText('Sources')).not.toBeInTheDocument();
    expect(screen.queryByText('System')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Conflict-5 — IngestionSection filterGroups split
//
// Models → LLM must render the full IngestionSection (AI models group).
// Research → Spaced Repetition must render ONLY the Spaced Repetition
// group via the SpacedRepetitionSection wrapper — no AI models / Preferences.
// ---------------------------------------------------------------------------

describe('SettingsDetailPane — IngestionSection filterGroups split (Conflict-5)', () => {
  const splitConfig = [
    { key: 'llm.smart_model', value: 'qwen3:14b' },
    { key: 'llm.fast_model', value: 'qwen3:4b' },
    { key: 'llm.embed_model', value: 'qwen3-embedding:0.6b' },
    { key: 'fsrs.desired_retention', value: 0.9 },
    { key: 'fsrs.learning_steps', value: [1, 10] },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchConfig).mockResolvedValue(splitConfig);
    vi.mocked(api.apiFetch).mockResolvedValue({ hardware: undefined, catalog: [] });
  });

  function renderDetail(section: string, item: string) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SettingsDetailPane section={section} item={item} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('Models → LLM renders the AI models group with the advanced backend disclosure', async () => {
    renderDetail('models', 'llm');
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'AI models', level: 4 })).toBeInTheDocument(),
    );
    expect(screen.getByTestId('advanced-backend-disclosure')).toBeInTheDocument();
  });

  it('Models → stale ?item=ai deep-link resolves to the consolidated AI models page', async () => {
    renderDetail('models', 'ai');
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'AI models', level: 4 })).toBeInTheDocument(),
    );
    expect(screen.getByTestId('advanced-backend-disclosure')).toBeInTheDocument();
  });

  it('Research → Spaced Repetition renders ONLY the Spaced Repetition group', async () => {
    renderDetail('research', 'spaced-repetition');
    await waitFor(() =>
      expect(
        screen.getByRole('heading', { name: 'Spaced Repetition', level: 4 }),
      ).toBeInTheDocument(),
    );
    // The AI models group (and any Preferences group) must NOT leak in.
    expect(screen.queryByRole('heading', { name: 'AI models', level: 4 })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Preferences', level: 4 })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// FE-RBAC-1 — bot-token item gate (defense-in-depth; backend already 403s)
//
// Three layers must all gate the bot-token admin-only item:
//  1. SettingsRail: bot-token rail item NOT rendered for non-admin users.
//  2. SettingsPage: deep-link ?section=integrations&item=bot-token redirects
//     non-admin to the default (research/topics).
//  3. SettingsDetailPane: even if rendered directly with bot-token, non-admin
//     sees access-denied message, NOT TelegramBotTokenSection.
// Admins must be unaffected (all three layers show/render bot-token normally).
// ---------------------------------------------------------------------------

describe('FE-RBAC-1 — bot-token item gate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ---- Layer 1: SettingsRail item visibility --------------------------------

  it('non-admin: bot-token rail item is NOT rendered in Integrations', async () => {
    renderSettingsPageAs('user');
    await waitFor(() => screen.getByRole('heading', { name: 'Settings' }));
    // Telegram bot key nav button must not exist
    expect(screen.queryByRole('button', { name: /Telegram bot key/i })).not.toBeInTheDocument();
  });

  it('null-user (API-key session): bot-token rail item is NOT rendered', async () => {
    renderSettingsPageAs(null);
    await waitFor(() => screen.getByRole('heading', { name: 'Settings' }));
    expect(screen.queryByRole('button', { name: /Telegram bot key/i })).not.toBeInTheDocument();
  });

  it('admin: bot-token rail item IS rendered in Integrations', async () => {
    renderSettingsPageAs('admin');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Telegram bot key/i })).toBeInTheDocument(),
    );
  });

  // ---- Layer 2: SettingsPage deep-link redirect ----------------------------

  it('non-admin deep-link ?section=integrations&item=bot-token → redirects to Topics', async () => {
    renderSettingsPageAs('user', '?section=integrations&item=bot-token');
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Topics', level: 2 })).toBeInTheDocument(),
    );
  });

  it('admin deep-link ?section=integrations&item=bot-token → stays on Telegram bot key', async () => {
    renderSettingsPageAs('admin', '?section=integrations&item=bot-token');
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Telegram bot key', level: 2 })).toBeInTheDocument(),
    );
    // Must NOT have been redirected to Topics
    expect(screen.queryByRole('heading', { name: 'Topics', level: 2 })).not.toBeInTheDocument();
  });

  // ---- Layer 3: SettingsDetailPane direct render guard --------------------

  it('non-admin DetailPane with bot-token: renders access-denied message, NOT TelegramBotTokenSection', async () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: null,
      user: { id: 1, email: 'user@example.com', role: 'user' },
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SettingsDetailPane section="integrations" item="bot-token" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(screen.getByText(/Admin access required/i)).toBeInTheDocument(),
    );
    // TelegramBotTokenSection renders a card — its header must not appear
    expect(screen.queryByText(/bot token/i, { selector: 'h3,h4,p' })).not.toBeInTheDocument();
  });

  it('admin DetailPane with bot-token: renders TelegramBotTokenSection (no access-denied)', async () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: null,
      user: { id: 1, email: 'admin@example.com', role: 'admin' },
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SettingsDetailPane section="integrations" item="bot-token" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // Access-denied must NOT appear
    expect(screen.queryByText(/Admin access required/i)).not.toBeInTheDocument();
    // Positive: TelegramBotTokenSection renders its settled content for an admin
    // (mock resolves has_token:false → "No bot token set").
    expect(await screen.findByText(/No bot token set/i)).toBeInTheDocument();
    expect(screen.queryByText(/Admin access required/i)).not.toBeInTheDocument();
  });
});
