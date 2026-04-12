import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { SettingsPage } from '@/pages/SettingsPage';

// Mock all api calls used by settings sections
vi.mock('@/lib/api', () => ({
  fetchTopics: vi.fn().mockResolvedValue([]),
  createTopic: vi.fn(),
  updateTopic: vi.fn(),
  deleteTopic: vi.fn(),
  fetchSources: vi.fn().mockResolvedValue([]),
  updateSource: vi.fn(),
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

describe('SettingsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the settings heading', () => {
    renderSettingsPage();
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('renders all tab triggers', () => {
    renderSettingsPage();
    expect(screen.getByRole('tab', { name: 'Topics' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Sources' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Authors' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Ingestion' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Automation' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Extraction Templates' })).toBeInTheDocument();
  });

  it('defaults to Topics tab', () => {
    renderSettingsPage();
    const topicsTab = screen.getByRole('tab', { name: 'Topics' });
    expect(topicsTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Sources tab on click', async () => {
    const user = userEvent.setup();
    renderSettingsPage();
    const sourcesTab = screen.getByRole('tab', { name: 'Sources' });
    await user.click(sourcesTab);
    expect(sourcesTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Authors tab on click', async () => {
    const user = userEvent.setup();
    renderSettingsPage();
    const authorsTab = screen.getByRole('tab', { name: 'Authors' });
    await user.click(authorsTab);
    expect(authorsTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Automation tab on click', async () => {
    const user = userEvent.setup();
    renderSettingsPage();
    const autoTab = screen.getByRole('tab', { name: 'Automation' });
    await user.click(autoTab);
    expect(autoTab).toHaveAttribute('data-state', 'active');
  });
});
