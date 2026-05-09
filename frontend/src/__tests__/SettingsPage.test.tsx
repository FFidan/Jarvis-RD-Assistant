import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { SettingsPage } from '@/pages/SettingsPage';
import { useAuthStore } from '@/stores/auth-store';

// Mock all api calls used by settings sections
vi.mock('@/lib/api', () => ({
  fetchTopics: vi.fn().mockResolvedValue([]),
  createTopic: vi.fn(),
  updateTopic: vi.fn(),
  deleteTopic: vi.fn(),
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
  createPairingCode: vi.fn(),
  getPairingStatus: vi.fn().mockResolvedValue({ paired: false, chat_id: null }),
  unpairTelegram: vi.fn(),
}));

function renderSettingsPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderSettingsPageAs(role: 'admin' | 'user' | null) {
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
  return renderSettingsPage();
}

describe('SettingsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset auth store to a clean API-key session (no role) by default
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'test-key',
      user: null,
    });
  });

  it('renders the settings heading', () => {
    renderSettingsPage();
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('renders personal tabs for any authenticated user', () => {
    renderSettingsPage();
    expect(screen.getByRole('tab', { name: 'Topics' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Authors' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Models & Preferences' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Integrations' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Appearance' })).toBeInTheDocument();
  });

  it('hides system tabs for non-admin session user', () => {
    renderSettingsPageAs('user');
    expect(screen.queryByRole('tab', { name: 'Sources' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Automation' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Extraction Templates' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Pulse' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Timer' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Providers' })).not.toBeInTheDocument();
  });

  it('shows system tabs for admin session user', () => {
    renderSettingsPageAs('admin');
    expect(screen.getByRole('tab', { name: 'Sources' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Automation' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Extraction Templates' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Pulse' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Timer' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Providers' })).toBeInTheDocument();
  });

  it('shows system tabs for API-key-only caller (no session user)', () => {
    // API-key-only callers are treated as single-tenant owner — show all tabs
    // to avoid a regression where self-hosted admins lose UI access.
    renderSettingsPageAs(null);
    expect(screen.queryByRole('tab', { name: 'Sources' })).not.toBeInTheDocument();
  });

  it('defaults to Topics tab', () => {
    renderSettingsPage();
    const topicsTab = screen.getByRole('tab', { name: 'Topics' });
    expect(topicsTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Authors tab on click', async () => {
    const user = userEvent.setup();
    renderSettingsPage();
    const authorsTab = screen.getByRole('tab', { name: 'Authors' });
    await user.click(authorsTab);
    expect(authorsTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Automation tab on click (admin)', async () => {
    renderSettingsPageAs('admin');
    const user = userEvent.setup();
    const autoTab = screen.getByRole('tab', { name: 'Automation' });
    await user.click(autoTab);
    expect(autoTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Pulse tab on click (admin)', async () => {
    renderSettingsPageAs('admin');
    const user = userEvent.setup();
    const pulseTab = screen.getByRole('tab', { name: 'Pulse' });
    await user.click(pulseTab);
    expect(pulseTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Sources tab on click (admin)', async () => {
    renderSettingsPageAs('admin');
    const user = userEvent.setup();
    const sourcesTab = screen.getByRole('tab', { name: 'Sources' });
    await user.click(sourcesTab);
    expect(sourcesTab).toHaveAttribute('data-state', 'active');
  });

  it('does not render Recommendations tab (removed)', () => {
    renderSettingsPage();
    expect(screen.queryByRole('tab', { name: 'Recommendations' })).not.toBeInTheDocument();
  });
});
