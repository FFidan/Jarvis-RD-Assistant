import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TopicSection } from '@/components/settings/TopicSection';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchTopics: vi.fn(),
    fetchMySubscriptions: vi.fn(),
    subscribeToTopic: vi.fn().mockResolvedValue(undefined),
    unsubscribeFromTopic: vi.fn().mockResolvedValue(undefined),
    createTopic: vi.fn(),
    updateTopic: vi.fn(),
    deleteTopic: vi.fn(),
  };
});

const { fetchTopics, fetchMySubscriptions, subscribeToTopic, unsubscribeFromTopic } =
  await import('@/lib/api');

const TOPIC = {
  id: 1,
  name: 'Diffusion Models',
  query_terms: ['diffusion', 'score-based'],
  category: 'ML',
  enabled: true,
  description: null,
  created_at: '2026-01-01T00:00:00Z',
};

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TopicSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('TopicSection subscription switch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchTopics).mockResolvedValue([TOPIC]);
    vi.mocked(fetchMySubscriptions).mockResolvedValue([]);
  });

  it('renders the subscription switch for each topic', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByLabelText('Auto-add matches to my library')).toBeInTheDocument();
    });
  });

  it('switch is unchecked when topic is not subscribed', async () => {
    vi.mocked(fetchMySubscriptions).mockResolvedValue([]);
    renderSection();
    const sw = await screen.findByLabelText('Auto-add matches to my library');
    expect(sw).not.toBeChecked();
  });

  it('switch is checked when topic is subscribed', async () => {
    vi.mocked(fetchMySubscriptions).mockResolvedValue([1]);
    renderSection();
    const sw = await screen.findByLabelText('Auto-add matches to my library');
    await waitFor(() => expect(sw).toBeChecked());
  });


  it('explains enabled topics versus auto-add subscriptions without toggling from the tooltip', async () => {
    vi.mocked(fetchMySubscriptions).mockResolvedValue([]);
    const user = userEvent.setup();
    renderSection();

    await screen.findByLabelText('Auto-add matches to my library');
    const info = screen.getByLabelText('More info');

    await user.hover(info);
    await screen.findAllByText(/Enabled topics are used for discovery and ranking/i);
    expect(screen.getAllByText(/adds newly fetched matching papers to your library/i).length).toBeGreaterThan(0);

    await user.click(info);
    expect(vi.mocked(subscribeToTopic)).not.toHaveBeenCalled();
    expect(vi.mocked(unsubscribeFromTopic)).not.toHaveBeenCalled();
  });

  it('toggling on calls subscribeToTopic', async () => {
    vi.mocked(fetchMySubscriptions).mockResolvedValue([]);
    const user = userEvent.setup();
    renderSection();
    const sw = await screen.findByLabelText('Auto-add matches to my library');
    await user.click(sw);
    await waitFor(() => {
      expect(vi.mocked(subscribeToTopic)).toHaveBeenCalledWith(1);
    });
  });

  it('toggling off calls unsubscribeFromTopic', async () => {
    vi.mocked(fetchMySubscriptions).mockResolvedValue([1]);
    const user = userEvent.setup();
    renderSection();
    const sw = await screen.findByLabelText('Auto-add matches to my library');
    await waitFor(() => expect(sw).toBeChecked());
    await user.click(sw);
    await waitFor(() => {
      expect(vi.mocked(unsubscribeFromTopic)).toHaveBeenCalledWith(1);
    });
  });
});
