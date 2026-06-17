import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { KnowledgeGraphPage } from '@/pages/KnowledgeGraphPage';
import { useAuthStore } from '@/stores/auth-store';
import * as api from '@/lib/api';

// Mock cytoscape so jsdom doesn't choke on canvas
vi.mock('cytoscape', () => {
  const mockCy = {
    on: vi.fn(),
    destroy: vi.fn(),
  };
  return { default: vi.fn(() => mockCy) };
});

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    getKnowledgeGraph: vi.fn().mockResolvedValue({
      entities: [
        { id: 1, name: 'Transformer', canonical_name: 'transformer', entity_type: 'method', description: 'Self-attention architecture', metadata: {}, paper_count: 12, created_at: '2026-01-01T00:00:00Z', display_size: 40 },
        { id: 2, name: 'ImageNet', canonical_name: 'imagenet', entity_type: 'dataset', description: 'Large image dataset', metadata: {}, paper_count: 8, created_at: '2026-01-01T00:00:00Z', display_size: 39 },
        { id: 3, name: 'BLEU', canonical_name: 'bleu', entity_type: 'metric', description: 'Translation quality metric', metadata: {}, paper_count: 5, created_at: '2026-01-01T00:00:00Z', display_size: 30 },
      ],
      relationships: [
        { id: 1, source_entity_id: 1, target_entity_id: 2, relationship_type: 'evaluated_on', paper_id: 1, evidence_quote: null, confidence: 0.9, created_at: '2026-01-01T00:00:00Z' },
        { id: 2, source_entity_id: 1, target_entity_id: 3, relationship_type: 'measured_by', paper_id: 1, evidence_quote: null, confidence: 0.85, created_at: '2026-01-01T00:00:00Z' },
      ],
      entity_type_counts: { method: 1, dataset: 1, metric: 1 },
    }),
    queryKnowledgeGraph: vi.fn().mockResolvedValue({
      results: [{ answer: 'Transformer is a method used on ImageNet' }],
    }),
    batchExtractEntities: vi.fn().mockResolvedValue({ extracted: 0 }),
  };
});

const WITH_ENTITIES = {
  entities: [
    { id: 1, name: 'Transformer', canonical_name: 'transformer', entity_type: 'method', description: 'Self-attention architecture', metadata: {}, paper_count: 12, created_at: '2026-01-01T00:00:00Z', display_size: 40 },
    { id: 2, name: 'ImageNet', canonical_name: 'imagenet', entity_type: 'dataset', description: 'Large image dataset', metadata: {}, paper_count: 8, created_at: '2026-01-01T00:00:00Z', display_size: 39 },
    { id: 3, name: 'BLEU', canonical_name: 'bleu', entity_type: 'metric', description: 'Translation quality metric', metadata: {}, paper_count: 5, created_at: '2026-01-01T00:00:00Z', display_size: 30 },
  ],
  relationships: [
    { id: 1, source_entity_id: 1, target_entity_id: 2, relationship_type: 'evaluated_on', paper_id: 1, evidence_quote: null, confidence: 0.9, created_at: '2026-01-01T00:00:00Z' },
    { id: 2, source_entity_id: 1, target_entity_id: 3, relationship_type: 'measured_by', paper_id: 1, evidence_quote: null, confidence: 0.85, created_at: '2026-01-01T00:00:00Z' },
  ],
  entity_type_counts: { method: 1, dataset: 1, metric: 1 },
};

const EMPTY_GRAPH = { entities: [], relationships: [], entity_type_counts: {} };

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <KnowledgeGraphPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('KnowledgeGraphPage', () => {
  beforeEach(() => {
    vi.mocked(api.getKnowledgeGraph).mockResolvedValue(WITH_ENTITIES);
  });

  it('renders the page title', () => {
    renderPage();
    expect(screen.getByText('Knowledge Graph')).toBeInTheDocument();
  });

  it('renders entity type filter', () => {
    renderPage();
    expect(screen.getByText('Entity Type')).toBeInTheDocument();
  });

  it('renders min paper count slider', () => {
    renderPage();
    expect(screen.getByText(/Min Paper Count:/)).toBeInTheDocument();
  });

  it('renders layout selector', () => {
    renderPage();
    expect(screen.getByText('Layout:')).toBeInTheDocument();
  });

  it('renders query input', () => {
    renderPage();
    expect(
      screen.getByPlaceholderText("Query (e.g., 'What methods are used on ImageNet?')"),
    ).toBeInTheDocument();
  });

  it('shows graph stats after data loads', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Total Entities')).toBeInTheDocument();
      expect(screen.getByText('Total Relationships')).toBeInTheDocument();
      expect(screen.getByText('Entity Types')).toBeInTheDocument();
    });
  });

  it('shows entity breakdown after data loads', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Entities by Type')).toBeInTheDocument();
      expect(screen.getByText('method: 1')).toBeInTheDocument();
      expect(screen.getByText('dataset: 1')).toBeInTheDocument();
      expect(screen.getByText('metric: 1')).toBeInTheDocument();
    });
  });

  it('shows cytoscape container when data is available', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('cytoscape-container')).toBeInTheDocument();
    });
  });

  it('renders the Query button', () => {
    renderPage();
    expect(screen.getByText('Query')).toBeInTheDocument();
  });

  it('shows total relationships stat', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Total Relationships')).toBeInTheDocument();
      expect(screen.getByText('2')).toBeInTheDocument(); // 2 relationships
    });
  });
});

describe('KnowledgeGraphPage — Batch Extract admin gate', () => {
  beforeEach(() => {
    vi.mocked(api.getKnowledgeGraph).mockResolvedValue(EMPTY_GRAPH);
  });

  function renderAsRole(role: 'user' | 'admin') {
    useAuthStore.setState({ user: { id: 1, email: 'a@b.c', role } });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <KnowledgeGraphPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('hides "Batch Extract Entities" button from non-admin users', async () => {
    renderAsRole('user');
    await waitFor(() => {
      expect(screen.getByText('No entities extracted yet')).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /Batch Extract Entities/i })).not.toBeInTheDocument();
  });

  it('shows "Batch Extract Entities" button to admin users', async () => {
    renderAsRole('admin');
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Batch Extract Entities/i })).toBeInTheDocument();
    });
  });
});
