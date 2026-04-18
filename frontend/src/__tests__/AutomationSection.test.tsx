import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AutomationSection } from '@/components/settings/AutomationSection';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchNudges: vi.fn(),
    updateNudge: vi.fn().mockResolvedValue({}),
  };
});

const { fetchNudges } = await import('@/lib/api');

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AutomationSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AutomationSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNudges).mockResolvedValue([]);
  });

  it('shows empty state when no nudges are configured', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/No automation jobs/i)).toBeInTheDocument();
    });
  });

  it('renders nudge card when nudges are returned', async () => {
    vi.mocked(fetchNudges).mockResolvedValue([
      {
        id: 1,
        nudge_type: 'review_reminder',
        enabled: true,
        cron_expression: '0 8 * * *',
        last_fired_at: null,
        config: {},
        created_at: '2026-04-17T00:00:00Z',
      },
    ]);
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('Flashcard Review Reminder')).toBeInTheDocument();
    });
  });

  it('switches nudge enabled state on button click', async () => {
    const { updateNudge } = await import('@/lib/api');
    vi.mocked(fetchNudges).mockResolvedValue([
      {
        id: 2,
        nudge_type: 'daily_summary',
        enabled: false,
        cron_expression: '0 7 * * *',
        last_fired_at: null,
        config: {},
        created_at: '2026-04-17T00:00:00Z',
      },
    ]);
    const user = userEvent.setup();
    renderSection();
    const enableBtn = await screen.findByRole('button', { name: /enable/i });
    await user.click(enableBtn);
    await waitFor(() => {
      expect(vi.mocked(updateNudge)).toHaveBeenCalledWith(2, { enabled: true });
    });
  });
});
