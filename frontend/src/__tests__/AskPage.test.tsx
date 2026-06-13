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
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AskPage } from '@/pages/AskPage';
import { QUERY_KEYS } from '@/lib/query-keys';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

// Mock the SSE streaming so we can test without a real backend
vi.mock('@/lib/sse', () => ({
  streamSSE: vi.fn().mockImplementation(async function* () {
    yield { type: 'token', content: 'Mocked answer from the Ask backend.' };
    yield { type: 'done', model_used: null };
  }),
}));

// Mock auth store (needed by downstream components)
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: vi.fn().mockReturnValue({
    user: { id: 1, email: 'test@example.com', role: 'user' },
    isAuthenticated: true,
    apiKey: 'test-key',
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  queryClient.setQueryData(QUERY_KEYS.dashboard.metrics(), {
    chunked_papers: chunkedPapers,
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AskPage />
      </MemoryRouter>
    </QueryClientProvider>,
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

  it('gates the input when the library has no analyzed papers', () => {
    renderAskPage(0);
    const textarea = screen.getByPlaceholderText(/Ask a question/);
    expect(textarea).toBeDisabled();
  });
});
