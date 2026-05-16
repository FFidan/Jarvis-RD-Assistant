/**
 * EndOfDaySection — the "shutdown ritual that closes the loop" (spec §3.10):
 * 3 structured prompts, prefill hints from day signals, "make this a thread",
 * optional free-note, persisted via the journal POST-upsert.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { EndOfDaySection } from '@/components/my-day/sections/EndOfDaySection';
import type { MyDayResponse } from '@/types';

vi.mock('@/lib/api', () => ({
  getJournalEntry: vi.fn(),
  upsertJournalEntry: vi.fn(),
  fetchMyDay: vi.fn(),
  seedThreadFromEod: vi.fn(),
}));

const { getJournalEntry, upsertJournalEntry, fetchMyDay, seedThreadFromEod } =
  await import('@/lib/api');

const MY_DAY: MyDayResponse = {
  tasks: [
    {
      id: 1,
      project_id: null,
      title: 'done one',
      priority: 1,
      deadline: null,
      status: 'done',
      completed_at: null,
      project_name: null,
      project_color: null,
    },
  ],
  cards_due: 0,
  recommendations: [],
  today_focus_hours: 2.5,
  focus_streak_days: 3,
  project_pulse: [],
};

function renderEod() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <EndOfDaySection />
    </QueryClientProvider>,
  );
}

describe('EndOfDaySection shutdown ritual', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getJournalEntry).mockResolvedValue(null);
    vi.mocked(fetchMyDay).mockResolvedValue(MY_DAY);
    vi.mocked(upsertJournalEntry).mockResolvedValue({
      id: 1,
      date: '2026-05-15',
      prompts: {},
      created_at: '',
      updated_at: '',
    });
    vi.mocked(seedThreadFromEod).mockResolvedValue({
      thread: {
        id: 9,
        title: 'blocker',
        anchor: null,
        progress: 0,
        last_at: '',
        status: 'open',
        created_at: '',
      },
      created: true,
    });
  });

  it('renders the 3 structured prompts', async () => {
    renderEod();
    expect(await screen.findByText(/§ End of day/i)).toBeInTheDocument();
    expect(screen.getByLabelText('One thing that worked')).toBeInTheDocument();
    expect(screen.getByLabelText("What's still blocking me")).toBeInTheDocument();
    expect(screen.getByLabelText('First move tomorrow')).toBeInTheDocument();
  });

  it('prefills the "worked" placeholder from the day signals (mechanical)', async () => {
    renderEod();
    const worked = await screen.findByLabelText('One thing that worked');
    await waitFor(() =>
      expect(worked).toHaveAttribute(
        'placeholder',
        expect.stringMatching(/closed 1 task, 2\.5h focused/),
      ),
    );
  });

  it('persists prompt edits through the journal upsert', async () => {
    const user = userEvent.setup();
    renderEod();
    const first = await screen.findByLabelText('First move tomorrow');
    await user.type(first, 'Reread Kidger §4.2');

    await waitFor(
      () => expect(vi.mocked(upsertJournalEntry)).toHaveBeenCalled(),
      { timeout: 3000 },
    );
    const calls = vi.mocked(upsertJournalEntry).mock.calls;
    const lastCall = calls[calls.length - 1]!;
    expect(lastCall[1]).toEqual(
      expect.objectContaining({ first_move: 'Reread Kidger §4.2' }),
    );
  });

  it('"make this a thread" seeds a thread from the blocking text', async () => {
    const user = userEvent.setup();
    renderEod();
    const blocked = await screen.findByLabelText("What's still blocking me");
    await user.type(blocked, 'Adjoint commutator step');

    const makeThread = await screen.findByRole('button', {
      name: /make this a thread/i,
    });
    await user.click(makeThread);

    expect(vi.mocked(seedThreadFromEod)).toHaveBeenCalledWith({
      title: 'Adjoint commutator step',
    });
  });

  it('reveals the optional free-note escape hatch on demand', async () => {
    const user = userEvent.setup();
    renderEod();
    await user.click(await screen.findByRole('button', { name: /\+ anything else/i }));
    expect(screen.getByLabelText('Anything else')).toBeInTheDocument();
  });
});
