import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { JournalSection } from '@/components/my-day/sections/JournalSection';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/lib/api', () => ({
  getJournalEntry: vi.fn(),
  upsertJournalEntry: vi.fn(),
}));

const { getJournalEntry, upsertJournalEntry } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderJournal() {
  return render(<JournalSection />);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('JournalSection — saveTimer cleanup on unmount', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getJournalEntry).mockResolvedValue(null);
    vi.mocked(upsertJournalEntry).mockResolvedValue(undefined as any);
  });

  it('clears the pending save timer when the component unmounts mid-debounce', async () => {
    const user = userEvent.setup();
    const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');

    const { unmount } = renderJournal();

    // Wait for the initial load to settle
    await screen.findByPlaceholderText("What's the one thing you'll do first tomorrow?");

    // Type into the textarea — this schedules the debounced save (1500ms)
    const textarea = screen.getByPlaceholderText("What's the one thing you'll do first tomorrow?");
    await user.type(textarea, 'Plan the sprint');

    // At this point a save timer is pending; upsertJournalEntry has NOT been called
    expect(vi.mocked(upsertJournalEntry)).not.toHaveBeenCalled();

    // Unmount the component — the cleanup useEffect should clear the timer
    unmount();

    // clearTimeout should have been called (cleanup useEffect ran)
    expect(clearTimeoutSpy).toHaveBeenCalled();

    clearTimeoutSpy.mockRestore();
  });

  it('saves when the component stays mounted until the debounce fires', async () => {
    const user = userEvent.setup();
    renderJournal();

    await screen.findByPlaceholderText("What's the one thing you'll do first tomorrow?");

    const textarea = screen.getByPlaceholderText("What's the one thing you'll do first tomorrow?");

    // Type something — schedules a 1500ms save
    await user.type(textarea, 'Ship the feature');

    // Wait for the debounce to fire (> 1500ms)
    await waitFor(
      () => expect(vi.mocked(upsertJournalEntry)).toHaveBeenCalledOnce(),
      { timeout: 3000 },
    );

    expect(vi.mocked(upsertJournalEntry)).toHaveBeenCalledWith(
      expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
      expect.objectContaining({ first_move: expect.stringContaining('Ship the feature') }),
    );
  });
});
