import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AuthorSection } from '@/components/settings/AuthorSection';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchTrackedAuthors: vi.fn(),
    createTrackedAuthor: vi.fn(),
    updateTrackedAuthor: vi.fn(),
    deleteTrackedAuthor: vi.fn(),
    autoDetectAuthors: vi.fn(),
    checkTrackedAuthors: vi.fn(),
  };
});

const { fetchTrackedAuthors, createTrackedAuthor } = await import('@/lib/api');
const { toast } = await import('sonner');

const AUTHOR: Awaited<ReturnType<typeof fetchTrackedAuthors>>[number] = {
  id: 1,
  author_name: 'Yoshua Bengio',
  s2_author_id: null,
  source: 'manual',
  enabled: true,
  last_checked_at: null,
  created_at: '2026-01-01T00:00:00Z',
};

function renderSection() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <AuthorSection />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('AuthorSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a tracked author row', async () => {
    vi.mocked(fetchTrackedAuthors).mockResolvedValue([AUTHOR]);
    renderSection();
    expect(await screen.findByText('Yoshua Bengio')).toBeInTheDocument();
  });

  it('shows the empty state when the list loads empty', async () => {
    vi.mocked(fetchTrackedAuthors).mockResolvedValue([]);
    renderSection();
    expect(await screen.findByText('No tracked authors')).toBeInTheDocument();
    expect(screen.queryByText('Failed to load tracked authors.')).toBeNull();
  });

  it('shows an error message, not the empty state, when the list fails to load', async () => {
    vi.mocked(fetchTrackedAuthors).mockRejectedValue(new Error('network down'));
    renderSection();
    expect(await screen.findByText('Failed to load tracked authors.')).toBeInTheDocument();
    expect(screen.queryByText('No tracked authors')).toBeNull();
  });

  it('shows an error toast when adding an author fails', async () => {
    vi.mocked(fetchTrackedAuthors).mockResolvedValue([]);
    vi.mocked(createTrackedAuthor).mockRejectedValue(new Error('Author already tracked'));
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Add Author' }));
    await user.type(screen.getByPlaceholderText('e.g. Yoshua Bengio'), 'Yoshua Bengio');
    await user.click(screen.getByRole('button', { name: 'Add Author' }));

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Author already tracked');
    });
  });
});
