import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NotesTab } from '@/components/paper/NotesTab';
import type { Note } from '@/types';

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
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function renderTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <NotesTab paperId={42} />
    </QueryClientProvider>,
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
});
