import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { KGQueryInput } from '@/components/knowledge/KGQueryInput';
import * as api from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    queryKnowledgeGraph: vi.fn(),
  };
});

function renderInput() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <KGQueryInput />
    </QueryClientProvider>,
  );
}

async function runQuery(text: string) {
  const user = userEvent.setup();
  renderInput();
  await user.type(
    screen.getByPlaceholderText("Query (e.g., 'What methods are used on ImageNet?')"),
    text,
  );
  await user.click(screen.getByRole('button', { name: /Query/i }));
}

describe('KGQueryInput — card-rendered results', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders "used on" relationship rows as cards (not raw JSON)', async () => {
    vi.mocked(api.queryKnowledgeGraph).mockResolvedValue({
      results: [
        {
          method_name: 'ResNet',
          method_type: 'method',
          target_name: 'ImageNet',
          target_type: 'dataset',
          relationship_type: 'used_on',
          evidence_quote: 'ResNet was evaluated on ImageNet.',
          confidence: 0.92,
        },
      ],
    });
    await runQuery('What methods are used on ImageNet?');

    await waitFor(() => {
      expect(screen.getByText('Query Results')).toBeInTheDocument();
    });
    expect(screen.getByText('ResNet')).toBeInTheDocument();
    expect(screen.getByText('ImageNet')).toBeInTheDocument();
    expect(screen.getByText(/used on/)).toBeInTheDocument();
    expect(screen.getByText(/confidence 92%/)).toBeInTheDocument();
    expect(screen.getByText(/ResNet was evaluated on ImageNet/)).toBeInTheDocument();
    // No raw JSON dump.
    expect(screen.queryByText(/"method_name"/)).not.toBeInTheDocument();
    expect(document.querySelector('pre')).toBeNull();
  });

  it('renders "outperforms" comparison rows as cards', async () => {
    vi.mocked(api.queryKnowledgeGraph).mockResolvedValue({
      results: [
        {
          method_name: 'Transformer',
          compared_to: 'LSTM',
          evidence_quote: 'Transformers outperform LSTMs.',
          confidence: 0.8,
        },
      ],
    });
    await runQuery('What outperforms LSTM?');

    await waitFor(() => {
      expect(screen.getByText('Transformer')).toBeInTheDocument();
    });
    expect(screen.getByText('LSTM')).toBeInTheDocument();
    expect(screen.getByText(/outperforms/)).toBeInTheDocument();
    expect(document.querySelector('pre')).toBeNull();
  });

  it('renders generic entity rows as cards', async () => {
    vi.mocked(api.queryKnowledgeGraph).mockResolvedValue({
      results: [
        {
          id: 7,
          name: 'BERT',
          entity_type: 'method',
          description: 'Bidirectional encoder.',
          paper_title: 'Attention paper',
        },
      ],
    });
    await runQuery('BERT');

    await waitFor(() => {
      expect(screen.getByText('BERT')).toBeInTheDocument();
    });
    expect(screen.getByText('Bidirectional encoder.')).toBeInTheDocument();
    expect(screen.getByText(/Attention paper/)).toBeInTheDocument();
    expect(document.querySelector('pre')).toBeNull();
  });

  it('renders an unknown shape as a labelled key/value list, never raw JSON', async () => {
    vi.mocked(api.queryKnowledgeGraph).mockResolvedValue({
      results: [{ some_field: 'some value', another: 42 }],
    });
    await runQuery('weird query');

    await waitFor(() => {
      expect(screen.getByText('Query Results')).toBeInTheDocument();
    });
    expect(screen.getByText('some field')).toBeInTheDocument();
    expect(screen.getByText('some value')).toBeInTheDocument();
    expect(document.querySelector('pre')).toBeNull();
  });

  it('shows a friendly empty state when there are no results', async () => {
    vi.mocked(api.queryKnowledgeGraph).mockResolvedValue({ results: [] });
    await runQuery('nothing matches');

    await waitFor(() => {
      expect(screen.getByText('No results found for this query.')).toBeInTheDocument();
    });
    expect(document.querySelector('pre')).toBeNull();
  });
});
