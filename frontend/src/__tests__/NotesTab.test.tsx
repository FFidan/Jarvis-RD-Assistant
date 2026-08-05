import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NotesTab } from '@/components/paper/NotesTab';
import type { Note } from '@/types';

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

import { toast } from 'sonner';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (s: { trackExternalJob: ReturnType<typeof vi.fn> }) => unknown) =>
    selector({ trackExternalJob: vi.fn() }),
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    fetchNotes: vi.fn(),
    createNote: vi.fn(),
    deleteNote: vi.fn(),
    promoteZoteroNote: vi.fn(),
    zoteroSyncAnnotations: vi.fn(),
  };
});

const { fetchNotes, promoteZoteroNote, deleteNote } = await import('@/lib/api');

function makeNote(overrides: Partial<Note> = {}): Note {
  return {
    id: 1,
    paper_id: 42,
    user_note: 'Worth checking',
    highlight_text: 'The method improves accuracy.',
    page_number: 3,
    source: 'zotero',
    zotero_annotation_key: 'ANN1',
    verification_status: 'unverified',
    verified_quote: null,
    verified_page_number: null,
    promoted_at: null,
    stale: false,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function renderTab() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <NotesTab paperId={42} />,
    { queryClient },
  );
}

describe('NotesTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNotes).mockImplementation(async (_paperId, source) => {
      if (source === 'zotero') return [makeNote()];
      return [];
    });
  });

  it('test_delete_error_renders_banner: shows inline error when deleteMut fails', async () => {
    const userNote = makeNote({ id: 7, source: 'user', user_note: 'My note to delete' });
    vi.mocked(fetchNotes).mockImplementation(async (_paperId, source) => {
      if (source === 'user') return [userNote];
      return [];
    });
    vi.mocked(deleteNote).mockRejectedValue(new Error('Network error'));
    const user = userEvent.setup();
    renderTab();

    // Wait for the note to appear
    const deleteBtn = await screen.findByRole('button', { name: /delete/i });
    await user.click(deleteBtn);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Network error');
  });

  it('test_delete_error_fires_toast: deleteMut onError fires toast.error on failure', async () => {
    const userNote = makeNote({ id: 7, source: 'user', user_note: 'My note to delete' });
    vi.mocked(fetchNotes).mockImplementation(async (_paperId, source) => {
      if (source === 'user') return [userNote];
      return [];
    });
    vi.mocked(deleteNote).mockRejectedValue(new Error('Server error'));
    const user = userEvent.setup();
    renderTab();

    const deleteBtn = await screen.findByRole('button', { name: /delete/i });
    await user.click(deleteBtn);

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Failed to delete note', expect.objectContaining({ description: 'Server error' }));
    });
  });

  it('test_promote_error_fires_toast: promoteZoteroMut onError fires toast.error on failure', async () => {
    vi.mocked(promoteZoteroNote).mockRejectedValue(new Error('Promote failed'));
    const user = userEvent.setup();
    renderTab();

    await user.click(await screen.findByRole('button', { name: /promote verified evidence/i }));

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Failed to promote Zotero highlight', expect.objectContaining({ description: 'Promote failed' }));
    });
  });

  it('shows Zotero highlights as unpromoted until explicit verification', async () => {
    renderTab();

    expect(await screen.findByText('Not promoted as evidence')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /promote verified evidence/i })).toBeEnabled();
  });

  it('calls the promote endpoint and refreshes Zotero notes', async () => {
    vi.mocked(promoteZoteroNote).mockResolvedValue(
      makeNote({
        verification_status: 'verified',
        verified_quote: 'The method improves accuracy.',
        verified_page_number: 3,
        promoted_at: '2026-01-02T00:00:00Z',
      }),
    );
    const user = userEvent.setup();
    renderTab();

    await user.click(await screen.findByRole('button', { name: /promote verified evidence/i }));

    await waitFor(() => {
      expect(vi.mocked(promoteZoteroNote)).toHaveBeenCalledWith(1);
    });
  });

  it('shows verified Zotero notes without a promote button', async () => {
    vi.mocked(fetchNotes).mockImplementation(async (_paperId, source) => {
      if (source === 'zotero') {
        return [
          makeNote({
            verification_status: 'verified',
            verified_quote: 'The method improves accuracy.',
            verified_page_number: 3,
            promoted_at: '2026-01-02T00:00:00Z',
          }),
        ];
      }
      return [];
    });

    renderTab();

    expect(await screen.findByText('Verified evidence, page 3')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /promote verified evidence/i })).not.toBeInTheDocument();
  });

  it('labels an earlier-version highlight and disables evidence promotion', async () => {
    vi.mocked(fetchNotes).mockImplementation(async (_paperId, source) => {
      if (source === 'zotero') return [makeNote({ stale: true })];
      return [];
    });

    renderTab();

    expect(
      await screen.findByText('This highlight belongs to an earlier version of the paper.'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /promote verified evidence/i }),
    ).toBeDisabled();
    expect(promoteZoteroNote).not.toHaveBeenCalled();
  });
});
