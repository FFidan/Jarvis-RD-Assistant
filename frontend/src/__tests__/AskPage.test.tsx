/**
 * AskPage.test.tsx — unit tests for the Ask page (nav group Ⅳ).
 *
 * Covers:
 * - Page renders with correct heading
 * - StreamingChat is rendered (cross-paper scope)
 * - Input field is present and usable
 * - Submitting a question calls sendMessage (mock)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AskPage } from '@/pages/AskPage';
import { fetchDashboardMetrics } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

// Mock the SSE streaming so we can test without a real backend
vi.mock('@/lib/sse', async (importActual) => {
  const actual = await importActual<typeof import('@/lib/sse')>();
  return {
    ...actual,
    streamSSE: vi.fn().mockImplementation(async function* () {
      yield { type: 'token', content: 'Mocked answer from the Ask backend.' };
      yield { type: 'done', full_answer: 'Mocked answer from the Ask backend.', model_used: null };
    }),
  };
});

// Partial-mock the API barrel so the metrics query can be forced to fail while
// the rest of @/lib/api (used by StreamingChat) stays real.
vi.mock('@/lib/api', async (importActual) => {
  const actual = await importActual<typeof import('@/lib/api')>();
  return { ...actual, fetchDashboardMetrics: vi.fn() };
});

// Mock auth store (needed by downstream components)
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: vi.fn().mockReturnValue({
    user: { id: 1, email: 'test@example.com', role: 'user' },
    isAuthenticated: true,
    logout: vi.fn(),
    loginWithApiKey: vi.fn(),
    loginWithSession: vi.fn(),
    currentUser: vi.fn(),
  }),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderAskPage(chunkedPapers: number = 1) {
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(QUERY_KEYS.dashboard.metrics(), {
    chunked_papers: chunkedPapers,
  });

  return renderWithProviders(
    <MemoryRouter>
      <AskPage />
    </MemoryRouter>,
    { queryClient },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AskPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the Ask page heading', () => {
    renderAskPage();
    expect(screen.getByRole('heading', { name: 'Ask' })).toBeInTheDocument();
  });

  it('renders the cross-paper subtitle', () => {
    renderAskPage();
    expect(screen.getByText(/Cross-paper reasoning/)).toBeInTheDocument();
  });

  it('renders the chat input field', () => {
    renderAskPage();
    expect(screen.getByPlaceholderText(/Ask a question/)).toBeInTheDocument();
  });

  it('renders the send button (disabled when input is empty)', () => {
    renderAskPage();
    const sendBtn = screen.getByRole('button', { name: 'Send message' });
    expect(sendBtn).toBeDisabled();
  });

  it('enables the send button when input is non-empty', async () => {
    renderAskPage();
    const textarea = screen.getByPlaceholderText(/Ask a question/);
    fireEvent.change(textarea, { target: { value: 'What is the main finding?' } });

    const sendBtn = screen.getByRole('button', { name: 'Send message' });
    expect(sendBtn).not.toBeDisabled();
  });

  it('submitting a question shows a user message in chat', async () => {
    renderAskPage();
    const textarea = screen.getByPlaceholderText(/Ask a question/);
    fireEvent.change(textarea, { target: { value: 'What do papers say about attention?' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });

    await waitFor(() => {
      expect(screen.getByText('What do papers say about attention?')).toBeInTheDocument();
    });
  });

  it('clears the input after submit', async () => {
    renderAskPage();
    const textarea = screen.getByPlaceholderText(/Ask a question/);
    fireEvent.change(textarea, { target: { value: 'Test question' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });

    await waitFor(() => {
      expect(textarea).toHaveValue('');
    });
  });

  it('has data-testid="ask-page"', () => {
    renderAskPage();
    expect(screen.getByTestId('ask-page')).toBeInTheDocument();
  });

  it('shows the onboarding empty-state (explainer + CTA) instead of a dead input when no analyzed papers', () => {
    renderAskPage(0);
    // No chat input is rendered in the empty-state.
    expect(screen.queryByPlaceholderText(/Ask a question/)).not.toBeInTheDocument();
    // Onboarding guidance is shown.
    const emptyState = screen.getByTestId('ask-empty-state');
    expect(emptyState).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Nothing to ask yet' })).toBeInTheDocument();
    expect(screen.getByText(/Import and analyze at least one paper/)).toBeInTheDocument();
    // Primary CTA links to the discover/import surface.
    const cta = screen.getByRole('link', { name: 'Find papers to analyze' });
    expect(cta).toBeInTheDocument();
    expect(cta).toHaveAttribute('href', '/feed?surface=search');
  });

  it('renders the chat input (not the empty-state) when analyzed papers exist', () => {
    renderAskPage(1);
    expect(screen.getByPlaceholderText(/Ask a question/)).toBeInTheDocument();
    expect(screen.queryByTestId('ask-empty-state')).not.toBeInTheDocument();
  });

  it('falls back to the chat workspace (not the empty-state) when the metrics request fails', async () => {
    // Invariant #1: a failed request must render a degraded/working state, never a
    // misleading empty-state. With no cached metrics and a rejecting query, the
    // page must NOT claim "Nothing to ask yet".
    vi.mocked(fetchDashboardMetrics).mockRejectedValue(new Error('metrics down'));
    const queryClient = createTestQueryClient();

    renderWithProviders(
      <MemoryRouter>
        <AskPage />
      </MemoryRouter>,
      { queryClient },
    );

    await waitFor(() => {
      expect(vi.mocked(fetchDashboardMetrics)).toHaveBeenCalled();
    });
    expect(screen.queryByTestId('ask-empty-state')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/Ask a question/)).toBeInTheDocument();
  });

  it('keeps the chat input usable while the metrics request is failed or pending', async () => {
    // Degraded, not dead: a failed/pending metrics query must never disable the
    // workspace or claim a prerequisite ("Analyze at least one paper first").
    vi.mocked(fetchDashboardMetrics).mockRejectedValue(new Error('metrics down'));
    const queryClient = createTestQueryClient();

    renderWithProviders(
      <MemoryRouter>
        <AskPage />
      </MemoryRouter>,
      { queryClient },
    );

    await waitFor(() => {
      expect(vi.mocked(fetchDashboardMetrics)).toHaveBeenCalled();
    });

    const textarea = screen.getByPlaceholderText(/Ask a question/);
    expect(textarea).not.toBeDisabled();
    expect(screen.queryByText('Analyze at least one paper first')).not.toBeInTheDocument();

    // Send is disabled only while the input is empty — typing enables it.
    fireEvent.change(textarea, { target: { value: 'Still works?' } });
    expect(screen.getByRole('button', { name: 'Send message' })).not.toBeDisabled();
  });
});
